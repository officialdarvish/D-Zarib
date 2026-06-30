# 3x-ui 3.4.2 source analysis for traffic factor sidecar

Relevant files from the uploaded 3x-ui source:

- `internal/web/job/xray_traffic_job.go`
- `internal/web/service/inbound_traffic.go`
- `internal/xray/api.go`
- `internal/web/service/setting.go`
- `internal/web/service/url_safety.go`

## Traffic scan flow

`XrayTrafficJob.Run()` does this:

1. `xrayService.GetXrayTraffic()` reads Xray stats.
2. `inboundService.AddTraffic(traffics, clientTraffics)` writes inbound and client traffic to DB.
3. If enabled, `informTrafficToExternalAPI()` sends POST to External Traffic Inform URI.
4. WebSocket payloads are broadcast.

This means an external sidecar can hook immediately after 3x-ui writes traffic by using External Traffic Inform.

## Client traffic write

`addClientTraffic()` performs atomic DB updates:

```go
UPDATE client_traffics SET up = up + ?, down = down + ?, last_online = ... WHERE email = ?
```

The key is `email`.

## Important limitation

Xray client traffic is parsed with:

```go
user>>>([^>]+)>>>traffic>>>(downlink|uplink)
```

This is email-level traffic. 3x-ui also comments that Xray's per-user counter aggregates across every inbound a client is attached to. Therefore, exact inbound-level charging for a client attached to multiple inbounds is ambiguous.

The sidecar defaults to strict single-inbound mode.

## Webhook detail

3x-ui has settings:

- `externalTrafficInformEnable`
- `externalTrafficInformURI`

When enabled, it posts:

```json
{"clientTraffics": [...], "inboundTraffics": [...]}
```

However, `SanitizePublicHTTPURL(..., false)` blocks localhost/private/internal targets. Use a public domain and reverse proxy it to the local sidecar.
