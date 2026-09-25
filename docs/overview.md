---
type: Architecture
title: alert-troubleshooter — architecture
description: How a HyperDX alert becomes an autopilot-authored TroubleshootingReport the portal renders.
tags: [observability, alerts, autopilot]
timestamp: 2026-08-20T00:00:00Z
---

# alert-troubleshooter

A webhook receiver + controller (not an agent): on a HyperDX alert-fired `POST /webhook` it
creates a `TroubleshootingReport` CR (`observability.krateo.io/v1alpha1`, phase `Analyzing`),
calls the **krateo-autopilot** A2A agent for a root-cause analysis, and patches the CR status
(`phase: Ready`, `report: <markdown>`) which the portal Alerts section renders. The webhook
is acked 202 immediately; analysis runs in a background thread.

It lives in `krateo-platformops` (not `krateo-agentiko`) because it is observability
plumbing keyed on the platform `observability.krateo.io` API group and rendered by the
portal — it *calls* an agent, it is not one.

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
3. `state` and `okSince` are written like a HyperDX alert's. On ALERT the same RCA path as a
   webhook runs, with `items` in the prompt, under the same report cooldown.

HyperDX is not involved, and apiRef alerts are evaluated before the HyperDX pass, so a HyperDX
outage does not stop them. HyperDX objects left from when a CR used `where` are deleted. The
snowplow call runs as group `krateo:alert-troubleshooter`, which has cluster-wide read (the
`krateo-alert-troubleshooter-observer` ClusterRole), so it can get any RESTAction and everything a
read-only one reads. A failed call is `phase: Error` and is retried every reconcile cycle.
