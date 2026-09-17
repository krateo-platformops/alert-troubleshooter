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
  * remediationPlan[].payload must be a JSON object (the CRD preserves unknown fields there);
    observedOutcome is forced empty — it is filled post-apply by the remediation flow, not here.
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
                  "reasoningTrace", "rootCause", "remediationPlan", "evidence")

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
  "remediationPlan": [
    {"description": "Increase memory limit for payments-api", "verb": "patch", "gvr": "apps/v1/deployments", "target": {"name": "payments-api", "namespace": "payments"}, "payload": {"spec":{"template":{"spec":{"containers":[{"name":"payments-api","image":"ghcr.io/example/payments-api:1.2.3","resources":{"limits":{"memory":"512Mi"},"requests":{"memory":"128Mi"}}}]}}}}, "successCriterion": "Deployment payments-api reports 1/1 ready replicas with no OOMKilled events", "verifyCommand": "kubectl get deployment payments-api -n payments", "sourceRef": 0},
    {"description": "Add service exclusions to alert query", "verb": "patch", "gvr": "observability.krateo.io/v1alpha1/alerts", "target": {"name": "krateo-alert-composition-reconcile-error", "namespace": "krateo-system"}, "payload": {"spec":{"where":"Body:ReconcileError AND NOT ServiceName:krateo-observability AND NOT ServiceName:krateo-alert-troubleshooter AND NOT ServiceName:krateo-clickstack-clickhouse AND NOT ServiceName:krateo-clickstack AND NOT ServiceName:krateo-clickstack-clickhouse-clickhouse AND NOT ServiceName:clickhouse-mcp-server"}}, "successCriterion": "Alert krateo-alert-composition-reconcile-error transitions to OK state", "verifyCommand": "kubectl get alert krateo-alert-composition-reconcile-error -n krateo-system -o jsonpath='{.status.state}'", "sourceRef": 1}
  ]
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
- remediationPlan is a PLAN only — it will not be executed automatically; order the steps, and
  make each successCriterion independently checkable. Leave observed outcomes out; they are
  recorded later.
- BE ACTIONABLE: when the evidence identifies a safe corrective write (adjust a limit, fix an
  image tag, patch an Alert query, delete a stuck object), emit AT LEAST ONE step whose verb is
  patch/apply/delete with a concrete payload — not a plan made only of "get" verification steps.
  A remediationPlan that is ENTIRELY "get" steps when a real fix is identifiable is a MISS: an
  operator can't apply a `get`. Only fall back to get-only when you genuinely cannot determine a
  safe write from the evidence (say why in missingContext). Keep every existing safety rail — in
  particular NEVER emit patch/apply/delete for v1/nodes or other cluster-scoped kinds.
- MARK VERIFICATION-ONLY STEPS: a step that merely CHECKS state (verb "get") must say so in its
  description (e.g. prefix "Verify: ...") so it is never mistaken for the corrective action. Put
  the actual corrective write(s) first; list any get-only confirmation steps after them.
- remediationPlan[].description MUST be a SHORT imperative phrase, ≤60 chars, no trailing period
  (e.g. "Restart payments-api deployment", "Provision payments-db database"). Do NOT put constraints,
  namespaces, ports, or labels in description — those belong in successCriterion. Never repeat
  the description text inside successCriterion.
- remediationPlan[].verifyCommand MUST be a single kubectl command (no shell pipes, no &&) that
  lets an operator check the successCriterion (e.g. "kubectl get endpoints payments-db -n payments").
  Omit if no single kubectl command captures the check.
- remediationPlan[].sourceRef MUST be the 0-based index into "sources" of the evidence that best
  explains WHY this step is needed. Required for every step — pick the closest match if none is
  perfect.
- CLUSTER-SCOPED RESOURCES (v1/nodes, v1/namespaces, …): the portal write path requires a
  namespace query parameter that cluster-scoped resources don't have. Use verb "get" ONLY for
  nodes and other cluster-scoped kinds. Never emit verb "patch"/"apply"/"delete" for v1/nodes.
- DEPLOYMENT CONTAINER PATCHES: a merge-patch on spec.template.spec.containers[] MUST include
  both "name" and "image" in the payload's container entry to identify which container to update.
  Omitting "image" causes a 422 Invalid. Fetch the current image with k8s_get_resources first,
  then include it verbatim: {"spec":{"template":{"spec":{"containers":[{"name":"X","image":"<current>","resources":{…}}]}}}}.
- ALERT QUERY FALSE-POSITIVES: when the root cause is a self-referential telemetry feedback loop
  (the alert evaluator/ClickHouse/troubleshooter logs the SQL query into the log store, and those
  log lines match the alert's own where clause), the fix is to patch the Alert CR's spec.where to
  add NOT ServiceName: exclusions for the observability stack. Use gvr
  "observability.krateo.io/v1alpha1/alerts", verb "patch", and payload
  {"spec":{"where":"<original query> AND NOT ServiceName:krateo-observability AND NOT
  ServiceName:krateo-alert-troubleshooter AND NOT ServiceName:krateo-clickstack-clickhouse AND NOT
  ServiceName:krateo-clickstack AND NOT ServiceName:krateo-clickstack-clickhouse-clickhouse AND NOT
  ServiceName:clickhouse-mcp-server"}}. Never target a Deployment or Composition for an alert
  query fix — the alert rule lives in the Alert CR.
- VERIFY WORKLOAD EXISTS before building a remediationPlan step targeting it. If a workload (pod,
  deployment, namespace) is missing or not found when you query k8s_get_resources, do NOT produce
  a remediationPlan step to create or provision it — report it in missingContext instead.
- Output STRICT JSON (double quotes, no comments, no trailing commas)."""

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


def _plan(v):
    out = []
    if not isinstance(v, list):
        return out
    for x in v[:32]:
        o = _obj(x, ("description", "verb", "gvr", "successCriterion", "verifyCommand"))
        if not o or "description" not in o:
            continue
        target = _obj(x.get("target"), ("name", "namespace"))
        if target:
            o["target"] = target
        payload = x.get("payload")
        if isinstance(payload, dict) and payload:
            o["payload"] = payload  # CRD: object + x-kubernetes-preserve-unknown-fields
        sr = x.get("sourceRef")
        if isinstance(sr, bool):
            sr = None
        elif isinstance(sr, float) and sr.is_integer():
            sr = int(sr)
        if isinstance(sr, int) and sr >= 0:
            o["sourceRef"] = sr
        o["observedOutcome"] = ""  # filled post-apply by the remediation flow, never here
        out.append(o)
    return out


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
            v2 = {
                "analyzedResources": _obj_list(data.get("analyzedResources"),
                                               ("gvr", "name", "namespace", "whatWasRead")),
                "sources": sources,
                "missingContext": _str_list(data.get("missingContext")),
                "assumptions": _str_list(data.get("assumptions")),
                "reasoningTrace": _trace(data.get("reasoningTrace"), len(sources)),
                "rootCause": _root_cause(data.get("rootCause")),
                "remediationPlan": _plan(data.get("remediationPlan")),
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
            return prose, v2
    except Exception:  # noqa: BLE001 — the structured block is best-effort, never fatal
        pass
    # FALLBACK STAYS BYTE-IDENTICAL. handler.py keep-last-good keys off `not prose and not v2`;
    # a banner here would make an empty A2A reply look like a result and overwrite a good report.
    return text, {}
