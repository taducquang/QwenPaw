# -*- coding: utf-8 -*-
"""
Zalo Personal Channel for CoPaw.

Uses a Node.js bridge subprocess (zca-js) to automate a personal Zalo account.
Communication happens via stdin/stdout JSON-line protocol.

WARNING: This is an unofficial integration. Using Zalo automation may
result in account suspension or ban. Use at your own risk.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agentscope_runtime.engine.schemas.agent_schemas import (
    TextContent,
    ContentType,
)

try:
    from copaw.app.channels.base import (
        BaseChannel,
        OnReplySent,
        ProcessHandler,
        OutgoingContentPart,
    )
except ImportError:
    from copaw.app.channels.base import (
        BaseChannel,
        OnReplySent,
        ProcessHandler,
    )

    OutgoingContentPart = Any

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────
ZALO_TEXT_LIMIT = 2000
_BRIDGE_DIR = Path(__file__).parent
_BRIDGE_SCRIPT = _BRIDGE_DIR / "bridge.mjs"
_DEFAULT_STATE_DIR = Path("~/.copaw/zalouser").expanduser()
_TYPING_INTERVAL_S = 4
_TYPING_TIMEOUT_S = 180


class ZaloBridge:
    """Manage the Node.js bridge subprocess."""

    def __init__(self, state_dir: str = ""):
        self._state_dir = (
            Path(state_dir).expanduser() if state_dir else _DEFAULT_STATE_DIR
        )
        self._process: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._pending: Dict[str, asyncio.Future] = {}
        self._event_handlers: Dict[str, List[Any]] = {}
        self._ready = asyncio.Event()
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running and self._process is not None

    def on(self, event: str, handler):
        """Register event handler."""
        self._event_handlers.setdefault(event, []).append(handler)

    def off(self, event: str, handler):
        """Remove event handler."""
        handlers = self._event_handlers.get(event, [])
        if handler in handlers:
            handlers.remove(handler)

    def _emit(self, event: str, data: Any):
        """Emit event to handlers."""
        for handler in self._event_handlers.get(event, []):
            try:
                result = handler(data)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:
                logger.exception("zalouser bridge event handler error: %s", event)

    async def start(self):
        """Start the Node.js bridge subprocess."""
        if self._running:
            return

        self._state_dir.mkdir(parents=True, exist_ok=True)

        # Check bridge script exists
        if not _BRIDGE_SCRIPT.exists():
            raise FileNotFoundError(
                f"Bridge script not found: {_BRIDGE_SCRIPT}"
            )

        # Check node_modules
        node_modules = _BRIDGE_DIR / "node_modules"
        if not node_modules.exists():
            logger.info("zalouser: installing npm dependencies...")
            proc = await asyncio.create_subprocess_exec(
                "npm",
                "install",
                "--production",
                cwd=str(_BRIDGE_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(
                    f"npm install failed: {stderr.decode()}"
                )

        self._process = await asyncio.create_subprocess_exec(
            "node",
            str(_BRIDGE_SCRIPT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(_BRIDGE_DIR),
        )

        self._running = True
        self._reader_task = asyncio.create_task(self._read_loop())

        # Wait for ready signal
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=10)
        except asyncio.TimeoutError:
            logger.error("zalouser: bridge did not become ready in 10s")
            await self.stop()
            raise RuntimeError("Bridge startup timeout")

        logger.info("zalouser: bridge started (pid=%s)", self._process.pid)

    async def stop(self):
        """Stop the bridge subprocess."""
        self._running = False

        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None

        if self._process:
            try:
                self._process.stdin.write(
                    json.dumps({"cmd": "shutdown", "id": "shutdown"}).encode()
                    + b"\n"
                )
                await self._process.stdin.drain()
            except Exception:
                pass

            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except (asyncio.TimeoutError, Exception):
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None

        # Cancel pending futures
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        self._ready.clear()

    async def _read_loop(self):
        """Read JSON lines from bridge stdout."""
        try:
            while self._running and self._process:
                line = await self._process.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode().strip())
                except json.JSONDecodeError:
                    continue

                # Handle event
                if "event" in msg:
                    event = msg["event"]
                    data = msg.get("data", {})
                    if event == "ready":
                        self._ready.set()
                    self._emit(event, data)
                    continue

                # Handle command reply
                msg_id = msg.get("id")
                if msg_id and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if not fut.done():
                        fut.set_result(msg)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("zalouser: bridge read loop error")
        finally:
            self._running = False

    async def send_command(
        self, cmd: str, timeout: float = 30, **kwargs
    ) -> Dict[str, Any]:
        """Send a command to the bridge and wait for reply."""
        if not self._process or not self._running:
            raise RuntimeError("Bridge not running")

        msg_id = str(uuid.uuid4())[:8]
        payload = {"cmd": cmd, "id": msg_id, **kwargs}

        fut = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut

        try:
            self._process.stdin.write(
                json.dumps(payload).encode() + b"\n"
            )
            await self._process.stdin.drain()
        except Exception as e:
            self._pending.pop(msg_id, None)
            raise RuntimeError(f"Failed to send command: {e}")

        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise TimeoutError(f"Command {cmd} timed out after {timeout}s")

        if not result.get("ok"):
            raise RuntimeError(
                result.get("error", f"Command {cmd} failed")
            )

        return result.get("data", {})


class ZaloUserChannel(BaseChannel):
    """
    Zalo Personal channel for CoPaw.

    Uses zca-js via a Node.js bridge subprocess to automate a personal
    Zalo account. Receives and sends messages through the Zalo platform.

    WARNING: Unofficial integration - may result in account suspension.
    """

    channel = "zalouser"
    uses_manager_queue = True

    def __init__(
        self,
        process: ProcessHandler,
        enabled: bool = False,
        state_dir: str = "",
        bot_prefix: str = "",
        show_typing: bool = True,
        on_reply_sent: OnReplySent = None,
        show_tool_details: bool = True,
        filter_tool_messages: bool = False,
        filter_thinking: bool = False,
        dm_policy: str = "open",
        group_policy: str = "open",
        allow_from: Optional[list] = None,
        deny_message: str = "",
        require_mention: bool = False,
    ):
        super().__init__(
            process,
            on_reply_sent=on_reply_sent,
            show_tool_details=show_tool_details,
            filter_tool_messages=filter_tool_messages,
            filter_thinking=filter_thinking,
            dm_policy=dm_policy,
            group_policy=group_policy,
            allow_from=allow_from,
            deny_message=deny_message,
            require_mention=require_mention,
        )
        self.enabled = enabled
        self._state_dir = state_dir
        self.bot_prefix = bot_prefix
        self._show_typing = show_typing
        self._bridge = ZaloBridge(state_dir=state_dir)
        self._typing_tasks: Dict[str, asyncio.Task] = {}
        self._connected = False

    @classmethod
    def from_env(
        cls,
        process: ProcessHandler,
        on_reply_sent: OnReplySent = None,
    ) -> "ZaloUserChannel":
        allow_from_env = os.getenv("ZALOUSER_ALLOW_FROM", "")
        allow_from = (
            [s.strip() for s in allow_from_env.split(",") if s.strip()]
            if allow_from_env
            else []
        )
        return cls(
            process=process,
            enabled=os.getenv("ZALOUSER_CHANNEL_ENABLED", "0") == "1",
            state_dir=os.getenv("ZALOUSER_STATE_DIR", ""),
            bot_prefix=os.getenv("ZALOUSER_BOT_PREFIX", ""),
            show_typing=os.getenv("ZALOUSER_SHOW_TYPING", "1") == "1",
            on_reply_sent=on_reply_sent,
            dm_policy=os.getenv("ZALOUSER_DM_POLICY", "open"),
            group_policy=os.getenv("ZALOUSER_GROUP_POLICY", "open"),
            allow_from=allow_from,
            deny_message=os.getenv("ZALOUSER_DENY_MESSAGE", ""),
            require_mention=os.getenv(
                "ZALOUSER_REQUIRE_MENTION", "0"
            ) == "1",
        )

    @classmethod
    def from_config(
        cls,
        process: ProcessHandler,
        config: Any,
        on_reply_sent: OnReplySent = None,
        show_tool_details: bool = True,
        filter_tool_messages: bool = False,
        filter_thinking: bool = False,
    ) -> "ZaloUserChannel":
        if isinstance(config, dict):
            c = config
        else:
            c = (
                config.model_dump()
                if hasattr(config, "model_dump")
                else vars(config)
            )

        def _get(key: str, default=""):
            val = c.get(key, default)
            return (val or "").strip() if isinstance(val, str) else val

        show_typing = c.get("show_typing")
        if show_typing is None:
            show_typing = True

        return cls(
            process=process,
            enabled=bool(c.get("enabled", False)),
            state_dir=_get("state_dir"),
            bot_prefix=_get("bot_prefix"),
            show_typing=show_typing,
            on_reply_sent=on_reply_sent,
            show_tool_details=show_tool_details,
            filter_tool_messages=filter_tool_messages,
            filter_thinking=filter_thinking,
            dm_policy=c.get("dm_policy") or "open",
            group_policy=c.get("group_policy") or "open",
            allow_from=c.get("allow_from") or [],
            deny_message=c.get("deny_message") or "",
            require_mention=c.get("require_mention", False),
        )

    def _on_message(self, data: dict):
        """Handle inbound message from Zalo bridge."""
        thread_id = data.get("threadId", "")
        is_group = data.get("isGroup", False)
        sender_id = data.get("senderId", "")
        sender_name = data.get("senderName") or ""
        group_name = data.get("groupName") or ""
        content = data.get("content", "")
        timestamp_ms = data.get("timestampMs", 0)
        msg_id = data.get("msgId")
        cli_msg_id = data.get("cliMsgId")
        was_mentioned = data.get("wasExplicitlyMentioned", False)
        has_any_mention = data.get("hasAnyMention", False)

        if not content or not sender_id:
            return

        # Access control
        allowed, error_msg = self._check_allowlist(sender_id, is_group)
        if not allowed:
            logger.info(
                "zalouser allowlist blocked: sender=%s is_group=%s",
                sender_id,
                is_group,
            )
            # Send rejection message
            asyncio.create_task(
                self._send_rejection(thread_id, error_msg, is_group)
            )
            return

        # Mention gating for groups
        meta = {
            "thread_id": thread_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "is_group": is_group,
            "group_name": group_name,
            "timestamp_ms": timestamp_ms,
            "msg_id": msg_id,
            "cli_msg_id": cli_msg_id,
            "bot_mentioned": was_mentioned,
            "has_any_mention": has_any_mention,
        }

        if not self._check_group_mention(is_group, meta):
            return

        content_parts = [TextContent(type=ContentType.TEXT, text=content)]

        native = {
            "channel_id": self.channel,
            "sender_id": sender_id,
            "content_parts": content_parts,
            "meta": meta,
        }

        if self._enqueue is not None:
            if self._show_typing:
                self._start_typing(thread_id, is_group)
            self._enqueue(native)
        else:
            logger.warning("zalouser: _enqueue not set, message dropped")

    async def _send_rejection(
        self, thread_id: str, message: str, is_group: bool
    ):
        """Send rejection message to unauthorized user."""
        try:
            await self._bridge.send_command(
                "send_message",
                threadId=thread_id,
                text=message,
                isGroup=is_group,
            )
        except Exception:
            logger.debug(
                "zalouser: failed to send rejection to %s", thread_id
            )

    def _start_typing(self, thread_id: str, is_group: bool = False):
        """Start typing indicator loop."""
        if not self._show_typing:
            return
        self._stop_typing(thread_id)
        self._typing_tasks[thread_id] = asyncio.create_task(
            self._typing_loop(thread_id, is_group)
        )

    def _stop_typing(self, thread_id: str):
        """Stop typing indicator."""
        task = self._typing_tasks.pop(thread_id, None)
        if task and not task.done():
            task.cancel()

    async def _typing_loop(
        self, thread_id: str, is_group: bool = False
    ):
        """Send typing events every few seconds."""
        try:
            deadline = asyncio.get_event_loop().time() + _TYPING_TIMEOUT_S
            while self._bridge.is_running:
                try:
                    await self._bridge.send_command(
                        "send_typing",
                        threadId=thread_id,
                        isGroup=is_group,
                        timeout=5,
                    )
                except Exception:
                    pass
                await asyncio.sleep(_TYPING_INTERVAL_S)
                if asyncio.get_event_loop().time() >= deadline:
                    break
        except asyncio.CancelledError:
            pass
        finally:
            if self._typing_tasks.get(thread_id) is asyncio.current_task():
                self._typing_tasks.pop(thread_id, None)

    def _chunk_text(self, text: str) -> List[str]:
        """Split text into chunks under Zalo's message length limit."""
        if not text or len(text) <= ZALO_TEXT_LIMIT:
            return [text] if text else []
        chunks = []
        rest = text
        while rest:
            if len(rest) <= ZALO_TEXT_LIMIT:
                chunks.append(rest)
                break
            chunk = rest[:ZALO_TEXT_LIMIT]
            last_nl = chunk.rfind("\n")
            if last_nl > ZALO_TEXT_LIMIT // 2:
                chunk = chunk[: last_nl + 1]
            else:
                last_space = chunk.rfind(" ")
                if last_space > ZALO_TEXT_LIMIT // 2:
                    chunk = chunk[: last_space + 1]
            chunks.append(chunk)
            rest = rest[len(chunk) :].lstrip("\n ")
        return chunks

    async def send(
        self,
        to_handle: str,
        text: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Send text message to a Zalo thread."""
        if not self.enabled or not self._bridge.is_running:
            return

        meta = meta or {}
        thread_id = meta.get("thread_id") or to_handle
        is_group = meta.get("is_group", False)

        if not thread_id:
            logger.warning(
                "zalouser send: no thread_id in to_handle or meta"
            )
            return

        self._stop_typing(thread_id)

        try:
            await self._bridge.send_command(
                "send_message",
                threadId=thread_id,
                text=text,
                isGroup=is_group,
                timeout=30,
            )
        except Exception:
            logger.exception("zalouser: send_message failed")

    async def send_media(
        self,
        to_handle: str,
        part: OutgoingContentPart,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Send a media part. Currently text-only fallback."""
        # zca-js supports uploadAttachment but it's complex.
        # For now, send media URLs as text links.
        meta = meta or {}
        part_type = getattr(part, "type", None)
        url = None

        if part_type == ContentType.IMAGE:
            url = getattr(part, "image_url", None)
        elif part_type == ContentType.VIDEO:
            url = getattr(part, "video_url", None)
        elif part_type == ContentType.FILE:
            url = getattr(part, "file_url", None) or getattr(
                part, "file_id", None
            )

        if url:
            await self.send(to_handle, f"[Media: {url}]", meta)

    async def start(self) -> None:
        """Start the Zalo channel."""
        if not self.enabled:
            logger.debug("zalouser: channel disabled (enabled=false)")
            return

        try:
            # Start bridge
            await self._bridge.start()

            # Register message handler
            self._bridge.on("message", self._on_message)
            self._bridge.on("error", self._on_bridge_error)
            self._bridge.on("disconnected", self._on_bridge_disconnect)

            # Initialize with state dir
            await self._bridge.send_command(
                "init", stateDir=str(self._state_dir)
            )

            # Try to login with saved credentials
            try:
                result = await self._bridge.send_command(
                    "login", stateDir=str(self._state_dir), timeout=20
                )
                self._connected = True
                logger.info(
                    "zalouser: logged in (userId=%s)",
                    result.get("userId", "unknown"),
                )

                # Start listener
                await self._bridge.send_command("start_listener")
                logger.info("zalouser: channel started and listening")
            except Exception as e:
                logger.warning(
                    "zalouser: no saved session, QR login required. "
                    "Error: %s",
                    e,
                )
                logger.info(
                    "zalouser: channel started but not authenticated. "
                    "Use the login command to authenticate."
                )

        except Exception:
            logger.exception("zalouser: failed to start channel")

    async def stop(self) -> None:
        """Stop the Zalo channel."""
        if not self.enabled:
            return

        for tid in list(self._typing_tasks):
            self._stop_typing(tid)

        await self._bridge.stop()
        self._connected = False
        logger.info("zalouser: channel stopped")

    def _on_bridge_error(self, data: dict):
        """Handle bridge error events."""
        msg = data.get("message", "Unknown error")
        logger.error("zalouser bridge error: %s", msg)

    def _on_bridge_disconnect(self, data: dict):
        """Handle bridge disconnect events."""
        code = data.get("code", -1)
        reason = data.get("reason", "unknown")
        logger.warning(
            "zalouser: disconnected (code=%s, reason=%s)", code, reason
        )
        self._connected = False

    def resolve_session_id(
        self,
        sender_id: str,
        channel_meta: Optional[dict] = None,
    ) -> str:
        """Session by thread_id (one session per chat/group)."""
        meta = channel_meta or {}
        thread_id = meta.get("thread_id")
        is_group = meta.get("is_group", False)
        if thread_id:
            prefix = "group" if is_group else "dm"
            return f"zalouser:{prefix}:{thread_id}"
        return f"zalouser:dm:{sender_id}"

    def get_to_handle_from_request(self, request: Any) -> str:
        """Send target is thread_id from meta."""
        meta = getattr(request, "channel_meta", None) or {}
        thread_id = meta.get("thread_id")
        if thread_id:
            return str(thread_id)
        sid = getattr(request, "session_id", "")
        if sid.startswith("zalouser:"):
            parts = sid.split(":", 2)
            if len(parts) >= 3:
                return parts[2]
            return parts[-1]
        return getattr(request, "user_id", "") or ""

    def build_agent_request_from_native(
        self, native_payload: Any
    ) -> Any:
        """Build AgentRequest from Zalo native dict."""
        payload = (
            native_payload if isinstance(native_payload, dict) else {}
        )
        channel_id = payload.get("channel_id") or self.channel
        sender_id = payload.get("sender_id") or ""
        content_parts = payload.get("content_parts") or []
        meta = payload.get("meta") or {}
        session_id = self.resolve_session_id(sender_id, meta)
        user_id = str(meta.get("sender_id") or sender_id)
        request = self.build_agent_request_from_user_content(
            channel_id=channel_id,
            sender_id=sender_id,
            session_id=session_id,
            content_parts=content_parts,
            channel_meta=meta,
        )
        request.user_id = user_id
        request.channel_meta = meta
        return request

    def to_handle_from_target(
        self, *, user_id: str, session_id: str
    ) -> str:
        """Cron dispatch: extract thread_id from session_id."""
        if session_id.startswith("zalouser:"):
            parts = session_id.split(":", 2)
            if len(parts) >= 3:
                return parts[2]
            return parts[-1]
        return user_id
