#!/usr/bin/env python3
"""Bearer-auth reconciler for Alert CRs (alerts.observability.krateo.io).

Runs as a background thread in the krateo-alert-troubleshooter process:

  every RECONCILE_INTERVAL seconds:
    ensure the shared webhook (-> this troubleshooter's /webhook) ->
    for each Alert CR:
        being deleted (deletionTimestamp) -> delete its HyperDX alert+dashboard, drop the finalizer
        else, no status.hyperdxAlertId    -> create dashboard-tile + alert, record ids in status
        else                              -> mirror the live alert state (OK/ALERT/PENDING) to status
    (a finalizer on each CR guarantees the HyperDX resources are removed before the CR is deleted)

Alerts flow: HyperDX evaluates the alert; when it fires it POSTs the webhook -> this service's
/webhook -> Autopilot RCA -> TroubleshootingReport. The reconciler only manages config + status.

Auth: HYPERDX_ACCESS_KEY (user.accessKey from hyperdx-api-token Secret, written by the bootstrap
Job). Calls go to HYPERDX_API_URL (krateo-clickstack-api.krateo-system.svc:8000, port 8000 =
HyperDX Express backend) using Bearer auth against the /api/v2 external API.
"""
import json
import os
import time

import requests

import hyperdx_v2
from handler import _get_report, _k8s, _now, _stable_name, patch_status  # reuse the apiserver helpers

GROUP, VERSION, PLURAL = "observability.krateo.io", "v1alpha1", "alerts"
NAMESPACE = os.environ.get("NAMESPACE", "krateo-system")


def _reconcile_report_lifecycle(alert_name, state):
    """Advance the incident report's lifecycle from the triggering alert's live state:
      - a user-set spec.lifecycle is mirrored to status.lifecycle (the portal's manual Resolve), and
      - an alert that has returned to OK auto-resolves its still-open report (-> resolved).
    Level-based and idempotent — a no-op when there is no report for this alert or nothing changes.
    Never raises into the reconcile loop."""
    if not alert_name:
        return
    name = _stable_name(alert_name)
    try:
        rep = _get_report(NAMESPACE, name)
        if not rep:
            return
        spec_lc = ((rep.get("spec") or {}).get("lifecycle") or "").strip()
        cur_lc = ((rep.get("status") or {}).get("lifecycle") or "open")
        desired = spec_lc or ("resolved" if (state == "OK" and cur_lc == "open") else cur_lc)
        if desired and desired != cur_lc:
            patch_status(NAMESPACE, name, {"lifecycle": desired})
            print(f"[reconciler] report {name} lifecycle {cur_lc} -> {desired}", flush=True)
    except Exception as e:  # noqa: BLE001 — lifecycle bookkeeping must never break alert sync
        print(f"[reconciler] report {name} lifecycle reconcile skipped ({e})", flush=True)
INTERVAL = int(os.environ.get("RECONCILE_INTERVAL", "60"))
WEBHOOK_NAME = os.environ.get("WEBHOOK_NAME", "krateo-autopilot")
WEBHOOK_TARGET = os.environ.get(
    "WEBHOOK_TARGET_URL", "http://krateo-alert-troubleshooter.krateo-system.svc:8080/webhook")
# Default Alert CRs to seed on startup (JSON array of specs+name). The composition ships these here
# rather than as chart CRs — Helm can't validate a CR before its CRD is installed in the same pass.
DEFAULT_ALERTS_JSON = os.environ.get("DEFAULT_ALERTS_JSON", "")
# Added to each Alert CR so its HyperDX alert+dashboard are removed before the CR is deleted.
FINALIZER = "observability.krateo.io/hyperdx-cleanup"


def seed_default_alerts():
    """Create the default Alert CRs (from DEFAULT_ALERTS_JSON) if absent. Returns True once done (or
    nothing to seed); False if the Alert CRD isn't established yet, so the caller retries next cycle."""
    if not DEFAULT_ALERTS_JSON:
        return True
    try:
        defaults = json.loads(DEFAULT_ALERTS_JSON)
    except (ValueError, TypeError) as e:
        print(f"[reconciler] bad DEFAULT_ALERTS_JSON ({e}); skipping seed", flush=True)
        return True
    try:
        existing = {cr["metadata"]["name"] for cr in _list_alert_crs()}
    except Exception as e:  # noqa: BLE001 — CRD not ready yet
        print(f"[reconciler] seed: Alert CRD not ready ({str(e)[:80]}); retrying", flush=True)
        return False
    for a in defaults:
        name = a.get("name")
        if not name or name in existing:
            continue
        body = {"apiVersion": f"{GROUP}/{VERSION}", "kind": "Alert",
                "metadata": {"name": name, "namespace": NAMESPACE},
                "spec": {k: v for k, v in a.items() if k != "name"}}
        try:
            _k8s("POST", f"/apis/{GROUP}/{VERSION}/namespaces/{NAMESPACE}/{PLURAL}", body)
            print(f"[reconciler] seeded default Alert {name}", flush=True)
        except Exception as e:  # noqa: BLE001 — one bad seed shouldn't block the rest
            print(f"[reconciler] seed {name} failed: {str(e)[:120]}", flush=True)
    return True


def _list_alert_crs():
    return _k8s("GET", f"/apis/{GROUP}/{VERSION}/namespaces/{NAMESPACE}/{PLURAL}").get("items", [])


def _patch_status(name, status):
    _k8s("PATCH", f"/apis/{GROUP}/{VERSION}/namespaces/{NAMESPACE}/{PLURAL}/{name}",
         {"status": status}, subresource="status")


def _patch_finalizers(name, finalizers):
    _k8s("PATCH", f"/apis/{GROUP}/{VERSION}/namespaces/{NAMESPACE}/{PLURAL}/{name}",
         {"metadata": {"finalizers": finalizers}})


def _ensure_finalizer(cr):
    fins = cr["metadata"].get("finalizers") or []
    if FINALIZER not in fins:
        _patch_finalizers(cr["metadata"]["name"], fins + [FINALIZER])


def _finalize(hdx, cr):
    """The CR is being deleted (deletionTimestamp set): delete its HyperDX alert + dashboard, then
    drop our finalizer so Kubernetes can complete the deletion. Best-effort on the HyperDX side."""
    meta, status = cr["metadata"], cr.get("status", {})
    name = meta["name"]
    for delete_fn, key in ((hdx.delete_alert, "hyperdxAlertId"),
                           (hdx.delete_dashboard, "hyperdxDashboardId")):
        oid = status.get(key)
        if oid:
            try:
                delete_fn(oid)
            except Exception:  # noqa: BLE001 — still drop the finalizer so delete isn't wedged
                pass
    _patch_finalizers(name, [f for f in (meta.get("finalizers") or []) if f != FINALIZER])
    print(f"[reconciler] finalized Alert {name} (removed HyperDX alert+dashboard)", flush=True)


def _push_spec(hdx, cr, source, webhook_id, live_alert):
    """Push the CR's spec onto the live HyperDX alert when they disagree. Returns what changed.

    THE MISSING HALF OF THE RECONCILER (#38). Once a CR had `status.hyperdxAlertId`, this function
    did not exist: the loop mirrored state back from HyperDX and returned, so every edit after
    creation — threshold, thresholdType, where, interval, message — was silently ignored while the
    CR reported `phase: Synced`. The 0.1.18 catalogue that corrected 25 off-by-one thresholds was
    therefore completely inert on any cluster that already had them: correct on a fresh install,
    and nothing on an upgrade.

    TWO OBJECTS, BECAUSE THE SPEC SPANS TWO. `threshold`/`thresholdType`/`interval`/`message` live
    on the ALERT; `where` lives on the dashboard TILE. A push that only did the first would leave a
    corrected filter unapplied, which is the same silent failure one level down.
    """
    spec = cr.get("spec", {})
    status = cr.get("status", {})
    name, display = cr["metadata"]["name"], spec.get("displayName") or cr["metadata"]["name"]

    # BOTH IDS COME FROM THE LIVE ALERT, and mixing the two sources is a real 400 seen on
    # krateo-057: `sre-krateo-composition-reconcile-error` evaluates tile 6aa3f3fe…5666 on dashboard
    # 6aa3f3fe…5667, while its CR status named dashboard 6aae4005…f4e3 — a different one. Sending
    # that status dashboard id paired with the live tile id describes a tile that is not on that
    # dashboard, and `validateAlertInput` rejects the whole PUT.
    #
    # The silent half was worse than the 400. `where` lands on the dashboard TILE, so a push keyed
    # on the status id rewrote a tile the alert does not evaluate — a corrected filter written to
    # the wrong object, reported as success.
    #
    # The alert is authoritative about which tile it reads: it is the thing being evaluated. Status
    # is the fallback for a first push where the live alert carries neither (it always carries both
    # for a tile-source alert, but falling back keeps this total rather than raising on a shape we
    # have not seen).
    dash_id = live_alert.get("dashboardId") or status.get("hyperdxDashboardId")
    tile_id = live_alert.get("tileId")
    changed = []

    # `where` first: it decides WHAT is counted, so pushing a threshold against a stale filter
    # would briefly evaluate the new bound over the old query.
    want_where = spec.get("where", "")
    if dash_id:
        live_where = hdx.tile_where(dash_id)
        if live_where is not None and live_where != want_where:
            hdx.update_dashboard_tile(dash_id, f"krateo-alert-{name}", source, want_where)
            changed.append("where")

    drift = hdx.alert_drift(live_alert,
                            interval=spec.get("interval", "5m"),
                            threshold=spec.get("threshold", 1),
                            threshold_type=spec.get("thresholdType", "above"),
                            message=spec.get("message", ""))
    if drift:
        # Both ids from the pair resolved above, so the body always describes a tile that is
        # actually on the dashboard it names.
        hdx.update_alert(live_alert["id"], display, dash_id, tile_id or "count", webhook_id,
                         interval=spec.get("interval", "5m"),
                         threshold=spec.get("threshold", 1),
                         threshold_type=spec.get("thresholdType", "above"),
                         message=spec.get("message", ""))
        changed.extend(sorted(drift))
    return changed


def _reconcile_cr(hdx, cr, source, webhook_id):
    meta, spec, status = cr["metadata"], cr.get("spec", {}), cr.get("status", {})
    name = meta["name"]
    display = spec.get("displayName") or name
    hdx_id = status.get("hyperdxAlertId")

    live = {a["id"]: a for a in hdx.list_alerts()}
    if hdx_id and hdx_id in live:
        st = live[hdx_id].get("state", "OK")
        # PUSH BEFORE MIRRORING. The old order was "mirror and return", which is what made the CR
        # assert agreement it had never established.
        try:
            changed = _push_spec(hdx, cr, source, webhook_id, live[hdx_id])
        except Exception as e:  # noqa: BLE001 — a failed push must not stop state mirroring
            # AND MUST NOT CLAIM Synced. `phase` used to say Synced unconditionally here; saying it
            # while the spec sits unpushed is what cost an afternoon to find, because the status
            # actively asserted the opposite of the truth.
            _patch_status(name, {"state": st, "phase": "SpecDrift", "error": str(e)[:300],
                                 "lastSyncedAt": _now()})
            print(f"[reconciler] Alert {name}: spec push failed, phase=SpecDrift ({e})", flush=True)
            _reconcile_report_lifecycle(display, st)
            return
        _patch_status(name, {"state": st, "phase": "Synced", "lastSyncedAt": _now()})
        if changed:
            print(f"[reconciler] Alert {name}: pushed {', '.join(changed)} to hyperdx {hdx_id}", flush=True)
        _reconcile_report_lifecycle(display, st)
        return

    # (re)create: dashboard-tile then alert on it, both ensure-by-name (idempotent)
    dash_id, tile_id = hdx.ensure_dashboard_tile(f"krateo-alert-{name}", source, spec.get("where", ""))
    alert = hdx.ensure_alert(display, dash_id, tile_id, webhook_id,
                             interval=spec.get("interval", "5m"),
                             threshold=spec.get("threshold", 1),
                             threshold_type=spec.get("thresholdType", "above"),
                             message=spec.get("message", ""))
    st = alert.get("state", "OK")
    _patch_status(name, {"hyperdxAlertId": alert["id"], "hyperdxDashboardId": dash_id,
                         "state": st, "phase": "Synced", "lastSyncedAt": _now()})
    _reconcile_report_lifecycle(display, st)
    print(f"[reconciler] synced Alert {name} -> hyperdx {alert['id']} ({st})", flush=True)


def reconcile_once(hdx):
    # One pass = one view of the dashboards. Cleared here rather than aged, so a `where` comparison
    # can never read an answer from a previous cycle.
    hdx.invalidate_cache()
    source = hdx.first_source()
    webhook_id, recreated = hdx.ensure_webhook(WEBHOOK_NAME, WEBHOOK_TARGET,
                                               description="Krateo Autopilot auto-troubleshooter")
    if recreated:
        # the webhook id changed -> alerts referencing the old id would notify a dead channel.
        # Drop the HyperDX alerts we manage + reset their CR status so they rebuild on this webhook.
        active = [cr for cr in _list_alert_crs() if not cr["metadata"].get("deletionTimestamp")]
        managed = {cr.get("status", {}).get("hyperdxAlertId") for cr in active} - {None, ""}
        for a in hdx.list_alerts():
            if a["id"] in managed:
                try:
                    hdx.delete_alert(a["id"])
                except Exception:  # noqa: BLE001
                    pass
        for cr in active:
            _patch_status(cr["metadata"]["name"], {"hyperdxAlertId": None, "phase": "Pending"})
    for cr in _list_alert_crs():
        try:
            if cr["metadata"].get("deletionTimestamp"):
                _finalize(hdx, cr)      # CR is being deleted -> clean up HyperDX + drop finalizer
                continue
            _ensure_finalizer(cr)       # guard the CR so its HyperDX resources are cleaned on delete
            _reconcile_cr(hdx, cr, source, webhook_id)
        except requests.HTTPError:
            raise  # bubble 401/session issues to the loop for re-login
        except Exception as e:  # noqa: BLE001 — one bad CR shouldn't stall the rest
            name = cr.get("metadata", {}).get("name", "?")
            try:
                _patch_status(name, {"phase": "Error", "error": str(e)[:300], "lastSyncedAt": _now()})
            except Exception:  # noqa: BLE001
                pass
            print(f"[reconciler] Alert {name} error: {e}", flush=True)


def run_forever():
    api_url = os.environ.get("HYPERDX_API_URL",
                             "http://krateo-clickstack-api.krateo-system.svc:8000")
    access_key = os.environ.get("HYPERDX_ACCESS_KEY")
    if not access_key:
        print("[reconciler] HYPERDX_ACCESS_KEY unset — reconciler disabled", flush=True)
        return
    hdx = hyperdx_v2.HyperDXV2(api_url, access_key)
    print(f"[reconciler] started (interval={INTERVAL}s, api={api_url})", flush=True)
    seeded = False
    while True:
        try:
            if not seeded:
                seeded = seed_default_alerts()  # k8s-only; retries until the Alert CRD is ready
            reconcile_once(hdx)
        except requests.HTTPError as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            print(f"[reconciler] http error ({code}); will retry: {e}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[reconciler] cycle error: {e}", flush=True)
        time.sleep(INTERVAL)
