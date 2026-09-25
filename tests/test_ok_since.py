"""Alert.status.okSince: stamped when the alert turns OK, cleared while it is anything else."""
import importlib
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

T0 = "2026-09-25T10:31:00+00:00"


def _reconciler():
    r = importlib.reload(importlib.import_module("reconciler"))
    r._now = lambda: "NOW"
    r._reconcile_report_lifecycle = lambda *a, **k: None
    return r


class TestOkSinceRule(unittest.TestCase):
    def test_turning_OK_stamps_now(self):
        self.assertEqual(_reconciler()._ok_since({"state": "ALERT"}, "OK"), "NOW")

    def test_staying_OK_keeps_the_stamp(self):
        self.assertEqual(_reconciler()._ok_since({"state": "OK", "okSince": T0}, "OK"), T0)

    def test_OK_with_no_stamp_gets_now(self):
        """An alert that was OK before okSince existed, or a brand-new one."""
        r = _reconciler()
        self.assertEqual(r._ok_since({"state": "OK"}, "OK"), "NOW")
        self.assertEqual(r._ok_since({}, "OK"), "NOW")

    def test_any_other_state_clears_it(self):
        r = _reconciler()
        for state in ("ALERT", "PENDING", "DISABLED", "INSUFFICIENT_DATA"):
            self.assertIsNone(r._ok_since({"state": "OK", "okSince": T0}, state), state)

    def test_a_stamp_left_from_an_earlier_OK_is_not_reused(self):
        """OK(10:31) -> ALERT -> OK must read 'OK since the second OK', even if a stale stamp
        survived the ALERT (a patch that failed to land)."""
        self.assertEqual(_reconciler()._ok_since({"state": "ALERT", "okSince": T0}, "OK"), "NOW")


class TestOkSinceIsWrittenWithState(unittest.TestCase):
    """Every patch that writes `state` writes `okSince` next to it."""

    class Hdx:
        def __init__(self, state, fail_push=False):
            self.state, self.fail_push = state, fail_push

        def list_alerts(self):
            return [{"id": "h1", "state": self.state, "interval": "5m", "threshold": 1,
                     "thresholdType": "above", "message": "m", "tileId": "t1", "dashboardId": "d1"}]

        def tile_where(self, dash):
            return "w"

        def alert_drift(self, live, **desired):
            if self.fail_push:
                raise RuntimeError("push failed")
            return {}

        def ensure_dashboard_tile(self, name, source, where="", **kw):
            return "d2", "t2"

        def ensure_alert(self, name, dash, tile, hook, **fields):
            return {"id": "h2", "state": self.state}

    @staticmethod
    def _cr(status):
        return {"metadata": {"name": "a-1"},
                "spec": {"where": "w", "interval": "5m", "threshold": 1, "thresholdType": "above",
                         "message": "m"},
                "status": status}

    def _run(self, hdx, status):
        r = _reconciler()
        patched = []
        r._patch_status = lambda name, st: patched.append(st)
        r._reconcile_cr(hdx, self._cr(status), {"id": "s"}, "hook")
        return patched[-1]

    def test_synced_path(self):
        st = self._run(self.Hdx("OK"), {"hyperdxAlertId": "h1", "state": "ALERT", "okSince": None})
        self.assertEqual((st["state"], st["okSince"]), ("OK", "NOW"))
        st = self._run(self.Hdx("ALERT"), {"hyperdxAlertId": "h1", "state": "OK", "okSince": T0})
        self.assertIn("okSince", st)
        self.assertIsNone(st["okSince"])

    def test_spec_drift_path(self):
        st = self._run(self.Hdx("OK", fail_push=True),
                       {"hyperdxAlertId": "h1", "state": "OK", "okSince": T0})
        self.assertEqual((st["phase"], st["okSince"]), ("SpecDrift", T0))

    def test_create_path(self):
        st = self._run(self.Hdx("OK"), {})
        self.assertEqual((st["hyperdxAlertId"], st["okSince"]), ("h2", "NOW"))


if __name__ == "__main__":
    unittest.main()
