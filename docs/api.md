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

`TroubleshootingReport` has no `howToFix` field, so the apiserver prunes it; the `Incident` CRD
(incident-controller) stores it. The handler does not write `status.remediationPlan`.
