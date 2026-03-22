# Zalo Personal Channel for CoPaw

Connects CoPaw to a personal Zalo account using [zca-js](https://github.com/RFS-ADRENO/zca-js).

**WARNING:** This is an unofficial integration using a reverse-engineered API.
Using Zalo automation may result in account suspension or ban. Use at your own risk.

## Requirements

- Node.js 18+ (for the bridge subprocess)
- npm (auto-installs dependencies on first start)

## Setup

1. Enable the channel in `~/.copaw/config.json`:

```json
{
  "channels": {
    "zalouser": {
      "enabled": true,
      "state_dir": "~/.copaw/zalouser",
      "show_typing": true,
      "dm_policy": "open",
      "group_policy": "open"
    }
  }
}
```

2. Start CoPaw. On first run, npm dependencies will auto-install.

3. Login via QR code (first time):
   - The channel will log a warning: "QR login required"
   - A QR code image is saved to `~/.copaw/zalouser/qr.png`
   - Open the QR image and scan with your Zalo mobile app
   - Credentials are auto-saved for future sessions

## Configuration

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Enable the channel |
| `state_dir` | string | `~/.copaw/zalouser` | Credential storage |
| `bot_prefix` | string | `""` | Prefix for bot replies |
| `show_typing` | bool | `true` | Show typing indicators |
| `dm_policy` | string | `"open"` | DM access: "open" or "allowlist" |
| `group_policy` | string | `"open"` | Group access: "open" or "allowlist" |
| `allow_from` | list | `[]` | Allowed sender IDs |
| `deny_message` | string | `""` | Rejection message |
| `require_mention` | bool | `false` | Require @mention in groups |
| `health_check_interval` | int | `30` | Bridge health ping interval (seconds) |
| `max_restart_attempts` | int | `5` | Max auto-restart attempts on crash |

## Security

- Credentials are stored as JSON at `{state_dir}/credentials.json` with mode 0600
- Ensure this file has restricted permissions
- Do not commit credentials to version control
- Add `credentials.json` to `.gitignore`

## Supported Features

- Text messages (DM and group)
- Image sending and receiving
- File sending and receiving
- Sticker sending
- Typing indicators
- Reactions
- Access control (allowlist, mention gating)
- Auto-reconnect on disconnect
- Crash recovery with exponential backoff
- Health checks (configurable ping interval)
