# Zalo User Channel Integration Design

**Date:** 2026-03-22
**Status:** Approved
**Author:** Claude (brainstorming session)

## Overview

Rebuild the custom `zalouser` channel for CoPaw from scratch using zca-js (unofficial Zalo API for personal accounts). The channel enables CoPaw to send and receive messages through a personal Zalo account, supporting direct messages, group chats, and full multimedia (images, files, stickers, videos).

**Architecture:** Python `ZaloUserChannel` (BaseChannel subclass) manages lifecycle and message routing. A hardened Node.js subprocess (`bridge.mjs`) wraps zca-js for Zalo API interactions. Communication happens via stdin/stdout JSON-line protocol.

**Warning:** This is an unofficial integration using a reverse-engineered API. Using Zalo automation may result in account suspension or ban.

## Scope

**Rebuild = rewrite `channel.py` and `bridge.mjs` from scratch**, deleting all existing code in `custom_channels/zalouser/`. Reuses the same file locations and custom_channels registration pattern. The existing implementation's architecture (Python + Node.js bridge) was sound but the code has accumulated issues that make a clean rewrite more efficient than patching.

## Requirements

- **Rebuild from scratch** — clean rewrite of all zalouser channel files
- **Full multimedia support:** text, images, files, videos, stickers
- **Authentication:** QR code login + credential-based login with auto-save
- **Chat support:** DM + group chats with optional @mention requirement
- **Hardened subprocess:** health checks, crash recovery, auto-reconnection
- **Pinned dependencies:** zca-js ~2.1.2, sharp ~0.33.0 (tilde for narrow range)
- **Concurrency:** global rate limiter in Node.js bridge
- **Cross-platform:** Windows and Unix support

## Architecture

```
CoPaw Process (Python)
+------------------------------------------------+
|  ZaloUserChannel (BaseChannel subclass)        |
|  - from_config() / from_env()                  |
|  - build_agent_request_from_native()           |
|  - send() / send_media() / send_content_parts()|
|  - resolve_session_id()                        |
|  - start() / stop()                            |
|  - Typing indicator management                 |
|  - Access control (DM + group policies)        |
+------------------------+-----------------------+
                         | stdin/stdout JSON-line
+------------------------+-----------------------+
|  ZaloBridge (hardened subprocess manager)       |
|  - start() -> spawn Node.js, wait "ready"      |
|  - _health_check_loop() -> ping every 30s      |
|  - _crash_handler() -> auto-restart w/ backoff  |
|  - _reconnect() -> re-login after crash         |
|  - send_command() -> request/reply futures      |
|  - _stderr_reader() -> log Node.js stderr       |
+------------------------+-----------------------+
                         | subprocess
+------------------------+-----------------------+
|  Node.js Bridge (bridge.mjs)                   |
|  - Command handler (JSON-line protocol)        |
|  - Global rate limiter (fixed-interval, 5 msg/s)|
|  - zca-js (pinned ~2.1.2) + sharp (~0.33.0)   |
|  - imageMetadataGetter via sharp               |
+------------------------+-----------------------+
                         | HTTPS/WebSocket
                    Zalo Servers
```

## File Structure

```
custom_channels/zalouser/
+-- __init__.py          # Exports ZaloUserChannel
+-- channel.py           # Python channel + ZaloBridge classes
+-- bridge.mjs           # Node.js bridge (zca-js wrapper)
+-- package.json         # Pinned dependencies
+-- README.md            # Setup and usage guide
```

## Configuration

In CoPaw `config.json` under `channels.zalouser`:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable the channel |
| `state_dir` | string | `~/.copaw/zalouser` | Credential storage path |
| `bot_prefix` | string | `""` | Prefix for bot replies |
| `show_typing` | bool | `true` | Send typing indicators |
| `dm_policy` | string | `"open"` | DM access: "open" or "allowlist" |
| `group_policy` | string | `"open"` | Group access: "open" or "allowlist" |
| `allow_from` | list | `[]` | Allowed sender IDs |
| `deny_message` | string | `""` | Rejection message for blocked users |
| `require_mention` | bool | `false` | Require @mention in groups |
| `filter_tool_messages` | bool | `false` | Hide tool execution details |
| `filter_thinking` | bool | `false` | Hide model thinking |
| `max_send_rate` | int | `5` | Max messages per second (global) |
| `health_check_interval` | int | `30` | Ping interval in seconds |
| `max_restart_attempts` | int | `5` | Max crash recovery attempts |

Environment variable equivalents use `ZALOUSER_` prefix (e.g., `ZALOUSER_ENABLED=1`).

## Message Flow: Inbound

```
Zalo Servers
    | WebSocket message
    v
bridge.mjs: listener.on("message", handler)
    | Parse message type (User/Group)
    | Extract: threadId, senderId, content, attachments, mentions
    | Normalize content (text, link objects, etc.)
    | Detect media: images, files, videos, stickers
    v
stdout JSON-line event:
{
  "event": "message",
  "data": {
    "threadId": "123456",
    "isGroup": false,
    "senderId": "789",
    "senderName": "John",
    "content": "Hello!",
    "attachments": [
      {"type": "image", "url": "https://...", "thumbnailUrl": "..."},
      {"type": "file", "url": "https://...", "filename": "doc.pdf"}
    ],
    "timestampMs": 1711100000000,
    "msgId": "msg_abc",
    "wasExplicitlyMentioned": false
  }
}
    |
    v
ZaloBridge._read_loop() -> _emit("message", data)
    |
    v
ZaloUserChannel._on_message(data)
    | 1. Access control: _check_allowlist(sender_id, is_group)
    | 2. Mention gating: _check_group_mention(is_group, meta)
    | 3. Build content_parts:
    |    - TextContent for text
    |    - ImageContent for images (using thumbnail/URL)
    |    - FileContent for files
    |    - VideoContent for videos
    | 4. Build native payload dict
    | 5. Start typing indicator if enabled
    v
self._enqueue(native) -> ChannelManager queue -> Agent processing
```

## Message Flow: Outbound

```
Agent produces response (text + optional media)
    |
    v
BaseChannel.send_message_content(to_handle, message, meta)
    | Extract content_parts from Message
    v
send_content_parts(to_handle, parts, meta)
    | Merge text/refusal parts into single text
    | For each media part: call send_media()
    v
send(to_handle, text, meta)          send_media(to_handle, part, meta)
    |                                     |
    | Stop typing indicator               | Determine media type
    | Chunk text at 2000 chars            | Download URL to temp file if needed
    v                                     v
bridge.send_command(                  bridge.send_command(
  "send_message",                       "send_image" / "send_file" /
  threadId, text, isGroup)              "send_sticker",
    |                                   threadId, filePath, isGroup)
    v                                     |
bridge.mjs:                               v
  Per-thread queue                    bridge.mjs:
  Rate limiter (5 msg/s)               Sharp image metadata (if local)
  api.sendMessage(...)                  Upload to Zalo CDN
    |                                   api.sendMessage({attachment}, ...)
    v                                     |
  Zalo Servers                            v
                                        Zalo Servers
```

## Authentication Flow

```
Channel.start()
    |
    v
Bridge.start() -> spawn Node.js -> wait "ready" event
    |
    v
Try: send_command("login", stateDir) -> sets creds path + login
    |
    +-- SUCCESS: credentials found & valid
    |   -> self._connected = True
    |   -> send_command("start_listener")
    |   -> Channel fully operational
    |
    +-- FAIL: no credentials or expired
        -> Log warning: "QR login required"
        -> Channel started but not authenticated
        -> User triggers QR login (console/HTTP API)
        |
        v
        send_command("login_qr", stateDir)
        -> Bridge generates QR code
        -> Events: qr_generated -> qr_scanned -> GotLoginInfo
        -> Credentials auto-saved to state_dir/credentials.json
        -> send_command("start_listener")
        -> Channel fully operational

--- On Crash / Disconnect ---
Health check detects failure -> _crash_handler()
-> Exponential backoff (2s -> 4s -> 8s -> 30s max)
-> Auto-restart bridge subprocess
-> Auto-login with saved credentials
-> Restart listener
-> Resume normal operation
```

## Bridge Protocol

### Commands (Python -> Node.js)

| Command | Params | Description |
|---------|--------|-------------|
| `login` | `stateDir` | Login with saved credentials (sets credentials path internally) |
| `login_qr` | `stateDir` | QR code login flow (sets credentials path internally) |
| `logout` | -- | Clear credentials, disconnect |
| `start_listener` | -- | Start message listener |
| `stop_listener` | -- | Stop message listener |
| `send_message` | `threadId, text, isGroup` | Send text message |
| `send_image` | `threadId, filePath/url, isGroup, caption?` | Send image |
| `send_file` | `threadId, filePath/url, isGroup` | Send file/video attachment (videos also routed here) |
| `send_sticker` | `threadId, stickerQuery/stickerId, isGroup` | Send sticker |
| `send_typing` | `threadId, isGroup` | Send typing indicator |
| `send_reaction` | `threadId, msgId, emoji, isGroup` | React to message |
| `get_account_info` | -- | Get logged-in user profile |
| `get_friends` | -- | List friends |
| `get_groups` | -- | List groups |
| `check_auth` | -- | Check auth status |
| `ping` | -- | Health check |
| `shutdown` | -- | Graceful shutdown |

### Events (Node.js -> Python)

| Event | Data | Description |
|-------|------|-------------|
| `ready` | `{pid}` | Bridge process started |
| `message` | `{threadId, senderId, content, attachments, ...}` | Incoming message |
| `qr_generated` | `{image, code}` | QR code ready for scanning |
| `qr_scanned` | `{avatar, displayName}` | QR scanned by user |
| `qr_expired` | `{}` | QR code expired |
| `error` | `{message}` | Error occurred |
| `disconnected` | `{code, reason}` | WebSocket disconnected |
| `log` | `{message}` | Debug log from bridge |
| `pong` | `{timestamp}` | Health check response |

### JSON-Line Format

```
Python -> Node: {"cmd": "send_message", "id": "abc123", "threadId": "12345", "text": "Hello", "isGroup": false}
Node -> Python: {"id": "abc123", "ok": true, "data": {"messageId": "msg1"}}
Node -> Python: {"event": "message", "data": {...}}
```

## Concurrency Model

```
Node.js Bridge Concurrency:
+----------------------------------------------+
| Event Loop (single thread)                   |
|                                              |
| Inbound:                                     |
|   zca-js listener -> emit events             | <- non-blocking
|   -> JSON-line to stdout                     |
|                                              |
| Outbound:                                    |
|   stdin command -> handleCommand()           | <- sequential via readline
|   -> rate limiter (token bucket, 5 msg/s)    |
|   -> zca-js API call                         |
|                                              |
| Image processing:                            |
|   sharp (native threads internally)          | <- handled by sharp
+----------------------------------------------+

Python CoPaw Side:
- ChannelManager: per-session queuing (4 workers default)
- Session-level locks prevent concurrent processing of same chat
- ZaloBridge.send_command(): async futures with timeout
- Text pre-chunked at 2000 chars before sending to bridge
```

## Error Handling

| Scenario | Handling |
|----------|----------|
| Bridge process crashes | Auto-restart with exponential backoff (2s->4s->8s->30s max), max 5 attempts |
| Zalo session expires | Detect via auth error, log warning, require QR re-login |
| Zalo rate limit hit | Back off sending, queue messages, retry after delay |
| Bridge startup fails (no Node.js) | Clear error: "Node.js is required for zalouser channel" |
| npm install fails | Clear error with stderr output |
| No credentials on start | Channel starts in "unauthenticated" mode, waits for QR login |
| Message send fails | Log error, call _on_consume_error() to notify user |
| Health check fails (3 consecutive) | Force restart bridge |
| Concurrent listener conflict | Only 1 listener per Zalo account. Detect disconnect, auto-reconnect. |
| Large media upload timeout | Extended timeout for send_image/send_file (60s vs 30s default) |

## Session Resolution

```python
def resolve_session_id(sender_id, channel_meta):
    thread_id = channel_meta.get("thread_id")
    is_group = channel_meta.get("is_group", False)
    if thread_id:
        prefix = "group" if is_group else "dm"
        return f"zalouser:{prefix}:{thread_id}"
    return f"zalouser:dm:{sender_id}"
```

- DMs: one session per conversation partner (`zalouser:dm:{user_id}`)
- Groups: one session per group (`zalouser:group:{group_id}`)

### Request/Handle Resolution

**`get_to_handle_from_request(request)`** — Extracts `thread_id` from `request.channel_meta["thread_id"]`. Falls back to parsing session_id (`zalouser:{type}:{thread_id}` → extracts the third segment). Used by BaseChannel's consume loop to determine where to send replies.

**`to_handle_from_target(user_id, session_id)`** — For cron/proactive dispatch: extracts thread_id from `session_id` by splitting on `:` and taking the third segment. Same pattern as Discord's `to_handle_from_target`.

## Stderr Handling

`ZaloBridge` spawns a `_stderr_reader` asyncio task alongside `_read_loop`. This task reads `self._process.stderr` line by line and pipes each line to `logger.warning("zalouser bridge stderr: %s", line)`. Non-JSON output on stdout (e.g., `console.log` from zca-js internals) is silently skipped by `_read_loop`'s `try/except JSONDecodeError: continue`.

## send_content_parts Override

`ZaloUserChannel` overrides `send_content_parts()` following the Discord channel pattern:

1. Merge all `TextContent` and `RefusalContent` parts into a single text string
2. Call `self.send(to_handle, merged_text, meta)` for the text portion
3. Iterate remaining media parts and call `self.send_media(to_handle, part, meta)` for each

This separation is needed because the bridge has distinct commands for text (`send_message`) vs media (`send_image`, `send_file`, `send_sticker`).

## Media Handling

### Outbound Media (Python Side)

For outbound media in `send_media()`:

1. Determine media type from `part.type` (IMAGE, VIDEO, FILE, AUDIO)
2. Extract URL: `part.image_url`, `part.video_url`, `part.file_url`, etc.
3. For `file://` URLs: resolve to local path using `file_url_to_local_path()` from `copaw.app.channels.utils`
4. For `http(s)://` URLs: download to temp file in `{state_dir}/media/` using `httpx`
5. Send via bridge: `send_image` for images, `send_file` for files/videos/audio
6. Cleanup: temp files deleted after successful send (or on failure after logging)

**Temp file location:** `{state_dir}/media/` (default `~/.copaw/zalouser/media/`)

### Inbound Media (Bridge Side)

zca-js message events carry media in different `data.content` structures. The bridge must detect and extract:

| zca-js Message Type | Detection | Extracted Fields |
|---------------------|-----------|-----------------|
| Plain text | `typeof data.content === "string"` | `content: string` |
| Link/URL share | `data.content.href` exists | `content: title + description + href` |
| Image | `data.content.thumb` or `data.content.hdUrl` exists | `attachments: [{type: "image", url: hdUrl, thumbnailUrl: thumb}]` |
| File | `data.content.params.fileName` exists | `attachments: [{type: "file", url: href, filename: params.fileName}]` |
| GIF/Animation | `data.content.type === "gif"` or animation markers | `attachments: [{type: "image", url: originUrl}]` |
| Sticker | `data.content.id` is sticker ID | `attachments: [{type: "sticker", stickerId: id, url: spriteUrl}]` |
| Video | `data.content.url` with video markers | `attachments: [{type: "video", url}]` |

The bridge's message handler inspects `data.content` type and structure, builds the `attachments` array accordingly, and includes it in the event payload to Python.

### Outbound Media (Bridge Side)

Bridge commands for sending media:

```javascript
// send_image: Uses zca-js sendMessage with image attachment
import sharp from 'sharp';

async function imageMetadataGetter(filePath) {
  const data = await fs.promises.readFile(filePath);
  const metadata = await sharp(data).metadata();
  return { height: metadata.height, width: metadata.width, size: data.length };
}

// Initialize Zalo with imageMetadataGetter
const zalo = new Zalo({ imageMetadataGetter });

// Send image
await api.sendMessage(
  { msg: caption || "", attachment: [filePath] },
  threadId,
  type
);

// send_file: Similar pattern with file attachment
// Note: Videos also routed through send_file. The msg field is empty because
// text content is sent separately via send_message (per send_content_parts pattern).
await api.sendMessage(
  { msg: "", attachment: [filePath] },
  threadId,
  type
);

// send_sticker: Lookup sticker then send
const stickerIds = await api.getStickers(query);
const stickerObj = await api.getStickersDetail(stickerIds[0]);
await api.sendMessageSticker(stickerObj, threadId, type);

// send_typing: Simple API call
await api.sendTypingEvent(threadId, type);

// send_reaction: Map emoji to Zalo reaction, then call API
const icon = reactionMap[emoji] || emoji;
await api.addReaction(icon, { data: { msgId, cliMsgId }, threadId, type });
```

## Text Chunking

Text chunking happens on the **Python side only** in `send()`. The `_chunk_text()` method splits text at ~2000 characters, preferring newline/space boundaries. Each chunk is sent as a separate `send_message` bridge command. The bridge's `send_message` handler sends the text as-is without re-chunking.

## Health Check

1. `ZaloBridge._health_check_loop()` sends a `ping` command every `health_check_interval` seconds (default 30s)
2. Expects `pong` reply within 10 seconds
3. On timeout/failure: increment `_health_fail_count`
4. On success: reset `_health_fail_count` to 0
5. At 3 consecutive failures: call `_crash_handler()` to force-restart the bridge
6. `_crash_handler()` stops the current bridge, waits `backoff_delay`, spawns a new bridge, auto-logins, and restarts the listener
7. Backoff schedule: 2s → 4s → 8s → 16s → 30s (capped), resets on successful restart

## Rate Limiter

The bridge implements a fixed-interval rate limiter (minimum gap between sends):

```javascript
const MAX_RATE = 5;          // messages per second
const MIN_INTERVAL_MS = 200; // 1000/MAX_RATE
let lastSendMs = 0;

async function rateLimitedSend(fn) {
  const now = Date.now();
  const wait = MIN_INTERVAL_MS - (now - lastSendMs);
  if (wait > 0) await new Promise(r => setTimeout(r, wait));
  lastSendMs = Date.now();
  return fn();
}
```

All outbound API calls (`send_message`, `send_image`, `send_file`, `send_sticker`, `send_typing`) pass through `rateLimitedSend()`. Commands that don't hit Zalo's API (`ping`, `check_auth`, `shutdown`) bypass the limiter.

## QR Login Trigger

When the channel starts without saved credentials, it enters "unauthenticated" mode and logs a warning. The user can trigger QR login through:

1. **CoPaw HTTP API**: `POST /api/channels/zalouser/login-qr` — the channel exposes this endpoint. The bridge generates a QR code, emits `qr_generated` event with the QR image data (base64), which the API returns to the caller. The user scans with their Zalo mobile app.
2. **Console logging**: The QR code image is saved to `{state_dir}/qr.png` and the file path is logged so the user can open it manually.
3. **Future**: Could expose a web UI page at `/zalouser/login` showing the QR code.

After successful QR scan, credentials (IMEI, cookie, userAgent, language) are auto-saved to `{state_dir}/credentials.json` and the listener starts automatically.

## Credential Security

- Credentials stored at `{state_dir}/credentials.json` as plaintext JSON
- On Unix: file created with mode 0o600 (owner read/write only)
- On Windows: relies on user-level NTFS permissions
- The credentials file should be excluded from backups and version control
- README warns about credential file security
- `language` field saved during QR login alongside `imei`, `cookie`, `userAgent`

## Windows Compatibility

- Subprocess spawning: use `"npm.cmd"` on Windows, `"npm"` on Unix (detect via `sys.platform`)
- Signal handling in bridge.mjs: `SIGTERM`/`SIGINT` may not work on Windows — use `process.on("exit")` as fallback
- Bridge shutdown: use `process.kill()` as fallback if graceful shutdown via `shutdown` command times out
- Path handling: use `pathlib.Path` throughout (handles `/` vs `\` automatically)

## Dependencies

**Python:** Uses CoPaw's existing dependencies only (no new pip packages).

**Node.js (package.json):**
```json
{
  "name": "copaw-zalouser-bridge",
  "version": "2.0.0",
  "type": "module",
  "dependencies": {
    "zca-js": "~2.1.2",
    "sharp": "~0.33.0"
  }
}
```

Auto-installed via `npm install --production` (or `npm.cmd install --production` on Windows) on first channel start if `node_modules` is missing.

## Testing

### Unit Tests (Python)

- `test_resolve_session_id()` — verify DM vs group session ID formats
- `test_chunk_text()` — verify chunking at 2000 chars, newline/space boundaries, edge cases
- `test_from_config()` — verify config loading with all fields, defaults, missing fields
- `test_from_env()` — verify environment variable loading
- `test_on_message_access_control()` — verify allowlist blocking and group mention gating
- `test_build_agent_request_from_native()` — verify content_parts construction (text + media)
- `test_get_to_handle_from_request()` — verify thread_id extraction from meta
- `test_to_handle_from_target()` — verify session_id parsing for cron dispatch

### Integration Tests

- `test_bridge_start_stop()` — verify bridge subprocess spawns, ready event received, graceful shutdown
- `test_bridge_health_check()` — verify ping/pong cycle, failure counting, restart trigger
- `test_bridge_crash_recovery()` — kill bridge process, verify auto-restart with backoff

### Mock Bridge Tests

- `test_on_message_enqueue()` — mock bridge emitting message event, verify `_enqueue()` called with correct native payload
- `test_send_text_chunked()` — mock bridge `send_command`, verify multiple chunks sent for long text
- `test_send_media_image()` — mock bridge `send_command`, verify `send_image` command sent with correct params

Place tests in `tests/unit/channels/test_zalouser_channel.py` following the pattern of `test_qq_channel.py`.

## Out of Scope

- Voice/video calls
- Message editing/deletion sync
- Read receipts
- Multi-account support
- End-to-end encryption key management
- Custom bot commands (handled by CoPaw's agent layer)
