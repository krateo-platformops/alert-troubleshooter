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

Point a HyperDX alert action at `http://alert-troubleshooter.krateo-system:8080/webhook`.
Reports appear as `TroubleshootingReport` CRs and in the portal Alerts section.
