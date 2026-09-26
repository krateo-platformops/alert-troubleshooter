"""The Incident writer, Policy A: one open incident per alert, one RCA per incident.

Every apiserver call goes to tests/fake_k8s.FakeK8s, which keeps the Incident CRD's rules the
writer depends on; the RCA is a stub returning a canned answer.
"""
import json
import os
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import handler  # noqa: E402
from fake_k8s import FakeK8s, http_error  # noqa: E402

NS, ALERT = "krateo-system", "cd-not-ready"
HOW = {"precondition": "#!/usr/bin/env bash\n# holds while fireworksapp is not Ready\nexit 1\n",
       "apply": "#!/usr/bin/env bash\n# pin chart 1.1.10\ntrue\n",
       "verify": "#!/usr/bin/env bash\n# fixed once Ready\nexit 0\n"}
BLOCK = {"sources": [{"type": "object", "ref": "cd/fireworksapp", "excerpt": "Ready=False"}],
         "rootCause": {"statement": "chart 1.1.9 is missing", "confidence": 0.8, "category": "config"},
         "howToFix": HOW}


def answer(block=BLOCK, prose="## Root cause\nchart 1.1.9 is missing"):
    return f"{prose}\n\n```json\n{json.dumps(block)}\n```"


def alert_cr(name=ALERT, where="Body LIKE '%x%'", interval=None):
    spec = {"displayName": "CompositionDefinition not ready", "where": where}
    if interval:
        spec["interval"] = interval
    return {"metadata": {"name": name, "namespace": NS}, "spec": spec, "status": {"state": "ALERT"}}


def ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class WriterCase(unittest.TestCase):
    def setUp(self):
        self.k8s = FakeK8s([alert_cr()])
        self.rca, self.answer, self.during_rca = [], answer(), None
        self._orig = (handler._k8s, handler.a2a_analyze)
        handler._k8s = self.k8s
        handler.a2a_analyze = self._a2a

    def tearDown(self):
        handler._k8s, handler.a2a_analyze = self._orig

    def _a2a(self, prompt, context_id=None):
        self.rca.append((prompt, context_id))
        if self.during_rca:
            self.during_rca()
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer, []

    def fire(self, alert=ALERT, ns=NS, **kw):
        handler.analyze("CompositionDefinition not ready", "ALERT", alert, ns, **kw)

    def status_writes(self):
        return [body["status"] for method, _, sub, body in self.k8s.calls
                if method == "PATCH" and sub == "status"]


class TestOpening(WriterCase):
    def test_the_name_is_the_alert_and_the_opening_second(self):
        at = datetime(2026, 9, 25, 14, 0, 5, tzinfo=timezone.utc)
        self.assertEqual(handler.incident_name(ALERT, at), "cd-not-ready-20260925-140005")

    def test_a_first_firing_opens_an_incident_and_writes_its_analysis(self):
        self.fire(where="Body LIKE '%x%'")
        inc = self.k8s.only()
        name = inc["metadata"]["name"]
        self.assertRegex(name, r"^cd-not-ready-\d{8}-\d{6}$")
        self.assertEqual(inc["metadata"]["labels"], {"observability.krateo.io/alert": ALERT})
        spec = inc["spec"]
        self.assertEqual(spec["alertRef"], {"name": ALERT, "namespace": NS})
        self.assertEqual(spec["trigger"], "alert")
        self.assertIn("`Body LIKE '%x%'`", spec["prompt"])
        st = inc["status"]
        self.assertEqual((st["state"], st["firings"], st["lastFiredAt"]),
                         ("Open", 1, spec["triggeredAt"]))
        self.assertEqual(st["howToFix"], HOW)
        self.assertEqual(st["rootCause"]["statement"], "chart 1.1.9 is missing")
        self.assertTrue(st["report"].startswith("## Root cause"))
        self.assertIn("completedAt", st)
        self.assertNotIn("error", st)
        self.assertNotIn("evidence", st)       # the Incident CRD has no such field
        self.assertEqual(self.rca, [(spec["prompt"], str(uuid.uuid5(uuid.NAMESPACE_DNS, name)))])

    def test_it_is_Analyzing_until_the_analysis_is_written(self):
        self.fire()
        first, last = self.status_writes()[0], self.status_writes()[-1]
        self.assertEqual((first["state"], first["firings"]), ("Analyzing", 1))
        self.assertEqual(last["state"], "Open")
        self.assertIn("howToFix", last)        # Open and its scripts land in one write

    def test_the_incident_lives_in_the_alerts_namespace(self):
        self.fire(ns="team-a")
        self.assertEqual(self.k8s.only("team-a")["spec"]["alertRef"]["namespace"], "team-a")

    def test_an_alert_name_over_63_characters_opens_nothing(self):
        self.fire(alert="a" * 64)
        self.assertEqual((self.k8s.incidents, self.rca), ({}, []))

    def test_the_webhook_takes_the_same_path(self):
        handler.process({"alertName": f"🚨 {ALERT}", "state": "ALERT"})
        inc = self.k8s.only()
        self.assertEqual(inc["status"]["state"], "Open")
        self.assertIn("CompositionDefinition not ready", inc["spec"]["prompt"])


class TestPolicyA(WriterCase):
    def test_a_firing_on_an_open_incident_is_counted_and_runs_no_RCA(self):
        for state in ("Analyzing", "Open", "Verifying"):
            with self.subTest(state=state):
                self.k8s.incidents.clear()
                self.k8s.put(NS, f"{ALERT}-x", ALERT, state=state, firings=3)
                self.fire()
                inc = self.k8s.only()
                self.assertEqual(inc["status"]["firings"], 4)
                self.assertIn("lastFiredAt", inc["status"])
                self.assertEqual(inc["status"]["state"], state)
        self.assertEqual(self.rca, [])

    def test_an_ended_incident_does_not_count_a_new_firing_opens_one(self):
        self.k8s.put(NS, f"{ALERT}-resolved", ALERT, state="Resolved")
        self.k8s.write_status(NS, f"{ALERT}-resolved",
                              {"resolution": {"by": "verify", "at": ago(3600)}})  # past the window
        self.k8s.put(NS, f"{ALERT}-closed", ALERT, state="Closed")
        self.fire()
        self.assertEqual(len(self.k8s.incidents), 3)
        self.assertEqual(len(self.rca), 1)
        self.assertEqual(self.k8s.incidents[(NS, f"{ALERT}-resolved")]["status"]["firings"], 1)

    def test_another_alerts_incident_is_not_counted(self):
        self.k8s.put(NS, "other-x", "other", state="Open")
        self.fire()
        self.assertEqual(len(self.k8s.incidents), 2)

    def test_the_newest_open_incident_counts(self):
        self.k8s.put(NS, f"{ALERT}-old", ALERT, state="Open", created="2026-09-25T09:00:00Z")
        self.k8s.put(NS, f"{ALERT}-new", ALERT, state="Open", created="2026-09-25T10:00:00Z")
        self.fire()
        self.assertEqual(self.k8s.incidents[(NS, f"{ALERT}-new")]["status"]["firings"], 2)
        self.assertEqual(self.k8s.incidents[(NS, f"{ALERT}-old")]["status"]["firings"], 1)

    def test_a_concurrent_status_write_is_not_lost(self):
        """The controller appends a check between our read and our write: the conditioned
        patch is refused, re-read and retried, and both writes survive."""
        self.k8s.put(NS, f"{ALERT}-x", ALERT, state="Open", firings=2)
        hits = []

        def controller(ns, name):
            if not hits:
                hits.append(name)
                self.k8s.write_status(ns, name, {"checks": [{"script": "precondition", "exit": 1}]})
        self.k8s.before_patch = controller
        self.fire()
        st = self.k8s.only()["status"]
        self.assertEqual(st["firings"], 3)
        self.assertEqual(st["checks"], [{"script": "precondition", "exit": 1}])

    def test_a_concurrent_create_is_counted_on(self):
        """Another firing created the same-second incident first: this one counts on it."""
        def other_firing(ns, name):
            if (ns, name) not in self.k8s.incidents:
                self.k8s.put(ns, name, ALERT, state="Analyzing")
        self.k8s.before_create = other_firing
        self.fire()
        self.assertEqual(self.k8s.only()["status"]["firings"], 2)
        self.assertEqual(self.rca, [])

    def test_an_apiserver_failure_loses_the_firing_not_the_caller(self):
        def down(*a, **k):
            raise http_error(500)
        handler._k8s = down
        self.fire()                            # does not raise
        self.assertEqual(self.rca, [])


class TestResolvedGrace(WriterCase):
    """For one spec.interval after the alert's latest incident is Resolved, a firing counts on it:
    a `where` alert keeps counting pre-fix rows for its lookback window."""

    def ended(self, name, state, seconds_ago, created="2026-09-25T10:00:00Z"):
        self.k8s.put(NS, name, ALERT, state=state, created=created)
        self.k8s.write_status(NS, name, {"resolution": {"by": "verify" if state == "Resolved"
                                                        else "user", "at": ago(seconds_ago)}})

    def test_inside_the_window_the_firing_counts_on_the_Resolved_incident(self):
        self.ended(f"{ALERT}-r", "Resolved", 60)
        self.fire()
        st = self.k8s.only()["status"]
        self.assertEqual((st["state"], st["firings"]), ("Resolved", 2))
        self.assertIn("lastFiredAt", st)
        self.assertEqual(self.rca, [])

    def test_outside_the_window_a_new_incident_opens(self):
        self.ended(f"{ALERT}-r", "Resolved", 600)
        self.fire()
        self.assertEqual(len(self.k8s.incidents), 2)
        self.assertEqual(len(self.rca), 1)

    def test_a_Closed_incident_never_takes_a_firing(self):
        self.ended(f"{ALERT}-c", "Closed", 10)
        self.fire()
        self.assertEqual(len(self.k8s.incidents), 2)
        self.assertEqual(self.k8s.incidents[(NS, f"{ALERT}-c")]["status"]["firings"], 1)

    def test_only_the_latest_incident_gives_grace(self):
        """An older Resolved one inside the window does not count once a later one was Closed."""
        self.ended(f"{ALERT}-r", "Resolved", 30, created="2026-09-25T09:00:00Z")
        self.ended(f"{ALERT}-c", "Closed", 10, created="2026-09-25T10:00:00Z")
        self.fire()
        self.assertEqual(len(self.k8s.incidents), 3)

    def test_an_open_incident_takes_precedence(self):
        self.k8s.put(NS, f"{ALERT}-o", ALERT, state="Open", created="2026-09-25T09:00:00Z")
        self.ended(f"{ALERT}-r", "Resolved", 10, created="2026-09-25T10:00:00Z")
        self.fire()
        self.assertEqual(self.k8s.incidents[(NS, f"{ALERT}-o")]["status"]["firings"], 2)
        self.assertEqual(self.k8s.incidents[(NS, f"{ALERT}-r")]["status"]["firings"], 1)

    def test_the_window_is_the_alerts_interval(self):
        for interval, seconds_ago, counted in (("15m", 600, True), ("1m", 90, False),
                                               (None, 200, True), ("weird", 400, False)):
            with self.subTest(interval=interval, seconds_ago=seconds_ago):
                self.k8s.incidents.clear()
                self.ended(f"{ALERT}-r", "Resolved", seconds_ago)
                self.fire(interval=interval)
                self.assertEqual(len(self.k8s.incidents) == 1, counted)

    def test_the_boundary_is_exclusive(self):
        resolved = datetime(2026, 9, 25, 14, 0, 0, tzinfo=timezone.utc)
        inc = {"metadata": {"name": "r", "creationTimestamp": "t"},
               "status": {"state": "Resolved", "resolution": {"by": "verify",
                                                              "at": "2026-09-25T14:00:00Z"}}}
        pick = handler.incident_to_count
        self.assertIs(pick([inc], resolved + timedelta(seconds=299), 300), inc)
        self.assertIsNone(pick([inc], resolved + timedelta(seconds=300), 300))
        for broken in ({}, {"by": "verify"}, {"by": "verify", "at": "not a time"}):
            inc["status"]["resolution"] = broken
            self.assertIsNone(pick([inc], resolved, 300))

    def test_a_concurrent_write_on_the_Resolved_incident_is_retried(self):
        self.ended(f"{ALERT}-r", "Resolved", 60)
        hits = []

        def controller(ns, name):
            if not hits:
                hits.append(name)
                self.k8s.write_status(ns, name, {"conditions": [{"type": "Ready"}]})
        self.k8s.before_patch = controller
        self.fire()
        self.assertEqual(self.k8s.only()["status"]["firings"], 2)

    def test_the_webhook_passes_the_alerts_interval(self):
        self.k8s.alerts[ALERT] = alert_cr(interval="15m")
        self.ended(f"{ALERT}-r", "Resolved", 600)
        handler.process({"alertName": f"🚨 {ALERT}", "state": "ALERT"})
        self.assertEqual(self.k8s.only()["status"]["firings"], 2)


class TestAnalysisOutcome(WriterCase):
    def test_a_failed_RCA_opens_the_incident_with_the_error(self):
        self.answer = RuntimeError("A2A timed out")
        self.fire()
        st = self.k8s.only()["status"]
        self.assertEqual(st["state"], "Open")
        self.assertIn("A2A timed out", st["error"])
        self.assertNotIn("howToFix", st)
        self.assertNotIn("report", st)

    def test_an_empty_answer_is_an_error(self):
        self.answer = ""
        self.fire()
        st = self.k8s.only()["status"]
        self.assertEqual((st["state"], st["error"]), ("Open", "The analysis returned no output."))

    def test_an_unstructured_answer_keeps_its_prose(self):
        self.answer = "The composition is broken."
        self.fire()
        st = self.k8s.only()["status"]
        self.assertEqual(st["report"], "The composition is broken.")
        self.assertIn("no structured block", st["error"])
        self.assertNotIn("howToFix", st)

    def test_an_unusable_how_to_fix_is_an_error_and_a_gap(self):
        self.answer = answer(dict(BLOCK, howToFix={"precondition": HOW["precondition"]}))
        self.fire()
        st = self.k8s.only()["status"]
        self.assertIn("no usable howToFix", st["error"])
        self.assertTrue(st["missingContext"][-1].startswith("No usable howToFix (apply missing"))

    def test_closed_while_analyzing_stays_Closed_and_gets_the_analysis(self):
        def human_closes():
            inc = self.k8s.only()
            self.k8s.write_status(NS, inc["metadata"]["name"],
                                  {"state": "Closed", "resolution": {"by": "user", "at": "t"}})
        self.during_rca = human_closes
        self.fire()
        st = self.k8s.only()["status"]
        self.assertEqual(st["state"], "Closed")
        self.assertEqual(st["howToFix"], HOW)

    def test_closed_between_the_reread_and_the_write(self):
        """The conditioned write is refused, and the re-read sees Closed."""
        patches = []

        def human_closes(ns, name):
            patches.append(name)
            if len(patches) == 2:              # the analysis write, after the Analyzing one
                self.k8s.write_status(ns, name, {"state": "Closed"})
        self.k8s.before_patch = human_closes
        self.fire()
        st = self.k8s.only()["status"]
        self.assertEqual(st["state"], "Closed")
        self.assertEqual(st["howToFix"], HOW)
        self.assertNotIn("state", self.status_writes()[-1])

    def test_an_incident_a_human_moved_on_keeps_its_state(self):
        """Opened by the restart sweep and then applied: the late analysis does not reset it."""
        def moved_on():
            inc = self.k8s.only()
            self.k8s.write_status(NS, inc["metadata"]["name"], {"state": "Verifying"})
        self.during_rca = moved_on
        self.fire()
        st = self.k8s.only()["status"]
        self.assertEqual(st["state"], "Verifying")
        self.assertEqual(st["howToFix"], HOW)


class TestRecoverInterrupted(WriterCase):
    def test_incidents_a_restart_left_analyzing_are_opened_with_the_reason(self):
        self.k8s.put(NS, "a-1", "a", state="Analyzing")
        self.k8s.put(NS, "b-1", "b")                       # no state: its first write never landed
        self.k8s.put(NS, "c-1", "c", state="Open")
        handler.recover_interrupted(NS)
        got = {n: o["status"] for (_, n), o in self.k8s.incidents.items()}
        self.assertEqual((got["a-1"]["state"], got["a-1"]["error"]), ("Open", handler.INTERRUPTED))
        self.assertEqual(got["b-1"]["state"], "Open")
        self.assertNotIn("error", got["c-1"])

    def test_a_late_analysis_clears_the_interruption(self):
        def swept():
            handler.recover_interrupted(NS)
        self.during_rca = swept
        self.fire()
        st = self.k8s.only()["status"]
        self.assertEqual(st["state"], "Open")
        self.assertNotIn("error", st)
        self.assertEqual(st["howToFix"], HOW)

    def test_no_Incident_CRD_is_not_an_error(self):
        def missing(*a, **k):
            raise http_error(404)
        handler._k8s = missing
        handler.recover_interrupted(NS)                    # does not raise


if __name__ == "__main__":
    unittest.main()
