"""Unit tests for the remediation-outcome observer (observer.py).

Covers the pure correlation core (observer.correlate) + the target-key/verb/gvr normalization +
idempotency, and one integration-style pass over observe_once() with the apiserver stubbed.
Stdlib unittest; no cluster, no network.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import observer


def audit_record(*, group="apps", version="v1", resource="deployments", name="payments-api",
                 namespace="payments", verb="PATCH", ok=True, status=200, message="",
                 resolved_at="2026-08-26T10:11:12Z", ar_name="ar-abc123", ar_ns="payments"):
    """A minimal AuditRecord CR in the frontend provenance emitter's shape (verified-live CRD)."""
    action = {"group": group, "version": version, "resource": resource, "verb": verb}
    if name:
        action["name"] = name
    if namespace:
        action["namespace"] = namespace
    outcome = {"ok": ok, "status": status}
    if message:
        outcome["message"] = message
    return {
        "apiVersion": "audit.krateo.io/v1alpha1", "kind": "AuditRecord",
        "metadata": {"name": ar_name, "namespace": ar_ns},
        "spec": {"actor": "human", "action": action, "outcome": outcome,
                 "resolvedAt": resolved_at, "requestedAt": "2026-08-26T10:11:10Z"},
    }


def report(plan, *, refs=None, name="report-x", namespace="krateo-system"):
    status = {"phase": "Ready", "remediationPlan": plan}
    if refs is not None:
        status["auditRecordRefs"] = refs
    return {"apiVersion": "observability.krateo.io/v1alpha1", "kind": "TroubleshootingReport",
            "metadata": {"name": name, "namespace": namespace}, "status": status}


STEP_PATCH_DEPLOY = {
    "description": "Increase memory limit for payments-api", "verb": "patch",
    "gvr": "apps/v1/deployments", "target": {"name": "payments-api", "namespace": "payments"},
    "payload": {"spec": {}}, "observedOutcome": "",
}


class TestNormalization(unittest.TestCase):
    def test_http_verb_maps_intents_to_patch(self):
        for v in ("apply", "scale", "restart", "APPLY", "Scale"):
            self.assertEqual(observer._http_verb(v), "PATCH", v)
        self.assertEqual(observer._http_verb("patch"), "PATCH")
        self.assertEqual(observer._http_verb("delete"), "DELETE")
        self.assertEqual(observer._http_verb("POST"), "POST")

    def test_split_gvr_group_version_resource(self):
        self.assertEqual(observer._split_gvr("apps/v1/deployments"), ("apps", "v1", "deployments"))
        self.assertEqual(observer._split_gvr("v1/pods"), ("", "v1", "pods"))          # core kind
        self.assertEqual(observer._split_gvr("observability.krateo.io/v1alpha1/alerts"),
                         ("observability.krateo.io", "v1alpha1", "alerts"))
        self.assertEqual(observer._split_gvr("garbage"), ("", "", "garbage"))         # won't match

    def test_step_and_audit_keys_match_across_verb_case_and_gvr_split(self):
        # a lowercase 'patch' step + an uppercase 'PATCH' AuditRecord on the same target correlate
        step_key = observer._step_target_key(STEP_PATCH_DEPLOY)
        ar_key = observer._audit_target_key(audit_record())
        self.assertIsNotNone(step_key)
        self.assertEqual(step_key, ar_key)

    def test_get_step_has_no_target_key(self):
        self.assertIsNone(observer._step_target_key(
            {"verb": "get", "gvr": "v1/pods", "target": {"name": "x", "namespace": "y"}}))

    def test_audit_key_none_without_resource_or_verb(self):
        self.assertIsNone(observer._audit_target_key({"spec": {"action": {"verb": "PATCH"}}}))
        self.assertIsNone(observer._audit_target_key({"spec": {"action": {"resource": "pods"}}}))


class TestCorrelate(unittest.TestCase):
    def test_fills_observed_outcome_and_appends_ref(self):
        rep = report([dict(STEP_PATCH_DEPLOY)], refs=[])
        patch = observer.correlate(rep, audit_record(ar_name="ar-1", status=200))
        self.assertIsNotNone(patch)
        self.assertNotEqual(patch["remediationPlan"][0]["observedOutcome"], "")
        self.assertIn("PATCH succeeded (HTTP 200)", patch["remediationPlan"][0]["observedOutcome"])
        self.assertIn("2026-08-26T10:11:12Z", patch["remediationPlan"][0]["observedOutcome"])
        self.assertEqual(patch["auditRecordRefs"], ["payments/ar-1"])

    def test_failed_write_receipt_carries_error(self):
        rep = report([dict(STEP_PATCH_DEPLOY)], refs=[])
        ar = audit_record(ok=False, status=422, message="Invalid: missing image")
        patch = observer.correlate(rep, ar)
        outcome = patch["remediationPlan"][0]["observedOutcome"]
        self.assertIn("failed", outcome)
        self.assertIn("HTTP 422", outcome)
        self.assertIn("Invalid: missing image", outcome)

    def test_idempotent_skip_when_ref_already_recorded(self):
        rep = report([dict(STEP_PATCH_DEPLOY)], refs=["payments/ar-1"])
        self.assertIsNone(observer.correlate(rep, audit_record(ar_name="ar-1")))

    def test_no_match_returns_none(self):
        rep = report([dict(STEP_PATCH_DEPLOY)], refs=[])
        # different target name -> no correlation
        self.assertIsNone(observer.correlate(rep, audit_record(name="other-deploy")))
        # different verb -> no correlation
        self.assertIsNone(observer.correlate(rep, audit_record(verb="DELETE")))
        # different namespace -> no correlation
        self.assertIsNone(observer.correlate(rep, audit_record(namespace="other")))

    def test_only_first_matching_step_is_recorded(self):
        rep = report([dict(STEP_PATCH_DEPLOY), dict(STEP_PATCH_DEPLOY)], refs=[])
        patch = observer.correlate(rep, audit_record())
        self.assertNotEqual(patch["remediationPlan"][0]["observedOutcome"], "")
        self.assertEqual(patch["remediationPlan"][1]["observedOutcome"], "")  # second left untouched

    def test_get_only_plan_never_matches(self):
        rep = report([{"description": "Verify: pod ready", "verb": "get", "gvr": "v1/pods",
                       "target": {"name": "payments-api", "namespace": "payments"},
                       "observedOutcome": ""}], refs=[])
        # even a POST audit on pods can't correlate to a get step
        self.assertIsNone(observer.correlate(rep, audit_record(resource="pods", verb="POST")))

    def test_empty_or_missing_plan_returns_none(self):
        self.assertIsNone(observer.correlate(report([]), audit_record()))
        self.assertIsNone(observer.correlate({"status": {}}, audit_record()))

    def test_audit_without_correlatable_fields_returns_none(self):
        rep = report([dict(STEP_PATCH_DEPLOY)])
        self.assertIsNone(observer.correlate(rep, {"spec": {"action": {}}}))

    def test_core_kind_gvr_correlates(self):
        step = {"description": "Delete stuck pod", "verb": "delete", "gvr": "v1/pods",
                "target": {"name": "stuck", "namespace": "payments"}, "observedOutcome": ""}
        rep = report([step], refs=[])
        ar = audit_record(group="", version="v1", resource="pods", name="stuck",
                          namespace="payments", verb="DELETE", ar_name="ar-del")
        patch = observer.correlate(rep, ar)
        self.assertIsNotNone(patch)
        self.assertIn("DELETE", patch["remediationPlan"][0]["observedOutcome"])
        self.assertEqual(patch["auditRecordRefs"], ["payments/ar-del"])


class TestObserveOncePass(unittest.TestCase):
    """observe_once() with the apiserver stubbed: it lists audits + reports and patches matches,
    and a re-run over the SAME audit is a no-op (idempotent end-to-end)."""

    def _run(self, audits, reports):
        patches = []

        def fake_list(group, version, plural, namespace):
            return audits if plural == observer.AUDIT_PLURAL else reports

        def fake_patch_status(ns, name, status):
            patches.append((ns, name, status))
            # reflect into the stubbed report list so the same-pass local update path is exercised
            for r in reports:
                if r["metadata"]["name"] == name:
                    r.setdefault("status", {}).update(status)

        orig_list, orig_patch = observer._list, observer.patch_status
        observer._list, observer.patch_status = fake_list, fake_patch_status
        try:
            observer.observe_once()
            observer.observe_once()  # second pass must add nothing (idempotent)
        finally:
            observer._list, observer.patch_status = orig_list, orig_patch
        return patches

    def test_matching_record_patches_once_across_two_passes(self):
        reports = [report([dict(STEP_PATCH_DEPLOY)], refs=[], name="report-pay")]
        patches = self._run([audit_record(ar_name="ar-1")], reports)
        self.assertEqual(len(patches), 1, "exactly one patch across two idempotent passes")
        ns, name, status = patches[0]
        self.assertEqual((ns, name), ("krateo-system", "report-pay"))
        self.assertEqual(status["auditRecordRefs"], ["payments/ar-1"])
        self.assertNotEqual(status["remediationPlan"][0]["observedOutcome"], "")

    def test_no_audits_no_patches(self):
        reports = [report([dict(STEP_PATCH_DEPLOY)], refs=[])]
        self.assertEqual(self._run([], reports), [])

    def test_unrelated_audit_no_patch(self):
        reports = [report([dict(STEP_PATCH_DEPLOY)], refs=[])]
        self.assertEqual(self._run([audit_record(name="unrelated")], reports), [])


if __name__ == "__main__":
    unittest.main()
