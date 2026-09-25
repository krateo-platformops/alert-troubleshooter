---
type: Usage
title: Install and use
description: Install the chart, point HyperDX alerts at the webhook, read incidents in the portal.
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
only if its name is an `Alert` CR's `metadata.name`. Each firing is recorded on an `Incident`, so
incident-controller's CRD chart must be installed first; without it a firing is logged and lost.

## Who can see alerts

The portal reads `Alert` CRs with the signed-in user's own token. The chart ships the ClusterRole
`krateo-alert-viewer` (get/list/watch on `alerts.observability.krateo.io`) with no binding and no
aggregation labels, so no user sees alerts until an admin binds it to a group. A RoleBinding
limits it to the Alerts' namespace:

```sh
kubectl create rolebinding krateo-alert-viewer-devs -n krateo-system --clusterrole=krateo-alert-viewer --group=devs
```

A ClusterRoleBinding (`kubectl create clusterrolebinding … --clusterrole=krateo-alert-viewer`)
grants it in every namespace.
