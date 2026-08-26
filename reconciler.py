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


def _reconcile_cr(hdx, cr, source, webhook_id):
    meta, spec, status = cr["metadata"], cr.get("spec", {}), cr.get("status", {})
    name = meta["name"]
    display = spec.get("displayName") or name
    hdx_id = status.get("hyperdxAlertId")

    live = {a["id"]: a for a in hdx.list_alerts()}
    if hdx_id and hdx_id in live:
        st = live[hdx_id].get("state", "OK")
        _patch_status(name, {"state": st, "phase": "Synced", "lastSyncedAt": _now()})
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
