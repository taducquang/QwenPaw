# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""Unit tests for Webhook channel."""
from __future__ import annotations

import asyncio
import hmac
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agentscope_runtime.engine.schemas.agent_schemas import (
    ContentType,
    TextContent,
)

from custom_channels.webhook.channel import WebhookChannel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_channel(**overrides: Any) -> WebhookChannel:
    """Create a WebhookChannel with dummy process handler."""
    async def _noop_process(_request):
        yield  # pragma: no cover

    defaults = {
        "process": _noop_process,
        "enabled": True,
        "token": "test-secret-token",
        "port": 0,  # random port for tests
        "host": "127.0.0.1",
    }
    defaults.update(overrides)
    return WebhookChannel(**defaults)


@pytest.fixture
async def webhook_client():
    """Create a test client for the webhook channel's aiohttp app."""
    ch = _make_channel(token="test-token", rate_limit_per_minute=5)
    ch._build_app()
    async with TestClient(TestServer(ch._app)) as client:
        client._channel = ch  # attach for test access
        yield client


# ===================================================================
# Configuration
# ===================================================================


class TestFromConfig:
    def test_from_config_dict(self):
        async def _noop(_req):
            yield
        config = {
            "enabled": True,
            "token": "my-token",
            "port": 9999,
            "host": "0.0.0.0",
            "sync_timeout": 30,
            "max_request_size_bytes": 512000,
            "rate_limit_per_minute": 120,
            "trust_proxy": True,
            "max_concurrent_sync": 5,
            "dm_policy": "allowlist",
            "allow_from": ["ci-bot"],
            "deny_message": "nope",
        }
        ch = WebhookChannel.from_config(process=_noop, config=config)
        assert ch.enabled is True
        assert ch._token == "my-token"
        assert ch._port == 9999
        assert ch._host == "0.0.0.0"
        assert ch._sync_timeout == 30
        assert ch._max_request_size == 512000
        assert ch._rate_limit_per_minute == 120
        assert ch._trust_proxy is True
        assert ch._max_concurrent_sync == 5
        assert ch.dm_policy == "allowlist"
        assert "ci-bot" in ch.allow_from
        assert ch.deny_message == "nope"

    def test_from_config_defaults(self):
        async def _noop(_req):
            yield
        config = {"enabled": True, "token": "tk"}
        ch = WebhookChannel.from_config(process=_noop, config=config)
        assert ch._port == 18790
        assert ch._host == "127.0.0.1"
        assert ch._sync_timeout == 60
        assert ch._max_concurrent_sync == 10
        assert ch._trust_proxy is False

    def test_from_env(self, monkeypatch):
        async def _noop(_req):
            yield
        monkeypatch.setenv("WEBHOOK_CHANNEL_ENABLED", "1")
        monkeypatch.setenv("WEBHOOK_TOKEN", "env-token")
        monkeypatch.setenv("WEBHOOK_PORT", "7777")
        monkeypatch.setenv("WEBHOOK_HOST", "0.0.0.0")
        monkeypatch.setenv("WEBHOOK_SYNC_TIMEOUT", "45")
        ch = WebhookChannel.from_env(process=_noop)
        assert ch.enabled is True
        assert ch._token == "env-token"
        assert ch._port == 7777
        assert ch._host == "0.0.0.0"
        assert ch._sync_timeout == 45


class TestResolveSessionId:
    def test_default_session(self):
        ch = _make_channel()
        assert ch.resolve_session_id("ci-bot") == "webhook:ci-bot"

    def test_custom_session_key(self):
        ch = _make_channel()
        meta = {"session_key": "github:pr-123"}
        assert ch.resolve_session_id("ci-bot", meta) == "webhook:github:pr-123"

    def test_none_meta_fallback(self):
        ch = _make_channel()
        assert ch.resolve_session_id("sender1", None) == "webhook:sender1"


class TestBuildAgentRequest:
    def test_builds_correct_request(self):
        ch = _make_channel()
        native = {
            "channel_id": "webhook",
            "sender_id": "ci-bot",
            "content_parts": [TextContent(type=ContentType.TEXT, text="hello")],
            "meta": {"sender_id": "ci-bot", "session_key": "pr-42"},
        }
        req = ch.build_agent_request_from_native(native)
        assert req.session_id == "webhook:pr-42"
        assert req.channel_meta["session_key"] == "pr-42"

    def test_defaults_when_payload_empty(self):
        ch = _make_channel()
        req = ch.build_agent_request_from_native({})
        assert req.session_id.startswith("webhook:")


class TestSendNoOp:
    @pytest.mark.asyncio
    async def test_send_is_noop(self):
        ch = _make_channel()
        # Should not raise
        await ch.send("some-handle", "some text", {"key": "value"})
        # No side effects to assert — just verify no exception


# ===================================================================
# Health Endpoint
# ===================================================================


class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health_no_auth_required(self, webhook_client):
        resp = await webhook_client.get("/hooks/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"
        assert data["channel"] == "webhook"
        assert "uptime_seconds" in data


# ===================================================================
# Authentication
# ===================================================================


class TestAuth:
    @pytest.mark.asyncio
    async def test_missing_token_returns_401(self, webhook_client):
        resp = await webhook_client.post(
            "/hooks/message",
            json={"message": "hello"},
        )
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_invalid_token_returns_401(self, webhook_client):
        resp = await webhook_client.post(
            "/hooks/message",
            json={"message": "hello"},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_valid_bearer_token(self, webhook_client):
        resp = await webhook_client.post(
            "/hooks/message",
            json={"message": "hello"},
            headers={"Authorization": "Bearer test-token"},
        )
        # Should not be 401 (may be 202 or other, but not auth failure)
        assert resp.status != 401

    @pytest.mark.asyncio
    async def test_x_copaw_token_header(self, webhook_client):
        resp = await webhook_client.post(
            "/hooks/message",
            json={"message": "hello"},
            headers={"X-CoPaw-Token": "test-token"},
        )
        assert resp.status != 401

    @pytest.mark.asyncio
    async def test_query_string_token_rejected(self, webhook_client):
        resp = await webhook_client.post(
            "/hooks/message?token=test-token",
            json={"message": "hello"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_constant_time_comparison(self, webhook_client):
        """Verify hmac.compare_digest is used for token validation."""
        from unittest.mock import patch as mock_patch
        with mock_patch(
            "custom_channels.webhook.channel.hmac.compare_digest",
            return_value=True,
        ) as mock_cmp:
            resp = await webhook_client.post(
                "/hooks/message",
                json={"message": "hello"},
                headers={"Authorization": "Bearer test-token"},
            )
            mock_cmp.assert_called()

    @pytest.mark.asyncio
    async def test_malformed_json_returns_400(self, webhook_client):
        resp = await webhook_client.post(
            "/hooks/message",
            data=b"not-json{{{",
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json",
            },
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_oversized_body_returns_error(self):
        """Body exceeding max_request_size_bytes returns 4xx error."""
        ch = _make_channel(token="test-token", max_request_size_bytes=100)
        ch._build_app()
        async with TestClient(TestServer(ch._app)) as client:
            big_body = b'{"message": "' + b"x" * 200 + b'"}'
            resp = await client.post(
                "/hooks/message",
                data=big_body,
                headers={
                    "Authorization": "Bearer test-token",
                    "Content-Type": "application/json",
                },
            )
            # aiohttp may return 400 (body parse error) or 413 depending on version
            assert resp.status in (400, 413)


# ===================================================================
# Rate Limiting
# ===================================================================


class TestRateLimiting:
    @pytest.mark.asyncio
    async def test_under_limit_allowed(self, webhook_client):
        for _ in range(5):
            resp = await webhook_client.post(
                "/hooks/message",
                json={"message": "hello"},
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status != 429

    @pytest.mark.asyncio
    async def test_over_limit_returns_429(self, webhook_client):
        # rate_limit_per_minute=5, so 6th request should fail
        for _ in range(5):
            await webhook_client.post(
                "/hooks/message",
                json={"message": "hello"},
                headers={"Authorization": "Bearer test-token"},
            )
        resp = await webhook_client.post(
            "/hooks/message",
            json={"message": "hello"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 429
        assert "Retry-After" in resp.headers

    @pytest.mark.asyncio
    async def test_trust_proxy_uses_forwarded_for(self):
        """With trust_proxy=True, rate limit tracks X-Forwarded-For IP."""
        ch = _make_channel(
            token="test-token", trust_proxy=True, rate_limit_per_minute=2,
        )
        ch._build_app()
        async with TestClient(TestServer(ch._app)) as client:
            # 2 requests from "ip-a" should exhaust ip-a's limit
            for _ in range(2):
                await client.post(
                    "/hooks/message",
                    json={"message": "hi"},
                    headers={
                        "Authorization": "Bearer test-token",
                        "X-Forwarded-For": "1.2.3.4",
                    },
                )
            # 3rd from ip-a should be 429
            resp = await client.post(
                "/hooks/message",
                json={"message": "hi"},
                headers={
                    "Authorization": "Bearer test-token",
                    "X-Forwarded-For": "1.2.3.4",
                },
            )
            assert resp.status == 429
            # But ip-b should still work
            resp = await client.post(
                "/hooks/message",
                json={"message": "hi"},
                headers={
                    "Authorization": "Bearer test-token",
                    "X-Forwarded-For": "5.6.7.8",
                },
            )
            assert resp.status != 429


# ===================================================================
# Start Guards
# ===================================================================


class TestStartGuards:
    @pytest.mark.asyncio
    async def test_no_token_refuses_to_start(self):
        ch = _make_channel(token="", enabled=True)
        await ch.start()
        assert ch._site is None


# ===================================================================
# Async Message Endpoint
# ===================================================================


class TestAsyncMessage:
    @pytest.mark.asyncio
    async def test_valid_async_message_returns_202(self, webhook_client):
        resp = await webhook_client.post(
            "/hooks/message",
            json={"message": "hello agent"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 202
        data = await resp.json()
        assert data["accepted"] is True
        assert "request_id" in data
        assert data["session_key"].startswith("webhook:")

    @pytest.mark.asyncio
    async def test_custom_sender_and_session(self, webhook_client):
        resp = await webhook_client.post(
            "/hooks/message",
            json={
                "message": "deploy status",
                "sender_id": "ci-bot",
                "session_key": "github:pr-42",
            },
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 202
        data = await resp.json()
        assert data["session_key"] == "webhook:github:pr-42"

    @pytest.mark.asyncio
    async def test_missing_message_returns_400(self, webhook_client):
        resp = await webhook_client.post(
            "/hooks/message",
            json={"sender_id": "bot"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_empty_message_returns_400(self, webhook_client):
        resp = await webhook_client.post(
            "/hooks/message",
            json={"message": ""},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_invalid_session_key_returns_400(self, webhook_client):
        resp = await webhook_client.post(
            "/hooks/message",
            json={"message": "hi", "session_key": "a" * 201},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_invalid_session_key_chars_returns_400(self, webhook_client):
        resp = await webhook_client.post(
            "/hooks/message",
            json={"message": "hi", "session_key": "has spaces $#@!"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_enqueue_called_with_correct_payload(self):
        """Verify _enqueue is called with proper native payload structure."""
        ch = _make_channel(token="test-token")
        ch._build_app()
        enqueued = []
        ch._enqueue = lambda payload: enqueued.append(payload)
        async with TestClient(TestServer(ch._app)) as client:
            resp = await client.post(
                "/hooks/message",
                json={"message": "hello agent", "sender_id": "ci-bot"},
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status == 202
        assert len(enqueued) == 1
        native = enqueued[0]
        assert native["channel_id"] == "webhook"
        assert native["sender_id"] == "ci-bot"
        assert len(native["content_parts"]) == 1
        assert native["content_parts"][0].text == "hello agent"
        assert native["meta"]["sender_id"] == "ci-bot"

    @pytest.mark.asyncio
    async def test_allowlist_blocks_sender(self):
        """sender_id not in allow_from -> 403."""
        async def _noop(_req):
            yield
        ch = _make_channel(
            token="test-token",
            dm_policy="allowlist",
            allow_from=["allowed-bot"],
        )
        ch._build_app()
        async with TestClient(TestServer(ch._app)) as client:
            resp = await client.post(
                "/hooks/message",
                json={"message": "hi", "sender_id": "blocked-bot"},
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status == 403


# ===================================================================
# Wake Endpoint
# ===================================================================


class TestWakeEndpoint:
    @pytest.mark.asyncio
    async def test_wake_returns_200(self, webhook_client):
        resp = await webhook_client.post(
            "/hooks/wake",
            json={"text": "deploy completed"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["accepted"] is True

    @pytest.mark.asyncio
    async def test_wake_missing_text_returns_400(self, webhook_client):
        resp = await webhook_client.post(
            "/hooks/wake",
            json={},
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_wake_requires_auth(self, webhook_client):
        resp = await webhook_client.post(
            "/hooks/wake",
            json={"text": "deploy done"},
        )
        assert resp.status == 401


# ===================================================================
# Sync Mode
# ===================================================================


class TestSyncMode:
    @pytest.mark.asyncio
    async def test_sync_concurrent_cap(self):
        """Exceeding max_concurrent_sync returns 429."""
        ch = _make_channel(token="test-token", max_concurrent_sync=1)
        ch._build_app()
        # Manually fill pending to simulate concurrent sync
        loop = asyncio.get_event_loop()
        ch._pending_responses["fake"] = loop.create_future()
        async with TestClient(TestServer(ch._app)) as client:
            resp = await client.post(
                "/hooks/message",
                json={"message": "hi", "sync": True},
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status == 429
        # Cleanup
        ch._pending_responses["fake"].cancel()
        ch._pending_responses.clear()

    @pytest.mark.asyncio
    async def test_sync_timeout_returns_504(self):
        """Sync mode with very short timeout returns 504."""
        async def _slow_process(_req):
            yield  # never completes with a message event
        ch = _make_channel(
            token="test-token",
            process=_slow_process,
            sync_timeout=1,
        )
        ch._build_app()
        # Mock enqueue to do nothing (no consumer running)
        ch._enqueue = MagicMock()
        async with TestClient(TestServer(ch._app)) as client:
            resp = await client.post(
                "/hooks/message",
                json={"message": "hi", "sync": True, "timeout_seconds": 1},
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status == 504

    @pytest.mark.asyncio
    async def test_sync_response_resolved(self):
        """When Future is resolved, sync returns 200 with response text."""
        ch = _make_channel(token="test-token")
        ch._build_app()

        # Intercept _enqueue to resolve the pending future
        def fake_enqueue(native):
            meta = native.get("meta", {})
            req_id = meta.get("request_id")
            if req_id and req_id in ch._pending_responses:
                fut = ch._pending_responses[req_id]
                if not fut.done():
                    fut.set_result("Agent says hello")
        ch._enqueue = fake_enqueue

        async with TestClient(TestServer(ch._app)) as client:
            resp = await client.post(
                "/hooks/message",
                json={"message": "hi", "sync": True},
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["response"] == "Agent says hello"
            assert "processing_time_ms" in data

    @pytest.mark.asyncio
    async def test_sync_agent_error_returns_500(self):
        """When Future is rejected, sync returns 500."""
        ch = _make_channel(token="test-token")
        ch._build_app()

        def fake_enqueue(native):
            meta = native.get("meta", {})
            req_id = meta.get("request_id")
            if req_id and req_id in ch._pending_responses:
                fut = ch._pending_responses[req_id]
                if not fut.done():
                    fut.set_exception(RuntimeError("Agent crashed"))
        ch._enqueue = fake_enqueue

        async with TestClient(TestServer(ch._app)) as client:
            resp = await client.post(
                "/hooks/message",
                json={"message": "hi", "sync": True},
                headers={"Authorization": "Bearer test-token"},
            )
            assert resp.status == 500
            data = await resp.json()
            assert "error" in data

    @pytest.mark.asyncio
    async def test_stop_cancels_pending_futures(self):
        """Calling stop() cancels all pending sync futures."""
        ch = _make_channel(token="test-token")
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        ch._pending_responses["req1"] = fut
        await ch.stop()
        assert fut.cancelled()
        assert len(ch._pending_responses) == 0

    @pytest.mark.asyncio
    async def test_concurrent_sync_different_senders(self):
        """Two sync requests from different senders can proceed concurrently."""
        ch = _make_channel(token="test-token", max_concurrent_sync=10)
        ch._build_app()

        def fake_enqueue(native):
            meta = native.get("meta", {})
            req_id = meta.get("request_id")
            sender = meta.get("sender_id", "unknown")
            if req_id and req_id in ch._pending_responses:
                async def resolve():
                    await asyncio.sleep(0.05)
                    fut = ch._pending_responses.get(req_id)
                    if fut and not fut.done():
                        fut.set_result(f"Hello {sender}")
                asyncio.create_task(resolve())
        ch._enqueue = fake_enqueue

        async with TestClient(TestServer(ch._app)) as client:
            resp1_coro = client.post(
                "/hooks/message",
                json={"message": "hi", "sync": True, "sender_id": "bot-a"},
                headers={"Authorization": "Bearer test-token"},
            )
            resp2_coro = client.post(
                "/hooks/message",
                json={"message": "hi", "sync": True, "sender_id": "bot-b"},
                headers={"Authorization": "Bearer test-token"},
            )
            resp1, resp2 = await asyncio.gather(resp1_coro, resp2_coro)
            assert resp1.status == 200
            assert resp2.status == 200
            data1 = await resp1.json()
            data2 = await resp2.json()
            assert data1["response"] == "Hello bot-a"
            assert data2["response"] == "Hello bot-b"
