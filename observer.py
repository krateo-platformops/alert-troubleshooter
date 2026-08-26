#!/usr/bin/env python3
"""Remediation-outcome observer — the missing half of the remediation loop.

A TroubleshootingReport's status.remediationPlan[i].observedOutcome is created EMPTY (report_v2.py
forces it "" — "filled post-apply by the remediation flow"), and status.auditRecordRefs is created
EMPTY. Nothing ever filled them. The portal computes `applied = (observedOutcome != "")`, so a step
never flips to "applied": the Apply button reappears and the sequential flow is stuck.

This observer closes that loop. The frontend provenance layer emits ONE immutable AuditRecord CR
(audit.krateo.io/v1alpha1) for EVERY portal write — human- or agent-originated — AFTER the write
resolves, carrying the exact k8s target (spec.action.{group,version,resource,name,namespace,verb})
and the outcome (spec.outcome.{ok,status,message}) + spec.resolvedAt. When a remediation step is
applied through the portal, the resulting AuditRecord's target + verb MATCH that step. So:

  poll AuditRecords ->
    for each, find any TroubleshootingReport whose remediationPlan[i] targets the SAME
    (group/version/resource + name + namespace) AND verb ->
      PATCH that step's observedOutcome with a short receipt (verb + ok/failed + status + resolvedAt)
      and append the AuditRecord ref to status.auditRecordRefs — IDEMPOTENT (never double-records the
      same AuditRecord; skips a step whose outcome is already filled by that same record).

Runs as a background thread in the same process as the webhook server + the Alert reconciler,
polling every OBSERVER_INTERVAL seconds (level-based, restart-safe, no watch bookmarks to persist).

Correlation is EXACT on the apiserver-normalized target, tolerant only of the two representational
gaps between the two schemas:
  * verb case — remediationPlan verbs are lowercase intents (patch/apply/delete/scale/restart) while
    an AuditRecord records the HTTP verb (PATCH/PUT/POST/DELETE). We normalize both to an HTTP verb
    (apply/scale/restart -> PATCH; plus a straight case-fold) before comparing.
  * gvr split — a remediationPlan step carries one "group/version/resource" string (with an EMPTY
    leading group for core kinds, e.g. "v1/pods" -> group "", version "v1", resource "pods"); an
    AuditRecord carries the three parts separately. We split the step's gvr the same way to compare.

Stdlib + the handler's apiserver helpers only (reuses _k8s / patch_status / _get_report / _stable_name
/ _now) — no new deps. Never raises into the poll loop; one bad record/report never stalls the rest.
"""
import os

import requests

from handler import _k8s, _now, patch_status  # reuse the apiserver helpers

# The report side (what we patch).
GROUP, VERSION, PLURAL = "observability.krateo.io", "v1alpha1", "troubleshootingreports"
# The audit side (what we watch). The frontend provenance emitter writes these on every portal write.
AUDIT_GROUP, AUDIT_VERSION, AUDIT_PLURAL = "audit.krateo.io", "v1alpha1", "auditrecords"

NAMESPACE = os.environ.get("NAMESPACE", "krateo-system")
INTERVAL = int(os.environ.get("OBSERVER_INTERVAL", "30"))
# Observe AuditRecords cluster-wide (a remediation write can target any namespace, and the record
# lives next to its target object — provenance.ts sets metadata.namespace = the write's namespace).
# Reports are searched cluster-wide too. Both default on; scope down with *_NAMESPACE if desired.
AUDIT_NAMESPACE = os.environ.get("AUDIT_NAMESPACE", "")     # "" = all namespaces
REPORT_NAMESPACE = os.environ.get("REPORT_NAMESPACE", "")   # "" = all namespaces


def _http_verb(verb):
    """Normalize a remediationPlan intent verb OR an AuditRecord HTTP verb to a single HTTP verb, so
    both sides compare on the same value. remediationPlan uses lowercase intents (patch/apply/scale/
    restart/delete); the AuditRecord records the literal HTTP verb (PATCH/PUT/POST/DELETE). apply,
    scale and restart are all dispatched as a PATCH by the portal write path, so they map to PATCH."""
    v = (verb or "").strip().upper()
    if v in ("APPLY", "SCALE", "RESTART"):
        return "PATCH"
    return v


def _split_gvr(gvr):
    """Split a remediationPlan 'group/version/resource' string into (group, version, resource) the
    same way the apiserver / AuditRecord represents them. A core kind has an EMPTY group and a
    two-part gvr: 'v1/pods' -> ('', 'v1', 'pods'); 'apps/v1/deployments' -> ('apps','v1','deployments').
    Anything else (too few parts) yields ('', '', <whole>) so it simply won't match a real target."""
    parts = [p for p in (gvr or "").strip().split("/") if p != ""]
    if len(parts) >= 3:
        return parts[0], parts[1], "/".join(parts[2:])
    if len(parts) == 2:            # core kind: version/resource, empty group
        return "", parts[0], parts[1]
    return "", "", (parts[0] if parts else "")


def _norm(s):
    return (s or "").strip()


def _target_key(group, version, resource, name, namespace, verb):
    """A hashable, apiserver-normalized identity for a write target + verb, used to compare a
    remediation step to an AuditRecord. version is INCLUDED for a precise match (both schemas
    carry it); an empty group means the core API group."""
    return (_norm(group), _norm(version), _norm(resource),
            _norm(name), _norm(namespace), _http_verb(verb))


def _audit_target_key(ar):
    """The target key of an AuditRecord's spec.action, or None if it lacks the required fields."""
    action = (ar.get("spec") or {}).get("action") or {}
    resource = _norm(action.get("resource"))
    verb = _norm(action.get("verb"))
    if not resource or not verb:
        return None  # a record with no resource/verb can't be correlated
    return _target_key(action.get("group"), action.get("version"), resource,
                       action.get("name"), action.get("namespace"), verb)


def _step_target_key(step):
    """The target key of a remediationPlan step, or None if it lacks a gvr/target to correlate on.
    A `get`/verification-only step is not a write, so it can never match an AuditRecord — skip it."""
    verb = _norm(step.get("verb"))
    if not verb or verb.lower() == "get":
        return None
    group, version, resource = _split_gvr(step.get("gvr"))
    if not resource:
        return None
    target = step.get("target") or {}
    return _target_key(group, version, resource,
                       target.get("name"), target.get("namespace"), verb)


def _ar_ref(ar):
    """A stable, human-readable reference to an AuditRecord for status.auditRecordRefs, distinct per
    record (namespace/name). Used BOTH as the stored ref and as the idempotency key."""
    meta = ar.get("metadata") or {}
    name = meta.get("name") or meta.get("uid") or ""
    ns = meta.get("namespace") or ""
    return f"{ns}/{name}" if ns else name


def _receipt(ar):
    """A short observedOutcome receipt from an AuditRecord: verb + ok/failed (+ HTTP status) + when.
    E.g. 'PATCH succeeded (HTTP 200) at 2026-08-26T10:11:12Z' — enough for the portal to flip the
    step to 'applied' (observedOutcome != '') and for a human to see what happened."""
    spec = ar.get("spec") or {}
    action = spec.get("action") or {}
    outcome = spec.get("outcome") or {}
    verb = _norm(action.get("verb")) or "write"
    ok = outcome.get("ok")
    result = "succeeded" if ok else ("failed" if ok is not None else "recorded")
    status = outcome.get("status")
    status_part = f" (HTTP {status})" if isinstance(status, int) and status else ""
    when = _norm(spec.get("resolvedAt")) or _norm(spec.get("requestedAt")) or _now()
    receipt = f"{verb} {result}{status_part} at {when}"
    msg = _norm(outcome.get("message"))
    if msg and not ok:  # surface the apiserver error on a failed write
        receipt += f": {msg}"
    return receipt[:4096]


def _list(group, version, plural, namespace):
    base = f"/apis/{group}/{version}"
    path = f"{base}/namespaces/{namespace}/{plural}" if namespace else f"{base}/{plural}"
    return _k8s("GET", path).get("items", [])


def correlate(report, ar):
    """Pure correlation + idempotent status delta for ONE (report, AuditRecord) pair. Returns the
    status patch to apply (a dict with observedOutcome-bearing remediationPlan and/or an appended
    auditRecordRefs), or None if this record doesn't touch this report / everything is already
    recorded. Does NOT call the apiserver — unit-testable in isolation.

    Idempotency: a step is only (re)written when THIS AuditRecord is not already in
    status.auditRecordRefs. The ref is appended at most once. A re-poll of the same record after it
    has been recorded is a no-op (returns None)."""
    ar_key = _audit_target_key(ar)
    if ar_key is None:
        return None
    status = report.get("status") or {}
    plan = status.get("remediationPlan")
    if not isinstance(plan, list) or not plan:
        return None
    existing_refs = status.get("auditRecordRefs")
    existing_refs = [r for r in existing_refs if isinstance(r, str)] if isinstance(existing_refs, list) else []
    ref = _ar_ref(ar)
    if ref and ref in existing_refs:
        return None  # already recorded this exact AuditRecord — idempotent no-op

    receipt = _receipt(ar)
    matched = False
    new_plan = []
    for step in plan:
        step = dict(step) if isinstance(step, dict) else {}
        if not matched and _step_target_key(step) == ar_key:
            # Record on the FIRST matching step only, so a report with two identical-target steps
            # doesn't get one AuditRecord written onto both (each apply emits its own record).
            step["observedOutcome"] = receipt
            matched = True
        new_plan.append(step)
    if not matched:
        return None  # this AuditRecord targets nothing in this report's plan

    patch = {"remediationPlan": new_plan}
    if ref:
        patch["auditRecordRefs"] = existing_refs + [ref]  # append-once; never double-append
    return patch


def _list_reports_with_plan(namespace):
    """Reports that HAVE a non-empty remediationPlan — the only ones an AuditRecord can correlate to.
    Filtered client-side (the apiserver has no field selector for a nested array)."""
    out = []
    for rep in _list(GROUP, VERSION, PLURAL, namespace):
        plan = ((rep.get("status") or {}).get("remediationPlan"))
        if isinstance(plan, list) and plan:
            out.append(rep)
    return out


def observe_once():
    """One pass: for each AuditRecord, patch every report whose plan it satisfies. Level-based and
    idempotent — safe to run every interval; a record already recorded on a report is skipped."""
    audits = _list(AUDIT_GROUP, AUDIT_VERSION, AUDIT_PLURAL, AUDIT_NAMESPACE)
    if not audits:
        return
    reports = _list_reports_with_plan(REPORT_NAMESPACE)
    if not reports:
        return
    for ar in audits:
        for rep in reports:
            try:
                patch = correlate(rep, ar)
            except Exception as e:  # noqa: BLE001 — one bad pair never stalls the rest
                print(f"[observer] correlate skipped ({e})", flush=True)
                continue
            if not patch:
                continue
            meta = rep.get("metadata") or {}
            ns, name = meta.get("namespace") or NAMESPACE, meta.get("name")
            try:
                patch_status(ns, name, patch)
                # Reflect the patch locally so a second AuditRecord in the SAME pass sees the
                # already-appended ref / already-filled step (avoids a redundant re-patch this pass).
                rep.setdefault("status", {})["remediationPlan"] = patch["remediationPlan"]
                if "auditRecordRefs" in patch:
                    rep["status"]["auditRecordRefs"] = patch["auditRecordRefs"]
                print(f"[observer] report {ns}/{name}: recorded outcome from AuditRecord "
                      f"{_ar_ref(ar)}", flush=True)
            except Exception as e:  # noqa: BLE001 — record the miss, keep going
                print(f"[observer] report {ns}/{name} patch skipped ({e})", flush=True)


def run_forever():
    """Poll AuditRecords -> fill matching remediation steps' observedOutcome + auditRecordRefs.
    Level-based; never crashes the process (mirrors reconciler.run_forever)."""
    import time
    print(f"[observer] started (interval={INTERVAL}s, audits={AUDIT_GROUP}/{AUDIT_VERSION}, "
          f"audit-ns={AUDIT_NAMESPACE or 'ALL'}, report-ns={REPORT_NAMESPACE or 'ALL'})", flush=True)
    while True:
        try:
            observe_once()
        except requests.HTTPError as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            # A 404 on the AuditRecord list = the CRD isn't installed on this cluster (provenance
            # not shipped). The observer is correct-but-dormant; log once-per-cycle at low volume.
            print(f"[observer] http error ({code}); will retry: {e}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[observer] cycle error: {e}", flush=True)
        time.sleep(INTERVAL)
