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
    then, when REPORT_RETENTION_DAYS > 0: delete CLOSED TroubleshootingReports past that age.
    Reports are deliberately NOT owned by their Alert — an incident outlives the alert that
    produced it, so retention is opt-in bookkeeping rather than Kubernetes garbage collection.

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
from datetime import datetime, timezone

from handler import (LAST_RUN_ANNO, PLURAL as REPORTS, _get_report, _k8s, _now, _stable_name,
                     patch_status)  # reuse the apiserver helpers

GROUP, VERSION, PLURAL = "observability.krateo.io", "v1alpha1", "alerts"
NAMESPACE = os.environ.get("NAMESPACE", "krateo-system")

# Days after which a CLOSED incident may be reaped. 0 (the default) = keep forever.
#
# OFF BY DEFAULT, DELIBERATELY. An incident report is an audit record: it is expected to outlive the
# Alert that produced it, so that "why did this fire in March" is still answerable after the alert
# has been renamed or retired. That is the product decision this knob is built around, and it is why
# there is no ownerReference on a report — Kubernetes would then delete the record with its Alert,
# which is exactly the behaviour we do NOT want (#35, #36).
#
# What it fixes is the other half of #35: nothing could reap a report even when an operator wanted
# to. Turning this on is an explicit choice to trade audit history for a bounded population; leaving
# it at 0 keeps today's behaviour byte for byte, so an upgrade never silently deletes anyone's
# records.
REPORT_RETENTION_DAYS = int(os.environ.get("REPORT_RETENTION_DAYS", "0"))


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
def _report_age_days(rep):
    """Days since this report last did anything — or None when no timestamp can be read.

    Newest wins: a report that completed, was re-run, or was merely created is aged from whichever
    of those happened last. None means "cannot tell", and an unreadable timestamp must never be
    treated as old — see the caller.
    """
    meta, status = rep.get("metadata") or {}, rep.get("status") or {}
    stamps = [status.get("completedAt"), (meta.get("annotations") or {}).get(LAST_RUN_ANNO),
              meta.get("creationTimestamp")]
    newest = None
    for ts in stamps:
        try:
            when = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
        except (ValueError, AttributeError, TypeError):
            continue
        if newest is None or when > newest:
            newest = when
    if newest is None:
        return None
    return (datetime.now(timezone.utc) - newest).total_seconds() / 86400.0


def _reapable(rep, live_alert_refs):
    """May this report be reaped? Three conditions, and every one of them protects a record.

    1. IT IS NOT OPEN. An open incident is live work — someone is looking at it, and the portal
       shows it in the incidents list. Only a report the loop has resolved (the alert returned to
       OK) or a person closed is a candidate. Phase matters too: a report still Analyzing has no
       useful timestamps and a half-written RCA is not an audit record.
    2. IT IS OLDER THAN THE WINDOW. Measured from the last thing that happened to it, so a report
       re-run yesterday is young however old its first run was. An age that cannot be READ is not
       an age of zero and not an age of infinity — it is a refusal, because deleting on a timestamp
       we failed to parse is how a retention knob eats the whole namespace.
    3. ITS ALERT IS GONE, or it is closed on its own terms. An orphan — a report whose Alert has
       been deleted — is the case #35 is actually about. It can never be resolved by the reconcile
       loop again, because that loop iterates Alert CRs and this one has none, so without this
       clause an orphan left `open` would be immortal even with retention switched on.
    """
    age = _report_age_days(rep)
    if age is None or age < REPORT_RETENTION_DAYS:
        return False
    status, spec = rep.get("status") or {}, rep.get("spec") or {}
    if (status.get("phase") or "") in ("Pending", "Analyzing"):
        return False
    orphaned = bool(spec.get("alertRef")) and spec["alertRef"] not in live_alert_refs
    return ((status.get("lifecycle") or "open") != "open") or orphaned


def reap_expired_reports():
    """Delete closed incidents past the retention window. A no-op unless REPORT_RETENTION_DAYS > 0.

    Level-based and idempotent, like everything else in this loop: it re-derives what is expired
    from the live objects every cycle and holds no state. Never raises into the caller — losing a
    reap cycle costs nothing, while breaking alert sync costs alerting.
    """
    if REPORT_RETENTION_DAYS <= 0:
        return 0
    try:
        reports = _k8s("GET", f"/apis/{GROUP}/{VERSION}/namespaces/{NAMESPACE}/{REPORTS}").get("items", [])
        # The live Alert slugs, so an orphan is identified by absence rather than guessed at. Read
        # ONCE per cycle: a per-report lookup would be N gets against the apiserver for no gain.
        live = {(cr.get("metadata") or {}).get("name", "") for cr in _list_alert_crs()}
    except Exception as e:  # noqa: BLE001 — a failed list must not stall alert reconciliation
        print(f"[reconciler] report reap skipped ({e})", flush=True)
        return 0
    reaped = 0
    for rep in reports:
        name = (rep.get("metadata") or {}).get("name", "")
        if not name or not _reapable(rep, live):
            continue
        try:
            _k8s("DELETE", f"/apis/{GROUP}/{VERSION}/namespaces/{NAMESPACE}/{REPORTS}/{name}")
            reaped += 1
            print(f"[reconciler] reaped report {name} (closed, older than {REPORT_RETENTION_DAYS}d)", flush=True)
        except Exception as e:  # noqa: BLE001 — one stubborn report shouldn't stop the rest
            print(f"[reconciler] report {name} reap failed: {e}", flush=True)
    return reaped


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
    retention = f"{REPORT_RETENTION_DAYS}d" if REPORT_RETENTION_DAYS > 0 else "off (reports kept)"
    print(f"[reconciler] started (interval={INTERVAL}s, api={api_url}, report-retention={retention})", flush=True)
    seeded = False
    while True:
        try:
            if not seeded:
                seeded = seed_default_alerts()  # k8s-only; retries until the Alert CRD is ready
            reconcile_once(hdx)
            # After the alerts, and outside their error path: reaping is bookkeeping, and a bad
            # cycle of it must not cost an alert sync.
            reap_expired_reports()
        except requests.HTTPError as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            print(f"[reconciler] http error ({code}); will retry: {e}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[reconciler] cycle error: {e}", flush=True)
        time.sleep(INTERVAL)
