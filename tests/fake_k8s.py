"""An in-memory apiserver for handler._k8s: Alert GETs, and Incidents with the rules the writer
depends on. The Incident CRD is installed nowhere these tests run, so this fake stands in for it:

- a create of an existing name is 409 AlreadyExists, and a create ignores `status`;
- a patch whose body carries metadata.resourceVersion is conditioned on it (409 on a mismatch);
- a status patch merges `status` only (null deletes) and bumps the resourceVersion;
- the CRD's state rules: Resolved can only become Closed, and Closed is final (422).
"""
import copy
import types
from urllib.parse import unquote

import requests

ENDED = ("Resolved", "Closed")


def http_error(code):
    return requests.HTTPError(f"{code}", response=types.SimpleNamespace(status_code=code))


def _merge(into, patch):
    for k, v in patch.items():
        if v is None:
            into.pop(k, None)
        elif isinstance(v, dict) and isinstance(into.get(k), dict):
            _merge(into[k], v)
        else:
            into[k] = copy.deepcopy(v)


class FakeK8s:
    def __init__(self, alerts=()):
        self.alerts = {a["metadata"]["name"]: copy.deepcopy(a) for a in alerts}
        self.incidents = {}          # (namespace, name) -> object
        self.calls = []
        self.rv = 0
        self.before_patch = None     # hook(ns, name): a concurrent write just before a patch lands
        self.before_create = None    # hook(ns, name): a concurrent create just before ours lands

    def _bump(self, obj):
        self.rv += 1
        obj["metadata"]["resourceVersion"] = str(self.rv)

    def put(self, ns, name, alert, state=None, firings=1, created="2026-09-25T10:00:00Z"):
        """Seed an Incident as the apiserver would hold it."""
        obj = {"metadata": {"name": name, "namespace": ns, "creationTimestamp": created,
                            "labels": {"observability.krateo.io/alert": alert}},
               "spec": {"alertRef": {"name": alert, "namespace": ns}},
               "status": {"firings": firings} | ({"state": state} if state else {})}
        self._bump(obj)
        self.incidents[(ns, name)] = obj
        return obj

    def write_status(self, ns, name, status):
        """A write by another client (the controller, a human), bumping the resourceVersion."""
        obj = self.incidents[(ns, name)]
        _merge(obj.setdefault("status", {}), status)
        self._bump(obj)

    def __call__(self, method, path, body=None, subresource=""):
        self.calls.append((method, path, subresource, copy.deepcopy(body)))
        path, _, query = path.partition("?")
        parts = path.split("/")  # ['', 'apis', group, version, 'namespaces', ns, plural, name?]
        ns, plural = parts[5], parts[6]
        name = parts[7] if len(parts) > 7 else ""
        if plural == "alerts" and method == "GET":
            if name not in self.alerts:
                raise http_error(404)
            return copy.deepcopy(self.alerts[name])
        if plural != "incidents":
            raise AssertionError(f"unexpected {method} {path}")
        if method == "GET" and not name:
            selector = unquote(query.partition("labelSelector=")[2])
            key, _, value = selector.partition("=")
            items = [copy.deepcopy(o) for (n, _), o in self.incidents.items()
                     if n == ns and key in o["metadata"].get("labels", {})
                     and (not value or o["metadata"]["labels"][key] == value)]
            return {"items": items}
        if method == "GET":
            if (ns, name) not in self.incidents:
                raise http_error(404)
            return copy.deepcopy(self.incidents[(ns, name)])
        if method == "POST":
            name = body["metadata"]["name"]
            if self.before_create:
                self.before_create(ns, name)
            if (ns, name) in self.incidents:
                raise http_error(409)
            obj = copy.deepcopy(body)
            obj.pop("status", None)
            obj["metadata"]["creationTimestamp"] = f"2026-09-25T12:00:{len(self.incidents):02d}Z"
            self._bump(obj)
            self.incidents[(ns, name)] = obj
            return copy.deepcopy(obj)
        if method == "PATCH" and subresource == "status":
            if self.before_patch:
                self.before_patch(ns, name)
            obj = self.incidents[(ns, name)]
            want = (body.get("metadata") or {}).get("resourceVersion")
            if want is not None and want != obj["metadata"]["resourceVersion"]:
                raise http_error(409)
            old = (obj.get("status") or {}).get("state")
            new = (body.get("status") or {}).get("state", old)
            if old == "Closed" and new != "Closed" or old == "Resolved" and new not in ENDED:
                raise http_error(422)
            _merge(obj.setdefault("status", {}), body["status"])
            self._bump(obj)
            return copy.deepcopy(obj)
        raise AssertionError(f"unexpected {method} {path} {subresource}")

    def only(self, ns="krateo-system"):
        """The one Incident in `ns`."""
        found = [o for (n, _), o in self.incidents.items() if n == ns]
        assert len(found) == 1, found
        return found[0]
