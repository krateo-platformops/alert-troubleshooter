---
type: Example
title: Default alerts
description: The Alert CRs the chart seeds by default, and the rules for writing your own.
tags: [observability, alerts]
timestamp: 2026-08-24T00:00:00Z
---

# Default alerts

The chart seeds three high-signal, platform-scoped alerts (`config.defaultAlerts`), shown in
[`default-alerts.yaml`](./default-alerts.yaml) as applyable `Alert` CRs for reference and editing:

| alert | fires on | interval / threshold |
|---|---|---|
| `krateo-composition-reconcile-error` | control-plane compositions failing to reconcile | 15m / `>2` |
| `krateo-platform-crashloop` | a platform pod crash-looping | 15m / `>3` |
| `krateo-platform-image-pull-failure` | a platform pod cannot pull its image | 15m / `>2` |

Each firing auto-launches an incident-agent RCA, so the default set is deliberately **small,
platform-scoped and high-threshold** — the goal is a few signals that never storm, not broad coverage.
Add your own by applying more `Alert` CRs.

## Writing an Alert

`spec.where` is **ClickHouse SQL over `otel_logs`** (the reconciler pins `whereLanguage=sql`). Three rules:

1. **SQL, not Lucene.**
2. **Filter on `Body` / `ResourceAttributes`** — this OTel pipeline leaves `SeverityText` empty, so a
   severity predicate counts zero and never fires.
3. **Scope every alert** — either to a namespace (`ResourceAttributes['k8s.namespace.name'] = '...'`) or
   with a `ServiceName NOT IN (...)` self-exclusion — so it never matches the observability stack's own
   log of the query it runs (which would make it diagnose itself).

`thresholdType: above` is strictly-greater, so `threshold: 0` fires on the first matching row; give
defaults a real threshold over a window.

```sh
kubectl apply -f default-alerts.yaml
kubectl -n krateo-system get alerts
```

For a full fault-injection lab (13 alerts paired with a `breakers.yaml` that injects each fault), see
[`krateo-agentiko/incident-agent` → `examples/alert-lab/`](https://github.com/krateo-agentiko/incident-agent/tree/main/examples/alert-lab).
