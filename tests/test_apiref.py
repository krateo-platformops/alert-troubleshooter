"""apiRef alerts: a RESTAction's value, polled by the reconciler, compared with the threshold."""
import importlib
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import apiref  # noqa: E402
import handler  # noqa: E402
from fake_k8s import FakeK8s  # noqa: E402

REF = {"name": "compositiondefinitions-not-ready", "namespace": "krateo-system"}


def _alert(spec=None, status=None, name="cd-not-ready"):
    base = {"displayName": "CompositionDefinition not ready", "apiRef": dict(REF),
            "interval": "5m", "threshold": 1, "thresholdType": "above", "message": "m"}
    base.update(spec or {})
    return {"metadata": {"name": name, "namespace": "krateo-system"}, "spec": base,
            "status": dict(status or {})}


class TestCompare(unittest.TestCase):
    """The same semantics HyperDX applies to a row count (checkAlerts doesExceedThreshold)."""

    def test_each_threshold_type(self):
        cases = [("above", 1, 1, True), ("above", 0, 1, False),
                 ("above_exclusive", 1, 1, False), ("above_exclusive", 2, 1, True),
                 ("below", 0, 1, True), ("below", 1, 1, False),
                 ("below_or_equal", 1, 1, True), ("below_or_equal", 2, 1, False),
                 ("equal", 3, 3, True), ("equal", 2, 3, False),
                 ("not_equal", 2, 3, True), ("not_equal", 3, 3, False)]
        for kind, value, threshold, want in cases:
            self.assertEqual(apiref.exceeds(value, threshold, kind), want, (kind, value, threshold))

    def test_between_needs_a_thresholdMax_the_Alert_lacks(self):
        self.assertIn("thresholdMax", apiref.invalid("between"))
        self.assertIn("thresholdMax", apiref.invalid("not_between"))
        self.assertIsNone(apiref.invalid("above"))

    def test_value_must_be_a_number(self):
        self.assertEqual(apiref.value_of({"value": 3}), 3)
        self.assertEqual(apiref.value_of({"value": 2.5}), 2.5)
        for bad in ({}, {"value": "3"}, {"value": True}, {"value": None}, {"value": [1]}):
            with self.assertRaises(apiref.ApiRefError):
                apiref.value_of(bad)


class TestResolve(unittest.TestCase):
    def setUp(self):
        self._orig = (handler._service_jwt, apiref.requests.get)
        self.calls = []

    def tearDown(self):
        handler._service_jwt, apiref.requests.get = self._orig

    def _respond(self, code, body):
        def get(url, params=None, headers=None, timeout=None):
            self.calls.append((url, params, headers))
            return types.SimpleNamespace(status_code=code, text=str(body), json=lambda: body)
        apiref.requests.get = get

    def test_calls_snowplow_as_the_service_identity(self):
        handler._service_jwt = lambda: "jwt-1"
        self._respond(200, {"kind": "RESTAction", "status": {"value": 2, "items": ["a", "b"]}})
        self.assertEqual(apiref.resolve(REF), {"value": 2, "items": ["a", "b"]})
        url, params, headers = self.calls[0]
        self.assertEqual(url, f"{apiref.SNOWPLOW_URL}/call")
        self.assertEqual(params, {"apiVersion": "templates.krateo.io/v1", "resource": "restactions",
                                  "namespace": "krateo-system",
                                  "name": "compositiondefinitions-not-ready"})
        self.assertEqual(headers["Authorization"], "Bearer jwt-1")

    def test_no_service_JWT_says_authnUrl_is_needed(self):
        handler._service_jwt = lambda: ""
        self._respond(200, {})
        with self.assertRaisesRegex(apiref.ApiRefError, "authnUrl"):
            apiref.resolve(REF)
        self.assertEqual(self.calls, [])

    def test_a_non_200_is_an_error(self):
        handler._service_jwt = lambda: "jwt-1"
        self._respond(403, {"message": "forbidden"})
        with self.assertRaisesRegex(apiref.ApiRefError, "403"):
            apiref.resolve(REF)

    def test_a_status_that_is_not_an_object_is_an_error(self):
        handler._service_jwt = lambda: "jwt-1"
        self._respond(200, {"status": 3})
        with self.assertRaisesRegex(apiref.ApiRefError, "value: N"):
            apiref.resolve(REF)


class TestDue(unittest.TestCase):
    NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)

    def _at(self, minutes_ago):
        return (self.NOW - timedelta(minutes=minutes_ago)).isoformat()

    def test_interval_is_the_polling_period(self):
        self.assertTrue(apiref.due({}, "5m", self.NOW))
        self.assertFalse(apiref.due({"phase": "Synced", "lastSyncedAt": self._at(4)}, "5m", self.NOW))
        self.assertTrue(apiref.due({"phase": "Synced", "lastSyncedAt": self._at(5)}, "5m", self.NOW))
        self.assertFalse(apiref.due({"phase": "Synced", "lastSyncedAt": self._at(50)}, "1h", self.NOW))

    def test_a_failed_evaluation_is_retried_next_cycle(self):
        self.assertTrue(apiref.due({"phase": "Error", "lastSyncedAt": self._at(0)}, "1d", self.NOW))


class TestReconcileApiRef(unittest.TestCase):
    def setUp(self):
        self.r = importlib.reload(importlib.import_module("reconciler"))
        self.r._now = lambda: "NOW"
        self.patched, self.started, self.resolved = [], [], []
        self.r._patch_status = lambda name, st: self.patched.append((name, st))
        self.r._start_analysis = lambda **kw: self.started.append(kw)
        self.status = {"value": 2, "items": ["fireworksapp"]}
        self.r.apiref.resolve = lambda ref: self.resolved.append(ref) or dict(self.status)

    def tearDown(self):
        importlib.reload(apiref)

    def test_a_value_over_the_threshold_fires_and_starts_the_RCA(self):
        self.r._reconcile_apiref(_alert(status={"state": "OK", "okSince": "T0"}))
        name, st = self.patched[-1]
        self.assertEqual(name, "cd-not-ready")
        self.assertEqual((st["state"], st["okSince"], st["phase"]), ("ALERT", None, "Synced"))
        self.assertIsNone(st["error"])
        kw = self.started[0]
        self.assertEqual(kw["alert_ref"], "cd-not-ready")
        self.assertEqual(kw["alert_state"], "ALERT")
        self.assertEqual(kw["interval"], "5m")                # the Resolved grace window
        self.assertEqual(kw["api"], {**REF, "value": 2, "threshold": 1, "thresholdType": "above",
                                     "items": ["fireworksapp"]})

    def test_a_value_under_the_threshold_is_OK_and_runs_no_RCA(self):
        self.status = {"value": 0, "items": []}
        self.r._reconcile_apiref(_alert(status={"state": "ALERT"}))
        st = self.patched[-1][1]
        self.assertEqual((st["state"], st["okSince"]), ("OK", "NOW"))
        self.assertEqual(self.started, [])

    def test_a_snowplow_failure_is_phase_Error_and_keeps_the_state(self):
        def boom(ref):
            raise apiref.ApiRefError("snowplow returned 403: forbidden")
        self.r.apiref.resolve = boom
        self.r._reconcile_apiref(_alert(status={"state": "ALERT"}))
        st = self.patched[-1][1]
        self.assertEqual(st["phase"], "Error")
        self.assertIn("403", st["error"])
        self.assertNotIn("state", st)
        self.assertNotIn("value", st)
        self.assertEqual(self.started, [])

    def test_the_RESTAction_value_is_written_with_the_state(self):
        self.r._reconcile_apiref(_alert(status={"state": "OK"}))
        self.assertEqual(self.patched[-1][1]["value"], 2)
        self.status = {"value": 0.5}
        self.r._reconcile_apiref(_alert(status={"state": "ALERT"}))
        self.assertEqual((self.patched[-1][1]["state"], self.patched[-1][1]["value"]), ("OK", 0.5))

    def test_not_due_means_no_call(self):
        recent = datetime.now(timezone.utc).isoformat()
        self.r._reconcile_apiref(_alert(status={"phase": "Synced", "lastSyncedAt": recent}))
        self.assertEqual((self.resolved, self.patched), ([], []))

    def test_between_is_Invalid_before_any_call(self):
        self.r._reconcile_apiref(_alert(spec={"thresholdType": "between"}))
        self.assertEqual(self.patched[-1][1]["phase"], "Invalid")
        self.assertEqual(self.resolved, [])

    def test_the_row_count_tautology_does_not_apply(self):
        """'above 0' is refused for a count, but a RESTAction's value may be negative."""
        self.status = {"value": -1}
        self.r._reconcile_apiref(_alert(spec={"threshold": 0}))
        self.assertEqual(self.patched[-1][1]["phase"], "Synced")


class TestReconcileOnceWithApiRef(unittest.TestCase):
    class DownHdx:
        def invalidate_cache(self):
            pass

        def first_source(self):
            raise RuntimeError("HyperDX is down")

    class Hdx:
        def __init__(self):
            self.deleted, self.created = [], []

        def invalidate_cache(self):
            pass

        def first_source(self):
            return {"id": "s"}

        def ensure_webhook(self, *a, **k):
            return "hook", False

        def list_alerts(self):
            return []

        def delete_alert(self, i):
            self.deleted.append(("alert", i))

        def delete_dashboard(self, i):
            self.deleted.append(("dashboard", i))

        def ensure_dashboard_tile(self, name, *a, **k):
            self.created.append(name)
            return "d", "t"

        def ensure_alert(self, name, *a, **k):
            return {"id": "h", "state": "OK"}

    def setUp(self):
        self.r = importlib.reload(importlib.import_module("reconciler"))
        self.patched = []
        self.r._patch_status = lambda name, st: self.patched.append((name, st))
        self.r._patch_finalizers = lambda *a: None
        self.r._start_analysis = lambda **kw: None
        self.r.apiref.resolve = lambda ref: {"value": 0}

    def tearDown(self):
        importlib.reload(apiref)

    def test_apiRef_alerts_are_evaluated_while_HyperDX_is_down(self):
        self.r._list_alert_crs = lambda: [_alert()]
        with self.assertRaises(RuntimeError):
            self.r.reconcile_once(self.DownHdx())
        self.assertEqual(self.patched[0][1]["state"], "OK")

    def test_apiRef_alerts_get_no_HyperDX_objects_and_lose_leftover_ones(self):
        switched = _alert(status={"hyperdxAlertId": "h-old", "hyperdxDashboardId": "d-old"},
                          name="switched")
        where = {"metadata": {"name": "logs"},
                 "spec": {"where": "w", "interval": "5m", "threshold": 1, "thresholdType": "above"},
                 "status": {}}
        self.r._list_alert_crs = lambda: [_alert(), switched, where]
        hdx = self.Hdx()
        self.r.reconcile_once(hdx)
        self.assertEqual(hdx.created, ["krateo-alert-logs"])
        self.assertEqual(sorted(hdx.deleted), [("alert", "h-old"), ("dashboard", "d-old")])
        self.assertIn(("switched", {"hyperdxAlertId": None, "hyperdxDashboardId": None}),
                      self.patched)

    def test_a_where_alert_has_no_value(self):
        where = {"metadata": {"name": "logs"},
                 "spec": {"where": "w", "interval": "5m", "threshold": 1, "thresholdType": "above"},
                 "status": {"value": 3}}
        self.r._list_alert_crs = lambda: [where]
        self.r.reconcile_once(self.Hdx())
        st = [st for name, st in self.patched if name == "logs" and "state" in st][-1]
        self.assertIn("value", st)
        self.assertIsNone(st["value"])


class TestPrompt(unittest.TestCase):
    API = {**REF, "value": 2, "threshold": 1, "thresholdType": "above",
           "items": ["fireworksapp", "other"]}

    def test_an_apiRef_prompt_names_the_RESTAction_value_and_items(self):
        p = handler.build_prompt("CompositionDefinition not ready", "ALERT", message="cds broken",
                                 api=self.API)
        self.assertIn("RESTAction `krateo-system/compositiondefinitions-not-ready` returned value 2 "
                      "(above 1)", p)
        self.assertIn('["fireworksapp", "other"]', p)
        self.assertIn("cds broken", p)
        self.assertNotIn("HyperDX alert", p)
        self.assertNotIn("log records matching", p)

    def test_items_are_bounded(self):
        api = dict(self.API, items=["x" * 100] * 200)
        p = handler.build_prompt("a", "ALERT", api=api)
        self.assertIn("…(truncated)", p)
        self.assertLess(len(p), len(handler.build_prompt("a", "ALERT")) + handler.ITEMS_PROMPT_CHARS + 500)

    def test_analyze_opens_an_incident_with_the_apiRef_prompt(self):
        k8s = FakeK8s()
        orig = (handler._k8s, handler.a2a_analyze)
        handler._k8s = k8s
        handler.a2a_analyze = lambda prompt, ctx=None: ("analysis", [])
        try:
            handler.analyze("CompositionDefinition not ready", "ALERT", "cd-not-ready",
                            "krateo-system", api=self.API)
        finally:
            handler._k8s, handler.a2a_analyze = orig
        spec = k8s.only()["spec"]
        self.assertEqual(spec["alertRef"], {"name": "cd-not-ready", "namespace": "krateo-system"})
        self.assertIn("fireworksapp", spec["prompt"])


if __name__ == "__main__":
    unittest.main()
