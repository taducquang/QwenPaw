# Webhook Channel for CoPaw

Inbound HTTP webhook channel that lets external systems trigger CoPaw agent processing via HTTP POST requests.

## Features

- **Async mode** — fire-and-forget message delivery (HTTP 202)
- **Sync mode** — hold connection until agent responds (HTTP 200)
- **Bearer token authentication** with constant-time comparison
- **Per-IP rate limiting** (fixed-window, configurable)
- **Health endpoint** — `GET /hooks/health` (no auth required)
- **Wake endpoint** — lightweight fire-and-forget system events
- **Access control** — allowlist-based sender filtering
- **Proxy-aware** — optional `X-Forwarded-For` trust

## Configuration

Add to your `config.json` under `channels`:

```json
{
  "channels": {
    "webhook": {
      "enabled": true,
      "token": "your-secret-token",
      "port": 18790,
      "host": "127.0.0.1",
      "sync_timeout": 60,
      "max_request_size_bytes": 1048576,
      "rate_limit_per_minute": 60,
      "trust_proxy": false,
      "max_concurrent_sync": 10,
      "dm_policy": "open",
      "allow_from": [],
      "deny_message": ""
    }
  }
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable the channel |
| `token` | string | `""` | **Required.** Bearer token for authentication |
| `port` | int | `18790` | HTTP server port |
| `host` | string | `"127.0.0.1"` | Bind address |
| `sync_timeout` | int | `60` | Max seconds to wait in sync mode |
| `max_request_size_bytes` | int | `1048576` | Max request body size (1 MB) |
| `rate_limit_per_minute` | int | `60` | Max requests per IP per minute |
| `trust_proxy` | bool | `false` | Use `X-Forwarded-For` for rate limiting |
| `max_concurrent_sync` | int | `10` | Max simultaneous sync requests |
| `dm_policy` | string | `"open"` | `"open"` or `"allowlist"` |
| `allow_from` | list | `[]` | Allowed sender IDs (when policy is allowlist) |
| `deny_message` | string | `""` | Custom rejection message |

### Environment Variables

```bash
WEBHOOK_CHANNEL_ENABLED=1
WEBHOOK_TOKEN=your-secret-token
WEBHOOK_PORT=18790
WEBHOOK_HOST=127.0.0.1
WEBHOOK_SYNC_TIMEOUT=60
```

## API Endpoints

### Health Check

```bash
curl http://localhost:18790/hooks/health
```

Response:
```json
{"status": "ok", "channel": "webhook", "uptime_seconds": 42}
```

### Async Message (fire-and-forget)

```bash
curl -X POST http://localhost:18790/hooks/message \
  -H "Authorization: Bearer your-secret-token" \
  -H "Content-Type: application/json" \
  -d '{"message": "What is the weather today?"}'
```

Response (202):
```json
{
  "accepted": true,
  "request_id": "a1b2c3d4",
  "session_key": "webhook:webhook",
  "message": "Request enqueued for processing"
}
```

Optional fields:
- `sender_id` — identify the caller (default: `"webhook"`)
- `session_key` — custom session grouping (alphanumeric + `:_-`, max 200 chars)
- `name` — display name for the sender

### Sync Message (wait for response)

```bash
curl -X POST http://localhost:18790/hooks/message \
  -H "Authorization: Bearer your-secret-token" \
  -H "Content-Type: application/json" \
  -d '{"message": "Summarize the latest report", "sync": true, "timeout_seconds": 30}'
```

Response (200):
```json
{
  "response": "The latest report shows...",
  "request_id": "a1b2c3d4",
  "session_key": "webhook:webhook",
  "processing_time_ms": 1523
}
```

Timeout response (504):
```json
{"error": "Agent did not respond within 30 seconds"}
```

### Wake Event (fire-and-forget system event)

```bash
curl -X POST http://localhost:18790/hooks/wake \
  -H "Authorization: Bearer your-secret-token" \
  -H "Content-Type: application/json" \
  -d '{"text": "Deployment to production completed successfully"}'
```

Response (200):
```json
{"accepted": true}
```

## Authentication

The token can be sent via:
- **Bearer header** (recommended): `Authorization: Bearer <token>`
- **Custom header**: `X-CoPaw-Token: <token>`

Query string tokens are explicitly rejected for security.

## Error Responses

| Status | Meaning |
|--------|---------|
| 400 | Malformed JSON, missing fields, invalid session_key |
| 401 | Missing or invalid token |
| 403 | Sender blocked by allowlist |
| 413 | Request body too large |
| 429 | Rate limit exceeded or too many concurrent sync requests |
| 500 | Agent processing error (sync mode) |
| 504 | Agent timeout (sync mode) |
