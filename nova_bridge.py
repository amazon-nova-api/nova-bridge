#!/usr/bin/env python3
"""
Nova WebSocket Bridge for OpenClaw.

Standalone sidecar that bridges the Nova WebSocket API Gateway to a local
OpenClaw bot's /v1/chat/completions endpoint.  No OpenClaw fork required.

    Nova user ──WS──▶ API Gateway ──WS──▶ this sidecar
                                            │  HTTP POST /v1/chat/completions
                                            ▼
                                       OpenClaw gateway (localhost)
                                            │
    Nova user ◀──WS── API Gateway ◀──WS── sidecar
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

import aiohttp
import websockets
import websockets.asyncio.client as ws_client

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("nova-bridge")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULTS = {
    "nova_ws_url": "wss://ws.nova-claw.agi.amazon.dev",
    "nova_api_key": "",
    "nova_user_id": "",
    "nova_device_id": "",
    "openclaw_gateway_url": "http://127.0.0.1:18789",
    "openclaw_gateway_token": "",
    "dm_policy": "allowlist",
    "allow_from": [],
    "max_history": 20,
}

ENV_MAP = {
    "nova_ws_url": "NOVA_WS_URL",
    "nova_api_key": "NOVA_API_KEY",
    "nova_user_id": "NOVA_USER_ID",
    "nova_device_id": "NOVA_DEVICE_ID",
    "openclaw_gateway_url": "OPENCLAW_GATEWAY_URL",
    "openclaw_gateway_token": "OPENCLAW_GATEWAY_TOKEN",
    "dm_policy": "DM_POLICY",
    "allow_from": "ALLOW_FROM",
    "max_history": "MAX_HISTORY",
}


def _load_config() -> dict[str, Any]:
    """Load configuration from JSON file, then overlay environment variables."""
    cfg: dict[str, Any] = dict(_DEFAULTS)

    # JSON config file (optional)
    config_path = os.environ.get("NOVA_BRIDGE_CONFIG", "config.json")
    p = Path(config_path)
    if p.is_file():
        log.info("Loading config from %s", p)
        with p.open() as f:
            file_cfg = json.load(f)
        for key in _DEFAULTS:
            if key in file_cfg:
                cfg[key] = file_cfg[key]

    # Environment variable overrides
    for key, env_name in ENV_MAP.items():
        val = os.environ.get(env_name)
        if val is None:
            continue
        if key == "allow_from":
            cfg[key] = [v.strip() for v in val.split(",") if v.strip()]
        elif key == "max_history":
            cfg[key] = int(val)
        else:
            cfg[key] = val

    # Auto-generate a stable device ID if not set
    if not cfg["nova_device_id"]:
        cfg["nova_device_id"] = str(uuid.uuid4())
        log.info("Auto-generated deviceId: %s", cfg["nova_device_id"])

    return cfg


def _validate_config(cfg: dict[str, Any]) -> None:
    if not cfg["nova_api_key"]:
        sys.exit("ERROR: NOVA_API_KEY is required")
    if not cfg["nova_user_id"]:
        sys.exit("ERROR: NOVA_USER_ID is required")


# ---------------------------------------------------------------------------
# Per-user conversation history
# ---------------------------------------------------------------------------

_histories: dict[str, deque[dict[str, str]]] = {}


def _get_history(user_id: str, max_len: int) -> deque[dict[str, str]]:
    if user_id not in _histories:
        _histories[user_id] = deque(maxlen=max_len)
    return _histories[user_id]


# ---------------------------------------------------------------------------
# Inbound message parsing  (mirrors inbound.ts)
# ---------------------------------------------------------------------------


def _parse_inbound(raw: str) -> dict[str, Any] | None:
    """Return a parsed inbound message dict, or None for non-message frames."""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(obj, dict) or obj.get("action") != "message":
        return None

    user_id = str(obj.get("userId", "")).strip()
    text = str(obj.get("text", ""))
    message_id = str(obj.get("messageId", "")).strip()
    timestamp = obj.get("timestamp")
    if not isinstance(timestamp, (int, float)):
        timestamp = int(time.time() * 1000)

    if not user_id or not message_id:
        return None

    return {
        "userId": user_id,
        "text": text,
        "messageId": message_id,
        "timestamp": int(timestamp),
    }


# ---------------------------------------------------------------------------
# DM policy  (mirrors monitor.ts allowlist logic)
# ---------------------------------------------------------------------------


def _is_allowed(user_id: str, cfg: dict[str, Any]) -> bool:
    policy = cfg["dm_policy"]
    if policy == "open":
        return True
    # allowlist — always include the bot's own user ID
    allow_from: list[str] = [s.lower() for s in cfg["allow_from"]]
    own_id = cfg.get("nova_user_id", "")
    if own_id and own_id.lower() not in allow_from:
        allow_from.append(own_id.lower())
    if "*" in allow_from:
        return True
    if not allow_from:
        log.info("Message from %s dropped (allowlist is empty)", user_id)
        return False
    if user_id.lower() not in allow_from:
        log.info("Message from %s dropped (not in allowlist)", user_id)
        return False
    return True


# ---------------------------------------------------------------------------
# OpenClaw HTTP forwarding
# ---------------------------------------------------------------------------


async def _forward_to_openclaw(
    session: aiohttp.ClientSession,
    cfg: dict[str, Any],
    user_id: str,
    text: str,
) -> str:
    """Send conversation to OpenClaw and return the assistant response text."""
    history = _get_history(user_id, cfg["max_history"])
    history.append({"role": "user", "content": text})

    messages = list(history)
    url = f"{cfg['openclaw_gateway_url'].rstrip('/')}/v1/chat/completions"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if cfg["openclaw_gateway_token"]:
        headers["Authorization"] = f"Bearer {cfg['openclaw_gateway_token']}"

    payload = {"model": "default", "messages": messages}

    async with session.post(url, json=payload, headers=headers) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(f"OpenClaw returned {resp.status}: {body[:500]}")
        data = await resp.json()

    assistant_text: str = data["choices"][0]["message"]["content"]
    history.append({"role": "assistant", "content": assistant_text})
    return assistant_text


# ---------------------------------------------------------------------------
# Response delivery  (mirrors send.ts)
# ---------------------------------------------------------------------------


def _build_response_frame(text: str, to: str, reply_to: str) -> str:
    return json.dumps({
        "action": "response",
        "type": "done",
        "text": text,
        "messageId": str(uuid.uuid4()),
        "replyTo": reply_to,
        "to": to,
    })


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------


async def _handle_message(
    ws: ws_client.ClientConnection,
    session: aiohttp.ClientSession,
    cfg: dict[str, Any],
    raw: str,
) -> None:
    msg = _parse_inbound(raw)
    if msg is None:
        return

    user_id = msg["userId"]
    if not _is_allowed(user_id, cfg):
        return

    log.info('Inbound: from=%s preview="%s"', user_id, msg["text"][:60])

    try:
        reply_text = await _forward_to_openclaw(session, cfg, user_id, msg["text"])
    except Exception:
        log.exception("OpenClaw call failed for user %s", user_id)
        return

    frame = _build_response_frame(reply_text, to=user_id, reply_to=msg["messageId"])
    await ws.send(frame)
    log.info("Response sent to %s (%d chars)", user_id, len(reply_text))


# ---------------------------------------------------------------------------
# Heartbeat  (mirrors monitor.ts startHeartbeat)
# ---------------------------------------------------------------------------

HEARTBEAT_INTERVAL = 30  # seconds


async def _heartbeat_loop(ws: ws_client.ClientConnection) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        try:
            ping = json.dumps({"action": "ping", "timestamp": int(time.time() * 1000)})
            await ws.send(ping)
            log.debug("Heartbeat sent")
        except Exception as exc:
            log.warning("Heartbeat failed, closing connection: %s", exc)
            await ws.close()
            return


# ---------------------------------------------------------------------------
# Reconnect with exponential backoff  (mirrors monitor.ts scheduleReconnect)
# ---------------------------------------------------------------------------

RECONNECT_BASE_S = 1.0
RECONNECT_MAX_S = 60.0


def _backoff_delay(attempt: int) -> float:
    import random
    jitter = random.uniform(0.85, 1.15)
    return min(RECONNECT_BASE_S * (2 ** attempt) * jitter, RECONNECT_MAX_S)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def _run(cfg: dict[str, Any]) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    attempt = 0

    async with aiohttp.ClientSession() as session:
        while not stop.is_set():
            ws_url = (
                f"{cfg['nova_ws_url']}"
                f"?userId={cfg['nova_user_id']}"
                f"&deviceId={cfg['nova_device_id']}"
            )
            headers = {"Authorization": f"Bearer {cfg['nova_api_key']}"}

            log.info("Connecting to %s", cfg["nova_ws_url"])

            try:
                async with ws_client.connect(
                    ws_url,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    log.info("WebSocket connected")
                    attempt = 0

                    async def _close_on_stop() -> None:
                        """Close the WebSocket when stop is signalled."""
                        await stop.wait()
                        log.info("Stop signal received, closing WebSocket")
                        await ws.close()

                    stop_task = asyncio.create_task(_close_on_stop())
                    heartbeat_task = asyncio.create_task(_heartbeat_loop(ws))
                    try:
                        async for raw in ws:
                            if stop.is_set():
                                break
                            asyncio.create_task(
                                _handle_message(ws, session, cfg, str(raw))
                            )
                    finally:
                        heartbeat_task.cancel()
                        stop_task.cancel()
                        for t in (heartbeat_task, stop_task):
                            try:
                                await t
                            except asyncio.CancelledError:
                                pass

            except (
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.InvalidURI,
                websockets.exceptions.InvalidHandshake,
                OSError,
            ) as exc:
                log.warning("WebSocket error: %s", exc)

            if stop.is_set():
                break

            delay = _backoff_delay(attempt)
            attempt += 1
            log.info("Reconnecting in %.1fs (attempt %d)", delay, attempt)

            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                break  # stop was set during the wait
            except asyncio.TimeoutError:
                pass  # timeout expired, retry

    log.info("Shut down cleanly")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    cfg = _load_config()
    _validate_config(cfg)

    log.info("Nova Bridge starting")
    log.info("  WS endpoint : %s", cfg["nova_ws_url"])
    log.info("  Bot userId  : %s", cfg["nova_user_id"])
    log.info("  OpenClaw    : %s", cfg["openclaw_gateway_url"])
    log.info("  DM policy   : %s", cfg["dm_policy"])
    if cfg["dm_policy"] == "allowlist":
        log.info("  Allow from  : %s", cfg["allow_from"] or "(empty — all blocked)")

    asyncio.run(_run(cfg))


if __name__ == "__main__":
    main()
