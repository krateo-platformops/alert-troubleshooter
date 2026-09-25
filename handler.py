#!/usr/bin/env python3
"""krateo-alert-troubleshooter — bridges a HyperDX alert webhook to an Autopilot root-cause analysis.

On POST /webhook (HyperDX alert-fired payload):
  1. create a TroubleshootingReport CR (phase=Analyzing) via the apiserver,
  2. call the krateo-autopilot A2A agent (JSON-RPC message/stream) with a troubleshooting prompt,
  3. accumulate the streamed analysis and patch the CR status (phase=Ready, report=<markdown>).

Runs anyone's-browser-independent: this is the "background" path. Minimal deps: stdlib + requests.
"""
import base64
import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

import report_v2  # the structured-report (v2) contract: prompt instructions + defensive parser

# --- config (env, with in-cluster defaults) ---
NAMESPACE = os.environ.get("NAMESPACE", "krateo-system")
AUTOPILOT_A2A = os.environ.get("AUTOPILOT_A2A_URL", "http://krateo-autopilot.krateo-system.svc:8080/")
APISERVER = os.environ.get("APISERVER", "https://kubernetes.default.svc")
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
GROUP, VERSION, PLURAL = "observability.krateo.io", "v1alpha1", "troubleshootingreports"
A2A_TIMEOUT = int(os.environ.get("A2A_TIMEOUT", "180"))
# HyperDX notifies every eval while an alert stays breached; dedup so we don't spam a new report
# each interval. Skip if one for the same alert is in-flight, or was created within this window.
REPORT_COOLDOWN = int(os.environ.get("REPORT_COOLDOWN", "1800"))
# The per-alert kagent A2A thread is continued (same contextId) across re-runs for rail/deep-link
# continuity, but each re-run forces a COMPLETE fresh RCA (build_prompt rerun=True) that re-pulls ALL
# telemetry (logs + k8s dumps) into that ONE thread. Over many fires the thread's accumulated
# tool-call history grows without bound and eventually overflows the model's input-token window
# (seen live 2026-07-16: 95 runs on one thread -> 1,097,807 tokens > Gemini's 1,048,576 limit -> the
# provider 400s and the RCA can no longer run at all). So the thread is ROTATED every
# CONTEXT_MAX_RUNS runs: the contextId is keyed on a run-count "epoch", so after this many runs a
# fresh empty thread opens and the accumulated context resets. Continuity is preserved within a
# window; no analysis quality is lost across a rotation because the next run re-derives everything
# from scratch anyway. At a conservative ~15k tokens of telemetry per run, 10 runs ~= 150k tokens --
# an order of magnitude under the limit, with ample headroom for a verbose run.
CONTEXT_MAX_RUNS = max(1, int(os.environ.get("CONTEXT_MAX_RUNS", "10")))

# Intra-service auth (Option A). The alert->RCA pipeline is autonomous — it carries NO user JWT —
# but incident-agent's MCP tools sit behind agentgateway, whose authz allows /mcp only with a valid
# Krateo JWT (`has(jwt.sub)`). So we mint a SERVICE identity: exchange this pod's projected
# ServiceAccount token (audience "authn") at authn's /serviceaccount/login for a Krateo JWT, and
# present it on the A2A call — incident-agent (KAGENT_PROPAGATE_TOKEN=true) then propagates it to the
# gateway on the MCP tool calls. Fully OPT-IN and graceful: with no AUTHN_URL / no projected token
# mounted (older clusters, no gateway) the exchange is skipped and the A2A call goes unauthenticated
# exactly as before. Enable by setting AUTHN_URL and mounting an audience-"authn" projected token at
# AUTHN_TOKEN_FILE (a serviceAccountToken projected volume, expirationSeconds ~600).
AUTHN_URL = os.environ.get("AUTHN_URL", "").rstrip("/")
AUTHN_TOKEN_FILE = os.environ.get("AUTHN_TOKEN_FILE", "/var/run/secrets/authn/token")

_create_lock = threading.Lock()  # serialize the dedup-check + create so simultaneous fires don't race
_jwt_cache = {"token": "", "exp": 0.0}
_jwt_lock = threading.Lock()  # serialize the token exchange so concurrent fires reuse one JWT


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sa_token():
    with open(f"{SA_DIR}/token") as f:
        return f.read().strip()


def _service_jwt():
    """Exchange this pod's projected SA token (audience "authn") for a Krateo JWT via authn's
    `serviceaccount` login strategy; cache it until shortly before its own expiry. Returns "" when
    the projected token / AUTHN_URL are absent OR the exchange fails — the A2A call then proceeds
    unauthenticated, exactly as before (never break the RCA on an auth hiccup)."""
    if not AUTHN_URL or not os.path.exists(AUTHN_TOKEN_FILE):
        return ""
    with _jwt_lock:
        if _jwt_cache["token"] and time.time() < _jwt_cache["exp"] - 60:
            return _jwt_cache["token"]
        try:
            with open(AUTHN_TOKEN_FILE) as f:
                sa_tok = f.read().strip()
            r = requests.post(f"{AUTHN_URL}/serviceaccount/login",
                              headers={"Authorization": f"Bearer {sa_tok}"}, timeout=15)
            r.raise_for_status()
            jwt = (r.json() or {}).get("accessToken") or ""
            if not jwt:
                raise ValueError("authn response had no accessToken")
            # Cache until the JWT's own `exp` (decode the base64url payload; pad to a multiple of 4).
            seg = jwt.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
            _jwt_cache.update(token=jwt, exp=float(claims.get("exp") or (time.time() + 300)))
            return jwt
        except Exception as e:  # degrade to unauthenticated rather than failing the analysis
            print(f"[authn] service-JWT exchange failed ({e}); calling A2A unauthenticated", flush=True)
            return ""


def _k8s(method, path, body=None, subresource=""):
    """Call the apiserver with the mounted SA token."""
    url = f"{APISERVER}{path}"
    if subresource:
        url += f"/{subresource}"
    headers = {"Authorization": f"Bearer {_sa_token()}", "Content-Type": "application/json"}
    if method == "PATCH":
        headers["Content-Type"] = "application/merge-patch+json"
    r = requests.request(method, url, headers=headers, data=json.dumps(body) if body else None,
                         verify=f"{SA_DIR}/ca.crt", timeout=30)
    r.raise_for_status()
    return r.json()


RUN_COUNT_ANNO = "observability.krateo.io/run-count"
FIRST_RUN_ANNO = "observability.krateo.io/first-run-at"
LAST_RUN_ANNO = "observability.krateo.io/last-run-at"
CONTEXT_ID_ANNO = "observability.krateo.io/context-id"


def _report_name(alert_name):
    """One DETERMINISTIC report CR per Alert CR (upserted): `report-<Alert metadata.name>`, so a
    re-firing alert bumps a run-count on the SAME report and two Alerts never share one.

    Past 63 characters the name is cut and suffixed with a hash of the whole Alert name, so two
    long names with a common prefix still get two reports."""
    name = f"report-{alert_name}"
    if len(name) <= 63:
        return name
    digest = hashlib.sha256(alert_name.encode()).hexdigest()[:8]
    return f"{name[:54].rstrip('-.')}-{digest}"


def _context_id(report_name, run_count=0):
    """A DETERMINISTIC A2A contextId per report (uuid5 of the report name + a run-count epoch),
    so RCA runs of the SAME alert continue ONE kagent conversation thread for rail/deep-link
    continuity — but that thread is ROTATED every CONTEXT_MAX_RUNS runs so its accumulated telemetry
    can never overflow the model's input-token window. Two DIFFERENT alerts get two distinct threads;
    the same alert gets a fresh thread each epoch. Deterministic + stable across restarts (the epoch
    is derived from the report's persisted run-count, not from process state).

    run_count is the count of THIS run (1-based). Epoch = (run_count - 1) // CONTEXT_MAX_RUNS, so
    runs 1..N share epoch 0, runs N+1..2N share epoch 1, etc. — each epoch a clean, empty thread."""
    epoch = max(0, (int(run_count) - 1)) // CONTEXT_MAX_RUNS
    seed = report_name if epoch == 0 else f"{report_name}#e{epoch}"
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))


def _get_report(ns, name):
    try:
        return _k8s("GET", f"/apis/{GROUP}/{VERSION}/namespaces/{ns}/{PLURAL}/{name}")
    except requests.HTTPError as e:
        if getattr(getattr(e, "response", None), "status_code", None) == 404:
            return None
        raise


def _next_run_count(existing):
    """The 1-based count of the run about to start: previous run-count + 1, or 1 for a first-ever
    report. Kept consistent with the increment in _upsert_report so the rotated contextId's epoch
    matches the run-count the report is stamped with."""
    if existing is None:
        return 1
    anns = (existing.get("metadata") or {}).get("annotations") or {}
    try:
        return int(anns.get(RUN_COUNT_ANNO, "0")) + 1
    except (ValueError, TypeError):
        return 1


def _upsert_report(ns, name, alert_name, alert_state, alert_id, prompt, now, existing,
                   context_id="", alert_ref="", alert_namespace=""):
    """Create the alert's report (run-count=1) or bump the existing one (run-count++, last-run=now),
    setting phase=Analyzing. Run info + the per-alert kagent context id live in annotations — no
    TroubleshootingReport CRD change.

    The join keys (hyperdxAlertId / alertRef / alertNamespace) are sourced from the MATCHED Alert CR
    (not the unreliable webhook payload) so the portal can join incident→alert by a robust HyperDX id
    or a deterministic /alerts/{ns}/{name} slug — never the fragile emoji displayName. They're set on
    every run (create AND patch), since a first run may not have found the Alert yet."""
    join = {"alertState": alert_state or "ALERT", "trigger": "alert", "triggeredAt": now}
    if alert_id:
        join["hyperdxAlertId"] = alert_id          # robust ID join to the Alert's status.hyperdxAlertId
    if alert_ref:
        join["alertRef"] = alert_ref               # the Alert's metadata.name slug → /alerts/{ns}/{name}
    if alert_namespace:
        join["alertNamespace"] = alert_namespace   # the Alert CR's real namespace (not just the report's)
    if existing is None:
        body = {
            "apiVersion": f"{GROUP}/{VERSION}", "kind": "TroubleshootingReport",
            "metadata": {"name": name,
                         "annotations": {RUN_COUNT_ANNO: "1", FIRST_RUN_ANNO: now, LAST_RUN_ANNO: now,
                                         CONTEXT_ID_ANNO: context_id}},
            "spec": ({"alertName": alert_name or "",
                      # default alertNamespace to the report's ns; a matched Alert overrides below
                      "alertNamespace": ns, "prompt": prompt} | join),
        }
        _k8s("POST", f"/apis/{GROUP}/{VERSION}/namespaces/{ns}/{PLURAL}", body)
    else:
        anns = (existing.get("metadata") or {}).get("annotations") or {}
        try:
            count = int(anns.get(RUN_COUNT_ANNO, "0")) + 1
        except (ValueError, TypeError):
            count = 1
        _k8s("PATCH", f"/apis/{GROUP}/{VERSION}/namespaces/{ns}/{PLURAL}/{name}",
             {"metadata": {"annotations": {RUN_COUNT_ANNO: str(count), LAST_RUN_ANNO: now,
                                           CONTEXT_ID_ANNO: context_id}},
              "spec": join})
    patch_status(ns, name, {"phase": "Analyzing"})


def patch_status(ns, name, status):
    _k8s("PATCH", f"/apis/{GROUP}/{VERSION}/namespaces/{ns}/{PLURAL}/{name}", {"status": status}, subresource="status")


def _within_cooldown(report):
    """Dedup: True if this alert's report is mid-analysis, or was last run within REPORT_COOLDOWN —
    so a perpetually-breached alert re-analyzes at most once per window (bumping its run-count then)."""
    if not report:
        return False
    if ((report.get("status") or {}).get("phase")) in ("Pending", "Analyzing"):
        return True
    anns = (report.get("metadata") or {}).get("annotations") or {}
    ts = anns.get(LAST_RUN_ANNO) or (report.get("metadata") or {}).get("creationTimestamp")
    try:
        if ts and (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds() < REPORT_COOLDOWN:
            return True
    except (ValueError, AttributeError):
        pass
    return False


# A DNS-1123 subdomain: what an Alert CR's metadata.name, and so its HyperDX alert's name, can be.
_ALERT_NAME = re.compile(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?")


def _alert_name_from_title(title):
    """The Alert CR name a webhook title carries, or "" if it carries none.

    HyperDX titles a notification with a state emoji ("🚨 " firing, "✅ " resolved) followed by
    the HyperDX alert's name, and the reconciler names that alert after its CR's metadata.name."""
    name = re.sub(r"^\W+", "", (title or "").strip())
    return name if len(name) <= 253 and _ALERT_NAME.fullmatch(name) else ""


def _match_alert(alert_name, ns):
    """The Alert CR (observability.krateo.io) a webhook fired for, by EXACT metadata.name, or None.

    Exact because anything looser picks the wrong CR: two Alerts may share a displayName, and one
    displayName may contain another ("Pod crash-looping" is inside "Krateo — platform pod
    crash-looping"). Returns None when the title names no Alert or the lookup fails."""
    name = _alert_name_from_title(alert_name)
    if not name:
        return None
    try:
        return _k8s("GET", f"/apis/{GROUP}/{VERSION}/namespaces/{ns}/alerts/{name}")
    except Exception as e:  # noqa: BLE001 — a 404 is the common case; HyperDX re-sends while ALERT
        if getattr(getattr(e, "response", None), "status_code", None) != 404:
            print(f"[webhook] Alert {ns}/{name} lookup failed ({e})", flush=True)
        return None


def _tool_results(parts):
    """Every tool RESULT in one A2A message, as report_v2's ledger entry shape.

    THE SHAPE IS report_v2._from_tool_ledger's CONTRACT — `name`, `payload`, `failed` — and it is
    written here to match, not approximated. Two functions deciding the same thing in different
    words is how the 515 status-write bug survived review in git-provider this week.

    THIS IS THE GROUND TRUTH THAT USED TO BE THROWN AWAY. kagent mirrors every non-partial part
    onto the status stream, including the tool-call and tool-result DataParts the ADK stamps
    `adk_type: function_response` — so "k8s_get_resources returned Forbidden" was already on the
    wire, one line above the filter that kept only `kind == "text"`. The observer could publish
    0.97 confidence on an analysis in which every cluster read was denied precisely because the
    denial never reached the code that scores the report (#30).

    Defensive by construction: kagent's exact part shape has changed before and this must never be
    the reason an analysis fails. Anything unrecognised yields nothing and the report degrades to
    model-declared retrieval, which is what 0.2.37 already did."""
    out = []
    for p in parts or []:
        if not isinstance(p, dict) or p.get("kind") == "text":
            continue
        data = p.get("data")
        if not isinstance(data, dict):
            continue
        if "function_response" not in str(data.get("adk_type", "")):
            continue
        resp = data.get("response")
        if resp is None:
            resp = {k: v for k, v in data.items() if k not in ("adk_type", "name", "id")}
        payload = resp if isinstance(resp, str) else json.dumps(resp, default=str)
        # `failed` says the CALL errored, which is what lets report_v2 tell a refusal we suffered
        # from a refusal we are REPORTING. The ADK does not set a uniform flag, so infer it from
        # the response carrying an error and let the text classifier settle denied-vs-errored.
        failed = bool(isinstance(resp, dict) and (resp.get("error") or resp.get("isError")))
        out.append({"name": str(data.get("name") or ""), "payload": payload, "failed": failed})
    return out


def a2a_analyze(prompt, context_id=None):
    """(text, tool_ledger) from the Autopilot A2A agent.

    A stable `context_id` continues ONE kagent thread across re-runs of the same alert (omitted =
    a fresh thread). `tool_ledger` is what the agent's tools ACTUALLY returned; report_v2 uses it
    to bound confidence rather than trusting the model's account of its own evidence (#30)."""
    message = {"kind": "message", "messageId": str(uuid.uuid4()), "role": "user",
               "parts": [{"kind": "text", "text": prompt}]}
    if context_id:
        message["contextId"] = context_id
    body = {"id": 1, "jsonrpc": "2.0", "method": "message/stream", "params": {"message": message}}
    out = ""
    ledger, seen = [], set()
    headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
    # Present the service JWT so incident-agent can propagate it to its agentgateway-gated MCP tools.
    jwt = _service_jwt()
    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"
    with requests.post(AUTOPILOT_A2A, json=body, stream=True, timeout=A2A_TIMEOUT,
                       headers=headers) as resp:
        resp.raise_for_status()
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            try:
                payload = json.loads(raw[len("data:"):].strip())
            except json.JSONDecodeError:
                continue
            result = payload.get("result") or {}
            msg = result.get("status", {}).get("message") or result.get("message") or {}
            if msg.get("role") != "agent":
                continue
            parts = msg.get("parts", [])
            # Collected BEFORE the `if not text: continue` below: a message carrying only tool
            # results has no text at all, and those are the very events that record a denial.
            # The stream re-sends cumulative snapshots, so de-duplicate by (name, payload).
            for tr in _tool_results(parts):
                key = (tr["name"], tr["payload"])
                if key not in seen:
                    seen.add(key)
                    ledger.append(tr)
            text = "".join(p["text"] for p in parts
                           if p.get("kind") == "text" and p.get("text"))
            if not text:
                continue
            # kagent's A2A stream re-sends cumulative snapshots of the message (and repeats the
            # final one), so blindly appending every event duplicated the whole report. Replace
            # when the new text extends what we already have (a snapshot), skip an exact-duplicate
            # tail, else append (a genuine delta) — correct for snapshot-, delta-, and repeat streams.
            if text.startswith(out):
                out = text
            elif not out.endswith(text):
                out += text
    return out.strip(), ledger


def build_prompt(alert_name, alert_state, where=None, message=None, rerun=False):
    """The user message carries the INCIDENT; the agent's system prompt carries the METHOD.

    This used to restate the investigation itself — find the rows by Body, add no severity filter,
    then read the workload's k8s state, do not substitute a louder problem — every line of which
    the incident-agent system prompt already says, several of them verbatim. Re-teaching it here
    bought nothing and was re-sent on every turn of the conversation.

    THE COUPLING THIS CREATES: the method now lives in ONE place, so AUTOPILOT_A2A_URL must point
    at an agent whose prompt carries it (incident-agent does; a bare generalist does not). Point
    this at a different agent and you must put the method back.
    """
    # RE-RUNS: the stable per-alert kagent thread makes the agent 'remember' its previous
    # answer and short-circuit ('I already analyzed this') — which parses to NOTHING and
    # starves re-analysis (seen live 2026-07-14: every post-first run returned 0 chars).
    # Force a complete fresh pass while keeping the thread (rail continuity).
    rerun_preamble = ""
    if rerun:
        rerun_preamble = (
            "RE-ANALYSIS REQUEST: this alert has fired again. Do NOT refer to, summarize or defer "
            "to your previous answers in this conversation. Re-verify against the CURRENT cluster "
            "and telemetry state, and output the COMPLETE analysis again — including the full "
            "structured JSON block — as if this were the first request.\n\n"
        )
    scope = ""
    if where:
        scope = (
            f"\n\nIt fired on log records matching `{where}`"
            + (f" — intent: {message}" if message else "")
            + ". That query is your entry point, and the workload those rows name is what you "
            "diagnose."
        )
    return rerun_preamble + (
        f'The HyperDX alert "{alert_name}" has fired (state {alert_state}) on this Krateo '
        "PlatformOps cluster." + scope +
        "\n\nRoot-cause it: the single most likely cause, the composition or component affected, "
        "and an ordered remediation plan."
        + report_v2.STRUCTURED_OUTPUT_INSTRUCTIONS
    )


def process(payload):
    # The body is hyperdx_v2.DEFAULT_WEBHOOK_BODY: alertName is the notification title, which
    # carries the Alert CR's metadata.name, and state is ALERT or OK.
    title = (payload.get("alertName") or payload.get("title") or payload.get("name")
             or (payload.get("alert") or {}).get("name") or "")
    alert_state = str(payload.get("state") or payload.get("status") or "ALERT").upper()
    alert_ns = payload.get("alertNamespace") or NAMESPACE
    if alert_state == "OK":
        print(f"[webhook] {title!r} resolved; nothing to analyze", flush=True)
        return
    # The fired Alert CR scopes the RCA (spec.where/message) and carries the join keys the report
    # needs (status.hyperdxAlertId + metadata.name/namespace). Without one there is no scope, so
    # no RCA runs.
    matched = _match_alert(title, alert_ns)
    if matched is None:
        print(f"[webhook] {title!r} names no Alert in {alert_ns}; skipping", flush=True)
        return
    m_spec = matched.get("spec") or {}
    m_meta = matched.get("metadata") or {}
    m_status = matched.get("status") or {}
    where, message = m_spec.get("where"), m_spec.get("message")
    alert_id = m_status.get("hyperdxAlertId") or ""          # ID join → Alert.status.hyperdxAlertId
    alert_ref = m_meta.get("name", "")                        # exact key → /alerts/{ns}/{name}
    alert_namespace = m_meta.get("namespace") or alert_ns     # the Alert CR's real namespace
    display = m_spec.get("displayName") or alert_ref
    prompt = None  # built after the dedup gate, when we know if this is a re-run
    name = _report_name(alert_ref)
    now = _now()
    # One report CR per alert, upserted under a lock: re-fires within the cooldown are skipped;
    # otherwise the same CR is re-analyzed and its run-count bumped — no pile-up of duplicate reports.
    with _create_lock:
        existing = _get_report(alert_ns, name)
        if _within_cooldown(existing):
            print(f"[dedup] {alert_ref!r} analyzed recently / in-flight; skipping", flush=True)
            return
        # THIS run's 1-based count = previous run-count + 1 (1 for a first-ever report). The kagent
        # thread is keyed on it so the thread rotates every CONTEXT_MAX_RUNS runs — bounding the
        # accumulated telemetry well under the model's input-token limit.
        run_count = _next_run_count(existing)
        ctx = _context_id(name, run_count)  # per-report thread, rotated every CONTEXT_MAX_RUNS
        prompt = build_prompt(display, alert_state, where, message, rerun=bool(existing))
        _upsert_report(alert_ns, name, display, alert_state, alert_id, prompt, now, existing,
                       ctx, alert_ref=alert_ref, alert_namespace=alert_namespace)
    try:
        raw, tool_ledger = a2a_analyze(prompt, ctx)
        # v2: split the answer into prose + the structured investigation. Parsing is defensive —
        # a missing/malformed JSON block degrades to a prose-only (v1) report, never a crash.
        print(f"[a2a] raw reply: {len(raw)} chars, {len(tool_ledger)} tool results", flush=True)
        prose, v2 = report_v2.parse_structured_report(raw, tool_ledger)
        # KEEP-LAST-GOOD: an EMPTY analysis (no prose AND no structure) is a FAILED run, not a
        # result — never let it overwrite a previous good investigation (seen live 2026-07-14:
        # an empty A2A reply wiped a full structured RCA to "Autopilot returned no analysis").
        # Record the failed re-run on the CR without touching report/v2 fields.
        if not prose.strip() and not v2:
            existing_now = _get_report(alert_ns, name) or {}
            had_analysis = bool(((existing_now.get("status") or {}).get("report") or "").strip()) \
                or bool((existing_now.get("status") or {}).get("rootCause"))
            if had_analysis:
                patch_status(alert_ns, name, {"phase": "Ready",
                                              "error": f"re-analysis at {_now()} returned no output; kept previous analysis"})
                print(f"[warn] report {alert_ns}/{name}: empty analysis — kept previous (keep-last-good)", flush=True)
                return
        status = {"phase": "Ready", "report": prose or "_Autopilot returned no analysis._",
                  "completedAt": _now(), "lifecycle": "open", "auditRecordRefs": []}
        # Latest-run-wins: ALWAYS send every v2 key — parsed value, or None (JSON null, which
        # merge-patch DELETES) so a re-run that lost structure also clears the stale previous one.
        for k in report_v2.V2_STATUS_KEYS:
            status[k] = v2.get(k)
        patch_status(alert_ns, name, status)
        print(f"[ok] report {alert_ns}/{name} ready ({len(prose)} chars prose, "
              f"structured={'yes' if v2 else 'no'})", flush=True)
    except Exception as e:  # noqa: BLE001 — record the failure on the CR
        patch_status(alert_ns, name, {"phase": "Failed", "error": str(e)[:500], "completedAt": _now()})
        print(f"[err] report {alert_ns}/{name} failed: {e}", flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # health
        self.send_response(200 if self.path == "/healthz" else 404)
        self.end_headers()
        self.wfile.write(b"ok" if self.path == "/healthz" else b"")

    def do_POST(self):
        # Accept any POST path as a webhook: HyperDX redacts the webhook URL path to `/****` in
        # its API and may deliver to a redacted/normalised path, so we don't gate on "/webhook".
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        print(f"[webhook] POST {self.path} ({length}B)", flush=True)  # observe the delivered path
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {"raw": raw.decode("utf-8", "replace")}
        # Ack fast; analyse in the background so HyperDX's webhook doesn't time out.
        threading.Thread(target=process, args=(payload,), daemon=True).start()
        self.send_response(202); self.end_headers(); self.wfile.write(b"accepted")

    def log_message(self, *args):  # quieter logs
        pass


if __name__ == "__main__":
    # Background: reconcile Alert CRs -> HyperDX (config + status) via the session API.
    if os.environ.get("RECONCILER_ENABLED", "true").lower() == "true":
        import reconciler  # imported here so the webhook path has no hard dep on it
        threading.Thread(target=reconciler.run_forever, daemon=True).start()
    # Background: observe AuditRecords -> fill each applied remediation step's observedOutcome +
    # auditRecordRefs, closing the remediation loop (the portal flips a step to "applied" on a
    # non-empty observedOutcome). Correct-but-dormant when provenance isn't enabled / no records flow.
    if os.environ.get("OBSERVER_ENABLED", "true").lower() == "true":
        import observer  # imported here so the webhook path has no hard dep on it
        threading.Thread(target=observer.run_forever, daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    print(f"krateo-alert-troubleshooter listening on :{port} → A2A {AUTOPILOT_A2A}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
