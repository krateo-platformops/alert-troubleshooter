"""Exact Alert identity: one Alert CR is one HyperDX alert, one webhook target, its own incidents.

The HyperDX side runs the real HyperDXV2 client against an in-memory API that keeps the two
behaviours these bugs hinge on: a dashboard PUT keeps a tile id only if it already exists, and
deletes the alerts on a replaced tile.
"""
import copy
import importlib
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

import handler  # noqa: E402
import hyperdx_v2  # noqa: E402
from fake_k8s import FakeK8s  # noqa: E402

DISPLAY = "Krateo — composition reconcile errors"
K, S = "krateo-composition-reconcile-error", "sre-krateo-composition-reconcile-error"


def _http_error(code):
    return requests.HTTPError(f"{code}", response=types.SimpleNamespace(status_code=code))


class FakeHyperDXAPI:
    """The subset of HyperDX's /api/v2 the reconciler calls."""

    def __init__(self):
        self.webhooks, self.dashboards, self.alerts = {}, {}, {}
        self.writes = []
        self._n = 0

    def _id(self):
        self._n += 1
        return f"oid{self._n:04d}"

    def _new_tiles(self, tiles, existing=()):
        out = []
        for t in tiles:
            t = copy.deepcopy(t)
            t["id"] = t["id"] if t.get("id") in existing else self._id()
            out.append(t)
        return out

    def req(self, method, path, body=None):
        parts = path.strip("/").split("/")  # api, v2, kind[, id]
        kind, oid = parts[2], (parts[3] if len(parts) > 3 else None)
        if method != "GET":
            self.writes.append((method, kind, oid))
        if kind == "sources":
            return [{"id": "src-1"}]
        store = {"webhooks": self.webhooks, "dashboards": self.dashboards, "alerts": self.alerts}[kind]
        if method == "GET":
            return copy.deepcopy(list(store.values()))
        if method == "POST":
            doc = copy.deepcopy(body)
            doc["id"] = self._id()
            if kind == "dashboards":
                doc["tiles"] = self._new_tiles(doc["tiles"])
            if kind == "alerts":
                doc["state"] = "OK"
            store[doc["id"]] = doc
            return copy.deepcopy(doc)
        if oid not in store:
            raise _http_error(404)
        if method == "DELETE":
            del store[oid]
            return None
        if method == "PUT":
            doc = copy.deepcopy(body)
            doc["id"] = oid
            if kind == "dashboards":
                old = {t["id"] for t in store[oid]["tiles"]}
                doc["tiles"] = self._new_tiles(doc["tiles"], old)
                gone = old - {t["id"] for t in doc["tiles"]}
                for aid in [a for a, al in self.alerts.items()
                            if al.get("dashboardId") == oid and al.get("tileId") in gone]:
                    del self.alerts[aid]
            if kind == "alerts":
                doc["state"] = store[oid].get("state", "OK")
            store[oid] = doc
            return copy.deepcopy(doc)
        raise AssertionError(f"unexpected {method} {path}")

    # --- views for assertions ---
    def where_of(self, alert):
        tiles = self.dashboards[alert["dashboardId"]]["tiles"]
        tile = next(t for t in tiles if t["id"] == alert["tileId"])
        return tile["config"]["select"][0]["where"]


def _client(api):
    h = hyperdx_v2.HyperDXV2.__new__(hyperdx_v2.HyperDXV2)
    h._dashboards = None
    h._req = api.req
    return h


def _seed_alert(api, dash_name, alert_name, where):
    """A pre-existing alert on the reconciler's own webhook, as on a running cluster."""
    hdx = _client(api)
    wid, _ = hdx.ensure_webhook("krateo-autopilot", "http://x/webhook")
    dash, tile = hdx.ensure_dashboard_tile(dash_name, {"id": "src-1"}, where)
    return hdx.ensure_alert(alert_name, dash, tile, wid, interval="15m", threshold=2,
                            message="m")["id"]


def _alert_cr(name, where, display=DISPLAY, status=None):
    return {"metadata": {"name": name, "namespace": "krateo-system", "finalizers": []},
            "spec": {"displayName": display, "where": where, "interval": "15m", "threshold": 2,
                     "thresholdType": "above", "message": "m"},
            "status": dict(status or {})}


class ReconcilerHarness:
    """reconcile_once over in-memory Alert CRs: status patches land on the stored CR."""

    def __init__(self, crs):
        self.r = importlib.reload(importlib.import_module("reconciler"))
        self.crs = {cr["metadata"]["name"]: cr for cr in crs}
        self.r._list_alert_crs = lambda: copy.deepcopy(list(self.crs.values()))
        self.r._patch_status = self._patch_status
        self.r._patch_finalizers = lambda name, fins: None

    def _patch_status(self, name, status):
        st = self.crs[name].setdefault("status", {})
        for k, v in status.items():
            if v is None:
                st.pop(k, None)
            else:
                st[k] = v

    def status(self, name):
        return self.crs[name]["status"]


class TestSameDisplayNameTwoAlerts(unittest.TestCase):
    """Bug #2: HyperDX alerts were matched by displayName, so two CRs sharing one fought over a
    single alert — on krateo-057, SpecDrift 400 every other cycle and a new alert id each minute."""

    def _pass(self, harness, api, n=1):
        hdx = _client(api)
        for _ in range(n):
            harness.r.reconcile_once(hdx)

    def test_two_CRs_with_one_displayName_get_two_hyperdx_alerts(self):
        api = FakeHyperDXAPI()
        h = ReconcilerHarness([_alert_cr(K, "where-k"), _alert_cr(S, "where-s")])
        self._pass(h, api, 2)
        by_name = {a["name"]: a for a in api.alerts.values()}
        self.assertEqual(sorted(by_name), [K, S])
        self.assertEqual(h.status(K)["hyperdxAlertId"], by_name[K]["id"])
        self.assertEqual(h.status(S)["hyperdxAlertId"], by_name[S]["id"])
        self.assertNotEqual(by_name[K]["tileId"], by_name[S]["tileId"])
        self.assertEqual(api.where_of(by_name[K]), "where-k")
        self.assertEqual(api.where_of(by_name[S]), "where-s")

    def test_the_pair_is_stable_no_write_after_it_converges(self):
        """The live symptom was churn: every cycle rewrote and recreated. Converged means a read."""
        api = FakeHyperDXAPI()
        h = ReconcilerHarness([_alert_cr(K, "where-k"), _alert_cr(S, "where-s")])
        self._pass(h, api, 2)
        ids = sorted(api.alerts)
        api.writes.clear()
        self._pass(h, api, 3)
        self.assertEqual([w for w in api.writes if w[1] != "webhooks"], [])
        self.assertEqual(sorted(api.alerts), ids)
        self.assertEqual({h.status(K)["phase"], h.status(S)["phase"]}, {"Synced"})

    def test_a_shared_legacy_alert_is_released_and_each_CR_gets_its_own(self):
        """krateo-057's state: one alert named after the displayName, claimed by both CRs."""
        api = FakeHyperDXAPI()
        legacy = _seed_alert(api, f"krateo-alert-{S}", DISPLAY, "where-s")
        h = ReconcilerHarness([_alert_cr(K, "where-k", status={"hyperdxAlertId": legacy}),
                               _alert_cr(S, "where-s", status={"hyperdxAlertId": legacy})])
        self._pass(h, api, 2)
        self.assertNotIn(legacy, api.alerts)
        by_name = {a["name"]: a for a in api.alerts.values()}
        self.assertEqual(sorted(by_name), [K, S])
        self.assertEqual(api.where_of(by_name[K]), "where-k")
        self.assertEqual(api.where_of(by_name[S]), "where-s")
        api.writes.clear()
        self._pass(h, api, 2)
        self.assertEqual([w for w in api.writes if w[1] != "webhooks"], [])

    def test_a_shared_alert_already_named_after_one_CR_stays_with_it(self):
        api = FakeHyperDXAPI()
        owned = _seed_alert(api, f"krateo-alert-{K}", K, "where-k")
        h = ReconcilerHarness([_alert_cr(K, "where-k", status={"hyperdxAlertId": owned}),
                               _alert_cr(S, "where-s", status={"hyperdxAlertId": owned})])
        self._pass(h, api, 2)
        self.assertEqual(h.status(K)["hyperdxAlertId"], owned)
        self.assertNotEqual(h.status(S)["hyperdxAlertId"], owned)
        self.assertEqual(api.alerts[h.status(S)["hyperdxAlertId"]]["name"], S)

    def test_a_legacy_alert_with_one_claimant_is_renamed_in_place(self):
        """Upgrade path for every other CR: same id, name moved from displayName to metadata.name."""
        api = FakeHyperDXAPI()
        legacy = _seed_alert(api, f"krateo-alert-{K}", DISPLAY, "where-k")
        h = ReconcilerHarness([_alert_cr(K, "where-k", status={"hyperdxAlertId": legacy})])
        self._pass(h, api)
        self.assertEqual(list(api.alerts), [legacy])
        self.assertEqual(api.alerts[legacy]["name"], K)

    def test_a_lost_status_id_adopts_the_alert_named_after_the_CR(self):
        api = FakeHyperDXAPI()
        h = ReconcilerHarness([_alert_cr(K, "where-k")])
        self._pass(h, api)
        first = h.status(K)["hyperdxAlertId"]
        h.status(K).pop("hyperdxAlertId")
        self._pass(h, api)
        self.assertEqual(h.status(K)["hyperdxAlertId"], first)
        self.assertEqual(len(api.alerts), 1)

    def test_a_where_edit_keeps_the_alert(self):
        """A dashboard PUT with an unknown tile id replaces the tile and HyperDX deletes the alerts
        on it. The push sends the live tile id, so the alert survives the edit."""
        api = FakeHyperDXAPI()
        h = ReconcilerHarness([_alert_cr(K, "where-k")])
        self._pass(h, api)
        aid = h.status(K)["hyperdxAlertId"]
        h.crs[K]["spec"]["where"] = "where-k2"
        self._pass(h, api)
        self.assertIn(aid, api.alerts)
        self.assertEqual(api.where_of(api.alerts[aid]), "where-k2")
        self.assertEqual(h.status(K)["hyperdxAlertId"], aid)

    def test_a_new_CR_never_reuses_a_tile_another_alert_evaluates(self):
        """Dashboard names were rewritten by the old ping-pong, so a lookup by name can land on a
        dashboard another alert already reads."""
        api = FakeHyperDXAPI()
        h = ReconcilerHarness([_alert_cr(K, "where-k")])
        self._pass(h, api)
        k_alert = api.alerts[h.status(K)["hyperdxAlertId"]]
        api.dashboards[k_alert["dashboardId"]]["name"] = f"krateo-alert-{S}"
        h.crs[S] = _alert_cr(S, "where-s")
        self._pass(h, api)
        s_alert = api.alerts[h.status(S)["hyperdxAlertId"]]
        self.assertNotEqual(s_alert["tileId"], k_alert["tileId"])
        self.assertEqual(api.where_of(api.alerts[k_alert["id"]]), "where-k")


class TestWebhookTemplate(unittest.TestCase):
    """HyperDX's generic webhook body can use {{title}}, {{body}}, {{link}}, {{state}},
    {{startTime}}, {{endTime}} and {{eventId}} — no alert id — so the title carries the identity."""

    def test_the_body_carries_the_title_and_the_real_state(self):
        body = hyperdx_v2.DEFAULT_WEBHOOK_BODY
        self.assertIn('"alertName":"{{title}}"', body)
        self.assertIn('"state":"{{state}}"', body)

    def test_an_existing_webhook_with_an_old_body_is_updated_in_place(self):
        api = FakeHyperDXAPI()
        api.webhooks["w1"] = {"id": "w1", "name": "krateo-autopilot", "service": "generic",
                              "url": "http://x/****",
                              "body": '{"alertName":"{{title}}","state":"ALERT","source":"hyperdx-alert"}'}
        hdx = _client(api)
        self.assertEqual(hdx.ensure_webhook("krateo-autopilot", "http://x/webhook"), ("w1", False))
        self.assertEqual(api.webhooks["w1"]["body"], hyperdx_v2.DEFAULT_WEBHOOK_BODY)
        self.assertEqual(api.writes, [("PUT", "webhooks", "w1")])
        api.writes.clear()
        self.assertEqual(hdx.ensure_webhook("krateo-autopilot", "http://x/webhook"), ("w1", False))
        self.assertEqual(api.writes, [])

    def test_a_missing_webhook_is_created(self):
        api = FakeHyperDXAPI()
        wid, created = _client(api).ensure_webhook("krateo-autopilot", "http://x/webhook")
        self.assertTrue(created)
        self.assertEqual(api.webhooks[wid]["body"], hyperdx_v2.DEFAULT_WEBHOOK_BODY)


class TestWebhookMapsToItsOwnCR(unittest.TestCase):
    """Bugs #3 and #4: a substring match tied a webhook to the wrong Alert, and same-named Alerts
    shared one record. Each Alert now labels its own incidents."""

    def setUp(self):
        self.k8s = FakeK8s([
            _alert_cr(K, "where-k", status={"hyperdxAlertId": "hk"}),
            _alert_cr(S, "where-s", status={"hyperdxAlertId": "hs"}),
            _alert_cr("krateo-platform-crashloop", "where-platform",
                      display="Krateo — platform pod crash-looping"),
            _alert_cr("sre-pod-crashloop", "where-cluster", display="Pod crash-looping"),
        ])
        self.prompts = []
        self._orig = (handler._k8s, handler.a2a_analyze)
        handler._k8s = self.k8s
        handler.a2a_analyze = lambda prompt, ctx=None: (self.prompts.append((prompt, ctx)) or
                                                        ("analysis", []))

    def tearDown(self):
        handler._k8s, handler.a2a_analyze = self._orig

    def incidents(self):
        return {o["spec"]["alertRef"]["name"]: o for o in self.k8s.incidents.values()}

    def test_two_same_displayName_alerts_get_two_incidents(self):
        handler.process({"alertName": f"🚨 {K}", "state": "ALERT"})
        handler.process({"alertName": f"🚨 {S}", "state": "ALERT"})
        got = self.incidents()
        self.assertEqual(sorted(got), [K, S])
        for name, where in ((K, "where-k"), (S, "where-s")):
            self.assertEqual(got[name]["metadata"]["labels"],
                             {"observability.krateo.io/alert": name})
            self.assertIn(f"`{where}`", got[name]["spec"]["prompt"])
            self.assertIn(DISPLAY, got[name]["spec"]["prompt"])
        self.assertNotEqual(self.prompts[0][1], self.prompts[1][1])  # two kagent threads

    def test_a_title_maps_to_the_exact_alert_not_one_containing_it(self):
        handler.process({"alertName": "🚨 sre-pod-crashloop", "state": "ALERT"})
        got = self.incidents()
        self.assertEqual(sorted(got), ["sre-pod-crashloop"])
        self.assertIn("`where-cluster`", got["sre-pod-crashloop"]["spec"]["prompt"])

    def test_a_title_naming_no_alert_runs_no_rca(self):
        """A displayName title (a HyperDX alert not yet renamed) names no CR: no scope, no RCA."""
        handler.process({"alertName": "🚨 Pod crash-looping", "state": "ALERT"})
        self.assertEqual(self.k8s.incidents, {})
        self.assertEqual(self.prompts, [])

    def test_a_resolve_notification_runs_no_rca(self):
        handler.process({"alertName": f"✅ {K}", "state": "OK"})
        self.assertEqual(self.k8s.incidents, {})
        self.assertEqual(self.k8s.calls, [])


if __name__ == "__main__":
    unittest.main()
