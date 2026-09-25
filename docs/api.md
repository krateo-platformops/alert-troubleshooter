---
type: API
title: API
description: The Alert CRD this controller owns, the Incident fields it writes, the RCA output contract and the HTTP surface.
tags: [observability, alerts]
timestamp: 2026-08-20T00:00:00Z
---

# API

- **`Alert`** (`observability.krateo.io/v1alpha1`) — the inbound alert shape (see `crds/crd.alert.yaml`). Exactly one of `spec.where` (HyperDX row count) or `spec.apiRef {name, namespace}` (a RESTAction whose filter returns `{value: N, items?: [...]}`, polled every `spec.interval`). `status.state` mirrors HyperDX or the RESTAction's value; `status.okSince` is when it last turned OK, unset while it is anything else; `status.value` is the number an apiRef alert's RESTAction last returned, unset for a `where` alert (both display only).
- **`Incident`** (`observability.krateo.io/v1alpha1`) — written here, owned by incident-controller, whose chart ships the CRD. The handler creates `<alert>-<yyyymmdd-hhmmss>` with the label `observability.krateo.io/alert` and `spec.alertRef`, `trigger`, `prompt`, `triggeredAt`, and writes status `state` (`Analyzing`, then `Open`), `firings`, `lastFiredAt`, `howToFix`, `error`, `completedAt` and the analysis fields below. See [overview](overview.md#incidents).

HTTP: `POST /webhook` (acked 202); `GET /healthz`. The webhook body is
`{"alertName":"<emoji> <Alert metadata.name>","state":"ALERT|OK","source":"hyperdx-alert"}`, the
template the reconciler installs on its HyperDX webhook. `alertName` must name an `Alert` in the
release namespace exactly; `state: OK` is ignored.

## RCA output contract

`report_v2.py` holds both sides of it: the instructions appended to every RCA prompt, and the
parser of the agent's answer. The answer ends with one `json` block. The parser keeps the
`V2_STATUS_KEYS` it finds there, sanitized, and falls back to a prose-only report when there is no
usable block.

`howToFix` is the fix as three bash scripts:

| Script | Run by | Exit codes |
|---|---|---|
| `precondition` | the incident controller, in a read-only sandbox, first right after the analysis | `1` the incident holds, `0` it is gone; its first run must exit `1` |
| `apply` | a human, after reviewing it | none; it may write and is idempotent |
| `verify` | the incident controller, in the sandbox, after precondition `0` or an applied fix | `0` the fix worked, `1` the incident still holds |

Any other exit code, or a timeout, is unknown and changes nothing. The sandbox has bash, kubectl
and jq, a ServiceAccount that reads but never writes and cannot read Secrets, egress to the API
server only, and a 60 s deadline. The precondition tests the root-cause object, never the alert's
rows, so it tells whether that cause is gone while the alert fires for another.

The parser keeps `howToFix` only when all three scripts are non-empty strings of at most 16384
characters (a list of lines is joined). Otherwise it drops `howToFix` whole, keeps the rest of the
report and, when there is a root cause, appends to `missingContext` why there are no scripts.

The handler writes the `V2_STATUS_KEYS` onto the Incident. The parser's `evidence` (the
retrieval ledger behind the confidence cap) is not stored; its sentence is already in the report
and `missingContext`.
