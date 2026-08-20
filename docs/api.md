---
type: API
title: API
description: The two CRDs this controller owns.
tags: [observability, alerts]
timestamp: 2026-08-20T00:00:00Z
---

# API

- **`Alert`** (`observability.krateo.io/v1alpha1`) — the inbound alert shape (see `crds/crd.alert.yaml`).
- **`TroubleshootingReport`** (`observability.krateo.io/v1alpha1`) — `status.phase` (`Analyzing`/`Ready`) + `status.report` markdown (see `crds/crd.troubleshootingreport.yaml`).

HTTP: `POST /webhook` (HyperDX alert payload, acked 202); `GET /healthz`.
