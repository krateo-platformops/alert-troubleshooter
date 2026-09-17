"""Confidence must reflect the evidence actually RETRIEVED (#30).

The observer published 0.95-0.98 confidence on analyses in which every Kubernetes read returned
Forbidden. PR #29 restored the RBAC; this suite covers the separate defect that outlived it — the
score was computed without reference to whether the evidence it rests on ever arrived.

The load-bearing test is `test_tool_ledger_caps_a_confident_but_blind_analysis`: it asserts on the
GROUND TRUTH path, where the model claims 0.98 and never mentions being denied. A model that
volunteers "I was denied" was never the hard case.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import report_v2
from report_v2 import parse_structured_report


def _reply(block, prose="## Root cause\nThe API server is unreachable."):
    return prose + "\n\n```json\n" + json.dumps(block) + "\n```"


def _block(confidence=0.98, **extra):
    b = {
        "analyzedResources": [{"gvr": "v1/pods", "name": "payment-api", "namespace": "prod",
                               "whatWasRead": "status"}],
        "sources": [{"type": "logs", "ref": "hyperdx: body:timeout", "excerpt": "i/o timeout"}],
        "rootCause": {"statement": "The payment-api pod cannot reach the database.",
                      "confidence": confidence},
    }
    b.update(extra)
    return b


DENIED_LEDGER = [
    {"name": "k8s_get_resources", "failed": True,
     "payload": 'pods is forbidden: User "system:serviceaccount:krateo-system:observer" '
                'cannot list resource "pods" in API group "" at the cluster scope'},
]


class EvidencePolicy(unittest.TestCase):

    def test_tool_ledger_caps_a_confident_but_blind_analysis(self):
        """THE CASE #30 IS ABOUT. The model asserts 0.98 and says nothing about being denied;
        the denial exists only in what its tools actually returned. Before this fix the handler
        discarded those parts one line before they could be used, so 0.98 was published as-is."""
        prose, v2 = parse_structured_report(_reply(_block(0.98)), DENIED_LEDGER)
        self.assertTrue(v2, "a valid structured block must still parse")
        conf = float(v2["rootCause"]["confidence"])
        self.assertLessEqual(conf, report_v2.CONFIDENCE_CEILING["denied"],
                             f"denied k8s reads must bound confidence, got {conf}")
        ev = v2.get("evidence") or {}
        self.assertEqual(ev.get("declaredConfidence"), "0.98",
                         "the model's original number must be preserved, not erased")
        self.assertIn(ev.get("coverage"), ("degraded", "partial", "unavailable"))

    def test_the_operator_can_see_it_without_reading_the_status(self):
        """A capped number nobody notices is the same bug in a different place."""
        prose, v2 = parse_structured_report(_reply(_block(0.98)), DENIED_LEDGER)
        self.assertRegex(prose.lower(), r"degraded|unavailable|forbidden|denied",
                         "the report prose must say the analysis was degraded")

    def test_a_grounded_report_quoting_forbidden_as_its_finding_is_not_capped(self):
        """THE FALSE POSITIVE THAT WOULD HAVE BEEN WORSE THAN THE BUG.

        'services is forbidden: ... cannot list resource' is a real excerpt from a real, fully
        grounded analysis in this repo's fixtures — the RBAC denial IS the root cause being
        reported. Scanning source excerpts for denial markers would cap exactly the reports that
        are working correctly, and the operator would learn to ignore the banner."""
        block = _block(0.95, sources=[
            {"type": "logs", "ref": "hyperdx: body:forbidden",
             "excerpt": 'services is forbidden: User "installers-v0-2-219" cannot list '
                        'resource "services" in API group ""'}])
        prose, v2 = parse_structured_report(_reply(block), [])
        self.assertEqual(float(v2["rootCause"]["confidence"]), 0.95,
                         "a denial quoted AS EVIDENCE must not cap the report that found it")

    def test_denied_and_empty_are_different_facts(self):
        """'no pods matched' and 'not allowed to look at pods' support different conclusions.
        An empty result is a real finding and must not be penalised."""
        empty = [{"name": "k8s_get_resources", "payload": "No resources found in prod namespace."}]
        _, v2 = parse_structured_report(_reply(_block(0.9)), empty)
        self.assertEqual(float(v2["rootCause"]["confidence"]), 0.9,
                         "an empty result is evidence, not a gap")

    def test_confidence_is_never_raised(self):
        """The policy is a ceiling, not a score. A cautious model stays cautious."""
        _, v2 = parse_structured_report(_reply(_block(0.10)), DENIED_LEDGER)
        self.assertLessEqual(float(v2["rootCause"]["confidence"]), 0.10)

    def test_no_ledger_and_no_declared_gaps_is_left_alone(self):
        """0.2.37 behaviour must be preserved when there is nothing to act on — otherwise every
        pre-existing report would suddenly read as degraded."""
        _, v2 = parse_structured_report(_reply(_block(0.88)), None)
        self.assertEqual(float(v2["rootCause"]["confidence"]), 0.88)
        self.assertNotIn("evidence", v2)

    def test_fallback_path_is_byte_identical(self):
        """handler.py keep-last-good keys off `not prose and not v2`. A banner on the prose-only
        fallback would make an empty A2A reply look like a result and overwrite a good report."""
        self.assertEqual(parse_structured_report("", DENIED_LEDGER), ("", {}))
        plain = "Autopilot could not reach the cluster."
        self.assertEqual(parse_structured_report(plain, DENIED_LEDGER), (plain, {}))

    def test_policy_failure_cannot_break_the_report(self):
        """A bug in the cap must never collapse a good structured report to prose-only."""
        boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        orig = report_v2.apply_evidence_policy
        report_v2.apply_evidence_policy = boom
        try:
            prose, v2 = parse_structured_report(_reply(_block(0.9)), DENIED_LEDGER)
            self.assertTrue(v2, "the structured report must survive a policy exception")
            self.assertIn("rootCause", v2)
        finally:
            report_v2.apply_evidence_policy = orig


class HandlerToolLedger(unittest.TestCase):
    """The plumbing half: the denial has to reach report_v2 at all."""

    def setUp(self):
        import handler
        self.handler = handler

    def test_function_response_parts_are_captured(self):
        parts = [
            {"kind": "text", "text": "Looking at the pods..."},
            {"kind": "data", "data": {"adk_type": "function_response", "name": "k8s_get_resources",
                                      "response": {"error": "pods is forbidden"}}},
        ]
        got = self.handler._tool_results(parts)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["name"], "k8s_get_resources")
        self.assertIn("forbidden", got[0]["payload"])
        self.assertTrue(got[0]["failed"], "an error response must be marked failed")

    def test_unknown_part_shapes_yield_nothing_rather_than_raising(self):
        """kagent's part shape has changed before. It must never be why an analysis fails."""
        for parts in ([{"kind": "data"}], [{"kind": "data", "data": "str"}], [None], [],
                      [{"kind": "data", "data": {"adk_type": "function_call", "name": "x"}}]):
            self.assertEqual(self.handler._tool_results(parts), [], parts)


if __name__ == "__main__":
    unittest.main()
