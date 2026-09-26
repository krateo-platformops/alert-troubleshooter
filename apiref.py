#!/usr/bin/env python3
"""apiRef alerts: an Alert whose value comes from a RESTAction instead of a HyperDX row count.

The reconciler polls each apiRef Alert every `spec.interval`. Snowplow resolves the RESTAction
(`GET /call`) under this service's authn identity, the same way core-provider's CDC resolves a
CompositionDefinition's apiRef. The RESTAction's filter returns `{value: N, items?: [...]}`;
`value` is compared with `spec.threshold` using HyperDX's `thresholdType` semantics.
"""
import os
from datetime import datetime, timezone

import requests

import handler

SNOWPLOW_URL = os.environ.get("SNOWPLOW_URL", "http://snowplow.krateo-system.svc:8081").rstrip("/")
SNOWPLOW_TIMEOUT = int(os.environ.get("SNOWPLOW_TIMEOUT", "60"))

class ApiRefError(RuntimeError):
    """The RESTAction could not be resolved into a value; the message goes to status.error."""


def resolve(ref):
    """The status snowplow computes for the RESTAction `ref` ({name, namespace}): its filter's
    output. Raises ApiRefError on anything but a 200 with a JSON object."""
    jwt = handler._service_jwt()
    if not jwt:
        raise ApiRefError("no service JWT: apiRef alerts need config.authnUrl, because snowplow "
                          "resolves the RESTAction as this service's authn identity")
    try:
        r = requests.get(f"{SNOWPLOW_URL}/call",
                         params={"apiVersion": "templates.krateo.io/v1", "resource": "restactions",
                                 "namespace": ref.get("namespace", ""), "name": ref.get("name", "")},
                         headers={"Authorization": f"Bearer {jwt}", "Accept": "application/json"},
                         timeout=SNOWPLOW_TIMEOUT)
    except requests.RequestException as e:
        raise ApiRefError(f"calling snowplow: {e}") from e
    if r.status_code != 200:
        raise ApiRefError(f"snowplow returned {r.status_code}: {r.text[:200]}")
    try:
        status = (r.json() or {}).get("status")
    except ValueError as e:
        raise ApiRefError(f"snowplow returned non-JSON: {r.text[:200]}") from e
    if not isinstance(status, dict):
        raise ApiRefError(f"RESTAction status is {type(status).__name__}, not an object: its "
                          "filter must return {value: N, items?: [...]}")
    return status


def value_of(status):
    """The number the RESTAction's filter returned under `value`."""
    value = status.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiRefError(f"RESTAction status.value is {value!r}, not a number: its filter must "
                          "return {value: N, items?: [...]}")
    return value


# HyperDX's semantics for each thresholdType (checkAlerts doesExceedThreshold).
COMPARE = {
    "above": lambda v, t: v >= t,
    "above_exclusive": lambda v, t: v > t,
    "below": lambda v, t: v < t,
    "below_or_equal": lambda v, t: v <= t,
    "equal": lambda v, t: v == t,
    "not_equal": lambda v, t: v != t,
}


def invalid(threshold_type):
    """Why an apiRef alert cannot be evaluated with this thresholdType, or None."""
    kind = (threshold_type or "above").lower()
    if kind in COMPARE:
        return None
    return (f"thresholdType '{kind}' needs a thresholdMax, which an Alert has no field for; "
            "use one of " + ", ".join(COMPARE))


def exceeds(value, threshold, threshold_type):
    """Whether `value` fires the alert. `threshold_type` must pass invalid()."""
    return COMPARE[(threshold_type or "above").lower()](value, float(threshold))


def due(status, interval, now=None):
    """Whether an apiRef alert is due for evaluation: never evaluated, last attempt not Synced
    (retried every reconcile cycle), or `interval` elapsed since the last evaluation."""
    last = status.get("lastSyncedAt")
    if not last or status.get("phase") != "Synced":
        return True
    try:
        at = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except ValueError:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - at).total_seconds() >= handler.interval_seconds(interval)
