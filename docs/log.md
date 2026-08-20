---
type: Log
title: Log
description: Notable changes.
tags: [observability, alerts]
timestamp: 2026-08-20T00:00:00Z
---

# Log

## 2026-08-20
- Moved `krateo-agentiko` -> `krateo-platformops` (observability plumbing, not an agent) and standardized: wrapped the raw manifests in a helm chart, bare-semver + `CHART_VERSION`, owner-derived image build (was pinned at `krateo-agentiko` `v0.2.17`), public security caller, preflight, OKF docs.
