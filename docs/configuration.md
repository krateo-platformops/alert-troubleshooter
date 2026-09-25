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
| `config.authnUrl` | `""` | authn base URL; the service JWT it issues is needed by the A2A call behind agentgateway and by apiRef alerts |
| `config.snowplowUrl` | `http://snowplow.krateo-system.svc:8081` | snowplow, which resolves apiRef alerts' RESTActions (`SNOWPLOW_URL`) |

Runtime env (set on the Deployment): the autopilot A2A endpoint and the HyperDX webhook token. See `helm/alert-troubleshooter/templates/deployment.yaml`.
