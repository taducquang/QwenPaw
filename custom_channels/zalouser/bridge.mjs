/**
 * CoPaw Zalo Personal Channel - Node.js Bridge
 *
 * This bridge runs as a subprocess managed by the Python channel.
 * It communicates via stdin/stdout using JSON-line protocol.
 *
 * Protocol:
 *   Python -> Node: {"cmd": "...", "id": "...", ...params}
 *   Node -> Python: {"id": "...", "ok": true/false, "data": ..., "error": ...}
 *   Node -> Python: {"event": "message", "data": ...}  (unsolicited events)
 */

import { createInterface } from "node:readline";
import { Zalo, ThreadType, Reactions, LoginQRCallbackEventType } from "zca-js";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";

// ─── State ───────────────────────────────────────────────────
let api = null;
let listener = null;
let ownUserId = null;
let credentialsPath = "";

// ─── Helpers ─────────────────────────────────────────────────

function send(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function sendEvent(event, data) {
  send({ event, data });
}

function sendReply(id, ok, data, error) {
  const reply = { id, ok };
  if (data !== undefined) reply.data = data;
  if (error !== undefined) reply.error = error;
  send(reply);
}

function log(msg) {
  sendEvent("log", { message: msg });
}

function toNumberId(value) {
  if (typeof value === "number" && Number.isFinite(value)) {
    return String(Math.trunc(value));
  }
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (trimmed.length > 0) return trimmed.replace(/_\d+$/, "");
  }
  return "";
}

function normalizeMessageContent(content) {
  if (typeof content === "string") return content;
  if (!content || typeof content !== "object") return "";
  const record = content;
  const title = typeof record.title === "string" ? record.title.trim() : "";
  const description = typeof record.description === "string" ? record.description.trim() : "";
  const href = typeof record.href === "string" ? record.href.trim() : "";
  const combined = [title, description, href].filter(Boolean).join("\n").trim();
  if (combined) return combined;
  try { return JSON.stringify(content); } catch { return ""; }
}

function resolveInboundTimestamp(rawTs) {
  if (typeof rawTs === "number" && Number.isFinite(rawTs)) {
    return rawTs > 1_000_000_000_000 ? rawTs : rawTs * 1000;
  }
  const parsed = parseInt(String(rawTs ?? ""), 10);
  if (!Number.isFinite(parsed) || parsed <= 0) return Date.now();
  return parsed > 1_000_000_000_000 ? parsed : parsed * 1000;
}

// ─── Credential Storage ──────────────────────────────────────

function resolveCredentialsPath(stateDir) {
  if (!stateDir) {
    stateDir = path.join(os.homedir(), ".copaw", "zalouser");
  }
  credentialsPath = path.join(stateDir, "credentials.json");
  return credentialsPath;
}

function readCredentials() {
  try {
    if (!credentialsPath || !fs.existsSync(credentialsPath)) return null;
    const raw = fs.readFileSync(credentialsPath, "utf-8");
    const parsed = JSON.parse(raw);
    if (!parsed.imei || !parsed.cookie || !parsed.userAgent) return null;
    return parsed;
  } catch {
    return null;
  }
}

function writeCredentials(creds) {
  const dir = path.dirname(credentialsPath);
  fs.mkdirSync(dir, { recursive: true });
  const data = {
    ...creds,
    createdAt: creds.createdAt || new Date().toISOString(),
    lastUsedAt: new Date().toISOString(),
  };
  fs.writeFileSync(credentialsPath, JSON.stringify(data, null, 2), "utf-8");
}

function clearCredentials() {
  try {
    if (credentialsPath && fs.existsSync(credentialsPath)) {
      fs.unlinkSync(credentialsPath);
      return true;
    }
  } catch {}
  return false;
}

// ─── Zalo API ────────────────────────────────────────────────

async function loginWithCredentials(stateDir) {
  resolveCredentialsPath(stateDir);
  const stored = readCredentials();
  if (!stored) {
    throw new Error("No saved Zalo session found. Please login with QR first.");
  }

  const zalo = new Zalo({ logging: false, selfListen: false });
  api = await zalo.login({
    imei: stored.imei,
    cookie: stored.cookie,
    userAgent: stored.userAgent,
    language: stored.language,
  });

  // Update last used
  writeCredentials(stored);

  try {
    ownUserId = api.getOwnId();
  } catch {
    ownUserId = null;
  }

  return { connected: true, userId: ownUserId };
}

async function loginWithQR(stateDir) {
  resolveCredentialsPath(stateDir);

  return new Promise((resolve, reject) => {
    const zalo = new Zalo({ logging: false, selfListen: false });
    let settled = false;

    const timeout = setTimeout(() => {
      if (!settled) {
        settled = true;
        reject(new Error("QR login timed out (3 minutes)"));
      }
    }, 3 * 60_000);

    zalo.loginQR(
      { qrPath: path.join(path.dirname(credentialsPath), "qr.png") },
      async (event) => {
        if (settled) return;

        switch (event.type) {
          case LoginQRCallbackEventType.QRCodeGenerated: {
            sendEvent("qr_generated", {
              image: event.data?.image || null,
              code: event.data?.code || null,
            });
            break;
          }
          case LoginQRCallbackEventType.QRCodeExpired: {
            sendEvent("qr_expired", {});
            if (event.actions?.retry) event.actions.retry();
            break;
          }
          case LoginQRCallbackEventType.QRCodeScanned: {
            sendEvent("qr_scanned", {
              avatar: event.data?.avatar || null,
              displayName: event.data?.display_name || null,
            });
            break;
          }
          case LoginQRCallbackEventType.QRCodeDeclined: {
            settled = true;
            clearTimeout(timeout);
            reject(new Error("QR code was declined"));
            break;
          }
          case LoginQRCallbackEventType.GotLoginInfo: {
            settled = true;
            clearTimeout(timeout);
            try {
              const creds = {
                imei: event.data.imei,
                cookie: event.data.cookie,
                userAgent: event.data.userAgent,
              };
              writeCredentials(creds);

              // Now login with the credentials
              const zalo2 = new Zalo({ logging: false, selfListen: false });
              api = await zalo2.login({
                imei: creds.imei,
                cookie: creds.cookie,
                userAgent: creds.userAgent,
              });

              try {
                ownUserId = api.getOwnId();
              } catch {
                ownUserId = null;
              }

              resolve({ connected: true, userId: ownUserId });
            } catch (err) {
              reject(err);
            }
            break;
          }
        }
      }
    ).then((apiResult) => {
      if (!settled) {
        settled = true;
        clearTimeout(timeout);
        api = apiResult;
        try { ownUserId = api.getOwnId(); } catch { ownUserId = null; }
        resolve({ connected: true, userId: ownUserId });
      }
    }).catch((err) => {
      if (!settled) {
        settled = true;
        clearTimeout(timeout);
        reject(err);
      }
    });
  });
}

function startListener() {
  if (!api) throw new Error("Not connected to Zalo");
  if (listener) {
    log("Listener already active, stopping old one");
    try { listener.stop(); } catch {}
  }

  listener = api.listener;

  listener.on("message", (message) => {
    try {
      const data = message.data || {};
      const isGroup = message.type === ThreadType.Group;
      const senderId = toNumberId(data.uidFrom);
      const threadId = isGroup
        ? toNumberId(data.idTo)
        : toNumberId(data.uidFrom) || toNumberId(data.idTo);

      if (!threadId || !senderId) return;
      if (message.isSelf) return; // Skip own messages

      const content = normalizeMessageContent(data.content);
      if (!content) return;

      // Extract mention info
      const mentions = Array.isArray(data.mentions) ? data.mentions : [];
      const mentionIds = mentions
        .map(m => toNumberId(m?.uid))
        .filter(Boolean);
      const wasExplicitlyMentioned = ownUserId
        ? mentionIds.includes(ownUserId)
        : false;

      sendEvent("message", {
        threadId,
        isGroup,
        senderId,
        senderName: typeof data.dName === "string" ? data.dName.trim() || null : null,
        groupName: isGroup ? (
          data.groupName || data.gName || data.idToName ||
          data.threadName || data.roomName || null
        ) : null,
        content,
        timestampMs: resolveInboundTimestamp(data.ts),
        msgId: data.msgId || null,
        cliMsgId: data.cliMsgId || null,
        wasExplicitlyMentioned,
        hasAnyMention: mentionIds.length > 0,
      });
    } catch (err) {
      sendEvent("error", { message: `Message parse error: ${err.message}` });
    }
  });

  listener.on("error", (error) => {
    sendEvent("error", { message: `Listener error: ${error?.message || String(error)}` });
  });

  listener.on("closed", (code, reason) => {
    sendEvent("disconnected", { code, reason });
  });

  listener.start({ retryOnClose: true });
  log("Zalo listener started");
}

function stopListener() {
  if (listener) {
    try { listener.stop(); } catch {}
    listener = null;
  }
}

async function sendTextMessage(threadId, text, isGroup = false) {
  if (!api) throw new Error("Not connected to Zalo");
  const type = isGroup ? ThreadType.Group : ThreadType.User;

  // Chunk text at ~2000 chars
  const LIMIT = 2000;
  const chunks = [];
  let rest = text;
  while (rest.length > 0) {
    if (rest.length <= LIMIT) {
      chunks.push(rest);
      break;
    }
    let cutAt = rest.lastIndexOf("\n", LIMIT);
    if (cutAt < LIMIT / 2) cutAt = rest.lastIndexOf(" ", LIMIT);
    if (cutAt < LIMIT / 2) cutAt = LIMIT;
    chunks.push(rest.slice(0, cutAt));
    rest = rest.slice(cutAt).trimStart();
  }

  let lastMsgId = null;
  for (const chunk of chunks) {
    const result = await api.sendMessage(chunk, threadId, type);
    const msgId =
      result?.msgId ||
      result?.message?.msgId ||
      result?.attachment?.[0]?.msgId ||
      null;
    if (msgId) lastMsgId = String(msgId);
  }

  return { ok: true, messageId: lastMsgId };
}

async function sendTyping(threadId, isGroup = false) {
  if (!api) throw new Error("Not connected to Zalo");
  try {
    const type = isGroup ? ThreadType.Group : ThreadType.User;
    await api.sendTypingEvent(threadId, type);
    return { ok: true };
  } catch (err) {
    return { ok: false, error: err.message };
  }
}

async function sendReaction(threadId, msgId, cliMsgId, emoji, isGroup = false) {
  if (!api) throw new Error("Not connected to Zalo");
  const type = isGroup ? ThreadType.Group : ThreadType.User;
  // Map common emoji names to Zalo reaction IDs
  const reactionMap = {
    "heart": Reactions.HEART,
    "❤️": Reactions.HEART,
    "like": Reactions.LIKE,
    "👍": Reactions.LIKE,
    "haha": Reactions.HAHA,
    "😂": Reactions.HAHA,
    "wow": Reactions.WOW,
    "😮": Reactions.WOW,
    "cry": Reactions.CRY,
    "😢": Reactions.CRY,
    "angry": Reactions.ANGRY,
    "😡": Reactions.ANGRY,
  };
  const icon = reactionMap[emoji.toLowerCase()] || emoji;
  await api.addReaction(icon, {
    data: { msgId, cliMsgId },
    threadId,
    type,
  });
  return { ok: true };
}

async function getAccountInfo() {
  if (!api) throw new Error("Not connected to Zalo");
  const info = await api.fetchAccountInfo();
  const user = info?.profile || info;
  return {
    userId: String(user?.userId || ownUserId || ""),
    displayName: user?.displayName || user?.zaloName || "",
    avatar: user?.avatar || "",
  };
}

async function getFriends() {
  if (!api) throw new Error("Not connected to Zalo");
  const friends = await api.getAllFriends();
  return friends.map(f => ({
    userId: String(f.userId),
    displayName: f.displayName || f.zaloName || f.username || String(f.userId),
    avatar: f.avatar || null,
  }));
}

async function getGroups() {
  if (!api) throw new Error("Not connected to Zalo");
  const allGroups = await api.getAllGroups();
  const ids = Object.keys(allGroups.gridVerMap || {});
  if (ids.length === 0) return [];

  // Fetch details in chunks
  const groups = [];
  for (let i = 0; i < ids.length; i += 80) {
    const chunk = ids.slice(i, i + 80);
    const response = await api.getGroupInfo(chunk);
    const map = response.gridInfoMap || {};
    for (const [gid, info] of Object.entries(map)) {
      groups.push({
        groupId: String(gid),
        name: info.name?.trim() || String(gid),
        memberCount: info.totalMember || null,
      });
    }
  }
  return groups;
}

// ─── Command Handler ─────────────────────────────────────────

async function handleCommand(cmd) {
  const { id } = cmd;

  try {
    switch (cmd.cmd) {
      case "init": {
        resolveCredentialsPath(cmd.stateDir || null);
        sendReply(id, true, { ready: true });
        break;
      }

      case "login": {
        const result = await loginWithCredentials(cmd.stateDir);
        sendReply(id, true, result);
        break;
      }

      case "login_qr": {
        const result = await loginWithQR(cmd.stateDir);
        sendReply(id, true, result);
        break;
      }

      case "logout": {
        stopListener();
        clearCredentials();
        api = null;
        ownUserId = null;
        sendReply(id, true, { loggedOut: true });
        break;
      }

      case "start_listener": {
        startListener();
        sendReply(id, true, { listening: true });
        break;
      }

      case "stop_listener": {
        stopListener();
        sendReply(id, true, { stopped: true });
        break;
      }

      case "send_message": {
        const result = await sendTextMessage(
          cmd.threadId,
          cmd.text,
          cmd.isGroup || false,
        );
        sendReply(id, true, result);
        break;
      }

      case "send_typing": {
        const result = await sendTyping(cmd.threadId, cmd.isGroup || false);
        sendReply(id, true, result);
        break;
      }

      case "send_reaction": {
        const result = await sendReaction(
          cmd.threadId,
          cmd.msgId,
          cmd.cliMsgId,
          cmd.emoji,
          cmd.isGroup || false,
        );
        sendReply(id, true, result);
        break;
      }

      case "get_account_info": {
        const info = await getAccountInfo();
        sendReply(id, true, info);
        break;
      }

      case "get_friends": {
        const friends = await getFriends();
        sendReply(id, true, { friends });
        break;
      }

      case "get_groups": {
        const groups = await getGroups();
        sendReply(id, true, { groups });
        break;
      }

      case "check_auth": {
        const hasCredentials = readCredentials() !== null;
        if (!hasCredentials) {
          sendReply(id, true, { authenticated: false, message: "No credentials" });
          break;
        }
        try {
          if (!api) await loginWithCredentials(cmd.stateDir);
          const info = await api.fetchAccountInfo();
          sendReply(id, true, {
            authenticated: true,
            message: "Connected",
            userId: ownUserId,
          });
        } catch (err) {
          sendReply(id, true, {
            authenticated: false,
            message: err.message,
          });
        }
        break;
      }

      case "ping": {
        sendReply(id, true, { pong: true, timestamp: Date.now() });
        break;
      }

      case "shutdown": {
        stopListener();
        sendReply(id, true, { shutdown: true });
        setTimeout(() => process.exit(0), 100);
        break;
      }

      default:
        sendReply(id, false, null, `Unknown command: ${cmd.cmd}`);
    }
  } catch (err) {
    sendReply(id, false, null, err.message || String(err));
  }
}

// ─── Main ────────────────────────────────────────────────────

const rl = createInterface({ input: process.stdin, terminal: false });

rl.on("line", (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;
  try {
    const cmd = JSON.parse(trimmed);
    handleCommand(cmd).catch((err) => {
      sendReply(cmd.id, false, null, err.message || String(err));
    });
  } catch (err) {
    sendEvent("error", { message: `Invalid JSON: ${err.message}` });
  }
});

rl.on("close", () => {
  stopListener();
  process.exit(0);
});

process.on("SIGTERM", () => {
  stopListener();
  process.exit(0);
});

process.on("SIGINT", () => {
  stopListener();
  process.exit(0);
});

// Signal ready
sendEvent("ready", { pid: process.pid });
