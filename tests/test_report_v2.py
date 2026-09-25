"""Unit tests for the TroubleshootingReport v2 structured-report contract (report_v2.py)
plus the thin handler-side wiring. Stdlib unittest; no cluster, no network.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import report_v2
from report_v2 import parse_structured_report

PROSE = "## Root cause\nThe deployment is crash-looping.\n\n## Remediation\nFix the image tag."

PRECONDITION = (
    "#!/usr/bin/env bash\n"
    "# Holds while payment-api runs the missing tag v9.\n"
    "i=$(kubectl get deployment payment-api -n prod"
    " -o jsonpath='{.spec.template.spec.containers[?(@.name==\"api\")].image}') || exit 2\n"
    "[ \"$i\" = payment:v9 ] && exit 1\n"
    "exit 0\n")
APPLY = ("#!/usr/bin/env bash\n# Point payment-api back to v8, the last tag the registry serves.\n"
         "kubectl set image deployment/payment-api api=payment:v8 -n prod\n")
VERIFY = ("#!/usr/bin/env bash\n# Fixed once payment-api is off v9 and available.\n"
          "d=$(kubectl get deployment payment-api -n prod -o json) || exit 2\n"
          "jq -e '.status.availableReplicas == .spec.replicas' <<<\"$d\" >/dev/null\n"
          "case $? in 0) exit 0 ;; 1) exit 1 ;; *) exit 2 ;; esac\n")

VALID_BLOCK = {
    "analyzedResources": [
        {"gvr": "apps/v1/deployments", "name": "payment-api", "namespace": "prod",
         "whatWasRead": "status + last 10 events"},
    ],
    "sources": [
        {"type": "logs", "ref": "hyperdx: body:CrashLoopBackOff", "excerpt": "Back-off restarting failed container"},
        {"type": "events", "ref": "prod/payment-api", "excerpt": "Failed to pull image \"payment:v9\""},
    ],
    "missingContext": ["No metrics retention beyond 1h"],
    "assumptions": ["The registry outage seen at 09:00 is over"],
    "reasoningTrace": [
        {"step": 1, "statement": "Pods crash-loop on image pull", "evidenceRefs": [0, 1]},
        {"step": 2, "statement": "Tag v9 does not exist in the registry", "evidenceRefs": [1]},
    ],
    "rootCause": {"statement": "Bad image tag v9", "confidence": 0.85, "category": "image"},
    "howToFix": {"precondition": PRECONDITION, "apply": APPLY, "verify": VERIFY},
}


def answer(block, prose=PROSE, fence="json"):
    return f"{prose}\n\n```{fence}\n{json.dumps(block, indent=1)}\n```"


class TestValidV2(unittest.TestCase):
    def test_full_valid_block(self):
        prose, v2 = parse_structured_report(answer(VALID_BLOCK))
        self.assertEqual(prose, PROSE)                      # block stripped from the prose
        self.assertNotIn("```", prose)
        self.assertEqual(v2["analyzedResources"][0]["gvr"], "apps/v1/deployments")
        self.assertEqual([s["type"] for s in v2["sources"]], ["logs", "events"])
        self.assertEqual(v2["missingContext"], ["No metrics retention beyond 1h"])
        self.assertEqual(v2["assumptions"], ["The registry outage seen at 09:00 is over"])
        self.assertEqual(v2["reasoningTrace"][0]["evidenceRefs"], [0, 1])
        self.assertEqual(v2["rootCause"],
                         {"statement": "Bad image tag v9", "category": "image", "confidence": "0.85"})
        self.assertEqual(v2["howToFix"],
                         {"precondition": PRECONDITION, "apply": APPLY, "verify": VERIFY})

    def test_unfenced_json_tag_still_parses(self):
        prose, v2 = parse_structured_report(answer(VALID_BLOCK, fence=""))  # bare ``` fence
        self.assertEqual(prose, PROSE)
        self.assertTrue(v2)

    def test_last_valid_block_wins(self):
        first = answer({"rootCause": {"statement": "early draft", "confidence": 0.2}}, prose="Draft.")
        text = first + "\n\nFinal answer.\n" + answer(VALID_BLOCK, prose="")
        _, v2 = parse_structured_report(text)
        self.assertEqual(v2["rootCause"]["statement"], "Bad image tag v9")

    def test_json_only_answer_keeps_report_readable(self):
        prose, v2 = parse_structured_report(answer(VALID_BLOCK, prose=""))
        self.assertEqual(prose, "Bad image tag v9")         # falls back to the root-cause statement
        self.assertTrue(v2)

    def test_remediation_plan_is_not_carried(self):
        block = json.loads(json.dumps(VALID_BLOCK))
        block["remediationPlan"] = [{"description": "Point the deployment back to v8", "verb": "patch"}]
        _, v2 = parse_structured_report(answer(block))
        self.assertNotIn("remediationPlan", v2)
        self.assertIn("howToFix", v2)


class TestFallbacks(unittest.TestCase):
    def test_v1_prose_only(self):
        prose, v2 = parse_structured_report(PROSE)
        self.assertEqual(prose, PROSE)
        self.assertEqual(v2, {})

    def test_malformed_json_falls_back_to_prose(self):
        text = PROSE + '\n```json\n{"rootCause": {"statement": "x", }\n```'  # trailing comma + unclosed
        prose, v2 = parse_structured_report(text)
        self.assertEqual(prose, text)                       # full text kept, nothing lost
        self.assertEqual(v2, {})

    def test_json_block_without_v2_keys_ignored(self):
        text = PROSE + '\n```json\n{"foo": 1}\n```'
        prose, v2 = parse_structured_report(text)
        self.assertEqual((prose, v2), (text, {}))

    def test_empty_and_none_input(self):
        self.assertEqual(parse_structured_report(""), ("", {}))
        self.assertEqual(parse_structured_report(None), ("", {}))

    def test_never_raises_on_garbage_shapes(self):
        garbage = {"sources": "not-a-list", "reasoningTrace": {"step": 1}, "rootCause": ["x"],
                   "howToFix": 42, "missingContext": {"a": 1}, "assumptions": [{}],
                   "analyzedResources": [None, 3, "x"]}
        text = answer(garbage)
        prose, v2 = parse_structured_report(text)
        self.assertEqual(v2, {})                            # right keys, no usable content → v1
        self.assertEqual(prose, text)


class TestEvidenceRefs(unittest.TestCase):
    def test_out_of_bounds_refs_dropped_step_kept(self):
        block = {"sources": [{"type": "logs", "ref": "q", "excerpt": "e"}],
                 "reasoningTrace": [{"step": 1, "statement": "ok", "evidenceRefs": [0, 1, -1, 99]}],
                 "rootCause": {"statement": "x"}}
        _, v2 = parse_structured_report(answer(block))
        self.assertEqual(v2["reasoningTrace"][0]["evidenceRefs"], [0])

    def test_non_int_refs_dropped_float_int_coerced(self):
        block = {"sources": [{"ref": "a", "excerpt": "x"}, {"ref": "b", "excerpt": "y"}],
                 "reasoningTrace": [{"statement": "s", "evidenceRefs": [1.0, "0", True, None, 0.5]}],
                 "rootCause": {"statement": "x"}}
        _, v2 = parse_structured_report(answer(block))
        self.assertEqual(v2["reasoningTrace"][0]["evidenceRefs"], [1])  # 1.0→1; "0"/True/None/0.5 dropped

    def test_no_sources_means_no_valid_refs(self):
        block = {"reasoningTrace": [{"statement": "s", "evidenceRefs": [0]}],
                 "rootCause": {"statement": "x"}}
        _, v2 = parse_structured_report(answer(block))
        self.assertEqual(v2["reasoningTrace"][0]["evidenceRefs"], [])

    def test_steps_renumbered_in_order_and_unstated_dropped(self):
        block = {"sources": [{"ref": "a", "excerpt": "x"}],
                 "reasoningTrace": [{"step": 7, "statement": "first"}, {"step": 2},
                                    {"step": "n/a", "statement": "second", "evidenceRefs": [0]}],
                 "rootCause": {"statement": "x"}}
        _, v2 = parse_structured_report(answer(block))
        self.assertEqual([(t["step"], t["statement"]) for t in v2["reasoningTrace"]],
                         [(1, "first"), (2, "second")])


class TestSanitizers(unittest.TestCase):
    def test_confidence_normalization(self):
        for raw, want in [(0.85, "0.85"), ("0.7", "0.7"), (1, "1"), (1.5, "1"), (-3, "0"),
                          (0, "0"), ("abc", None), (None, None), (True, None)]:
            self.assertEqual(report_v2._confidence(raw), want, f"confidence({raw!r})")

    def test_source_type_outside_enum_becomes_object(self):
        block = {"sources": [{"type": "trace", "ref": "r", "excerpt": "e"}, "junk",
                             {"type": "metrics", "ref": "cpu", "excerpt": "97%"}],
                 "rootCause": {"statement": "x"}}
        _, v2 = parse_structured_report(answer(block))
        self.assertEqual([s["type"] for s in v2["sources"]], ["object", "metrics"])

    def test_root_cause_requires_statement(self):
        block = {"rootCause": {"confidence": 0.9, "category": "config"},
                 "missingContext": ["kept so the block is non-empty"]}
        _, v2 = parse_structured_report(answer(block))
        self.assertNotIn("rootCause", v2)
        self.assertEqual(v2["missingContext"], ["kept so the block is non-empty"])


class TestHowToFix(unittest.TestCase):
    """status.howToFix: all three scripts or none, never truncated, and a dropped set is said in
    missingContext when there is a root cause to fix."""

    NOTE = ": the incident has no scripts to check or fix it."

    def _parse(self, how, **extra):
        block = {"rootCause": {"statement": "x"}, "missingContext": ["gap"],
                 "sources": [{"type": "object", "ref": "prod/payment-api", "excerpt": "payment:v9"}],
                 **extra}
        if how is not ...:
            block["howToFix"] = how
        return parse_structured_report(answer(block))[1]

    def test_scripts_are_kept_verbatim_with_one_trailing_newline(self):
        v2 = self._parse({"precondition": "\n  " + PRECONDITION + "\n\n", "apply": APPLY,
                          "verify": VERIFY.rstrip("\n")})
        self.assertEqual(v2["howToFix"],
                         {"precondition": PRECONDITION, "apply": APPLY, "verify": VERIFY})
        self.assertEqual(v2["missingContext"], ["gap"])

    def test_a_partial_set_is_dropped_whole_and_the_report_kept(self):
        v2 = self._parse({"precondition": PRECONDITION, "apply": APPLY})
        self.assertNotIn("howToFix", v2)
        self.assertEqual(v2["rootCause"]["statement"], "x")
        self.assertEqual(v2["missingContext"], ["gap", "No usable howToFix (verify missing)" + self.NOTE])

    def test_non_string_and_blank_scripts_are_missing(self):
        v2 = self._parse({"precondition": 42, "apply": {"cmd": "kubectl"}, "verify": "   "})
        self.assertNotIn("howToFix", v2)
        self.assertEqual(v2["missingContext"][-1], "No usable howToFix (precondition missing; "
                                                   "apply missing; verify missing)" + self.NOTE)

    def test_an_over_long_script_is_dropped_never_truncated(self):
        long_apply = "#!/usr/bin/env bash\n" + "x" * report_v2.SCRIPT_MAX_CHARS
        v2 = self._parse({"precondition": PRECONDITION, "apply": long_apply, "verify": VERIFY})
        self.assertNotIn("howToFix", v2)
        self.assertIn(f"apply over {report_v2.SCRIPT_MAX_CHARS} characters", v2["missingContext"][-1])

    def test_a_list_of_lines_is_joined(self):
        v2 = self._parse({"precondition": PRECONDITION.splitlines(), "apply": APPLY, "verify": VERIFY})
        self.assertEqual(v2["howToFix"]["precondition"], PRECONDITION)

    def test_keys_other_than_the_three_scripts_are_dropped(self):
        v2 = self._parse({"precondition": PRECONDITION, "apply": APPLY, "verify": VERIFY,
                          "description": "roll back"})
        self.assertEqual(sorted(v2["howToFix"]), ["apply", "precondition", "verify"])

    def test_a_non_object_is_dropped(self):
        v2 = self._parse("kubectl set image deployment/payment-api api=payment:v8")
        self.assertNotIn("howToFix", v2)
        self.assertEqual(v2["missingContext"][-1], "No usable howToFix (not an object)" + self.NOTE)

    def test_an_absent_how_to_fix_is_noted_only_under_a_root_cause(self):
        self.assertEqual(self._parse(...)["missingContext"][-1],
                         "No usable howToFix (none returned)" + self.NOTE)
        _, v2 = parse_structured_report(answer({"missingContext": ["could not read pods"]}))
        self.assertEqual(v2["missingContext"], ["could not read pods"])

    def test_how_to_fix_alone_is_a_structured_block(self):
        how = {"precondition": PRECONDITION, "apply": APPLY, "verify": VERIFY}
        prose, v2 = parse_structured_report(answer({"howToFix": how}))
        self.assertEqual(prose, PROSE)
        self.assertEqual(v2, {"howToFix": how})

    def test_the_handler_writes_and_clears_how_to_fix(self):
        """The handler sends every V2_STATUS_KEYS key on each run, null when absent."""
        self.assertIn("howToFix", report_v2.V2_STATUS_KEYS)
        self.assertNotIn("remediationPlan", report_v2.V2_STATUS_KEYS)


class TestHandlerWiring(unittest.TestCase):
    """The thin handler-side pieces: prompt carries the contract; the CR gets trigger=alert."""

    def _handler(self):
        import handler  # imports requests; safe — the server only starts under __main__
        return handler

    def test_prompt_requires_structured_block(self):
        h = self._handler()
        p = h.build_prompt("err-logs", "ALERT", where="body:ERROR", message="errors spiking")
        self.assertIn("```json", p)
        self.assertIn("missingContext", p)
        self.assertIn("MUST be honest", p)
        self.assertIn("0-based indices into \"sources\"", p)
        self.assertTrue(p.rstrip().endswith("no trailing commas)."))

    def test_prompt_carries_the_how_to_fix_contract(self):
        p = self._handler().build_prompt("err-logs", "ALERT", where="body:ERROR")
        self.assertIn('"howToFix": {"precondition"', p)
        self.assertIn("0 = the incident is gone, 1 = it still holds", p)
        self.assertIn("MUST exit 1 then", p)
        self.assertIn("TEST THE ROOT-CAUSE OBJECT, NEVER THE ALERT'S SIGNAL", p)
        self.assertIn("killed after 60 seconds", p)
        self.assertIn("how to fix it.", p)
        self.assertNotIn("remediationPlan", p)
        self.assertNotIn("remediation plan", p)

    def test_upsert_sets_trigger_alert_on_create_and_patch(self):
        h = self._handler()
        calls = []
        orig = h._k8s
        h._k8s = lambda method, path, body=None, subresource="": calls.append((method, body)) or {}
        try:
            h._upsert_report("ns", "report-x", "x", "ALERT", "id", "prompt", "now", existing=None)
            h._upsert_report("ns", "report-x", "x", "ALERT", "id", "prompt", "now",
                             existing={"metadata": {"annotations": {}}})
        finally:
            h._k8s = orig
        create = next(b for m, b in calls if m == "POST")
        patch = next(b for m, b in calls if m == "PATCH" and b and "spec" in b)
        self.assertEqual(create["spec"]["trigger"], "alert")
        self.assertEqual(patch["spec"]["trigger"], "alert")

    def test_upsert_populates_robust_join_keys_on_create_and_patch(self):
        """The report carries the ID + slug join keys (from the matched Alert CR) on BOTH the
        create and the re-run patch, so the portal can id-join or slug-join deterministically."""
        h = self._handler()
        calls = []
        orig = h._k8s
        h._k8s = lambda method, path, body=None, subresource="": calls.append((method, body)) or {}
        try:
            h._upsert_report("krateo-system", "report-x", "🔥 Error log volume", "ALERT",
                             "6a55c0ba903d2bac4e3615e2", "prompt", "now", existing=None,
                             context_id="ctx", alert_ref="error-log-volume",
                             alert_namespace="krateo-system")
            h._upsert_report("krateo-system", "report-x", "🔥 Error log volume", "ALERT",
                             "6a55c0ba903d2bac4e3615e2", "prompt", "now",
                             existing={"metadata": {"annotations": {}}}, context_id="ctx",
                             alert_ref="error-log-volume", alert_namespace="krateo-system")
        finally:
            h._k8s = orig
        create = next(b for m, b in calls if m == "POST")["spec"]
        patch = next(b for m, b in calls if m == "PATCH" and b and "spec" in b)["spec"]
        for spec in (create, patch):
            self.assertEqual(spec["hyperdxAlertId"], "6a55c0ba903d2bac4e3615e2")
            self.assertEqual(spec["alertRef"], "error-log-volume")   # stable slug, NOT the emoji name
            self.assertEqual(spec["alertNamespace"], "krateo-system")
        # the human-facing display name is kept on create, distinct from the slug
        self.assertEqual(create["alertName"], "🔥 Error log volume")

    def test_upsert_omits_join_keys_when_alert_unmatched(self):
        """No matched Alert (empty id/ref) → don't stamp empty join keys; still keep trigger=alert
        and default alertNamespace to the report namespace on create."""
        h = self._handler()
        calls = []
        orig = h._k8s
        h._k8s = lambda method, path, body=None, subresource="": calls.append((method, body)) or {}
        try:
            h._upsert_report("krateo-system", "report-x", "orphan-alert", "ALERT", "", "p", "now",
                             existing=None, alert_ref="", alert_namespace="")
        finally:
            h._k8s = orig
        create = next(b for m, b in calls if m == "POST")["spec"]
        self.assertNotIn("hyperdxAlertId", create)
        self.assertNotIn("alertRef", create)
        self.assertEqual(create["alertNamespace"], "krateo-system")  # defaulted, never empty
        self.assertEqual(create["trigger"], "alert")

    def test_match_alert_looks_up_the_exact_name_the_title_carries(self):
        """The title is a state emoji + the HyperDX alert name, which is the Alert's metadata.name;
        _match_alert GETs exactly that CR."""
        h = self._handler()
        alert_cr = {"metadata": {"name": "error-log-volume", "namespace": "krateo-system"},
                    "spec": {"displayName": "Error log volume"},
                    "status": {"hyperdxAlertId": "6a55c0ba903d2bac4e3615e2", "state": "ALERT"}}
        paths = []
        orig = h._k8s
        h._k8s = lambda method, path, body=None, subresource="": paths.append(path) or alert_cr
        try:
            m = h._match_alert("🚨 error-log-volume", "krateo-system")
        finally:
            h._k8s = orig
        self.assertEqual(m["metadata"]["name"], "error-log-volume")
        self.assertEqual(paths, ["/apis/observability.krateo.io/v1alpha1/namespaces/krateo-system"
                                 "/alerts/error-log-volume"])

    def test_match_alert_returns_none_when_the_title_is_no_alert_name(self):
        """A displayName title (spaces, capitals) is not a metadata.name: no lookup, no match."""
        h = self._handler()
        calls = []
        orig = h._k8s
        h._k8s = lambda *a, **k: calls.append(a) or {}
        try:
            self.assertIsNone(h._match_alert("🚨 Pod crash-looping", "krateo-system"))
        finally:
            h._k8s = orig
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()


class TestYamlThenJsonFixture(unittest.TestCase):
    """Regression: a ```yaml example BEFORE the ```json block must not break extraction
    (live 2026-07-14: the yaml closer mis-paired as a json opener; the valid structured
    block was never parsed)."""

    def test_live_reply_with_yaml_block_parses_structured(self):
        import os
        fixture = os.path.join(os.path.dirname(__file__), "fixtures", "reply_yaml_then_json.txt")
        with open(fixture) as f:
            raw = f.read()
        prose, v2 = report_v2.parse_structured_report(raw)
        self.assertTrue(v2, "structured block must parse")
        self.assertTrue((v2.get("rootCause") or {}).get("statement"))
        # the reply carries a remediationPlan and no howToFix
        self.assertNotIn("remediationPlan", v2)
        self.assertNotIn("howToFix", v2)
        self.assertTrue(v2["missingContext"][-1].startswith("No usable howToFix (none returned)"))
        # the yaml example stays in the prose; the json block is stripped
        self.assertIn("```yaml", prose)
        self.assertNotIn('"remediationPlan"', prose)


class TestAlertSpecPush(unittest.TestCase):
    """#38: the reconciler had no update path, so every edit after creation was silently ignored
    while the CR reported phase: Synced. This is what makes 0.1.18's corrected thresholds reach a
    cluster that already had the alerts."""

    def _reconciler(self):
        import importlib
        import reconciler as r
        importlib.reload(r)
        return r

    class FakeHdx:
        """Records what was pushed. `state` comes back from the live alert, as the real one does."""
        MUTABLE = ("interval", "threshold", "thresholdType", "message")

        def __init__(self, live_alert, tile_where=''):
            self.live_alert, self._tile_where = live_alert, tile_where
            self.alert_puts, self.tile_puts, self.tile_reads = [], [], []

        def list_alerts(self):
            return [self.live_alert]

        def tile_where(self, dash):
            self.tile_reads.append(dash)
            return self._tile_where

        def update_dashboard_tile(self, dash, name, source, where='', tile_id='count'):
            self.tile_puts.append({'dash': dash, 'where': where, 'tile': tile_id})
            self._tile_where = where

        def alert_drift(self, live, name=None, **desired):
            want = {'interval': desired['interval'], 'threshold': desired['threshold'],
                    'thresholdType': desired['threshold_type'], 'message': desired['message']}
            return {k: (live.get(k), v) for k, v in want.items() if str(live.get(k)) != str(v)}

        def update_alert(self, alert_id, name, dash, tile, hook, **fields):
            self.alert_puts.append({'id': alert_id, 'dash': dash, 'tile': tile, **fields})
            self.live_alert.update({'interval': fields['interval'], 'threshold': fields['threshold'],
                                    'thresholdType': fields['threshold_type'],
                                    'message': fields['message']})
            return {'id': alert_id, 'state': self.live_alert.get('state', 'OK')}

    @staticmethod
    def _cr(**spec):
        base = {'interval': '5m', 'threshold': 1, 'thresholdType': 'above', 'message': 'm', 'where': 'w'}
        base.update(spec)
        return {'metadata': {'name': 'a-1'}, 'spec': base,
                'status': {'hyperdxAlertId': 'h1', 'hyperdxDashboardId': 'd1'}}

    @staticmethod
    def _live(**over):
        base = {'id': 'h1', 'state': 'ALERT', 'interval': '5m', 'threshold': 1,
                'thresholdType': 'above', 'message': 'm', 'tileId': 'count'}
        base.update(over)
        return base

    def _run(self, r, cr, hdx):
        patched = []
        r._patch_status = lambda name, st: patched.append(st)
        r._reconcile_report_lifecycle = lambda *a, **k: None
        r._reconcile_cr(hdx, cr, {'id': 's1'}, 'hook1')
        return patched

    def test_pushes_a_corrected_threshold_instead_of_ignoring_it(self):
        """THE BUG. 0.1.18 corrected 25 thresholds from 0 to 1; on an upgrade every CR already had
        a hyperdxAlertId, so every one took the early return and the fix was completely inert."""
        r = self._reconciler()
        hdx = self.FakeHdx(self._live(threshold=0))
        self._run(r, self._cr(threshold=1), hdx)
        self.assertEqual(len(hdx.alert_puts), 1)
        self.assertEqual(hdx.alert_puts[0]['threshold'], 1)

    def test_pushes_a_changed_where_to_the_dashboard_TILE(self):
        """`where` is a property of the tile, not the alert. A push that only did the alert would
        leave a corrected filter unapplied — the same silent failure one level down."""
        r = self._reconciler()
        hdx = self.FakeHdx(self._live(), tile_where='old')
        self._run(r, self._cr(where='new'), hdx)
        self.assertEqual([p['where'] for p in hdx.tile_puts], ['new'])

    def test_pushes_NOTHING_when_the_spec_already_matches(self):
        """The common case is no change, and it must stay a read: a push every cycle would rewrite
        all 25 alerts once a minute forever."""
        r = self._reconciler()
        hdx = self.FakeHdx(self._live(), tile_where='w')
        patched = self._run(r, self._cr(), hdx)
        self.assertEqual(hdx.alert_puts, [])
        self.assertEqual(hdx.tile_puts, [])
        self.assertEqual(patched[-1]['phase'], 'Synced')

    def test_does_NOT_report_Synced_when_the_push_FAILED(self):
        """The status actively asserted the opposite of the truth, which is what made this cost an
        afternoon to find. A failed push is SpecDrift: the live alert does not match this CR."""
        r = self._reconciler()
        hdx = self.FakeHdx(self._live(threshold=0))
        def boom(*a, **k):
            raise RuntimeError('hyperdx said no')
        hdx.update_alert = boom
        patched = self._run(r, self._cr(threshold=1), hdx)
        self.assertEqual(patched[-1]['phase'], 'SpecDrift')
        self.assertIn('hyperdx said no', patched[-1]['error'])

    def test_mirrors_the_live_STATE_even_when_the_push_failed(self):
        """Losing the alert's state on top of a failed push would hide that it is firing."""
        r = self._reconciler()
        hdx = self.FakeHdx(self._live(threshold=0, state='ALERT'))
        def boom(*a, **k):
            raise RuntimeError('nope')
        hdx.update_alert = boom
        patched = self._run(r, self._cr(threshold=1), hdx)
        self.assertEqual(patched[-1]['state'], 'ALERT')

    def test_pairs_the_dashboard_and_tile_FROM_THE_SAME_SOURCE(self):
        """THE 400 SEEN ON krateo-057. `sre-krateo-composition-reconcile-error` evaluates a tile on
        dashboard 6aa3f3fe…5667 while its CR status named 6aae4005…f4e3 — a different one. Pairing
        the status dashboard with the live tile describes a tile that is not on that dashboard, and
        validateAlertInput rejects the whole PUT."""
        r = self._reconciler()
        live = self._live(threshold=0, dashboardId='live-dash', tileId='live-tile')
        hdx = self.FakeHdx(live)
        cr = self._cr(threshold=1)
        cr['status']['hyperdxDashboardId'] = 'stale-dash'
        self._run(r, cr, hdx)
        self.assertEqual(hdx.alert_puts[0]['tile'], 'live-tile')
        self.assertEqual(hdx.alert_puts[0].get('dash', 'live-dash'), 'live-dash')

    def test_pushes_WHERE_to_the_dashboard_the_alert_actually_READS(self):
        """The silent half, and the worse one. `where` lands on the tile, so a push keyed on a
        stale status id rewrites a tile the alert does not evaluate — a corrected filter written to
        the wrong object and reported as success."""
        r = self._reconciler()
        live = self._live(dashboardId='live-dash', tileId='live-tile')
        hdx = self.FakeHdx(live, tile_where='old')
        cr = self._cr(where='new')
        cr['status']['hyperdxDashboardId'] = 'stale-dash'
        self._run(r, cr, hdx)
        self.assertEqual([p['dash'] for p in hdx.tile_puts], ['live-dash'])
        self.assertEqual(hdx.tile_reads, ['live-dash'])

    def test_falls_back_to_the_status_dashboard_when_the_alert_carries_none(self):
        r = self._reconciler()
        live = self._live(threshold=0)
        live.pop('dashboardId', None)
        hdx = self.FakeHdx(live, tile_where='w')
        cr = self._cr(threshold=1, where='w')
        cr['status']['hyperdxDashboardId'] = 'status-dash'
        self._run(r, cr, hdx)
        self.assertEqual(hdx.tile_reads, ['status-dash'])

    def test_addresses_the_tile_the_LIVE_ALERT_names(self):
        """Not a status field: `status` is structural with no preserve-unknown-fields, so a new key
        there is pruned silently. The alert already knows which tile it evaluates."""
        r = self._reconciler()
        hdx = self.FakeHdx(self._live(threshold=0, tileId='tile-xyz'))
        self._run(r, self._cr(threshold=1), hdx)
        self.assertEqual(hdx.alert_puts[0]['tile'], 'tile-xyz')


class TestEnsureAlertReconciles(unittest.TestCase):
    """#38's second barrier: `ensure_alert` was ensure-EXISTS, so it returned a name-matched alert
    untouched. That is what defeated the obvious operator recovery — clearing
    `status.hyperdxAlertId` sent the CR back down the create path and it handed back the same stale
    alert, leaving no way to change a threshold from the Kubernetes side at all."""

    def _hdx(self, live):
        import hyperdx_v2
        h = hyperdx_v2.HyperDXV2.__new__(hyperdx_v2.HyperDXV2)
        h._dashboards = None
        h.calls = []

        def fake_req(method, path, body=None):
            h.calls.append((method, path, body))
            if method == 'GET' and path == '/api/v2/alerts':
                return live
            if method == 'PUT':
                return {'id': path.rsplit('/', 1)[-1], 'state': 'OK'}
            return {'id': 'new-1', 'state': 'OK'}
        h._req = fake_req
        return h

    def test_UPDATES_a_name_matched_alert_whose_threshold_drifted(self):
        h = self._hdx([{'id': 'h1', 'name': 'my-alert', 'state': 'ALERT',
                        'interval': '5m', 'threshold': 0, 'thresholdType': 'above', 'message': 'm'}])
        out = h.ensure_alert('my-alert', 'd1', 'count', 'hook', threshold=1, message='m')
        self.assertEqual(out['id'], 'h1')
        puts = [c for c in h.calls if c[0] == 'PUT']
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0][2]['threshold'], 1)

    def test_does_NOT_write_when_the_name_match_already_agrees(self):
        h = self._hdx([{'id': 'h1', 'name': 'my-alert', 'state': 'OK',
                        'interval': '5m', 'threshold': 1, 'thresholdType': 'above', 'message': 'm'}])
        h.ensure_alert('my-alert', 'd1', 'count', 'hook', threshold=1, message='m')
        self.assertEqual([c for c in h.calls if c[0] in ('PUT', 'POST')], [])

    def test_compares_as_STRINGS_so_1_and_quoted_1_are_not_perpetual_drift(self):
        """The API returns threshold as a number and a CR may carry either; `1 != "1"` would be a
        drift corrected on every cycle, rewriting the alert forever."""
        h = self._hdx([{'id': 'h1', 'name': 'my-alert', 'state': 'OK',
                        'interval': '5m', 'threshold': 1, 'thresholdType': 'above', 'message': 'm'}])
        h.ensure_alert('my-alert', 'd1', 'count', 'hook', threshold='1', message='m')
        self.assertEqual([c for c in h.calls if c[0] == 'PUT'], [])

    def test_still_CREATES_when_no_alert_carries_the_name(self):
        h = self._hdx([])
        out = h.ensure_alert('my-alert', 'd1', 'count', 'hook', threshold=1)
        self.assertEqual(out['id'], 'new-1')
        self.assertEqual([c[0] for c in h.calls if c[0] == 'POST'], ['POST'])


class TestTautologicalThresholds(unittest.TestCase):
    """A count is never negative, so some threshold/type pairs are decided before any data is read.
    Refusing them is arithmetic, not taste — and it has cost real money twice: the 0.1.17 catalogue
    shipped 25 alerts at `above 0`, and an Autopilot-authored alert on krateo-057 reached run 72 the
    same way, each firing launching an RCA."""

    def _r(self):
        import importlib
        import reconciler as r
        importlib.reload(r)
        return r

    def test_above_zero_is_refused_it_fires_on_an_empty_result(self):
        r = self._r()
        why = r.tautology(0, 'above')
        self.assertIsNotNone(why)
        self.assertIn('every evaluation', why)
        self.assertIn('Use 1', why)          # says which value, not just that it is wrong

    def test_below_zero_is_refused_it_can_NEVER_fire(self):
        """The worse shape: it looks armed and is not."""
        self.assertIsNotNone(self._r().tautology(0, 'below'))

    def test_below_or_equal_minus_one_is_refused(self):
        self.assertIsNotNone(self._r().tautology(-1, 'below_or_equal'))

    def test_the_ordinary_thresholds_are_ALLOWED(self):
        r = self._r()
        for value, kind in ((1, 'above'), (3, 'above'), (100, 'below'), (0, 'below_or_equal'), (11, 'above')):
            self.assertIsNone(r.tautology(value, kind), f'{kind} {value} must be allowed')

    def test_a_non_numeric_threshold_is_left_to_the_CRD(self):
        """The CRD types this field; duplicating that here would be a second opinion that can drift."""
        self.assertIsNone(self._r().tautology(None, 'above'))
        self.assertIsNone(self._r().tautology('abc', 'above'))

    def test_reconcile_REFUSES_and_says_what_to_change(self):
        r = self._r()
        patched = []
        r._patch_status = lambda name, st: patched.append(st)
        calls = []

        class Hdx:
            def list_alerts(self):
                calls.append('list')
                return []
        r._reconcile_cr(Hdx(), {'metadata': {'name': 'bad'}, 'spec': {'threshold': 0, 'thresholdType': 'above'},
                                'status': {}}, {'id': 's'}, 'hook')
        self.assertEqual(patched[-1]['phase'], 'Invalid')
        self.assertIn('Use 1', patched[-1]['error'])
        self.assertEqual(calls, [])   # refused BEFORE touching HyperDX at all

    def test_refusing_does_NOT_delete_an_alert_that_already_exists(self):
        """Stopping the pushes is enough to stop the damage. Tearing down an object someone may be
        looking at, on a rule we just added, is a bigger action than this warrants."""
        r = self._r()
        deleted = []
        r._patch_status = lambda name, st: None

        class Hdx:
            def list_alerts(self):
                return [{'id': 'h1', 'state': 'ALERT'}]
            def delete_alert(self, i):
                deleted.append(i)
        r._reconcile_cr(Hdx(), {'metadata': {'name': 'bad'}, 'spec': {'threshold': 0, 'thresholdType': 'above'},
                                'status': {'hyperdxAlertId': 'h1'}}, {'id': 's'}, 'hook')
        self.assertEqual(deleted, [])


class TestEnsureDashboardTileCreate(unittest.TestCase):
    """The CREATE path of ensure_dashboard_tile had NO test, which is exactly how a dangling name
    reached a cluster: the lookup returns first for every alert whose dashboard already exists, so
    every existing test walked the early return and the create body was never executed."""

    def _hdx(self, dashboards):
        import hyperdx_v2
        h = hyperdx_v2.HyperDXV2.__new__(hyperdx_v2.HyperDXV2)
        h._dashboards = None
        h.calls = []

        def fake_req(method, path, body=None):
            h.calls.append((method, path, body))
            if method == 'GET' and path == '/api/v2/dashboards':
                return dashboards
            return {'id': 'dash-new', 'tiles': [{'id': 'count'}]}
        h._req = fake_req
        return h

    def test_CREATES_the_dashboard_when_none_carries_the_name(self):
        h = self._hdx([])
        dash, tile = h.ensure_dashboard_tile('krateo-alert-x', {'id': 'src-1'}, "ServiceName = 'x'")
        self.assertEqual((dash, tile), ('dash-new', 'count'))
        post = [c for c in h.calls if c[0] == 'POST'][0]
        cfg = post[2]['tiles'][0]['config']
        self.assertEqual(cfg['sourceId'], 'src-1')          # the reference that was dangling
        self.assertEqual(cfg['select'][0]['where'], "ServiceName = 'x'")

    def test_the_created_tile_pins_whereLanguage_sql(self):
        """Load-bearing: omitted, HyperDX maps the series to aggConditionLanguage 'lucene' and the
        SQL filter becomes a full-text search that self-matches — the alert fires on a phantom."""
        h = self._hdx([])
        h.ensure_dashboard_tile('krateo-alert-x', {'id': 'src-1'}, 'a = 1')
        post = [c for c in h.calls if c[0] == 'POST'][0]
        self.assertEqual(post[2]['tiles'][0]['config']['select'][0]['whereLanguage'], 'sql')

    def test_REUSES_an_existing_dashboard_without_posting(self):
        h = self._hdx([{'id': 'd1', 'name': 'krateo-alert-x', 'tiles': [{'id': 't1'}]}])
        self.assertEqual(h.ensure_dashboard_tile('krateo-alert-x', {'id': 'src-1'}), ('d1', 't1'))
        self.assertEqual([c for c in h.calls if c[0] == 'POST'], [])

    def test_create_and_update_build_the_SAME_tile(self):
        """The property that makes the dangling reference unrepresentable: one builder, both verbs."""
        h = self._hdx([])
        h.ensure_dashboard_tile('krateo-alert-x', {'id': 'src-1'}, 'a = 1')
        created = [c for c in h.calls if c[0] == 'POST'][0][2]['tiles'][0]
        h2 = self._hdx([])
        h2.update_dashboard_tile('d1', 'krateo-alert-x', {'id': 'src-1'}, 'a = 1')
        updated = [c for c in h2.calls if c[0] == 'PUT'][0][2]['tiles'][0]
        self.assertEqual(created, updated)
