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

Runtime env (set on the Deployment): the autopilot A2A endpoint and the HyperDX webhook token. See `helm/alert-troubleshooter/templates/deployment.yaml`.
