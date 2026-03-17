# Zalo Personal Channel

This custom channel allows CoPaw to send and receive messages through a **personal Zalo account** (not the official Zalo Business OA API).

## ⚠️ Warning

This is an **unofficial integration** using a reverse-engineered Zalo API. Using this may result in **account suspension or ban**. Use at your own risk.

## Installation

1. Ensure Node.js (v18+) is installed
2. Copy this `zalouser` directory to your CoPaw `custom_channels/` directory
3. Install dependencies: `cd custom_channels/zalouser && npm install`
4. Add configuration to your `config.json` (see below)
5. Run QR login for first-time authentication
6. Restart CoPaw

## Configuration

Add to your `config.json` under `channels`:

```json
"zalouser": {
  "enabled": true,
  "bot_prefix": "",
  "filter_tool_messages": false,
  "filter_thinking": false,
  "dm_policy": "open",
  "group_policy": "open",  
  "allow_from": [],
  "deny_message": "",
  "require_mention": false,
  "state_dir": "~/.copaw/zalouser",
  "show_typing": true
}
```

## First-time Setup (QR Login)

Run the QR login script to authenticate with your Zalo account:

```bash
cd custom_channels/zalouser
node qr_gen_v2.mjs
```

This will generate a QR code that you can scan with your Zalo mobile app. After scanning, **confirm/approve the login on your phone**.

Credentials will be saved to `{state_dir}/credentials.json` and used for subsequent logins.

## Features

- ✅ Personal account support (not Business OA)
- ✅ DM and group messaging
- ✅ Access control (open/allowlist policies)
- ✅ Mention gating for groups
- ✅ Typing indicators
- ✅ Text message chunking (2000 char limit)
- ✅ Friends and groups API

## Architecture

- **Python Channel**: `ZaloUserChannel` extends CoPaw's `BaseChannel`
- **Node.js Bridge**: Uses `zca-js` library to communicate with Zalo servers
- **Communication**: JSON-line protocol over stdin/stdout

## Dependencies

- [`zca-js`](https://github.com/AugusVigworthy/zca-js) - Reverse-engineered Zalo API library

## References

- Based on [OpenClaw's zalouser plugin](https://github.com/openclaw/openclaw/tree/main/extensions/zalouser)
- CoPaw documentation: https://copaw.agentscope.io/docs