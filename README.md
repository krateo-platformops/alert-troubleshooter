# krateo-alert-troubleshooter

## What is this
Turns each firing of a Krateo observability **Alert** into an **Incident** with an
**incident-agent root-cause analysis**, in the background — no browser required.

```
Alert fires (HyperDX webhook, or an apiRef RESTAction) → krateo-alert-troubleshooter
    → open incident for this Alert?  yes → status.firings++
                                     no  → new Incident → A2A call to incident-agent
                                           → status: analysis + howToFix scripts, state Open
```

## What it does
On a firing (`POST /webhook`, or an apiRef alert the reconciler evaluates) the handler:
1. counts it on the Alert's open `Incident` (`observability.krateo.io/v1alpha1`) if there is one,
2. else creates one in state `Analyzing` and calls incident-agent over A2A (JSON-RPC
   `message/stream`) with the incident's prompt,
3. writes the analysis and its `howToFix` scripts to the Incident's status, in state `Open`.

The incident controller (incident-controller) runs the scripts from there. See
[docs/overview.md](docs/overview.md#incidents).

Acks the webhook immediately (202) and analyses in a background thread so HyperDX doesn't time out.

## Build
Image is built + pushed by CI (`.github/workflows/release.yaml`) to
`ghcr.io/krateo-platformops/alert-troubleshooter` on push to `main` / tags. No local docker push.

## Deploy
```sh
```
Then point a HyperDX webhook at `http://krateo-alert-troubleshooter.krateo-system.svc:8080/webhook`
and reference it as the `channel.webhookId` on your `Alert` CRs.

## Config (env)
- `NAMESPACE` (default `krateo-system`) — the namespace of its Alerts and their Incidents.
- `AUTOPILOT_A2A_URL` (default `http://krateo-autopilot.krateo-system.svc:8080/`).
- `A2A_TIMEOUT` (default `180`s).

## Install
```sh
helm install alert-troubleshooter oci://ghcr.io/krateo-platformops/charts/alert-troubleshooter --version <tag> -n krateo-system
```

## Configure
See [docs/configuration.md](docs/configuration.md).

## Examples
See [examples/webhook/](examples/webhook/).

## Docs
[docs/index.md](docs/index.md) — overview, usage, API, configuration, release, log.

## Develop & release
Tag a bare semver; CI builds the image and publishes the chart. See [docs/release.md](docs/release.md).
