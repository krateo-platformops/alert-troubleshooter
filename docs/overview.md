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
