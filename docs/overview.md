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

## Alert identity

One `Alert` CR is one HyperDX alert, one webhook target and one report, keyed on the CR's
`metadata.name`. `spec.displayName` is only a label and may repeat.

- The reconciler names each HyperDX alert after its CR's `metadata.name`, on a single-tile
  dashboard `krateo-alert-<name>` that no other alert evaluates. An alert claimed by several CRs
  stays with the CR it is named after; the others create their own.
- HyperDX's webhook template has no alert id. The shared webhook body is
  `{"alertName":"{{title}}","state":"{{state}}","source":"hyperdx-alert"}`, and the title is a state
  emoji plus the HyperDX alert name. The reconciler updates an existing webhook whose body differs.
- The handler strips the emoji and GETs the `Alert` with exactly that name. A title that names no
  `Alert` runs no RCA, and neither does a resolve (`state: OK`).
- The report is `report-<metadata.name>` (hash-suffixed past 63 characters). `spec.alertRef` and
  `spec.alertNamespace` identify the `Alert`; `spec.alertName` holds its `displayName`.
