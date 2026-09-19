---
type: Configuration
title: Configuration
description: Environment and values that tune the controller.
tags: [observability, alerts]
timestamp: 2026-08-20T00:00:00Z
---

# Configuration

| key | default | meaning |
|---|---|---|
| `image.repository` | `ghcr.io/krateo-platformops/alert-troubleshooter` | controller image |
| `image.tag` | chart appVersion | image tag; pin to override |
| `config.reportRetentionDays` | `0` | days after which a **closed** incident report is deleted; `0` keeps every report forever |

## Incident retention

An incident report is an **audit record**: it deliberately outlives the Alert that produced it, so
"why did this fire in March" is still answerable after that alert has been renamed or retired. This
is why a report carries no `ownerReference` — Kubernetes would otherwise delete the record together
with its Alert.

The cost is that reports accumulate. `config.reportRetentionDays` is the opt-in bound:

- `0` (the default) — nothing is ever deleted. Upgrading never removes existing records.
- `> 0` — the reconciler deletes reports past that age, but **only closed ones**:
  a report is a candidate when it is `resolved` (its alert returned to OK, or a person closed it in
  the portal) **or** orphaned, meaning the Alert named by `spec.alertRef` no longer exists.

Never reaped, whatever the age: an incident still `open`, one mid-analysis (`phase: Pending` or
`Analyzing`), and any report whose timestamps cannot be parsed.

The orphan clause is what makes the knob complete. A report whose Alert was deleted can never be
resolved again — the reconcile loop advances lifecycle by iterating Alert CRs, and that one has
none — so without it an orphan left `open` would be immortal even with retention switched on.

Runtime env (set on the Deployment): the autopilot A2A endpoint and the HyperDX webhook token. See `helm/alert-troubleshooter/templates/deployment.yaml`.
