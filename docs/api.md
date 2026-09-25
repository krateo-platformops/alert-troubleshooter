---
type: API
title: API
description: The two CRDs this controller owns.
tags: [observability, alerts]
timestamp: 2026-08-20T00:00:00Z
---

# API

- **`Alert`** (`observability.krateo.io/v1alpha1`) — the inbound alert shape (see `crds/crd.alert.yaml`). Exactly one of `spec.where` (HyperDX row count) or `spec.apiRef {name, namespace}` (a RESTAction whose filter returns `{value: N, items?: [...]}`, polled every `spec.interval`). `status.state` mirrors HyperDX or the RESTAction's value; `status.okSince` is when it last turned OK, unset while it is anything else; `status.value` is the number an apiRef alert's RESTAction last returned, unset for a `where` alert (both display only).
- **`TroubleshootingReport`** (`observability.krateo.io/v1alpha1`) — `status.phase` (`Analyzing`/`Ready`) + `status.report` markdown (see `crds/crd.troubleshootingreport.yaml`). One per `Alert`, named `report-<Alert metadata.name>`; `spec.alertRef`/`spec.alertNamespace` name the `Alert`.

HTTP: `POST /webhook` (acked 202); `GET /healthz`. The webhook body is
`{"alertName":"<emoji> <Alert metadata.name>","state":"ALERT|OK","source":"hyperdx-alert"}`, the
template the reconciler installs on its HyperDX webhook. `alertName` must name an `Alert` in the
release namespace exactly; `state: OK` is ignored.
