#!/usr/bin/env python3
"""krateo-alert-troubleshooter — turns an alert firing into an Incident with a root-cause analysis.

Both triggers, a HyperDX webhook (process) and an apiRef alert the reconciler evaluates, call
analyze(), which applies Policy A:
  * the alert has an open Incident (any state but Resolved and Closed): count the firing on it;
  * it has none: create one in state Analyzing, run the incident-agent RCA over A2A, and write the
    analysis with its howToFix scripts in state Open.
From Open on, the incident controller runs the scripts and moves the Incident. Stdlib + requests.
"""
import base64
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import requests

import report_v2  # the structured-report contract: prompt instructions + defensive parser

# --- config (env, with in-cluster defaults) ---
NAMESPACE = os.environ.get("NAMESPACE", "krateo-system")
AUTOPILOT_A2A = os.environ.get("AUTOPILOT_A2A_URL", "http://krateo-autopilot.krateo-system.svc:8080/")
APISERVER = os.environ.get("APISERVER", "https://kubernetes.default.svc")
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
GROUP, VERSION = "observability.krateo.io", "v1alpha1"
A2A_TIMEOUT = int(os.environ.get("A2A_TIMEOUT", "180"))
# How much of an apiRef alert's `items` goes into the RCA prompt.
ITEMS_PROMPT_CHARS = 4000

# The Incident contract (incident-controller apis/incident/v1alpha1).
LABEL_ALERT = "observability.krateo.io/alert"  # value: the Alert's metadata.name, so at most 63 chars
ENDED = ("Resolved", "Closed")                 # an incident in any other state is open
WRITE_ATTEMPTS = 5                             # conditioned status writes retried on a 409

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

_create_lock = threading.Lock()  # one firing at a time finds-or-creates, so two cannot both open one
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


def _http_status(e):
    return getattr(getattr(e, "response", None), "status_code", None)


def _incidents(ns, name=""):
    path = f"/apis/{GROUP}/{VERSION}/namespaces/{ns}/incidents"
    return f"{path}/{name}" if name else path


def incident_name(alert_ref, at):
    """`<alert>-<yyyymmdd-hhmmss>` of the opening firing, in UTC."""
    return f"{alert_ref}-{at.strftime('%Y%m%d-%H%M%S')}"


def _context_id(name):
    """The incident's own kagent thread: every RCA starts from an empty conversation."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


def _open_incident(ns, alert_ref):
    """The alert's open Incident, the newest if there are several, or None."""
    selector = quote(f"{LABEL_ALERT}={alert_ref}", safe="")
    items = _k8s("GET", f"{_incidents(ns)}?labelSelector={selector}").get("items") or []
    open_ = [i for i in items if (i.get("status") or {}).get("state") not in ENDED]
    return max(open_, default=None,
               key=lambda i: (i["metadata"].get("creationTimestamp", ""), i["metadata"]["name"]))


def _patch_status(ns, incident, status):
    """Merge `status` into the Incident's status, conditioned on the resourceVersion it was read
    at: the controller writes the same status, so a 409 means re-read and retry."""
    body = {"metadata": {"resourceVersion": incident["metadata"]["resourceVersion"]},
            "status": status}
    return _k8s("PATCH", _incidents(ns, incident["metadata"]["name"]), body, subresource="status")


def _count_firing(ns, incident, now):
    """firings++ and lastFiredAt on an open incident. False on a lost race (409)."""
    firings = int((incident.get("status") or {}).get("firings") or 0)
    try:
        _patch_status(ns, incident, {"firings": firings + 1, "lastFiredAt": now})
        return True
    except requests.HTTPError as e:
        if _http_status(e) == 409:
            return False
        raise


def _open_or_count(ns, alert_ref, prompt, at):
    """Policy A for one firing: count it on the alert's open incident, or create one.

    Returns the new Incident, or None when the firing was counted on an open one."""
    now = at.isoformat()
    name = incident_name(alert_ref, at)
    for _ in range(WRITE_ATTEMPTS):
        open_ = _open_incident(ns, alert_ref)
        if open_ is not None:
            if _count_firing(ns, open_, now):
                print(f"[incident] {ns}/{open_['metadata']['name']}: firing counted", flush=True)
                return None
            continue
        body = {"apiVersion": f"{GROUP}/{VERSION}", "kind": "Incident",
                "metadata": {"name": name, "namespace": ns, "labels": {LABEL_ALERT: alert_ref}},
                "spec": {"alertRef": {"name": alert_ref, "namespace": ns}, "trigger": "alert",
                         "prompt": prompt, "triggeredAt": now}}
        try:
            created = _k8s("POST", _incidents(ns), body)
        except requests.HTTPError as e:
            if _http_status(e) == 409:  # a concurrent firing created it: count on it instead
                continue
            raise
        # Unconditioned: the controller may already have touched the new object, and no other
        # writer sets these fields yet.
        _k8s("PATCH", _incidents(ns, name),
             {"status": {"state": "Analyzing", "firings": 1, "lastFiredAt": now}},
             subresource="status")
        print(f"[incident] {ns}/{name}: opened", flush=True)
        return created
    raise RuntimeError(f"alert {ns}/{alert_ref}: no incident write succeeded in "
                       f"{WRITE_ATTEMPTS} attempts")


def _finish(ns, name, status):
    """Write the analysis. An incident still Analyzing becomes Open; in any other state (a human
    closed it, or applied a fix, while it was analyzing) the analysis is written without a state,
    since Closed is final and the controller owns the rest."""
    for _ in range(WRITE_ATTEMPTS):
        incident = _k8s("GET", _incidents(ns, name))
        body = dict(status)
        if (incident.get("status") or {}).get("state") in (None, "Analyzing"):
            body["state"] = "Open"
        try:
            _patch_status(ns, incident, body)
            return
        except requests.HTTPError as e:
            if _http_status(e) != 409:
                raise
    raise RuntimeError(f"incident {ns}/{name}: no status write succeeded in {WRITE_ATTEMPTS} attempts")


INTERRUPTED = "The analysis was interrupted: the troubleshooter restarted before it finished."


def recover_interrupted(ns=NAMESPACE):
    """Open every incident a restart left Analyzing (or before its first status write), with the
    reason in `error`.

    An Analyzing incident is open, so it would count every later firing of its alert and never be
    checked. Open, it can be closed, and the next firing opens a fresh one. An analysis still
    running in another replica overwrites `error` when it finishes."""
    try:
        items = _k8s("GET", f"{_incidents(ns)}?labelSelector={quote(LABEL_ALERT, safe='')}")
    except Exception as e:  # noqa: BLE001 — no Incident CRD yet, or no apiserver: nothing to recover
        print(f"[incident] recovery skipped ({e})", flush=True)
        return
    for incident in items.get("items") or []:
        if (incident.get("status") or {}).get("state") not in (None, "Analyzing"):
            continue
        try:
            _patch_status(ns, incident, {"state": "Open", "error": INTERRUPTED})
            print(f"[incident] {ns}/{incident['metadata']['name']}: interrupted, opened", flush=True)
        except Exception as e:  # noqa: BLE001 — a 409 means another writer moved it
            print(f"[incident] {ns}/{incident['metadata']['name']}: recovery skipped ({e})",
                  flush=True)


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


def build_prompt(alert_name, alert_state, where=None, message=None, api=None):
    """The user message carries the INCIDENT; the agent's system prompt carries the METHOD.

    THE COUPLING THIS CREATES: the method lives in ONE place, so AUTOPILOT_A2A_URL must point at an
    agent whose prompt carries it (incident-agent does; a bare generalist does not). Point this at a
    different agent and you must put the method back.
    """
    scope = ""
    if where:
        scope = (
            f"\n\nIt fired on log records matching `{where}`"
            + (f" — intent: {message}" if message else "")
            + ". That query is your entry point, and the workload those rows name is what you "
            "diagnose."
        )
    source = "HyperDX alert"
    if api:
        # An apiRef alert: the value came from a RESTAction, and its items are the objects that
        # matched, so they are the entry point.
        source = "Krateo alert"
        scope = (
            f"\n\nIt fired because RESTAction `{api['namespace']}/{api['name']}` returned value "
            f"{api['value']} ({api['thresholdType']} {api['threshold']})"
            + (f" — intent: {message}" if message else "") + "."
        )
        items = api.get("items")
        if items:
            shown = json.dumps(items, default=str)
            if len(shown) > ITEMS_PROMPT_CHARS:
                shown = shown[:ITEMS_PROMPT_CHARS] + " …(truncated)"
            scope += (" The objects it matched are your entry point, and they are what you "
                      f"diagnose:\n{shown}")
    return (
        f'The {source} "{alert_name}" has fired (state {alert_state}) on this Krateo '
        "PlatformOps cluster." + scope +
        "\n\nRoot-cause it: the single most likely cause, the composition or component affected, "
        "and how to fix it."
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
    # The fired Alert CR scopes the RCA (spec.where/message) and names the incident
    # (metadata.name/namespace). Without one there is no scope, so nothing is recorded.
    matched = _match_alert(title, alert_ns)
    if matched is None:
        print(f"[webhook] {title!r} names no Alert in {alert_ns}; skipping", flush=True)
        return
    m_spec = matched.get("spec") or {}
    m_meta = matched.get("metadata") or {}
    where, message = m_spec.get("where"), m_spec.get("message")
    alert_ref = m_meta.get("name", "")                        # exact key → /alerts/{ns}/{name}
    alert_namespace = m_meta.get("namespace") or alert_ns     # the Alert CR's real namespace
    analyze(m_spec.get("displayName") or alert_ref, alert_state, alert_ref, alert_namespace,
            where=where, message=message)


def analyze(alert_name, alert_state, alert_ref, alert_namespace, where=None, message=None,
            api=None):
    """One firing of an alert, through Policy A.

    Both triggers call it: the HyperDX webhook (process) and the evaluation of an apiRef alert
    (reconciler), which passes `api` = {name, namespace, value, threshold, thresholdType, items}.
    `alert_name` is the displayName, `alert_ref` the Alert's metadata.name; the incident lives in
    the Alert's namespace. A firing on an open incident is counted on it and runs no RCA.
    """
    if len(alert_ref) > 63:
        print(f"[incident] alert {alert_ref!r}: a name over 63 characters cannot label an "
              "Incident; skipping", flush=True)
        return
    ns = alert_namespace
    prompt = build_prompt(alert_name, alert_state, where, message, api=api)
    try:
        with _create_lock:
            created = _open_or_count(ns, alert_ref, prompt, datetime.now(timezone.utc))
    except Exception as e:  # noqa: BLE001 — a failed write loses this firing, not the next one
        print(f"[err] alert {ns}/{alert_ref}: firing not recorded ({e})", flush=True)
        return
    if created is not None:
        run_analysis(ns, created["metadata"]["name"], prompt)


def run_analysis(ns, name, prompt):
    """The RCA of a new incident, then its one status write: the analysis and state Open.

    `error` says why an incident has no howToFix: the call failed, the answer was empty or
    unstructured, or its scripts were unusable. It is cleared when howToFix is written."""
    status = {}
    try:
        raw, tool_ledger = a2a_analyze(prompt, _context_id(name))
        print(f"[a2a] {ns}/{name}: {len(raw)} chars, {len(tool_ledger)} tool results", flush=True)
        prose, v2 = report_v2.parse_structured_report(raw, tool_ledger)
        if prose.strip():
            status["report"] = prose
        status.update({k: v2[k] for k in report_v2.V2_STATUS_KEYS if k in v2})
        if "howToFix" in v2:
            status["error"] = None
        elif not prose.strip() and not v2:
            status["error"] = "The analysis returned no output."
        elif not v2:
            status["error"] = ("The analysis returned no structured block, so the incident has no "
                               "scripts to check or fix it.")
        else:
            status["error"] = ("The analysis returned no usable howToFix, so the incident has no "
                               "scripts to check or fix it.")
    except Exception as e:  # noqa: BLE001 — a failed RCA still opens the incident
        status["error"] = f"The analysis failed: {str(e)[:500]}"
    status["completedAt"] = _now()
    try:
        _finish(ns, name, status)
        print(f"[ok] incident {ns}/{name}: analysis written "
              f"(howToFix={'yes' if status.get('howToFix') else 'no'})", flush=True)
    except Exception as e:  # noqa: BLE001 — recover_interrupted opens it after a restart
        print(f"[err] incident {ns}/{name}: analysis not written ({e})", flush=True)


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
    # Before any firing can open an incident, so only incidents a previous process left are swept.
    recover_interrupted()
    # Background: reconcile Alert CRs -> HyperDX (config + status) via the session API.
    if os.environ.get("RECONCILER_ENABLED", "true").lower() == "true":
        import reconciler  # imported here so the webhook path has no hard dep on it
        threading.Thread(target=reconciler.run_forever, daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    print(f"krateo-alert-troubleshooter listening on :{port} → A2A {AUTOPILOT_A2A}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
