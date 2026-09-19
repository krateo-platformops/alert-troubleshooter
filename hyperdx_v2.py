#!/usr/bin/env python3
"""HyperDX ≥2.28 external API v2 client (Bearer auth, port 8000).

Replaces the session-cookie hyperdx.py. All calls go directly to the Express
backend at port 8000 (krateo-clickstack-api.krateo-system.svc:8000) using the
user.accessKey from the hyperdx-api-token Secret (written by the bootstrap Job).

API shape notes:
  * All collection endpoints return {"data": [...], "meta": {...}}; _unwrap normalises.
  * Resources use "id" (not "_id") — consistent across alerts, webhooks, sources, dashboards.
  * Dashboard tile config v2 shape: {sourceId, select: [{aggFn, where}], displayType, ...}
    (NOT the legacy {source, select: "count()", whereLanguage, from, granularity, ...}).
"""
import requests

DEFAULT_WEBHOOK_BODY = '{"alertName":"{{title}}","state":"ALERT","source":"hyperdx-alert"}'


class HyperDXError(RuntimeError):
    pass


def _unwrap(x):
    if isinstance(x, dict) and "data" in x:
        return x["data"]
    return x


class HyperDXV2:
    def __init__(self, api_url, access_key, timeout=30):
        self.url = api_url.rstrip("/")  # http://krateo-clickstack-api.krateo-system.svc:8000
        self._headers = {
            "Authorization": f"Bearer {access_key}",
            "Content-Type": "application/json",
        }
        self.timeout = timeout
        # Per-cycle dashboard cache; the reconcile loop clears it each pass (invalidate_cache).
        self._dashboards = None

    def _req(self, method, path, body=None):
        r = requests.request(
            method,
            f"{self.url}{path}",
            headers=self._headers,
            json=body,
            timeout=self.timeout,
        )
        r.raise_for_status()
        return _unwrap(r.json()) if r.content else None

    # ---- domain primitives ----

    def first_source(self):
        srcs = self._req("GET", "/api/v2/sources") or []
        if not srcs:
            raise HyperDXError("no HyperDX sources configured")
        return srcs[0]

    def ensure_webhook(self, name, target_url, service="generic", description="", body=None):
        """Ensure a generic webhook named `name` exists with a body template.

        Returns (webhookId, recreated). A pre-existing body-less webhook is deleted and
        recreated so callers must re-point alerts when recreated is True.
        """
        body = body or DEFAULT_WEBHOOK_BODY
        for w in (self._req("GET", "/api/v2/webhooks") or []):
            if w.get("name") == name:
                if w.get("body"):
                    return w["id"], False
                self._req("DELETE", f"/api/v2/webhooks/{w['id']}")
                break
        created = self._req("POST", "/api/v2/webhooks",
                            {"name": name, "service": service, "url": target_url,
                             "description": description or name, "body": body})
        return created["id"], True

    def list_dashboards(self):
        """Dashboards, cached FOR ONE RECONCILE CYCLE (see invalidate_cache).

        The reconciler asks per Alert CR, and with 25 of them an uncached call is 25 identical
        round trips a minute to read a list that cannot have changed between them. The cache is
        cleared at the top of every cycle rather than aged, so it can never serve a stale answer
        across cycles — the failure that would matter, since a `where` comparison reads it.
        """
        if self._dashboards is None:
            self._dashboards = self._req("GET", "/api/v2/dashboards") or []
        return self._dashboards

    def invalidate_cache(self):
        """Drop the per-cycle cache. The reconcile loop calls this once, before each pass."""
        self._dashboards = None

    def _tile_for(self, name, source, where):
        """The single count-over-time tile, as both create and update send it."""
        return {
            "id": "count", "x": 0, "y": 0, "w": 6, "h": 3,
            "name": name,
            "config": {
                "displayType": "line",
                "sourceId": source["id"],
                "asRatio": False,
                "fillNulls": True,
                # whereLanguage:"sql" is load-bearing — see ensure_dashboard_tile.
                "select": [{"aggFn": "count", "where": where or "", "whereLanguage": "sql"}],
            },
        }

    def tile_where(self, dashboard_id):
        """The `where` the live tile is evaluating, or None if the dashboard is gone.

        This is where `spec.where` actually lands — it is a property of the dashboard TILE, not of
        the alert — which is why a CR's `where` edit needs its own comparison and its own push.
        """
        for d in self.list_dashboards():
            if d.get("id") == dashboard_id:
                tiles = d.get("tiles") or []
                if not tiles:
                    return None
                select = ((tiles[0].get("config") or {}).get("select") or [{}])[0]
                return select.get("where", "")
        return None

    def update_dashboard_tile(self, dashboard_id, name, source, where=""):
        """PUT the dashboard so its tile evaluates `where`.

        The tile keeps its id: the API resolves tiles against the existing ids
        (`convertExternalTilesToInternal(tiles, existingTileIds)`), so the alert's `tileId` still
        addresses this tile afterwards. Minting a new id here would leave the alert pointing at a
        tile that no longer exists, which fails silently — the alert simply never evaluates.
        """
        self._req("PUT", f"/api/v2/dashboards/{dashboard_id}",
                  {"name": name, "tags": [], "tiles": [self._tile_for(name, source, where)]})
        self.invalidate_cache()

    def ensure_dashboard_tile(self, name, source, where=""):
        """Ensure a single-tile dashboard `name` with a count-over-time line chart.

        `source` is the first_source() dict. Returns (dashboardId, tileId).
        The v2 tile config uses sourceId + select:[{aggFn, where}] (not the legacy
        {source, select:"count()", whereLanguage, from, granularity}).
        """
        for d in self.list_dashboards():
            if d.get("name") == name and d.get("tiles"):
                return d["id"], d["tiles"][0]["id"]
        tile = {
            "id": "count", "x": 0, "y": 0, "w": 6, "h": 3,
            "name": name,
            "config": {
                "displayType": "line",
                "sourceId": source_id,
                "asRatio": False,
                "fillNulls": True,
                # whereLanguage:"sql" is load-bearing. HyperDX's external API maps a tile series'
                # `whereLanguage` to the alert's `aggConditionLanguage`, DEFAULTING TO 'lucene' when
                # omitted (packages/api/src/utils/externalApi.ts: `aggConditionLanguage: s.whereLanguage ?? 'lucene'`).
                # Alert `spec.where` is ClickHouse SQL (ResourceAttributes[...], JSONExtractString(Body,...),
                # ServiceName NOT IN (...)). Without this pin the SQL is parsed as Lucene → it becomes a
                # full-text search for the words of the query itself (self-matching HyperDX's own echoed
                # query) and the alert fires on a phantom. Pin it to sql so the filter is evaluated as written.
                "select": [{"aggFn": "count", "where": where or "", "whereLanguage": "sql"}],
            },
        }
        d = self._req("POST", "/api/v2/dashboards", {"name": name, "tags": [], "tiles": [tile]})
        return d["id"], d["tiles"][0]["id"]

    def list_alerts(self):
        return self._req("GET", "/api/v2/alerts") or []

    # The alert fields a CR owns and may legitimately change after creation. `where` is NOT here:
    # it lives on the dashboard TILE, not the alert — see update_dashboard_tile.
    MUTABLE = ("interval", "threshold", "thresholdType", "message")

    def _alert_body(self, name, dashboard_id, tile_id, webhook_id,
                    interval="5m", threshold=1, threshold_type="above", message=""):
        """The alert document, as both POST / and PUT /:id take it.

        ONE builder for both verbs deliberately. PUT is a FULL REPLACE validated against the same
        `alertSchema` as POST (verified against the running build's
        routers/external-api/v2/alerts.js), so a separate update body is a second place for the two
        to drift — and the field that drifts silently here is the one that decides whether an alert
        can fire at all.
        """
        return {
            "name": name,
            "source": "tile",
            "dashboardId": dashboard_id,
            "tileId": tile_id,
            "interval": interval,
            "threshold": threshold,
            "thresholdType": threshold_type,
            "channel": {"type": "webhook", "webhookId": webhook_id},
            "message": message or f"{name} threshold crossed — incident-agent will auto-triage.",
        }

    def alert_drift(self, live, **desired):
        """Which of MUTABLE differ between the live alert and what the CR asks for.

        Returns a dict of field -> (live, desired); empty means in agreement. Compared as STRINGS:
        the API returns `threshold` as a number and a CR may carry it as either, and `1 != "1"` is
        a drift that would be "corrected" on every single cycle, rewriting the alert forever.
        """
        body = self._alert_body("", "", "", "", **{k: v for k, v in desired.items() if k in
                                                   ("interval", "threshold", "threshold_type", "message")})
        out = {}
        for field in self.MUTABLE:
            want = body[field]
            got = live.get(field)
            if str(got) != str(want):
                out[field] = (got, want)
        return out

    def update_alert(self, alert_id, name, dashboard_id, tile_id, webhook_id, **fields):
        """PUT the alert — the path that was missing, and why a CR edit never reached HyperDX."""
        a = self._req("PUT", f"/api/v2/alerts/{alert_id}",
                      self._alert_body(name, dashboard_id, tile_id, webhook_id, **fields))
        a = a.get("data", a) if isinstance(a, dict) else a
        return {"id": a.get("id", alert_id), "state": a.get("state", "OK")}

    def ensure_alert(self, name, dashboard_id, tile_id, webhook_id,
                     interval="5m", threshold=1, threshold_type="above", message=""):
        """Create the tile-based alert, or RECONCILE the one already carrying this name.

        It used to return an existing alert untouched, which made it ensure-EXISTS rather than
        ensure-AS-SPECIFIED. That is what defeated the obvious operator recovery: clearing
        `status.hyperdxAlertId` sent the CR back down this path, and it handed back the same stale
        alert — so there was no way to change a threshold from the Kubernetes side at all.

        Returns {id, state}.
        """
        fields = {"interval": interval, "threshold": threshold,
                  "threshold_type": threshold_type, "message": message}
        for a in self.list_alerts():
            if a.get("name") == name:
                drift = self.alert_drift(a, **fields)
                if not drift:
                    return {"id": a["id"], "state": a.get("state", "OK")}
                return self.update_alert(a["id"], name, dashboard_id, tile_id, webhook_id, **fields)
        a = self._req("POST", "/api/v2/alerts",
                      self._alert_body(name, dashboard_id, tile_id, webhook_id, **fields))
        return {"id": a["id"], "state": a.get("state", "OK")}

    def delete_alert(self, alert_id):
        self._req("DELETE", f"/api/v2/alerts/{alert_id}")

    def delete_dashboard(self, dashboard_id):
        self._req("DELETE", f"/api/v2/dashboards/{dashboard_id}")
