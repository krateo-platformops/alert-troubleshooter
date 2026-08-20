---
type: Runbook
title: Release
description: How this repo is built and released.
tags: [observability, alerts]
timestamp: 2026-08-20T00:00:00Z
---

# Release

Tag a **bare semver** (`X.Y.Z`). `release-image.yaml` builds and pushes `ghcr.io/krateo-platformops/alert-troubleshooter:<tag>` (owner-derived) with a `/healthz` gate; `release-tag.yaml` stamps `CHART_VERSION` and publishes `oci://ghcr.io/krateo-platformops/charts/alert-troubleshooter:<tag>`. Chart and image move together by construction.
