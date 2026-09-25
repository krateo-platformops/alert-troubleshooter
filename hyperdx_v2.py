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

# A generic webhook body can use only {{title}}, {{body}}, {{link}}, {{state}}, {{startTime}},
# {{endTime}} and {{eventId}} (a hash); there is no alert id. {{title}} is a state emoji plus the
# HyperDX alert name, and the reconciler names each HyperDX alert after its Alert CR's
# metadata.name, so the title identifies the CR. {{state}} is ALERT on a firing and OK on a resolve.
DEFAULT_WEBHOOK_BODY = '{"alertName":"{{title}}","state":"{{state}}","source":"hyperdx-alert"}'


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
        """Ensure a generic webhook named `name` exists and sends `body`.

        Returns (webhookId, created). An existing webhook whose body differs is updated in place
        (PUT keeps its id, so its alerts keep notifying it). `created` is True only for a new
        webhook, whose alerts must be re-pointed. The URL is not compared: the API redacts it.
        """
        body = body or DEFAULT_WEBHOOK_BODY
        doc = {"name": name, "service": service, "url": target_url,
               "description": description or name, "body": body}
        for w in (self._req("GET", "/api/v2/webhooks") or []):
            if w.get("name") == name:
                if w.get("body") != body:
                    self._req("PUT", f"/api/v2/webhooks/{w['id']}", doc)
                return w["id"], False
        created = self._req("POST", "/api/v2/webhooks", doc)
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

    def _tile_for(self, name, source, where, tile_id="count"):
        """The single count-over-time tile, as both create and update send it.

        A create mints a new tile id whatever is sent. An update must send the live tile's id: an
        unknown id makes HyperDX mint a new tile, drop the old one and delete every alert on it.
        """
        return {
            "id": tile_id, "x": 0, "y": 0, "w": 6, "h": 3,
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

    def update_dashboard_tile(self, dashboard_id, name, source, where="", tile_id="count"):
        """PUT the dashboard so its tile `tile_id` evaluates `where`.

        `tile_id` is the id the alert evaluates. The API keeps a tile id only if it already exists
        (`convertExternalTilesToInternal(tiles, existingTileIds)`); any other id replaces the tile,
        and `cleanupDashboardAlerts` then deletes the alerts on the replaced one.
        """
        self._req("PUT", f"/api/v2/dashboards/{dashboard_id}",
                  {"name": name, "tags": [], "tiles": [self._tile_for(name, source, where, tile_id)]})
        self.invalidate_cache()

    def ensure_dashboard_tile(self, name, source, where="", taken=()):
        """Ensure a single-tile dashboard `name` with a count-over-time line chart.

        `source` is the first_source() dict. A dashboard whose tile id is in `taken` (a tile another
        alert already evaluates) is not reused, so two alerts never share one `where`.
        Returns (dashboardId, tileId).

        THE TILE IS BUILT BY `_tile_for`, not inline. It was inline, and when the lookup above was
        switched to the cached `list_dashboards()` the local `source_id` it depended on went with
        the old loop — leaving a dangling reference that only fires on the CREATE path, because the
        lookup returns first for every alert whose dashboard already exists. It reached a cluster as
        `name 'source_id' is not defined` on the one alert that needed a new dashboard.

        One builder for create and update is what makes that unrepresentable: there is no second
        copy to leave behind.
        """
        for d in self.list_dashboards():
            if d.get("name") == name and d.get("tiles") and d["tiles"][0]["id"] not in taken:
                return d["id"], d["tiles"][0]["id"]
        d = self._req("POST", "/api/v2/dashboards",
                      {"name": name, "tags": [], "tiles": [self._tile_for(name, source, where)]})
        self.invalidate_cache()
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

    def alert_drift(self, live, name=None, **desired):
        """Which of MUTABLE (and `name`, when given) differ between the live alert and the CR.

        Returns a dict of field -> (live, desired); empty means in agreement. Compared as STRINGS:
        the API returns `threshold` as a number and a CR may carry it as either, and `1 != "1"` is
        a drift that would be "corrected" on every single cycle, rewriting the alert forever.
        `name` also seeds the default message, so an empty `message` compares equal to the live one.
        """
        body = self._alert_body(name or "", "", "", "",
                                **{k: v for k, v in desired.items() if k in
                                   ("interval", "threshold", "threshold_type", "message")})
        out = {}
        for field in self.MUTABLE + (("name",) if name is not None else ()):
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

        `name` is the Alert CR's metadata.name, unique per namespace, so one name is one CR.
        An existing alert is brought to the spec rather than returned untouched: clearing
        `status.hyperdxAlertId` is the operator's way to force a re-sync through this path.

        Returns {id, state}.
        """
        fields = {"interval": interval, "threshold": threshold,
                  "threshold_type": threshold_type, "message": message}
        for a in self.list_alerts():
            if a.get("name") == name:
                drift = self.alert_drift(a, name=name, **fields)
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
