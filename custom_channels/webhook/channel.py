# -*- coding: utf-8 -*-
"""
Webhook Channel for CoPaw.

Inbound HTTP webhook that lets external systems trigger agent processing.
Runs an embedded aiohttp server on a configurable port.

Spec: docs/superpowers/specs/2026-03-22-webhook-channel-design.md
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from aiohttp import web

from agentscope_runtime.engine.schemas.agent_schemas import (
    ContentType,
    TextContent,
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

# Validation
_SESSION_KEY_RE = re.compile(r"^[a-zA-Z0-9:_\-]{1,200}$")


class WebhookChannel(BaseChannel):
    """Inbound webhook channel for CoPaw.

    External services POST JSON to trigger agent processing.
    Supports sync (wait for response) and async (fire-and-forget) modes.
    """

    channel = "webhook"
    uses_manager_queue = True

    def __init__(
        self,
        process: ProcessHandler,
        enabled: bool = False,
        token: str = "",
        port: int = 18790,
        host: str = "127.0.0.1",
        sync_timeout: int = 60,
        max_request_size_bytes: int = 1_048_576,
        rate_limit_per_minute: int = 60,
        trust_proxy: bool = False,
        max_concurrent_sync: int = 10,
        on_reply_sent: OnReplySent = None,
        show_tool_details: bool = True,
        filter_tool_messages: bool = False,
        filter_thinking: bool = False,
        dm_policy: str = "open",
        group_policy: str = "open",
        allow_from: Optional[list] = None,
        deny_message: str = "",
        **kwargs: Any,
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
        )
        self.enabled = enabled
        self._token = token
        self._port = port
        self._host = host
        self._sync_timeout = sync_timeout
        self._max_request_size = max_request_size_bytes
        self._rate_limit_per_minute = rate_limit_per_minute
        self._trust_proxy = trust_proxy
        self._max_concurrent_sync = max_concurrent_sync

        # Runtime state (initialized in start())
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._start_time: float = 0.0
        self._pending_responses: Dict[str, asyncio.Future] = {}
        self._rate_counters: Dict[str, tuple] = {}  # ip -> (count, window_start)

    @classmethod
    def from_env(
        cls,
        process: ProcessHandler,
        on_reply_sent: OnReplySent = None,
    ) -> "WebhookChannel":
        return cls(
            process=process,
            enabled=os.getenv("WEBHOOK_CHANNEL_ENABLED", "0") == "1",
            token=os.getenv("WEBHOOK_TOKEN", ""),
            port=int(os.getenv("WEBHOOK_PORT", "18790")),
            host=os.getenv("WEBHOOK_HOST", "127.0.0.1"),
            sync_timeout=int(os.getenv("WEBHOOK_SYNC_TIMEOUT", "60")),
            on_reply_sent=on_reply_sent,
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
    ) -> "WebhookChannel":
        if isinstance(config, dict):
            c = config
        else:
            c = config.model_dump() if hasattr(config, "model_dump") else vars(config)
        return cls(
            process=process,
            enabled=bool(c.get("enabled", False)),
            token=c.get("token", ""),
            port=int(c.get("port", 18790)),
            host=c.get("host", "127.0.0.1"),
            sync_timeout=int(c.get("sync_timeout", 60)),
            max_request_size_bytes=int(c.get("max_request_size_bytes", 1_048_576)),
            rate_limit_per_minute=int(c.get("rate_limit_per_minute", 60)),
            trust_proxy=bool(c.get("trust_proxy", False)),
            max_concurrent_sync=int(c.get("max_concurrent_sync", 10)),
            on_reply_sent=on_reply_sent,
            show_tool_details=show_tool_details,
            filter_tool_messages=filter_tool_messages,
            filter_thinking=filter_thinking,
            dm_policy=c.get("dm_policy") or "open",
            group_policy=c.get("group_policy") or "open",
            allow_from=c.get("allow_from") or [],
            deny_message=c.get("deny_message") or "",
        )

    # ----- Session management -----

    def resolve_session_id(
        self,
        sender_id: str,
        channel_meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        meta = channel_meta or {}
        session_key = meta.get("session_key")
        if session_key:
            return f"webhook:{session_key}"
        return f"webhook:{sender_id}"

    def get_to_handle_from_request(self, request: Any) -> str:
        meta = getattr(request, "channel_meta", None) or {}
        return meta.get("sender_id") or getattr(request, "user_id", "") or "webhook"

    def build_agent_request_from_native(self, native_payload: Any) -> Any:
        payload = native_payload if isinstance(native_payload, dict) else {}
        channel_id = payload.get("channel_id") or self.channel
        sender_id = payload.get("sender_id") or "webhook"
        content_parts = payload.get("content_parts") or []
        meta = payload.get("meta") or {}
        session_id = self.resolve_session_id(sender_id, meta)
        request = self.build_agent_request_from_user_content(
            channel_id=channel_id,
            sender_id=sender_id,
            session_id=session_id,
            content_parts=content_parts,
            channel_meta=meta,
        )
        request.channel_meta = meta
        return request

    def to_handle_from_target(self, *, user_id: str, session_id: str) -> str:
        if session_id.startswith("webhook:"):
            return session_id.split(":", 1)[1]
        return user_id

    # ----- Auth & rate limiting -----

    def _validate_token(self, request: web.Request) -> Optional[web.Response]:
        """Validate bearer token. Returns error response or None if valid."""
        # Reject query string tokens
        if request.query_string and "token" in request.query_string.lower():
            return web.json_response(
                {"error": "Token must not be sent via query string"},
                status=400,
            )
        token = None
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
        if not token:
            token = request.headers.get("X-CoPaw-Token")
        if not token or not hmac.compare_digest(token, self._token):
            return web.json_response({"error": "Unauthorized"}, status=401)
        return None

    def _get_client_ip(self, request: web.Request) -> str:
        """Get client IP, respecting trust_proxy setting."""
        if self._trust_proxy:
            forwarded = request.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        peername = request.transport.get_extra_info("peername")
        return peername[0] if peername else "unknown"

    def _check_rate_limit(self, client_ip: str) -> Optional[web.Response]:
        """Fixed-window rate limiter. Returns 429 response or None."""
        now = time.monotonic()
        window_seconds = 60.0
        entry = self._rate_counters.get(client_ip)
        if entry is None or (now - entry[1]) >= window_seconds:
            self._rate_counters[client_ip] = (1, now)
            return None
        count, window_start = entry
        if count >= self._rate_limit_per_minute:
            retry_after = int(window_seconds - (now - window_start)) + 1
            return web.json_response(
                {"error": "Rate limit exceeded"},
                status=429,
                headers={"Retry-After": str(max(1, retry_after))},
            )
        self._rate_counters[client_ip] = (count + 1, window_start)
        return None

    # ----- HTTP handlers -----

    async def _handle_health(self, request: web.Request) -> web.Response:
        uptime = time.monotonic() - self._start_time if self._start_time else 0
        return web.json_response({
            "status": "ok",
            "channel": "webhook",
            "uptime_seconds": round(uptime),
        })

    async def _handle_message(self, request: web.Request) -> web.Response:
        """POST /hooks/message — enqueue message for agent processing."""
        # Auth
        auth_err = self._validate_token(request)
        if auth_err:
            return auth_err

        # Rate limit
        client_ip = self._get_client_ip(request)
        rate_err = self._check_rate_limit(client_ip)
        if rate_err:
            return rate_err

        # Parse body
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": "Malformed JSON"}, status=400,
            )

        message = (body.get("message") or "").strip()
        if not message:
            return web.json_response(
                {"error": "Missing or empty 'message' field"}, status=400,
            )

        sender_id = (body.get("sender_id") or "webhook").strip()
        session_key = (body.get("session_key") or "").strip()
        name = (body.get("name") or "").strip()
        sync = bool(body.get("sync", False))
        timeout_seconds = min(
            int(body.get("timeout_seconds", self._sync_timeout)),
            self._sync_timeout,
        )

        # Validate session_key
        if session_key and not _SESSION_KEY_RE.match(session_key):
            return web.json_response(
                {"error": "Invalid session_key (max 200 chars, alphanumeric + :_-)"},
                status=400,
            )

        # Access control
        allowed, deny_msg = self._check_allowlist(sender_id, False)
        if not allowed:
            return web.json_response(
                {"error": deny_msg or "Forbidden"}, status=403,
            )

        request_id = uuid.uuid4().hex[:8]
        meta = {
            "sender_id": sender_id,
            "session_key": session_key or None,
            "name": name,
            "sync": sync,
            "request_id": request_id,
        }
        resolved_session = self.resolve_session_id(sender_id, meta)

        content_parts = [TextContent(type=ContentType.TEXT, text=message)]
        native = {
            "channel_id": self.channel,
            "sender_id": sender_id,
            "content_parts": content_parts,
            "meta": meta,
        }

        if sync:
            return await self._handle_sync_message(
                native, request_id, resolved_session, timeout_seconds,
            )

        # Async mode
        if self._enqueue:
            self._enqueue(native)
        else:
            logger.warning("webhook: _enqueue not set, message dropped")

        return web.json_response(
            {
                "accepted": True,
                "request_id": request_id,
                "session_key": resolved_session,
                "message": "Request enqueued for processing",
            },
            status=202,
        )

    async def _handle_sync_message(
        self,
        native: dict,
        request_id: str,
        session_key: str,
        timeout_seconds: int,
    ) -> web.Response:
        """Hold HTTP connection until agent resolves or timeout."""
        # Check concurrent sync cap
        active_sync = len(self._pending_responses)
        if active_sync >= self._max_concurrent_sync:
            return web.json_response(
                {"error": "Too many concurrent sync requests"},
                status=429,
                headers={"Retry-After": "5"},
            )

        loop = asyncio.get_event_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._pending_responses[request_id] = fut

        start_ts = time.monotonic()

        # Enqueue for processing
        if self._enqueue:
            self._enqueue(native)
        else:
            self._pending_responses.pop(request_id, None)
            return web.json_response(
                {"error": "Channel not connected"}, status=503,
            )

        try:
            result = await asyncio.wait_for(fut, timeout=timeout_seconds)
            elapsed_ms = round((time.monotonic() - start_ts) * 1000)
            return web.json_response({
                "response": result,
                "request_id": request_id,
                "session_key": session_key,
                "processing_time_ms": elapsed_ms,
            })
        except asyncio.TimeoutError:
            self._pending_responses.pop(request_id, None)
            return web.json_response(
                {"error": f"Agent did not respond within {timeout_seconds} seconds"},
                status=504,
            )
        except asyncio.CancelledError:
            self._pending_responses.pop(request_id, None)
            return web.json_response(
                {"error": "Channel shutting down"},
                status=503,
            )
        except Exception as exc:
            self._pending_responses.pop(request_id, None)
            return web.json_response(
                {"error": str(exc)},
                status=500,
            )
        finally:
            self._pending_responses.pop(request_id, None)

    async def _handle_wake(self, request: web.Request) -> web.Response:
        """POST /hooks/wake — fire-and-forget system event."""
        # Auth
        auth_err = self._validate_token(request)
        if auth_err:
            return auth_err

        # Rate limit
        client_ip = self._get_client_ip(request)
        rate_err = self._check_rate_limit(client_ip)
        if rate_err:
            return rate_err

        # Parse body
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Malformed JSON"}, status=400)

        text = (body.get("text") or "").strip()
        if not text:
            return web.json_response(
                {"error": "Missing or empty 'text' field"}, status=400,
            )

        sender_id = (body.get("sender_id") or "webhook").strip()

        # Access control
        allowed, deny_msg = self._check_allowlist(sender_id, False)
        if not allowed:
            return web.json_response(
                {"error": deny_msg or "Forbidden"}, status=403,
            )

        content_parts = [TextContent(type=ContentType.TEXT, text=text)]
        meta = {"sender_id": sender_id}
        native = {
            "channel_id": self.channel,
            "sender_id": sender_id,
            "content_parts": content_parts,
            "meta": meta,
        }

        if self._enqueue:
            self._enqueue(native)
        else:
            logger.warning("webhook: _enqueue not set, wake dropped")

        return web.json_response({"accepted": True})

    # ----- App builder & lifecycle -----

    def _build_app(self) -> web.Application:
        """Build the aiohttp application with routes."""
        self._app = web.Application(client_max_size=self._max_request_size)
        self._app.router.add_get("/hooks/health", self._handle_health)
        self._app.router.add_post("/hooks/message", self._handle_message)
        self._app.router.add_post("/hooks/wake", self._handle_wake)
        return self._app

    async def start(self) -> None:
        if not self.enabled:
            logger.debug("webhook: channel disabled")
            return
        if not self._token:
            logger.error("webhook: token not configured, refusing to start")
            return
        self._start_time = time.monotonic()
        self._build_app()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        # Resolve actual port (useful when port=0 for tests)
        sockets = self._site._server.sockets
        if sockets:
            actual_port = sockets[0].getsockname()[1]
            self._port = actual_port
        logger.info("webhook: listening on %s:%s", self._host, self._port)

    async def stop(self) -> None:
        if not self.enabled:
            return
        # Cancel all pending sync futures
        for req_id, fut in list(self._pending_responses.items()):
            if not fut.done():
                fut.cancel()
        self._pending_responses.clear()
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        self._app = None
        self._runner = None
        self._site = None
        logger.info("webhook: stopped")

    async def send(
        self,
        to_handle: str,
        text: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        pass  # Webhook is inbound-only; send is a no-op

    # ----- BaseChannel overrides for sync response collection -----

    async def on_event_message_completed(
        self,
        request: Any,
        to_handle: str,
        event: Any,
        send_meta: Dict[str, Any],
    ) -> None:
        """Resolve sync Future if this was a sync request, otherwise no-op."""
        meta = getattr(request, "channel_meta", None) or send_meta or {}
        request_id = meta.get("request_id")
        is_sync = meta.get("sync", False)

        if is_sync and request_id and request_id in self._pending_responses:
            fut = self._pending_responses.get(request_id)
            if fut and not fut.done():
                # Extract response text from event
                parts = self._message_to_content_parts(event)
                text_parts = []
                for p in parts:
                    t = getattr(p, "type", None)
                    if t == ContentType.TEXT and getattr(p, "text", None):
                        text_parts.append(p.text)
                    elif t == ContentType.REFUSAL and getattr(p, "refusal", None):
                        text_parts.append(p.refusal)
                fut.set_result("\n".join(text_parts) if text_parts else "")
            return

        # Non-sync: default behavior (send_message_content is a no-op for webhook)
        await super().on_event_message_completed(request, to_handle, event, send_meta)

    async def _on_consume_error(
        self,
        request: Any,
        to_handle: str,
        err_text: str,
    ) -> None:
        """Reject sync Future on agent error."""
        meta = getattr(request, "channel_meta", None) or {}
        request_id = meta.get("request_id")
        is_sync = meta.get("sync", False)

        if is_sync and request_id and request_id in self._pending_responses:
            fut = self._pending_responses.get(request_id)
            if fut and not fut.done():
                fut.set_exception(RuntimeError(err_text))
            return

        # Non-sync: default error handling
        await super()._on_consume_error(request, to_handle, err_text)
