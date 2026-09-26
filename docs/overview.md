---
type: Architecture
title: alert-troubleshooter — architecture
description: How an alert firing becomes an Incident with an incident-agent root-cause analysis, one open incident per alert.
tags: [observability, alerts, autopilot]
timestamp: 2026-08-20T00:00:00Z
---

# alert-troubleshooter

A webhook receiver + controller (not an agent). Each firing of an `Alert`, a HyperDX webhook
(`POST /webhook`) or an apiRef alert the reconciler evaluates, is recorded on an `Incident`
(`observability.krateo.io/v1alpha1`, the CRD incident-controller ships): the alert's open incident
counts it, or a new incident opens and **incident-agent** root-causes it over A2A. The webhook is
acked 202 immediately; the analysis runs in a background thread.

It lives in `krateo-platformops` (not `krateo-agentiko`) because it is observability
plumbing keyed on the platform `observability.krateo.io` API group and rendered by the
portal — it *calls* an agent, it is not one.

## Alert identity

One `Alert` CR is one HyperDX alert, one webhook target and its own incidents, keyed on the CR's
`metadata.name`. `spec.displayName` is only a label and may repeat.

- The reconciler names each HyperDX alert after its CR's `metadata.name`, on a single-tile
  dashboard `krateo-alert-<name>` that no other alert evaluates. An alert claimed by several CRs
  stays with the CR it is named after; the others create their own.
- HyperDX's webhook template has no alert id. The shared webhook body is
  `{"alertName":"{{title}}","state":"{{state}}","source":"hyperdx-alert"}`, and the title is a state
  emoji plus the HyperDX alert name. The reconciler updates an existing webhook whose body differs.
- The handler strips the emoji and GETs the `Alert` with exactly that name. A title that names no
  `Alert` runs no RCA, and neither does a resolve (`state: OK`).
- Its incidents carry the label `observability.krateo.io/alert: <metadata.name>` and
  `spec.alertRef {name, namespace}`. A name over 63 characters cannot be a label value, so such an
  `Alert` opens no incident.

## Incidents

An alert has at most one open incident (Policy A); an incident is open in any state but
`Resolved` and `Closed`. For each firing, `handler.analyze`:

1. lists the Incidents in the Alert's namespace labelled with the Alert's name;
2. if one is open, adds one to its `status.firings`, sets `status.lastFiredAt`, and runs no RCA.
   The incident controller writes the same status, so the write is conditioned on the
   resourceVersion it read and retried on a conflict;
3. if none is open but the alert's latest incident is `Resolved` and its `status.resolution.at`
   is less than one `spec.interval` ago (5m when unset), counts the firing on that incident the
   same way, and it stays `Resolved`. A `where` alert keeps counting the rows from before the fix
   for its lookback window, and those firings belong to the incident the fix resolved. A `Closed`
   incident gets no such window: after a human close, the next firing opens a new one;
4. otherwise creates `<alert>-<yyyymmdd-hhmmss>` (the firing's UTC time) with the label and
   `spec.alertRef`, `trigger: alert`, `prompt` and `triggeredAt`, in state `Analyzing` with
   `firings: 1`;
5. runs the RCA on the incident's own kagent thread (contextId = uuid5 of its name), then writes
   the analysis, `howToFix` and `state: Open` in one status write.

- An RCA that fails, or whose answer is empty, unstructured or has no usable `howToFix`, still
  opens the incident, with `status.error` saying why it has no scripts.
- An incident a human closed while it was analyzing stays `Closed`, which is final: the analysis
  is written without a state.
- At startup the handler opens every incident a restart left `Analyzing`, with the reason in
  `error`, so it can be closed and the next firing opens a fresh one.
- The alert returning to OK changes nothing. From `Open` on, the incident controller runs its
  scripts and moves it, or a human closes it.

## Alert status

The reconciler mirrors the HyperDX alert's `state` onto `status.state` every cycle, and writes
`status.okSince` in the same patch: the time the alert last turned OK, kept while it stays OK and
unset in any other state. It is for display ("OK for 13 min") and never closes anything.

## apiRef alerts

An `Alert` sets exactly one of `spec.where` (a HyperDX row count) or `spec.apiRef` (a
RESTAction), enforced by a CEL rule. For an apiRef alert the reconciler itself polls, every
`spec.interval`:

1. `GET <snowplowUrl>/call?apiVersion=templates.krateo.io/v1&resource=restactions&namespace=&name=`
   with the service JWT (`config.authnUrl` required), the path core-provider's CDC uses for a
   CompositionDefinition's apiRef.
2. The RESTAction's filter returns `{value: N, items?: [...]}`. `value` is compared with
   `threshold` using HyperDX's `thresholdType` semantics; `between`/`not_between` are `Invalid`
   (no `thresholdMax`). The row-count tautology check does not apply.
3. `state` and `okSince` are written like a HyperDX alert's, and `status.value` holds the
   RESTAction's number (display only; a `where` alert has none). An ALERT evaluation is a firing,
   recorded like a webhook's; a new incident's prompt carries `items`.

HyperDX is not involved, and apiRef alerts are evaluated before the HyperDX pass, so a HyperDX
outage does not stop them. HyperDX objects left from when a CR used `where` are deleted. The
snowplow call runs as group `krateo:alert-troubleshooter`, which has cluster-wide read (the
`krateo-alert-troubleshooter-observer` ClusterRole), so it can get any RESTAction and everything a
read-only one reads. A failed call is `phase: Error` and is retried every reconcile cycle.
