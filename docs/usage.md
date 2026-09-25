---
type: Usage
title: Install and use
description: Install the chart, point HyperDX alerts at the webhook, read reports in the portal.
tags: [observability, alerts]
timestamp: 2026-08-20T00:00:00Z
---

# Usage

```sh
helm install alert-troubleshooter \
  oci://ghcr.io/krateo-platformops/charts/alert-troubleshooter --version <tag> \
  --namespace krateo-system
```

Apply `Alert` CRs (see [`examples/alerts/`](../examples/alerts/)). The reconciler creates one
HyperDX alert per CR, named after its `metadata.name`, and the shared webhook that posts to
`http://krateo-alert-troubleshooter.krateo-system.svc:8080/webhook`. A HyperDX alert fires an RCA
only if its name is an `Alert` CR's `metadata.name`. Reports appear as `TroubleshootingReport` CRs
and in the portal Alerts section.
