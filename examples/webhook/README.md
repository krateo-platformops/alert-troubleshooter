---
type: Example
title: Webhook example
description: A sample HyperDX alert payload and the report it yields.
tags: [observability, alerts]
timestamp: 2026-08-20T00:00:00Z
---

# Webhook example

```sh
curl -XPOST http://krateo-alert-troubleshooter.krateo-system.svc:8080/webhook -d @alert.json
```

`alert.json` is the body HyperDX sends: `alertName` is the notification title, a state emoji plus
the HyperDX alert name, which is the `Alert` CR's `metadata.name`. The handler analyzes only a
title that names an existing `Alert` exactly, and only for `state: ALERT`. A
`TroubleshootingReport` named `report-<Alert name>` appears with the analysis.
