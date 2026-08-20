---
type: Example
title: Webhook example
description: A sample HyperDX alert payload and the report it yields.
tags: [observability, alerts]
timestamp: 2026-08-20T00:00:00Z
---

# Webhook example

```sh
curl -XPOST http://alert-troubleshooter.krateo-system:8080/webhook -d @alert.json
```

See `alert.json` for the payload shape; a `TroubleshootingReport` CR appears with the analysis.
