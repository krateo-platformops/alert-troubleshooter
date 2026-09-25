"""TroubleshootingReport v2 — the structured-investigation contract with the Autopilot agent.

Owns BOTH sides of the contract so they can't drift:
  * STRUCTURED_OUTPUT_INSTRUCTIONS — appended to the RCA prompt; requires the agent to end its
    answer with ONE fenced ```json block matching the v2 status fields.
  * parse_structured_report(text) — defensively extracts + sanitizes that block into the CR's
    status fields. NEVER raises: any malformed/missing JSON degrades to a prose-only (v1) report.

Sanitizing rules (defensive, latest-run-wins):
  * unknown keys are dropped; wrong-typed values are coerced when safe, else dropped;
  * sources[].type outside the enum falls back to "object" (evidence is kept, never lost);
  * reasoningTrace[].evidenceRefs are validated against len(sources): out-of-bounds/non-int
    indices are dropped (the step is kept — a step may legitimately lose a bad citation);
  * steps are renumbered 1..N in the order given (the agent's order is authoritative);
  * rootCause.confidence normalizes number-or-string to a "0.00"-style decimal string in [0,1];
  * howToFix is all three scripts or nothing — see _how_to_fix;
  * confidence is then BOUNDED by the evidence that was actually retrieved — see the evidence
    policy below.

Stdlib only (json/re) — unit-testable without the cluster or requests.
"""
import json
import re

TRIGGERS = ("alert", "composition-condition", "user-ask")
SOURCE_TYPES = ("logs", "events", "metrics", "object")
LIFECYCLES = ("open", "mitigated", "resolved")

# Every v2 key the handler writes under .status. The Ready patch always carries ALL of them
# (parsed value or None→JSON null, which merge-patch DELETES) so a re-run that fails to produce
# structure also clears the previous run's structure — no stale mixed-run investigation.
V2_STATUS_KEYS = ("analyzedResources", "sources", "missingContext", "assumptions",
                  "reasoningTrace", "rootCause", "howToFix", "evidence")

# status.howToFix: bash scripts. The incident controller runs precondition and verify in a
# read-only sandbox (exit 0 = the incident is gone, 1 = it holds, anything else = unknown); a human
# runs apply.
HOW_TO_FIX_SCRIPTS = ("precondition", "apply", "verify")
SCRIPT_MAX_CHARS = 16384

STRUCTURED_OUTPUT_INSTRUCTIONS = """

After the markdown analysis, END your answer with EXACTLY ONE fenced ```json code block (no text
after it) containing a machine-readable summary of the SAME investigation, with these keys:

{
  "analyzedResources": [{"gvr": "apps/v1/deployments", "name": "...", "namespace": "...", "whatWasRead": "status + last events"}],
  "sources": [{"type": "logs|events|metrics|object", "ref": "where this evidence came from", "excerpt": "short verbatim excerpt"}],
  "retrieval": [{"source": "k8s|logs|metrics|repo", "scope": "pods in krateo-system", "outcome": "success|denied|empty|errored", "detail": "Forbidden: cannot list pods"}],
  "missingContext": ["what you could NOT see"],
  "assumptions": ["what you assumed because of that"],
  "reasoningTrace": [{"step": 1, "statement": "...", "evidenceRefs": [0]}],
  "rootCause": {"statement": "...", "confidence": 0.85, "category": "config|capacity|image|network|dependency|other"},
  "howToFix": {"precondition": "<bash script>", "apply": "<bash script>", "verify": "<bash script>"}
}

Hard rules for this block:
- analyzedResources/sources list ONLY what you actually read this run — never invent entries.
- missingContext and assumptions MUST be honest: if you could not read something you wanted
  (logs, metrics, an object, RBAC-denied, empty results), SAY SO there instead of papering over it.
- EVERY reasoningTrace step MUST cite evidence: evidenceRefs are 0-based indices into "sources",
  and every step needs at least one. A step you cannot back with a listed source belongs in
  "assumptions", not in the trace.
- RETRIEVAL LEDGER: "retrieval" records, for EVERY source you tried to read this run, what came
  back — outcome "success" (you got data), "empty" (the read ran and returned nothing), "denied"
  (authz refused you: Forbidden, 403, "cannot list ..."), "errored" (the tool itself failed:
  timeout, no healthy backend, 5xx). List the reads that produced NOTHING too — especially those.
  "empty" and "denied" are DIFFERENT facts and must never be merged: "there are no matching pods"
  and "I am not allowed to look at pods" support opposite conclusions, and reporting a denial as
  an absent section hides it. A denial you are REPORTING ON (some other account was refused) is a
  FINDING, not your own retrieval outcome — keep it in sources, not here.
- CONFIDENCE IS A FUNCTION OF THE EVIDENCE YOU ACTUALLY RETRIEVED, not of how coherent your story
  is. Start from what you read and discount for every source you depended on and did not get:
    * a depended-on source denied  → confidence AT MOST 0.4 (you are guessing about what it held)
    * a depended-on source errored → confidence AT MOST 0.6
    * nothing retrieved at all     → confidence AT MOST 0.2
  A conclusion you could not check against the cluster is a hypothesis; score it like one. These
  ceilings are ALSO enforced after you answer, so an unbounded number is simply overwritten —
  declaring an honest one is how your own reasoning stays visible in the report.
- Do NOT state a numeric confidence anywhere in the markdown prose — only in this JSON block. The
  published confidence may be bounded down by the retrieval ledger, and a percentage left in the
  prose would then contradict the report's own status.

HOW TO FIX. "howToFix" is the fix as three bash scripts, each ONE JSON string (newlines as \\n,
double quotes and backslashes escaped). A controller runs precondition and verify and moves the
incident on their exit codes; a human reviews apply and runs it. Nothing else runs automatically.
- EXIT CODES, precondition and verify alike: 0 = the incident is gone, 1 = it still holds,
  anything else or a timeout = unknown, which changes nothing. Exit 2 whenever the script cannot
  tell.
- PRECONDITION asks "does the incident still hold?". It first runs right after your analysis and
  MUST exit 1 then: a 0 on that first run flags the incident as not reproduced. Build its test from
  the values you read this run, so it holds against the state you saw.
- TEST THE ROOT-CAUSE OBJECT, NEVER THE ALERT'S SIGNAL. Read the one object rootCause names, by
  kind, name and namespace, and test the field that is wrong on it. Never re-run the alert's query,
  count its rows or re-evaluate everything it matched: the alert can keep firing for a second
  reason, and the check must still tell whether THIS cause is gone. Test current state (spec,
  status, existence), not logs or Events: those describe the past and expire, and a check built on
  them reports "gone" when they do.
- VERIFY asks "did the fix work?": the root cause is gone and what it broke is healthy again. Test
  that outcome, not the literal change apply makes, since a human may fix it another way. It runs
  after the precondition exits 0 or a human marks apply done, and is retried for a settle window of
  about five minutes, so read once; do not wait or poll.
- THE SANDBOX. Precondition and verify run in a pod with only bash, kubectl and jq, as a
  ServiceAccount that can get and list but never write, and cannot read Secrets. Its network
  reaches only the Kubernetes API server, and it is killed after 60 seconds. So a check uses bash
  builtins, read-only kubectl and jq, and nothing else (no grep, sed, awk or curl; no ClickHouse,
  HyperDX or snowplow), and finishes in seconds. kubectl already reaches the right cluster and the
  controller enforces the deadline: pass no --server, --kubeconfig, --context or --request-timeout.
  When the root-cause object is a Secret, test the object whose state shows it (the workload that
  mounts it) and say so in assumptions.
- A FAILED READ IS UNKNOWN, NEVER A VERDICT. kubectl exits 1 on any error (Forbidden, NotFound,
  timeout), and 1 means "still holds". So no `set -e` in a check; end every read with `|| exit 2`;
  map a `jq -e` test explicitly (0 = true, 1 = false, any other code is an error, so exit 2). When
  absence IS the answer, read with `--ignore-not-found` and decide what empty output means.
- Status can describe the previous spec: when a kind reports status.observedGeneration, trust its
  status only once that reaches metadata.generation.
- Name a custom resource by resource.group (alerts.observability.krateo.io): short names collide
  across API groups.
- APPLY is the corrective write, reviewed and run by a human with bash, kubectl and jq. It is
  readable (a comment saying what it changes and why) and idempotent: a second run changes nothing.
  It may `set -euo pipefail`. Prefer kubectl's own verbs (set image, set resources, scale) to
  hand-written patches. A `--type merge` patch replaces every list it touches, so never merge-patch
  a list such as containers; built-in kinds take kubectl's default strategic patch, which merges
  containers by name.
- BE ACTIONABLE: when the evidence identifies a safe corrective write (adjust a limit, fix an image
  tag, narrow an Alert's where, delete a stuck object), apply makes it; an apply that only reads is
  a MISS. Only when you genuinely cannot determine a safe write does apply hold comments alone,
  saying what a human must decide, with the reason in missingContext.
- VERIFY IT EXISTS: every object a script names is one you read this run. If a workload you would
  fix is missing, do not create or provision it in apply; report it in missingContext.
- apply never deletes a Namespace, a Node or a CustomResourceDefinition: each takes everything
  under it along.
- Every script starts with `#!/usr/bin/env bash` and a comment line saying what it tests or changes.

Example: payments-api is OOMKilled at a 128Mi memory limit.
  precondition:
    #!/usr/bin/env bash
    # Holds while payments-api still has the 128Mi memory limit it was OOMKilled at.
    d=$(kubectl get deployment payments-api -n payments -o json) || exit 2
    jq -e '.spec.template.spec.containers[] | select(.name == "payments-api")
      | .resources.limits.memory == "128Mi"' <<<"$d" >/dev/null
    case $? in 0) exit 1 ;; 1) exit 0 ;; *) exit 2 ;; esac
  apply:
    #!/usr/bin/env bash
    # Raise payments-api's memory limit from 128Mi, where it was OOMKilled, to 512Mi.
    set -euo pipefail
    kubectl set resources deployment payments-api -n payments -c payments-api --limits=memory=512Mi
  verify:
    #!/usr/bin/env bash
    # Fixed once payments-api is off the 128Mi limit and every replica runs the new spec.
    d=$(kubectl get deployment payments-api -n payments -o json) || exit 2
    jq -e '(.spec.template.spec.containers[] | select(.name == "payments-api")
        | .resources.limits.memory != "128Mi")
      and .status.observedGeneration >= .metadata.generation
      and .status.updatedReplicas == .spec.replicas
      and .status.availableReplicas == .spec.replicas' <<<"$d" >/dev/null
    case $? in 0) exit 0 ;; 1) exit 1 ;; *) exit 2 ;; esac

ALERT QUERY FALSE-POSITIVES: when the rows an Alert counts are written by the telemetry pipeline
itself (a component that evaluates, stores or analyzes the alert logs text matching the alert's
own where), the root-cause object is the Alert, not a workload. The fix narrows its spec.where
(ClickHouse SQL) to exclude the services those rows name. Take the names from the rows you read,
never from memory, and never target a Deployment or Composition for it: the rule lives in the
Alert. With <alert>, <namespace>, <svc-a> and <svc-b> filled in:
  precondition:
    #!/usr/bin/env bash
    # Holds while <alert>'s where does not exclude <svc-a> and <svc-b>.
    w=$(kubectl get alerts.observability.krateo.io <alert> -n <namespace> -o jsonpath='{.spec.where}') || exit 2
    for s in <svc-a> <svc-b>; do
      [[ $w == *"'$s'"* ]] || exit 1
    done
    exit 0
  apply:
    #!/usr/bin/env bash
    # Exclude <svc-a> and <svc-b>, whose logs echo this alert's own query, from its where.
    set -euo pipefail
    x="ServiceName NOT IN ('<svc-a>', '<svc-b>')"
    w=$(kubectl get alerts.observability.krateo.io <alert> -n <namespace> -o jsonpath='{.spec.where}')
    case "$w" in *"$x"*) exit 0 ;; esac
    kubectl patch alerts.observability.krateo.io <alert> -n <namespace> --type merge \\
      -p "$(jq -n --arg w "($w) AND $x" '{spec: {where: $w}}')"
  verify:
    #!/usr/bin/env bash
    # Fixed once the where excludes both services and the reconciler has pushed it (phase Synced).
    o=$(kubectl get alerts.observability.krateo.io <alert> -n <namespace> -o json) || exit 2
    jq -e --arg a "'<svc-a>'" --arg b "'<svc-b>'" \\
      '(.spec.where // "" | contains($a) and contains($b)) and .status.phase == "Synced"' <<<"$o" >/dev/null
    case $? in 0) exit 0 ;; 1) exit 1 ;; *) exit 2 ;; esac

Output STRICT JSON (double quotes, no comments, no trailing commas)."""

# Fence LINES (```lang or bare ```), walked as sequential open/close PAIRS. A single
# pairing regex mis-pairs when a non-json block precedes (a ```yaml example's CLOSING
# fence looks like a bare opener and swallows the prose up to the real ```json opener —
# seen live 2026-07-14: a full valid structured block was never extracted). The walker
# pairs fences in order and yields only blocks whose OPENER language is json/blank.
_FENCE_LINE_RE = re.compile(r"^```([A-Za-z0-9_-]*)[ \t]*\r?$", re.MULTILINE)


class _BlockSpan:
    """Minimal match-like span (start/end of the WHOLE fenced block, opener to closer)."""

    def __init__(self, start, end, body):
        self._start, self._end, self.body = start, end, body

    def start(self):
        return self._start

    def end(self):
        return self._end


def _fenced_blocks(text):
    """(span, body) for each properly PAIRED fenced block, in document order."""
    fences = list(_FENCE_LINE_RE.finditer(text))
    for i in range(0, len(fences) - 1, 2):
        opener, closer = fences[i], fences[i + 1]
        lang = (opener.group(1) or "").lower()
        body = text[opener.end():closer.start()].strip("\n")
        yield lang, _BlockSpan(opener.start(), closer.end(), body)


def _s(v, limit=4096):
    """A string, or None. Scalars are coerced; containers are rejected (never str(dict))."""
    if isinstance(v, str):
        v = v.strip()
        return v[:limit] if v else None
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return str(v)
    return None


def _str_list(v, limit=64):
    if not isinstance(v, list):
        return []
    return [s for s in (_s(x) for x in v[:limit]) if s]


def _obj(v, fields):
    """Pick the named string fields off a dict; None unless at least one is present."""
    if not isinstance(v, dict):
        return None
    out = {}
    for f in fields:
        s = _s(v.get(f))
        if s:
            out[f] = s
    return out or None


def _obj_list(v, fields, limit=64):
    if not isinstance(v, list):
        return []
    return [o for o in (_obj(x, fields) for x in v[:limit]) if o]


def _confidence(v):
    """Number-or-string 0..1 → clamped decimal string ("0.85"), else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, str):
        try:
            v = float(v.strip())
        except (ValueError, AttributeError):
            return None
    if not isinstance(v, (int, float)):
        return None
    return f"{min(max(float(v), 0.0), 1.0):.2f}".rstrip("0").rstrip(".") or "0"


def _sources(v):
    out = []
    if not isinstance(v, list):
        return out
    for x in v[:64]:
        o = _obj(x, ("ref", "excerpt"))
        if not o:
            continue
        t = _s(x.get("type")) if isinstance(x, dict) else None
        o["type"] = t if t in SOURCE_TYPES else "object"  # keep the evidence, sane the enum
        out.append(o)
    return out


def _trace(v, n_sources):
    """Ordered steps; drop step entries without a statement; drop out-of-bounds evidenceRefs;
    renumber 1..N in the given order (the order IS the trace)."""
    out = []
    if not isinstance(v, list):
        return out
    for x in v[:64]:
        if not isinstance(x, dict):
            continue
        stmt = _s(x.get("statement"))
        if not stmt:
            continue
        refs = x.get("evidenceRefs")
        good = []
        if isinstance(refs, list):
            for r in refs[:32]:
                if isinstance(r, bool):
                    continue
                if isinstance(r, float) and r.is_integer():
                    r = int(r)
                if isinstance(r, int) and 0 <= r < n_sources:
                    good.append(r)
        out.append({"step": len(out) + 1, "statement": stmt, "evidenceRefs": good})
    return out


def _root_cause(v):
    o = _obj(v, ("statement", "category"))
    if not o or "statement" not in o:
        return None
    conf = _confidence(v.get("confidence")) if isinstance(v, dict) else None
    if conf is not None:
        o["confidence"] = conf
    return o


def _script(v):
    """(script, None), or (None, why it is unusable). A list of lines is joined. A script over
    SCRIPT_MAX_CHARS is rejected, never truncated: a cut script is a different script."""
    if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
        v = "\n".join(v)
    if not isinstance(v, str) or not v.strip():
        return None, "missing"
    v = v.strip()
    if len(v) > SCRIPT_MAX_CHARS:
        return None, f"over {SCRIPT_MAX_CHARS} characters"
    return v + "\n", None


def _how_to_fix(v):
    """(howToFix, problems). howToFix holds all three scripts or is None: the controller needs
    precondition and verify to move the incident, and the human needs apply, so a partial set is
    dropped whole and the rest of the report is kept. Keys other than the three are dropped."""
    if v is None:
        return None, ["none returned"]
    if not isinstance(v, dict):
        return None, ["not an object"]
    out, problems = {}, []
    for k in HOW_TO_FIX_SCRIPTS:
        script, why = _script(v.get(k))
        if why:
            problems.append(f"{k} {why}")
        else:
            out[k] = script
    return (None, problems) if problems else (out, [])


def _no_fix_note(problems):
    """The missingContext line for a report whose howToFix was dropped."""
    return (f"No usable howToFix ({'; '.join(problems)}): the incident has no scripts to check or "
            "fix it.")


# ---------------------------------------------------------------------------------------------
# Evidence policy: the published confidence must reflect the evidence that was actually RETRIEVED.
#
# An analysis that cannot read the cluster and SAYS so is useful. One that cannot read the cluster
# and reports 0.97 is actively misleading — it spends the operator's trust on a conclusion drawn
# from a fraction of the intended evidence, at exactly the moment that costs most. Seen live on
# krateo-057 (#29/#30): three reports recorded "User krateo-alert-troubleshooter cannot get/list …"
# in missingContext and still published 0.95-0.98; the kept one reached 0.95 having read ZERO live
# objects. The RBAC gap behind that is fixed; this is the defect that outlives it.
#
# The number is 100% model-authored, and the only thing the contract ever said about it was
# "confidence is 0 to 1" — a range, with no tie to evidence. Defining it in the prompt (above) is
# necessary but not sufficient: a prompt is a promise the model can break, and it breaks silently.
# So the ceiling is ALSO enforced here, in code, on whatever the model returns.
# ---------------------------------------------------------------------------------------------

RETRIEVAL_OUTCOMES = ("success", "denied", "empty", "errored")
EVIDENCE_CLASSES = ("k8s", "logs", "metrics", "repo", "other")
COVERAGE = ("complete", "partial", "degraded", "unavailable")

# A depended-on source that came back DENIED (or ERRORED) is an UNKNOWN, and an unknown must BOUND
# the ceiling rather than silently shrink the evidence base. "success" and "empty" impose NO
# ceiling: "there are no matching pods" is a real, load-bearing finding — which is exactly why
# denied and empty must not collapse into the same absent section.
CONFIDENCE_CEILING = {"denied": 0.40, "errored": 0.60}
NO_EVIDENCE_CEILING = 0.20  # nothing cited and nothing read: no sources, no analyzed resources

# The analyzer's own identity as it appears in an apiserver refusal ("User
# \"system:serviceaccount:krateo-system:krateo-alert-troubleshooter\" cannot list …"). Used to tell
# a denial WE hit from one we are REPORTING ON — see _self_denial.
ANALYZER_IDENTITY = "krateo-alert-troubleshooter"

_CLASS_LABEL = {"k8s": "k8s reads", "logs": "log queries", "metrics": "metrics reads",
                "repo": "repository reads", "other": "some reads"}
# A cited source was, by definition, retrieved — map its type back to an evidence class.
_SOURCE_TYPE_CLASS = {"logs": "logs", "events": "k8s", "metrics": "metrics", "object": "k8s"}

_DENIAL_RE = re.compile(r"\bforbidden\b|\bcannot (?:get|list|watch|create|patch|delete)\b"
                        r"|\b403\b|rbac: access denied|permission denied|not authori[sz]ed"
                        r"|\bunauthorized\b|is not allowed\b", re.I)
_EMPTY_RE = re.compile(r"\bno (?:matching|results|rows|records|such)\b|returned 0 rows"
                       r"|\b0 rows\b|empty result|no items", re.I)
_ERROR_RE = re.compile(r"no healthy backend|timed out|timeout|connection refused"
                       r"|\b(?:500|502|503|504)\b|internal server error|tool (?:call )?failed", re.I)
# Who a refusal names. `User "system:serviceaccount:ns:name"` → the whole colon-joined subject.
_SUBJECT_RE = re.compile(r'user\s+"?([^"\s]+)', re.I)
# First-person phrasing: an UNATTRIBUTED refusal is the analyzer speaking about itself.
_SELF_REF_RE = re.compile(r"\b(?:i|we)\s+(?:could ?n[o']?t|cannot|can't|was|were|am|are)\b"
                          r"|\bunable to\b|\bwas denied\b|\bno (?:access|permission)\b"
                          r"|\bcould not (?:read|list|get|query|access|retrieve|inspect|see)\b", re.I)


def _self_denial(text):
    """True when `text` reports a refusal the ANALYZER hit — not one it is REPORTING ON.

    This distinction is load-bearing. tests/fixtures/reply_yaml_then_json.txt is a real,
    fully-grounded RCA whose SUBJECT is a third party's RBAC failure ("services is forbidden: User
    \"system:serviceaccount:krateo-system:installers-v0-2-219\" cannot list resource services"),
    quoted as evidence and concluded at 0.98. A naive "grep the report for forbidden" would cap
    precisely the reports that are working correctly, and an operator who sees the banner on good
    reports learns to ignore it. So a free-text refusal counts as ours only when it names US, or
    names nobody AND is phrased in the first person."""
    s = text or ""
    if not _DENIAL_RE.search(s):
        return False
    subjects = _SUBJECT_RE.findall(s)
    if subjects:
        return any(ANALYZER_IDENTITY in sub.lower() for sub in subjects)
    return bool(_SELF_REF_RE.search(s))


def _classify_outcome(text):
    """The retrieval outcome a tool result (or a free-text description of one) reports.

    Denial goes through _self_denial because a SUCCESSFUL read can legitimately RETURN a refusal:
    listing events hands back somebody else's "… is forbidden: User … cannot list resource
    services". That call was not denied — it worked, and what it found IS the finding."""
    s = text or ""
    if _self_denial(s):
        return "denied"
    if _ERROR_RE.search(s):
        return "errored"
    if _EMPTY_RE.search(s):
        return "empty"
    return "success"


def _text_class(text):
    """Best-effort evidence class for a free-text retrieval failure (drives the banner wording
    only — never a ceiling, so a miss costs a vaguer sentence, not a wrong number)."""
    s = (text or "").lower()
    if re.search(r"\bk8s\b|kube|kubectl|pod|deployment|namespace|event|composition|resource"
                 r"|secret|node|service", s):
        return "k8s"
    if re.search(r"\blogs?\b|clickhouse|otel|hyperdx|query|trace", s):
        return "logs"
    if "metric" in s:
        return "metrics"
    if re.search(r"\bgit\b|repo", s):
        return "repo"
    return "other"


def _tool_class(name):
    n = (name or "").lower()
    if n.startswith("k8s_") or "kube" in n or "kubectl" in n:
        return "k8s"
    if re.search(r"clickhouse|otel|hyperdx|log|query|sql", n):
        return "logs"
    if "metric" in n or "prometheus" in n:
        return "metrics"
    if "git" in n or "repo" in n:
        return "repo"
    return "other"


def _retrieval(v):
    """The model-DECLARED retrieval ledger: [{source, scope, outcome, detail}]."""
    out = []
    if not isinstance(v, list):
        return out
    for x in v[:64]:
        if not isinstance(x, dict):
            continue
        o = _obj(x, ("source", "scope", "detail")) or {}
        src = (o.get("source") or "").lower()
        o["source"] = src if src in EVIDENCE_CLASSES else _text_class(
            f"{src} {o.get('scope', '')} {o.get('detail', '')}")
        oc = (_s(x.get("outcome")) or "").lower()
        # An outcome we don't recognise must NOT quietly become "success" — that is the silent
        # shrink this whole policy exists to stop. Re-derive it from the words instead.
        o["outcome"] = oc if oc in RETRIEVAL_OUTCOMES else \
            _classify_outcome(f"{oc} {o.get('detail', '')}")
        out.append(o)
    return out


def _from_tool_ledger(tool_ledger):
    """Ground truth: the tool RESULTS the handler lifted off the A2A stream. `failed` means the
    call itself returned an error payload, so a refusal in it is unambiguously ours."""
    out = []
    if not isinstance(tool_ledger, list):
        return out
    for t in tool_ledger[:64]:
        if not isinstance(t, dict):
            continue
        name = _s(t.get("name"), 128) or "tool"
        payload = _s(t.get("payload"), 2048) or ""
        if t.get("failed"):
            outcome = "denied" if _DENIAL_RE.search(payload) else "errored"
        else:
            outcome = _classify_outcome(payload)
        e = {"source": _tool_class(name), "scope": name, "outcome": outcome}
        if outcome != "success":  # a success needs no explanation; keep the CR status small
            e["detail"] = payload[:512]
        out.append(e)
    return out


def build_retrieval_ledger(declared, missing_context=(), tool_ledger=None):
    """One ledger of what the analysis actually got back, from three signals that cannot all be
    gamed at once:
      1. the model-DECLARED "retrieval" entries (explicit, and what the contract now asks for);
      2. missingContext lines that read as a refusal/empty result the analyzer itself hit — the
         transition path: the live reports in #29 already wrote them, nothing ever read them;
      3. the TOOL-RESULT ledger off the A2A stream — ground truth, and the one layer a model
         cannot opt out of by omitting a field.

    Entries are UNIONed and the worst outcome per class governs (see _worst_by_class), so a later
    signal can only ever ADD a constraint, never lift one. Believing a self-reported denial the
    stream says succeeded costs a little confidence; disbelieving one costs the operator's trust."""
    ledger = list(_retrieval(declared))
    for s in _str_list(missing_context):
        outcome = _classify_outcome(s)
        if outcome != "success":
            ledger.append({"source": _text_class(s), "scope": "reported in missingContext",
                           "outcome": outcome, "detail": s[:512]})
    ledger += _from_tool_ledger(tool_ledger)
    seen, out = set(), []
    for e in ledger:
        key = (e.get("source"), e.get("scope"), e.get("outcome"))
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out[:32]


def _worst_by_class(ledger):
    """Worst outcome seen per evidence class. A class read twice, once fine and once refused, is
    still missing whatever the refused read held."""
    rank = {"success": 0, "empty": 1, "errored": 2, "denied": 3}
    worst = {}
    for e in ledger:
        c, o = e.get("source") or "other", e.get("outcome") or "success"
        if rank.get(o, 0) >= rank.get(worst.get(c, "success"), 0):
            worst[c] = o
    return worst


def _retrieved_classes(v2, worst):
    """Classes the analysis DID get evidence from: ledger successes, plus anything it cites (a
    cited source was retrieved) — minus any class a refusal/error left incomplete, so the banner
    can never say "based on k8s reads" about reads that were refused."""
    got = {c for c, o in worst.items() if o in ("success", "empty")}
    for s in v2.get("sources") or []:
        got.add(_SOURCE_TYPE_CLASS.get(s.get("type"), "k8s"))
    if v2.get("analyzedResources"):
        got.add("k8s")
    return got - {c for c, o in worst.items() if o in CONFIDENCE_CEILING}


def _labels(classes):
    labels = [_CLASS_LABEL.get(c, _CLASS_LABEL["other"]) for c in sorted(classes)]
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]


def _gap_statement(worst, retrieved, no_evidence):
    """The operator-facing sentence: what was missing, and what the analysis therefore rests on —
    e.g. "k8s reads unavailable (Forbidden); analysis based on log queries only"."""
    missing = []
    for outcome, phrase in (("denied", "unavailable (Forbidden)"), ("errored", "failed"),
                            ("empty", "returned no data")):
        classes = [c for c, o in worst.items() if o == outcome]
        if classes:
            missing.append(f"{_labels(classes)} {phrase}")
    if no_evidence and not missing:
        missing.append("no evidence source was read")
    if not missing:
        return "all depended-on sources returned data"
    missing.append(f"analysis based on {_labels(retrieved)} only" if retrieved
                   else "no evidence was retrieved")
    return "; ".join(missing)


def apply_evidence_policy(prose, v2, ledger):
    """Bound the reported confidence by the evidence actually retrieved, and SAY SO in the report.

    Returns (prose, v2), mutating v2 in place:
      * caps rootCause.confidence at the ceiling the worst retrieval outcome allows — the declared
        value is PRESERVED under evidence.declaredConfidence, never quietly replaced;
      * records evidence.{coverage,statement,retrieval} so "denied" and "empty" stop collapsing
        into the same absent section;
      * prepends a degraded-analysis banner to the prose and repeats it as the first
        missingContext entry. The portal deliberately does NOT render the raw model confidence
        (krateo-portal-chart restaction.incident-detail.yaml: "a fabricated NN% … erodes trust"),
        so the capped number alone would be invisible — the prose and the gaps list are what an
        operator actually reads.

    Only ever LOWERS a declared confidence, and never invents one the model did not state."""
    root = v2.get("rootCause")
    if not root:
        return prose, v2  # nothing concluded → no claim to qualify
    worst = _worst_by_class(ledger)
    # "no k8s evidence" must NOT by itself trigger a cap: a self-referential telemetry-loop alert
    # is legitimately concluded from log records plus the Alert CR. Only an OBSERVED denial/error
    # moves the number; plain absence moves the coverage LABEL. Cite nothing at all, though, and
    # there is no analysis to be confident about.
    no_evidence = (not v2.get("sources") and not v2.get("analyzedResources")
                   and not any(e.get("outcome") == "success" for e in ledger))
    ceilings = [CONFIDENCE_CEILING[o] for o in worst.values() if o in CONFIDENCE_CEILING]
    if no_evidence:
        ceilings.append(NO_EVIDENCE_CEILING)
    ceiling = min(ceilings) if ceilings else None
    if ceiling is None and not ledger:
        return prose, v2  # nothing to bound and nothing to record → byte-identical to before
    retrieved = _retrieved_classes(v2, worst)
    statement = _gap_statement(worst, retrieved, no_evidence)
    coverage = ("unavailable" if no_evidence else
                "degraded" if "denied" in worst.values() else
                "partial" if any(o in ("errored", "empty") for o in worst.values()) else
                "complete")
    evidence = {"coverage": coverage, "statement": statement}
    declared, bounded = root.get("confidence"), None
    if ceiling is not None:
        # Compare FLOATS. _confidence() strips trailing zeros, so a declared 1.0 formats to "1",
        # which sorts BELOW "0.5" as a string — a lexicographic comparison would fail to cap
        # exactly the most over-confident case there is.
        if declared is not None and float(declared) > ceiling:
            bounded = _confidence(ceiling)
            root["confidence"] = bounded
            evidence["declaredConfidence"] = declared
        evidence["confidenceCap"] = _confidence(ceiling)
        note = f"> ⚠︎ **Degraded analysis** — {statement}."
        if bounded is not None:
            note += f" Confidence bounded to {bounded} (the analysis declared {declared})."
        prose = f"{note}\n\n{prose}" if prose else note
        gap = statement[:1].upper() + statement[1:]
        mc = v2.get("missingContext") or []
        if gap not in mc:
            v2["missingContext"] = [gap] + mc[:63]
    evidence["retrieval"] = ledger
    v2["evidence"] = evidence
    return prose, v2


def _candidate_blocks(text):
    """json/blank-language fenced blocks that parse as a JSON object mentioning ≥1 v2 key,
    best (last) first."""
    blocks = [(lang, span) for lang, span in _fenced_blocks(text) if lang in ("", "json")]
    for lang, span in reversed(blocks):
        try:
            data = json.loads(span.body)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and any(k in data for k in V2_STATUS_KEYS + ("rootCause",)):
            yield span, data


def parse_structured_report(text, tool_ledger=None):
    """(prose, v2_status_fields) from the agent's raw answer. v2 is {} when no valid structured
    block exists (v1 fallback); prose is the answer with the block stripped (or the full answer
    on fallback). Never raises.

    `tool_ledger` is the handler's GROUND TRUTH about what the agent's tool calls actually
    returned (handler.a2a_analyze builds it from the A2A stream's non-text parts). It is optional
    so every existing caller and test keeps working, but when present it OVERRIDES what the model
    says about its own retrieval — a model that never mentions having been denied is exactly the
    case #30 is about."""
    text = (text or "").strip()
    try:
        for match, data in _candidate_blocks(text):
            sources = _sources(data.get("sources"))
            how_to_fix, fix_problems = _how_to_fix(data.get("howToFix"))
            v2 = {
                "analyzedResources": _obj_list(data.get("analyzedResources"),
                                               ("gvr", "name", "namespace", "whatWasRead")),
                "sources": sources,
                "missingContext": _str_list(data.get("missingContext")),
                "assumptions": _str_list(data.get("assumptions")),
                "reasoningTrace": _trace(data.get("reasoningTrace"), len(sources)),
                "rootCause": _root_cause(data.get("rootCause")),
                "howToFix": how_to_fix,
            }
            if not any(v2.values()):
                continue  # a JSON block with the right keys but no usable content → keep looking
            prose = (text[:match.start()] + text[match.end():]).strip()
            if not prose:  # agent answered JSON-only; keep the report human-readable
                prose = (v2.get("rootCause") or {}).get("statement") or text
            v2 = {k: v for k, v in v2.items() if v}
            # EVIDENCE POLICY IN ITS OWN try/except, deliberately. Everything above is already
            # wrapped by the outer handler, so a bug in policy code would not crash — it would
            # silently collapse a perfectly good structured report to a prose-only v1 one, which
            # is a worse failure than publishing an uncapped number. Bound the damage here.
            try:
                ledger = build_retrieval_ledger(data.get("retrieval"),
                                                v2.get("missingContext", ()),
                                                tool_ledger)
                prose, v2 = apply_evidence_policy(prose, v2, ledger)
            except Exception:  # noqa: BLE001 — never let the cap break the report it annotates
                pass
            # A root cause without usable scripts leaves the incident with nothing to check; the
            # gaps list is what the operator reads, so say why there.
            if fix_problems and v2.get("rootCause"):
                v2["missingContext"] = (v2.get("missingContext") or [])[:63] + \
                    [_no_fix_note(fix_problems)]
            return prose, v2
    except Exception:  # noqa: BLE001 — the structured block is best-effort, never fatal
        pass
    # FALLBACK STAYS BYTE-IDENTICAL. handler.py keep-last-good keys off `not prose and not v2`;
    # a banner here would make an empty A2A reply look like a result and overwrite a good report.
    return text, {}
