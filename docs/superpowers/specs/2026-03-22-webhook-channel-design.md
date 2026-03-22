# Webhook Channel for CoPaw

**Date:** 2026-03-22
**Status:** Draft
**Inspired by:** [OpenClaw Webhook](https://docs.openclaw.ai/automation/webhook)

## Problem

CoPaw currently has no way for external systems (CI/CD pipelines, monitoring tools, email processors, automation platforms like n8n/Make/Zapier) to programmatically trigger agent processing. Every message must originate from a human through a messaging channel (Discord, Telegram, Zalo, etc.). This limits CoPaw's usefulness as an automation backbone.

## Solution

Add an inbound webhook channel as a custom channel plugin. External services POST JSON payloads to CoPaw's webhook endpoint, triggering agent processing. The channel supports both synchronous mode (caller waits for the agent's response) and asynchronous mode (fire-and-forget). Authentication uses bearer tokens.

## Non-Goals

- **Outbound webhooks** (CoPaw POSTing agent responses to external URLs) are out of scope. The webhook channel is inbound-only.
- **Custom hook mappings** (OpenClaw's `/hooks/<name>` with payload transformers) are deferred to a future iteration.
- **Cross-channel delivery** (routing webhook-triggered agent responses to Zalo/Discord/etc.) is deferred. The async mode simply processes and logs; only sync mode returns the response.
- **Per-sender tokens or HMAC signatures** — a single shared bearer token is used. Token-per-sender and HMAC payload signing are deferred to a future iteration.
- **API versioning** — endpoints use unversioned paths (`/hooks/message`). If breaking changes are needed later, versioned paths can be introduced.

## Architecture

### Placement

Custom channel plugin at `custom_channels/webhook/`. Auto-discovered by CoPaw's channel registry on startup.

### HTTP Server

The webhook channel runs its own lightweight HTTP server using `aiohttp` on a configurable port (default `18790`), separate from CoPaw's main FastAPI server. This avoids coupling to CoPaw's internal app object, which a custom channel plugin cannot reliably access.

The server binds to `127.0.0.1` by default (loopback only). To expose externally, the user sets `host: "0.0.0.0"` and places a reverse proxy with TLS in front.

### Message Flow

```
External HTTP POST
    |
    v
aiohttp server (port 18790)
    |-- Validate bearer token (401 if invalid)
    |-- Validate request body (400 if malformed)
    |-- Rate limit check (429 if exceeded)
    |
    v
Build native payload dict:
  { channel_id: "webhook",
    sender_id: <from request>,
    content_parts: [TextContent(text=message)],
    meta: { session_key, name, sync, request_id } }
    |
    v
self._enqueue(native)
    |
    +--> [Async mode] Return HTTP 202 immediately
    |
    +--> [Sync mode] Create asyncio.Future keyed by request_id
         |
         v
    ChannelManager consumer picks up payload
         |
         v
    WebhookChannel.consume_one()
      -> build_agent_request_from_native()
      -> Runner processes request
      -> on_event_message_completed() resolves Future
         |
         v
    [Sync mode] HTTP handler receives Future result
      -> Return HTTP 200 with agent response text
      -> If timeout: HTTP 504
```

## Endpoints

### POST /hooks/message

The primary endpoint. External services send messages for agent processing.

**Request body:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `message` | string | yes | - | The prompt or message for the agent. Must be non-empty |
| `sender_id` | string | no | `"webhook"` | Caller identity for access control and logging |
| `session_key` | string | no | `webhook:{sender_id}` | Session scoping key (max 200 chars, alphanumeric + `:_-`). Messages with the same key share conversation context |
| `name` | string | no | `""` | Display name prefix for logs |
| `sync` | boolean | no | `false` | If true, wait for agent response and return it in the HTTP body |
| `timeout_seconds` | integer | no | `60` | Max wait time in sync mode (capped at config's `sync_timeout`). Ignored in async mode |

**Response (async, `sync: false`):**

```
HTTP 202 Accepted
Content-Type: application/json

{
  "accepted": true,
  "request_id": "a1b2c3d4",
  "session_key": "hook:github:pr-456",
  "message": "Request enqueued for processing"
}
```

**Response (sync, `sync: true`):**

```
HTTP 200 OK
Content-Type: application/json

{
  "response": "Here is the summary...",
  "request_id": "a1b2c3d4",
  "session_key": "hook:github:pr-456",
  "processing_time_ms": 3420
}
```

**Error responses:**

| Code | Condition |
|------|-----------|
| 400 | Missing or empty `message` field, malformed JSON, or token sent via query string |
| 401 | Missing or invalid bearer token |
| 403 | `sender_id` not in `allow_from` when `dm_policy` is `"allowlist"` |
| 413 | Request body exceeds `max_request_size_bytes` |
| 429 | Rate limit exceeded. Response includes `Retry-After` header |
| 500 | Agent error during sync processing (agent threw an exception) |
| 503 | Channel shutting down while sync request is in-flight |
| 504 | Sync mode timeout (agent did not respond within `timeout_seconds`) |

### POST /hooks/wake

Lightweight fire-and-forget system event. Enqueues a text message into the `webhook:{sender_id}` session's conversation log and triggers normal agent processing (same pipeline as `/hooks/message` but always async).

**Request body:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `text` | string | yes | - | Event description. Must be non-empty |
| `sender_id` | string | no | `"webhook"` | Caller identity |

**Response:**

```
HTTP 200 OK
{ "accepted": true }
```

**Error responses:** Same as `/hooks/message` (400, 401, 403, 413, 429) except sync-mode errors (500, 503, 504) do not apply.

### GET /hooks/health

Health check endpoint. Does not require authentication.

**Response:**

```
HTTP 200 OK
{
  "status": "ok",
  "channel": "webhook",
  "uptime_seconds": 3600
}
```

## Authentication

All endpoints except `/hooks/health` require a bearer token.

**Token transmission (choose one):**
- `Authorization: Bearer <token>` (recommended)
- `X-CoPaw-Token: <token>` (alternative)

**Query string tokens are rejected** — returns HTTP 400 to prevent token leakage in logs.

**Rate limiting:** Fixed-window counter per client identity, configurable (default: 60 requests/minute). Client identity is determined by: (1) `X-Forwarded-For` header if `trust_proxy` config is true, otherwise (2) the TCP peer IP address. Returns HTTP 429 with `Retry-After` header indicating seconds until the window resets.

## Configuration

In `~/.copaw/config.json` under `channels.webhook`:

```json
{
  "channels": {
    "webhook": {
      "enabled": true,
      "token": "your-secret-token-here",
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
| `enabled` | bool | `false` | Enable the webhook channel |
| `token` | string | **required** | Bearer token for authentication. Channel refuses to start without one |
| `port` | int | `18790` | HTTP server port |
| `host` | string | `"127.0.0.1"` | Bind address. Loopback by default for security |
| `sync_timeout` | int | `60` | Max seconds for sync mode responses. Caps `timeout_seconds` from requests |
| `max_request_size_bytes` | int | `1048576` | Max request body size (1 MB default) |
| `rate_limit_per_minute` | int | `60` | Per-client request rate limit (fixed-window counter) |
| `trust_proxy` | bool | `false` | If true, use `X-Forwarded-For` for client identity in rate limiting. Enable when behind a reverse proxy |
| `max_concurrent_sync` | int | `10` | Max simultaneous sync requests. Beyond this, returns 429 |
| `dm_policy` | string | `"open"` | Access control: `"open"` or `"allowlist"` |
| `allow_from` | list | `[]` | Allowed `sender_id` values when `dm_policy` is `"allowlist"` |
| `deny_message` | string | `""` | Message returned when sender is denied |

**Environment variable overrides:**

| Variable | Maps to |
|----------|---------|
| `WEBHOOK_CHANNEL_ENABLED` | `enabled` |
| `WEBHOOK_TOKEN` | `token` |
| `WEBHOOK_PORT` | `port` |
| `WEBHOOK_HOST` | `host` |
| `WEBHOOK_SYNC_TIMEOUT` | `sync_timeout` |

## Sync Mode: Implementation Detail

Sync mode requires holding the HTTP connection while the agent processes. The mechanism:

1. HTTP handler generates a unique `request_id` (8-char hex from `uuid4().hex[:8]`).
2. Checks `max_concurrent_sync` — if exceeded, returns 429 immediately.
3. Creates an `asyncio.Future` stored in `_pending_responses[request_id]`.
4. Adds `request_id` and `sync: true` to the native payload's `meta`.
5. Calls `_enqueue(native)` and `await`s the Future with timeout.
6. `on_event_message_completed()` checks `meta.sync` — if true, extracts the response text and resolves `_pending_responses[request_id]`.
7. HTTP handler receives the resolved value and returns it as JSON with HTTP 200.
8. Cleanup: Futures are removed from `_pending_responses` after resolution, timeout, or error.

**Error handling in sync mode:**

- **Timeout:** Future is cancelled, HTTP 504 returned with `{"error": "Agent did not respond within N seconds"}`.
- **Agent error:** `_on_consume_error()` override rejects the Future with the error message. HTTP handler catches the rejection and returns HTTP 500 with `{"error": "..."}`.
- **Channel shutdown:** `stop()` cancels all pending Futures. HTTP handler catches `CancelledError` and returns HTTP 503 with `{"error": "Channel shutting down"}`.

**Race condition safety:** The Future is created before `_enqueue()`, so the response cannot arrive before the Future exists.

**Concurrency safety:** `_pending_responses` is a plain dict accessed only from the event loop thread (aiohttp and ChannelManager both run on the same asyncio loop). No additional locking required.

## Session Management

- Default session ID: `webhook:{sender_id}` (one session per unique caller).
- Custom session scoping via `session_key` in the request body: `webhook:{session_key}`.
- Sessions with the same key share conversation history, enabling multi-turn conversations over webhook.

## File Structure

```
custom_channels/webhook/
  __init__.py        # exports WebhookChannel
  channel.py         # WebhookChannel class + embedded aiohttp server
  README.md          # setup & config documentation
```

No external subprocess or Node.js bridge. Pure Python, using `aiohttp` for the embedded HTTP server.

## Dependencies

- `aiohttp` — already in CoPaw's dependency tree (used by other packages). Lightweight async HTTP server.
- No new dependencies required.

## Testing

Unit tests at `tests/unit/channels/test_webhook_channel.py`:

- Token validation (valid, missing, invalid, constant-time comparison)
- Rate limiting (under limit, over limit, trust_proxy behavior)
- Request validation (missing message, empty message, oversized body, query string token rejection)
- Async mode (returns 202, enqueues payload correctly)
- Sync mode (waits for response, returns 200 with response text)
- Sync timeout (returns 504)
- Sync agent error (returns 500)
- Sync channel shutdown (returns 503, all pending Futures cancelled)
- Sync concurrency cap (returns 429 when `max_concurrent_sync` exceeded)
- Session resolution (default `webhook:{sender_id}`, custom `session_key`)
- Session key validation (max length, allowed characters)
- Health endpoint (no auth required, returns uptime)
- Wake endpoint (enqueues, returns 200, shares error codes with /hooks/message)
- Access control (allowlist blocking returns 403)
- Configuration loading (from_config, from_env)
- Concurrent sync requests from different senders

## Security Considerations

1. **Loopback binding** — default `host: 127.0.0.1` prevents external access without explicit configuration.
2. **Token required** — channel refuses to start if `token` is not set. Prevents accidental unauthenticated exposure.
3. **Constant-time comparison** — token validation uses `hmac.compare_digest()` to prevent timing attacks.
4. **No query string tokens** — prevents token leakage in server logs, proxy logs, and browser history.
5. **Rate limiting** — fixed-window counter per client identity, with `trust_proxy` option for reverse proxy deployments.
6. **Concurrent sync cap** — `max_concurrent_sync` prevents resource exhaustion from many simultaneous sync requests.
7. **Request size limit** — prevents memory exhaustion from oversized payloads.
8. **Payload treated as untrusted** — message content is passed through the agent's normal safety boundaries.
9. **No CORS headers** — the webhook endpoint is not intended for browser-based access. No `Access-Control-*` headers are sent.
10. **Single shared token** — known limitation. All callers share one token. If compromised, all webhook access is compromised. Per-sender tokens and HMAC payload signing are deferred to a future iteration.
