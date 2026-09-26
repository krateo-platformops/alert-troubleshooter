#!/usr/bin/env python3
"""Bearer-auth reconciler for Alert CRs (alerts.observability.krateo.io).

Runs as a background thread in the krateo-alert-troubleshooter process:

  every RECONCILE_INTERVAL seconds:
    for each apiRef Alert CR (spec.apiRef), when spec.interval has elapsed:
        resolve its RESTAction via snowplow, compare status.value with the threshold, write state;
        on ALERT record a firing through the webhook's path, handler.analyze (apiref.py)
    ensure the shared webhook (-> this troubleshooter's /webhook) ->
    for each `where` Alert CR:
        being deleted (deletionTimestamp) -> delete its HyperDX alert+dashboard, drop the finalizer
        else, no status.hyperdxAlertId    -> create dashboard-tile + alert, record ids in status
        else                              -> mirror the live alert state (OK/ALERT/PENDING) to status
    (a finalizer on each CR guarantees the HyperDX resources are removed before the CR is deleted)

Each HyperDX alert is named after its CR's metadata.name, and the webhook title carries that name
back to the handler, which looks the CR up by it.

Alerts flow: HyperDX evaluates the alert; when it fires it POSTs the webhook -> this service's
/webhook -> an Incident with an RCA (handler.analyze). The reconciler only manages config + status.

Auth: HYPERDX_ACCESS_KEY (user.accessKey from hyperdx-api-token Secret, written by the bootstrap
Job). Calls go to HYPERDX_API_URL (krateo-clickstack-api.krateo-system.svc:8000, port 8000 =
HyperDX Express backend) using Bearer auth against the /api/v2 external API.
"""
import json
import os
import threading
import time

import requests

import apiref
import handler
import hyperdx_v2
from handler import _k8s, _now  # reuse the apiserver helpers

GROUP, VERSION, PLURAL = "observability.krateo.io", "v1alpha1", "alerts"
NAMESPACE = os.environ.get("NAMESPACE", "krateo-system")
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


def tautology(threshold, threshold_type):
    """Why this alert can never change state — or None if it genuinely can.

    A COUNT IS NEVER NEGATIVE. Every alert here counts rows over a window, so the value compared is
    an integer >= 0, and some threshold/type pairs are decided before any data is read:

      above 0            value >= 0   — always true. Fires forever, on an empty cluster too.
      below 0            value <  0   — never true. Silently cannot fire, which is worse: it looks
                                        armed and is not.
      below_or_equal -1  value <= -1  — never true, same shape.

    This is not a heuristic about whether an alert is USEFUL. It is arithmetic: the comparison has
    one possible outcome, so the alert carries no information either way.

    IT HAS COST REAL MONEY TWICE. The 0.1.17 catalogue shipped 25 alerts at `above 0` and every one
    fired permanently, each firing launching an RCA (observability#60). And an Autopilot-authored
    alert on krateo-057 — `agent-guardrail-alert`, from the prompt "ok fix this" — reached run 72
    the same way. Nothing in either path refused it, because nothing was checking.

    Returns a human sentence for the status, since an operator reading `phase: Invalid` needs to
    know which field to change and to what.
    """
    try:
        value = float(threshold)
    except (TypeError, ValueError):
        return None  # not a number: the CRD's own typing owns that, not this
    kind = (threshold_type or "above").lower()
    if kind == "above" and value <= 0:
        return (f"thresholdType 'above' means value >= {threshold}, and a count is never negative — "
                "this fires on every evaluation including an empty result. Use 1 to mean 'at least one'.")
    if kind == "below" and value <= 0:
        return (f"thresholdType 'below' means value < {threshold}, which a count can never satisfy — "
                "this can never fire. Use 1 to mean 'none in this window'.")
    if kind == "below_or_equal" and value < 0:
        return (f"thresholdType 'below_or_equal' means value <= {threshold}, which a count can never "
                "satisfy — this can never fire. Use 0 to mean 'none in this window'.")
    return None


def _ok_since(status, state):
    """`status.okSince` for a CR whose live state is now `state`.

    The time the alert last turned OK, kept while it stays OK; an OK alert with none gets now.
    None in every other state, which a merge patch writes as a delete. Display-only: nothing
    reads it to close or resolve anything.
    """
    if state != "OK":
        return None
    if status.get("state") == "OK" and status.get("okSince"):
        return status["okSince"]
    return _now()


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

    The alert's name is pushed too: it is the CR's metadata.name, which the webhook title carries
    back to the handler. An alert still named after a displayName is renamed here.
    """
    spec = cr.get("spec", {})
    status = cr.get("status", {})
    name = cr["metadata"]["name"]

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
            hdx.update_dashboard_tile(dash_id, f"krateo-alert-{name}", source, want_where,
                                      tile_id or "count")
            changed.append("where")

    drift = hdx.alert_drift(live_alert, name=name,
                            interval=spec.get("interval", "5m"),
                            threshold=spec.get("threshold", 1),
                            threshold_type=spec.get("thresholdType", "above"),
                            message=spec.get("message", ""))
    if drift:
        # Both ids from the pair resolved above, so the body always describes a tile that is
        # actually on the dashboard it names.
        hdx.update_alert(live_alert["id"], name, dash_id, tile_id or "count", webhook_id,
                         interval=spec.get("interval", "5m"),
                         threshold=spec.get("threshold", 1),
                         threshold_type=spec.get("thresholdType", "above"),
                         message=spec.get("message", ""))
        changed.extend(sorted(drift))
    return changed


def _reconcile_cr(hdx, cr, source, webhook_id):
    meta, spec, status = cr["metadata"], cr.get("spec", {}), cr.get("status", {})
    name = meta["name"]
    hdx_id = status.get("hyperdxAlertId")

    # REFUSE BEFORE CREATING, and refuse an existing one too. A tautological threshold is not a
    # preference we are overriding: the comparison has one possible outcome, so the alert cannot
    # signal anything. Creating it anyway is how 25 alerts fired forever and an agent-authored one
    # reached run 72 — each firing launching an RCA.
    #
    # It does NOT delete an alert that already exists. Refusing to keep pushing is enough to stop
    # the damage, and tearing down an object a person may be looking at, on a rule we just added,
    # is a bigger action than this warrants. The phase says what to change.
    why = tautology(spec.get("threshold", 1), spec.get("thresholdType", "above"))
    if why:
        _patch_status(name, {"phase": "Invalid", "error": why, "lastSyncedAt": _now()})
        print(f"[reconciler] Alert {name} refused: {why}", flush=True)
        return

    live = {a["id"]: a for a in hdx.list_alerts()}
    # The alert the status names, else the one already named after this CR: a status that lost
    # its id adopts its own alert instead of creating a second one.
    alert = live.get(hdx_id) if hdx_id else None
    if alert is None:
        alert = next((a for a in live.values() if a.get("name") == name), None)
    if alert is not None:
        st = alert.get("state", "OK")
        # The ids come from the live alert, so the status names the dashboard it actually reads.
        ids = {"hyperdxAlertId": alert["id"]}
        if alert.get("dashboardId"):
            ids["hyperdxDashboardId"] = alert["dashboardId"]
        # PUSH BEFORE MIRRORING. The old order was "mirror and return", which is what made the CR
        # assert agreement it had never established.
        try:
            changed = _push_spec(hdx, cr, source, webhook_id, alert)
        except Exception as e:  # noqa: BLE001 — a failed push must not stop state mirroring
            # AND MUST NOT CLAIM Synced. `phase` used to say Synced unconditionally here; saying it
            # while the spec sits unpushed is what cost an afternoon to find, because the status
            # actively asserted the opposite of the truth.
            _patch_status(name, {**ids, "state": st, "okSince": _ok_since(status, st), "value": None,
                                 "phase": "SpecDrift", "error": str(e)[:300],
                                 "lastSyncedAt": _now()})
            print(f"[reconciler] Alert {name}: spec push failed, phase=SpecDrift ({e})", flush=True)
            return
        _patch_status(name, {**ids, "state": st, "okSince": _ok_since(status, st), "value": None,
                             "phase": "Synced", "lastSyncedAt": _now()})
        if changed:
            print(f"[reconciler] Alert {name}: pushed {', '.join(changed)} to hyperdx {alert['id']}", flush=True)
        return

    # create: dashboard-tile then an alert on it named after this CR. A tile another alert already
    # evaluates is never reused.
    taken = {a.get("tileId") for a in live.values()} - {None}
    dash_id, tile_id = hdx.ensure_dashboard_tile(f"krateo-alert-{name}", source,
                                                 spec.get("where", ""), taken=taken)
    alert = hdx.ensure_alert(name, dash_id, tile_id, webhook_id,
                             interval=spec.get("interval", "5m"),
                             threshold=spec.get("threshold", 1),
                             threshold_type=spec.get("thresholdType", "above"),
                             message=spec.get("message", ""))
    st = alert.get("state", "OK")
    _patch_status(name, {"hyperdxAlertId": alert["id"], "hyperdxDashboardId": dash_id,
                         "state": st, "okSince": _ok_since(status, st), "value": None,
                         "phase": "Synced", "lastSyncedAt": _now()})
    print(f"[reconciler] synced Alert {name} -> hyperdx {alert['id']} ({st})", flush=True)


def _release_shared(hdx, crs):
    """Leave each HyperDX alert claimed by several CRs to the CR it is named after, if any.

    The other claimants drop the id and create their own alert in this pass. A shared alert named
    after none of them is deleted, so it stops evaluating one CR's `where` for another.
    Mutates the claimants' in-memory status so the rest of the pass sees the release.
    """
    claims = {}
    for cr in crs:
        hdx_id = (cr.get("status") or {}).get("hyperdxAlertId")
        if hdx_id:
            claims.setdefault(hdx_id, []).append(cr)
    shared = {k: v for k, v in claims.items() if len(v) > 1}
    if not shared:
        return
    live = {a["id"]: a for a in hdx.list_alerts()}
    for hdx_id, group in shared.items():
        alert_name = (live.get(hdx_id) or {}).get("name")
        owner = next((cr for cr in group if cr["metadata"]["name"] == alert_name), None)
        if owner is None and hdx_id in live:
            hdx.delete_alert(hdx_id)
        for cr in group:
            if cr is owner:
                continue
            _patch_status(cr["metadata"]["name"], {"hyperdxAlertId": None, "phase": "Pending"})
            cr.setdefault("status", {})["hyperdxAlertId"] = None
            print(f"[reconciler] Alert {cr['metadata']['name']}: released shared hyperdx {hdx_id}",
                  flush=True)


def _api_ref(cr):
    return (cr.get("spec") or {}).get("apiRef")


def _start_analysis(**kwargs):
    """The webhook's firing path (handler.analyze), off the reconcile thread: a new incident's RCA
    takes minutes."""
    threading.Thread(target=handler.analyze, kwargs=kwargs, daemon=True).start()


def _reconcile_apiref(cr):
    """Evaluate an apiRef alert when it is due (every `spec.interval`).

    Snowplow resolves the RESTAction; its `value` is compared with the threshold. `state` and
    `okSince` are written like a HyperDX alert's, and an ALERT is a firing, recorded like a
    webhook's, with the RESTAction's `items` in the prompt of a new incident. No HyperDX object is involved, and the
    row-count tautology check does not apply: a RESTAction's value may be any number.
    """
    meta, spec, status = cr["metadata"], cr.get("spec", {}), cr.get("status", {})
    name = meta["name"]
    display = spec.get("displayName") or name
    threshold, kind = spec.get("threshold", 1), spec.get("thresholdType", "above")
    why = apiref.invalid(kind)
    if why:
        _patch_status(name, {"phase": "Invalid", "error": why, "lastSyncedAt": _now()})
        return
    if not apiref.due(status, spec.get("interval", "5m")):
        return
    ref = spec["apiRef"]
    try:
        out = apiref.resolve(ref)
        value = apiref.value_of(out)
    except apiref.ApiRefError as e:
        _patch_status(name, {"phase": "Error", "error": str(e)[:300], "lastSyncedAt": _now()})
        print(f"[reconciler] Alert {name}: apiRef {ref.get('namespace')}/{ref.get('name')} "
              f"failed ({e})", flush=True)
        return
    st = "ALERT" if apiref.exceeds(value, threshold, kind) else "OK"
    _patch_status(name, {"state": st, "okSince": _ok_since(status, st), "value": value,
                         "phase": "Synced", "error": None, "lastSyncedAt": _now()})
    if st == "ALERT":
        _start_analysis(alert_name=display, alert_state=st, alert_ref=name,
                        alert_namespace=meta.get("namespace") or NAMESPACE,
                        message=spec.get("message"), interval=spec.get("interval"),
                        api={"name": ref.get("name"), "namespace": ref.get("namespace"),
                             "value": value, "threshold": threshold, "thresholdType": kind,
                             "items": out.get("items")})


def _drop_hyperdx(hdx, cr):
    """An apiRef alert has no HyperDX objects. Ones left from when the CR used `where` are deleted,
    so they cannot fire a webhook for it."""
    status = cr.get("status") or {}
    ids = {k: status.get(k) for k in ("hyperdxAlertId", "hyperdxDashboardId") if status.get(k)}
    if not ids:
        return
    for delete_fn, key in ((hdx.delete_alert, "hyperdxAlertId"),
                           (hdx.delete_dashboard, "hyperdxDashboardId")):
        if ids.get(key):
            try:
                delete_fn(ids[key])
            except Exception:  # noqa: BLE001 — already gone is fine
                pass
    _patch_status(cr["metadata"]["name"], {k: None for k in ids})


def reconcile_once(hdx):
    crs = _list_alert_crs()
    # apiRef alerts first: HyperDX is not involved, so a HyperDX outage must not stop them.
    for cr in crs:
        if _api_ref(cr) and not cr["metadata"].get("deletionTimestamp"):
            try:
                _reconcile_apiref(cr)
            except Exception as e:  # noqa: BLE001 — one bad CR shouldn't stall the rest
                print(f"[reconciler] Alert {cr['metadata'].get('name', '?')} error: {e}", flush=True)
    # One pass = one view of the dashboards. Cleared here rather than aged, so a `where` comparison
    # can never read an answer from a previous cycle.
    hdx.invalidate_cache()
    source = hdx.first_source()
    webhook_id, recreated = hdx.ensure_webhook(WEBHOOK_NAME, WEBHOOK_TARGET,
                                               description="Krateo Autopilot auto-troubleshooter")
    if recreated:
        # the webhook id changed -> alerts referencing the old id would notify a dead channel.
        # Drop the HyperDX alerts we manage + reset their CR status so they rebuild on this webhook.
        active = [cr for cr in crs
                  if not cr["metadata"].get("deletionTimestamp") and not _api_ref(cr)]
        managed = {cr.get("status", {}).get("hyperdxAlertId") for cr in active} - {None, ""}
        for a in hdx.list_alerts():
            if a["id"] in managed:
                try:
                    hdx.delete_alert(a["id"])
                except Exception:  # noqa: BLE001
                    pass
        for cr in active:
            _patch_status(cr["metadata"]["name"], {"hyperdxAlertId": None, "phase": "Pending"})
    crs = _list_alert_crs()
    _release_shared(hdx, [cr for cr in crs
                          if not cr["metadata"].get("deletionTimestamp") and not _api_ref(cr)])
    for cr in crs:
        try:
            if cr["metadata"].get("deletionTimestamp"):
                _finalize(hdx, cr)      # CR is being deleted -> clean up HyperDX + drop finalizer
                continue
            if _api_ref(cr):
                _drop_hyperdx(hdx, cr)  # evaluated above; it keeps no HyperDX objects
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
