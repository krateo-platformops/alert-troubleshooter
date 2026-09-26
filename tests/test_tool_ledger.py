"""The tool ledger is read off the A2A stream of BOTH kagent runtimes.

The fixtures are real incident-agent runs on kind-krateo, trimmed (long payloads cut, thought
signatures dropped from call ids):

  * a2a_task_python_runtime.json — the `tasks/get` response for the RCA of Incident
    e2e-web-image-pull-20260926-175000 (kagent 0.10.1, `runtime: python`);
  * a2a_task_go_runtime.json — the stored Task of a 2026-09-24 RCA on `runtime: go`.

Each history message is exactly one `status-update` event's `status.message` on the stream, which is
how _stream() replays them.

Run from the repo root:  python3 -m pytest -q tests
"""
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import handler  # noqa: E402
import report_v2  # noqa: E402


def _load(name):
    with open(os.path.join(HERE, "fixtures", name)) as f:
        doc = json.load(f)
    return doc.get("result", doc)


PYTHON_TASK = _load("a2a_task_python_runtime.json")
GO_TASK = _load("a2a_task_go_runtime.json")


def _parts(task):
    return [p for m in task["history"] for p in m.get("parts", [])]


def _typed(task, adk_type):
    """The parts whose metadata carries `adk_type` under either runtime's key."""
    return [p for p in _parts(task) if adk_type in (p.get("metadata") or {}).values()]


class _Stream:
    """requests.post(stream=True) replaying a task's history as status-update SSE events."""

    def __init__(self, task):
        self.lines = [
            "data: " + json.dumps({"jsonrpc": "2.0", "id": 1, "result": {
                "kind": "status-update", "taskId": task["id"], "contextId": task["contextId"],
                "final": False, "status": {"state": "working", "message": m}}})
            for m in task["history"]]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_lines(self, decode_unicode=True):
        return iter(self.lines)


def _analyze(task):
    with mock.patch.object(handler.requests, "post", return_value=_Stream(task)), \
         mock.patch.object(handler, "_service_jwt", return_value=""):
        return handler.a2a_analyze("prompt", "ctx")


class PythonRuntime(unittest.TestCase):

    def test_every_tool_result_is_captured(self):
        got = handler._tool_results(_parts(PYTHON_TASK))
        self.assertEqual(len(got), len(_typed(PYTHON_TASK, "function_response")))
        self.assertEqual([t["name"] for t in got],
                         ["run_query", "k8s_get_resources", "k8s_get_resources",
                          "k8s_describe_resource"])

    def test_payload_is_the_text_the_tool_returned(self):
        pods = handler._tool_results(_parts(PYTHON_TASK))[1]
        self.assertTrue(pods["payload"].startswith("NAME "), pods["payload"][:80])
        self.assertIn("web-7b679f69bb-4xfck   0/1     ErrImagePull", pods["payload"])

    def test_is_error_marks_the_call_failed(self):
        failed = [t for t in handler._tool_results(_parts(PYTHON_TASK)) if t["failed"]]
        self.assertEqual(len(failed), 1)
        self.assertIn("get composition --all-namespaces -o wide failed", failed[0]["payload"])

    def test_a2a_analyze_returns_the_answer_and_the_ledger(self):
        text, ledger = _analyze(PYTHON_TASK)
        self.assertIn("```json", text)
        self.assertEqual(len(ledger), 4)

    def test_reading_the_incident_prompt_is_not_a_denial(self):
        """The Incident the agent describes quotes the output contract — "Forbidden: cannot list
        pods", "what you could NOT see". That read succeeded; it must not cap the report."""
        text, ledger = _analyze(PYTHON_TASK)
        _, v2 = report_v2.parse_structured_report(text, ledger)
        retrieval = v2["evidence"]["retrieval"]
        self.assertNotIn("denied", [e["outcome"] for e in retrieval], retrieval)
        describe = [e for e in retrieval if e["scope"] == "k8s_describe_resource"]
        self.assertEqual([e["outcome"] for e in describe], ["success"])

    def test_the_failed_read_still_bounds_confidence(self):
        text, ledger = _analyze(PYTHON_TASK)
        _, v2 = report_v2.parse_structured_report(text, ledger)
        self.assertIn(("k8s", "k8s_get_resources", "errored"),
                      [(e["source"], e["scope"], e["outcome"]) for e in v2["evidence"]["retrieval"]])
        self.assertLessEqual(float(v2["rootCause"]["confidence"]),
                             report_v2.CONFIDENCE_CEILING["errored"])


class GoRuntime(unittest.TestCase):

    def test_every_tool_result_is_captured(self):
        got = handler._tool_results(_parts(GO_TASK))
        self.assertEqual(len(got), len(_typed(GO_TASK, "function_response")))
        self.assertEqual([t["name"] for t in got],
                         ["k8s_get_resources", "run_query", "k8s_get_resources", "run_query"])

    def test_output_is_unwrapped(self):
        got = handler._tool_results(_parts(GO_TASK))
        self.assertTrue(got[0]["payload"].startswith("NAMESPACE "), got[0]["payload"][:80])
        self.assertTrue(got[1]["payload"].startswith('{"columns"'), got[1]["payload"][:80])

    def test_error_marks_the_call_failed(self):
        failed = [t for t in handler._tool_results(_parts(GO_TASK)) if t["failed"]]
        self.assertEqual([t["name"] for t in failed], ["k8s_get_resources", "run_query"])
        self.assertIn("Tool execution failed", failed[0]["payload"])
        self.assertIn("ACCESS_DENIED", failed[1]["payload"])

    def test_a2a_analyze_returns_the_answer_and_the_ledger(self):
        text, ledger = _analyze(GO_TASK)
        self.assertTrue(text)
        self.assertEqual(len(ledger), 4)


class Shapes(unittest.TestCase):

    def test_function_calls_are_not_results(self):
        calls = _typed(PYTHON_TASK, "function_call") + _typed(GO_TASK, "function_call")
        self.assertTrue(calls)
        self.assertEqual(handler._tool_results(calls), [])

    def test_a_type_inside_data_is_not_a_result(self):
        """Neither runtime types a part in its data; only part metadata counts."""
        parts = [{"kind": "data", "data": {"adk_type": "function_response", "name": "x",
                                           "response": {"output": "y"}}}]
        self.assertEqual(handler._tool_results(parts), [])


if __name__ == "__main__":
    unittest.main()
