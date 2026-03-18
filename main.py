from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import FileResponse
import asyncio
import json
import logging
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent / ".env"
    _loaded = load_dotenv(_env_path)
except ImportError:
    _loaded = False
    print("[WARNING] python-dotenv not installed – .env will NOT be loaded")
import httpx
import time
import traceback

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("openclaw-proxy")

# ── OpenClaw 网关 ─────────────────────────────────────────────────────────────
OPENCLAW_HTTP        = os.getenv("OPENCLAW_HTTP",        "http://127.0.0.1:18789")
OPENCLAW_WS          = os.getenv("OPENCLAW_WS",          "ws://127.0.0.1:18789")
OPENCLAW_TOKEN       = os.getenv("OPENCLAW_TOKEN",       "")
OPENCLAW_AGENT       = os.getenv("OPENCLAW_AGENT",       "main")
OPENCLAW_SESSION_KEY = os.getenv("OPENCLAW_SESSION_KEY", "agent:main:main")
OPENCLAW_CHAT_URL    = OPENCLAW_HTTP.rstrip('/') + '/v1/chat/completions'

# Try to import the WS client; fall back to HTTP-only if pynacl missing
try:
    from openclaw_ws_client import OpenClawWSClient
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False
    log.warning("openclaw_ws_client not importable – HTTP-only mode")

# Try to import the Hub client
try:
    from hub_client import HubClient
    _HUB_AVAILABLE = True
except ImportError:
    _HUB_AVAILABLE = False
    log.warning("hub_client not importable – hub disabled")

# Try to import the OpenIM client
try:
    from openim_client import OpenIMClient
    _OPENIM_AVAILABLE = True
except ImportError:
    _OPENIM_AVAILABLE = False
    log.warning("openim_client not importable – openim disabled")

HUB_WS_URL  = os.getenv("OPENCLAW_HUB_WS", "ws://127.0.0.1:9000/ws")
HUB_NAME    = os.getenv("OPENCLAW_HUB_NAME",   "Lobster")
HUB_AVATAR  = os.getenv("OPENCLAW_HUB_AVATAR",  "🦞")
HUB_ENABLED = os.getenv("OPENCLAW_HUB_ENABLED", "1") != "0"
log.info(".env loaded=%s  HUB_NAME=%r  HUB_AVATAR=%r  HUB_WS_URL=%s  HUB_ENABLED=%s",
         _loaded, HUB_NAME, HUB_AVATAR, HUB_WS_URL, HUB_ENABLED)

# ── OpenIM 设置 ──────────────────────────────────────────────────
IM_USER_ID      = os.getenv("IM_USER_ID",      "")
IM_TOKEN        = os.getenv("IM_TOKEN",        "")
IM_API_ADDR     = os.getenv("IM_API_ADDR",     "http://47.239.0.170:10002")
IM_POLL_INTERVAL= float(os.getenv("IM_POLL_INTERVAL", "3"))
IM_ENABLED      = os.getenv("IM_ENABLED",      "1") != "0"
log.info("OpenIM: user=%r  api=%s  poll=%.1fs  enabled=%s",
         IM_USER_ID, IM_API_ADDR, IM_POLL_INTERVAL, IM_ENABLED)

# ── Zulip 设置 ───────────────────────────────────────────────────────────────
ZULIP_SITE     = os.getenv("ZULIP_SITE",    "")
ZULIP_EMAIL    = os.getenv("ZULIP_EMAIL",   "")
ZULIP_API_KEY  = os.getenv("ZULIP_API_KEY", "")
ZULIP_LISTEN   = os.getenv("ZULIP_LISTEN",  "pm,mention")
ZULIP_PRESENCE = os.getenv("ZULIP_PRESENCE", "1") != "0"
ZULIP_ENABLED  = os.getenv("ZULIP_ENABLED",  "1") != "0"

try:
    from zulip_client import ZulipClient
    _ZULIP_AVAILABLE = True
except ImportError:
    _ZULIP_AVAILABLE = False
    log.warning("zulip_client not importable")

_gw_client:    "OpenClawWSClient | None" = None
_hub_client:   "HubClient | None"        = None
_zulip_client: "ZulipClient | None"      = None
_openim_client: "OpenIMClient | None"    = None


async def _start_ws_client():
    global _gw_client
    if not _WS_AVAILABLE:
        return
    client = OpenClawWSClient()
    try:
        await client.connect()
        _gw_client = client
        log.info("Gateway WS client connected")
    except Exception as e:
        log.warning("Gateway WS connect failed (%s) – falling back to HTTP", e)
        _gw_client = None


def _extract_text(events: list[dict]) -> str:
    """Pull the final assistant text out of a list of openclaw WS chat events.
    Only reads chat state=final; intermediate streaming chunks are ignored.
    """
    for e in reversed(events):
        if e.get("event") == "chat" and (e.get("payload") or {}).get("state") == "final":
            msgs = ((e.get("payload") or {}).get("message") or {}).get("content") or []
            text = " ".join(c.get("text", "") for c in msgs if c.get("type") == "text")
            if text.strip():
                return text.strip()
    return ""


async def _start_hub_client():
    global _hub_client
    if not HUB_ENABLED:
        log.info("Hub client disabled (OPENCLAW_HUB_ENABLED=0)")
        return
    if not _HUB_AVAILABLE:
        log.warning("Hub client not available (import failed)")
        return
    if not HUB_WS_URL:
        log.warning("Hub client disabled: OPENCLAW_HUB_WS not set")
        return

    async def on_friend_message(from_id: str, from_name: str, content: str) -> str | None:
        """Route an incoming hub message through the local OpenClaw agent."""
        if not _gw_client:
            log.warning("hub on_message: _gw_client is None, dropping message")
            return None
        if not _gw_client.connected:
            log.warning("hub on_message: _gw_client not connected, dropping message")
            return None
        try:
            prompt = f"[来自 {from_name}]: {content}"
            log.info("hub on_message: calling _gw_client.chat for %s", from_name)
            events = await _gw_client.chat(prompt)
            log.info("hub on_message: got %d events from gw", len(events) if events else 0)
            for i, e in enumerate(events or []):
                log.info("  event[%d]: %s", i, str(e)[:200])
            text   = _extract_text(events)
            # Collapse 3+ consecutive newlines down to 2 (one blank line max)
            if text:
                import re as _re
                text = _re.sub(r'\n{3,}', '\n\n', text).strip()
            log.info("hub on_message: extracted text=%s", (text or "")[:80])
            return text or None
        except Exception as e:
            log.warning("hub on_message handler error: %s", e, exc_info=True)
            return None

    hub = HubClient(HUB_WS_URL, HUB_NAME, HUB_AVATAR, on_message=on_friend_message)
    try:
        await hub.start()
        _hub_client = hub
        log.info("Hub client started (%s)", HUB_WS_URL)
    except Exception as e:
        log.warning("Hub client failed to start: %s", e)


async def _start_openim_client():
    global _openim_client
    if not IM_ENABLED:
        log.info("OpenIM client disabled (IM_ENABLED=0)")
        return
    if not _OPENIM_AVAILABLE:
        log.warning("OpenIM client not available (import failed)")
        return
    use_bridge = os.getenv("OPENIM_USE_BRIDGE", "1") != "0"
    if use_bridge:
        bridge_token = os.getenv("OPENIM_BRIDGE_TOKEN", "")
        bridge_email = os.getenv("OPENIM_BRIDGE_EMAIL", "")
        bridge_password = os.getenv("OPENIM_BRIDGE_PASSWORD", "")
        if not (bridge_token or (bridge_email and bridge_password)):
            log.info("OpenIM client disabled: set OPENIM_BRIDGE_TOKEN or OPENIM_BRIDGE_EMAIL+OPENIM_BRIDGE_PASSWORD")
            return
    elif not IM_TOKEN:
        log.info("OpenIM client disabled: set IM_TOKEN (non-bridge mode)")
        return

    async def on_im_message(
        from_id: str, from_name: str, content: str, *,
        session_type: int = 1, group_id: str = "", conv_id: str = ""
    ) -> str | None:
        """Route incoming OpenIM message through OpenClaw AI and return reply."""
        if not _gw_client or not _gw_client.connected:
            log.warning("openim on_message: gateway not ready, dropping")
            return None
        try:
            import re as _re
            who   = from_name or from_id
            where = f"[{group_id}] " if group_id else ""
            prompt = f"{where}[{who}]: {content}"
            log.info("openim on_message: calling AI for %s", who)
            events = await _gw_client.chat(prompt)
            text   = _extract_text(events)
            if text:
                text = _re.sub(r'\n{3,}', '\n\n', text).strip()
            log.info("openim on_message: reply=%s", (text or "")[:80])
            return text or None
        except Exception as e:
            log.warning("openim on_message error: %s", e, exc_info=True)
            return None

    client = OpenIMClient(
        user_id       = IM_USER_ID,
        im_token      = IM_TOKEN,
        api_addr      = IM_API_ADDR,
        poll_interval = IM_POLL_INTERVAL,
        on_message    = on_im_message,
    )
    try:
        await client.start()
        _openim_client = client
        log.info("OpenIM client started (user=%s)", client.user_id)
    except Exception as e:
        log.warning("OpenIM client failed to start: %s", e)


async def _start_zulip_client():
    global _zulip_client
    if not ZULIP_ENABLED:
        log.info("Zulip client disabled (ZULIP_ENABLED=0)")
        return
    if not _ZULIP_AVAILABLE:
        return
    if not (ZULIP_SITE and ZULIP_EMAIL and ZULIP_API_KEY):
        log.info("Zulip client disabled: ZULIP_SITE / ZULIP_EMAIL / ZULIP_API_KEY not set")
        return

    async def on_zulip_message(from_id: str, from_name: str, content: str) -> str | None:
        if not _gw_client or not _gw_client.connected:
            log.warning("zulip on_message: gateway not ready, dropping message")
            return None
        try:
            import re as _re
            prompt = f"[来自 {from_name}]: {content}"
            events = await _gw_client.chat(prompt)
            text   = _extract_text(events)
            if text:
                text = _re.sub(r'\n{3,}', '\n\n', text).strip()
            return text or None
        except Exception as e:
            log.warning("zulip on_message error: %s", e, exc_info=True)
            return None

    zc = ZulipClient(
        site      = ZULIP_SITE,
        email     = ZULIP_EMAIL,
        api_key   = ZULIP_API_KEY,
        name      = HUB_NAME,
        listen    = ZULIP_LISTEN,
        presence  = ZULIP_PRESENCE,
        on_message = on_zulip_message,
    )
    try:
        await zc.start()
        _zulip_client = zc
        log.info("Zulip client started (%s as %s)", ZULIP_SITE, ZULIP_EMAIL)
    except Exception as e:
        log.warning("Zulip client failed to start: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _start_ws_client()
    await _start_hub_client()
    await _start_zulip_client()
    await _start_openim_client()
    yield
    if _gw_client:
        await _gw_client.close()
    if _hub_client:
        await _hub_client.stop()
    if _zulip_client:
        await _zulip_client.stop()
    if _openim_client:
        await _openim_client.stop()


app = FastAPI(lifespan=lifespan)


# ── memory persistence ────────────────────────────────────────────────────────

async def append_conversation_to_memory(user_message: str, response_obj):
    try:
        mem_dir = Path("/root/.openclaw/workspace/memory")
        mem_dir.mkdir(parents=True, exist_ok=True)
        mem_path = mem_dir / f"{time.strftime('%Y-%m-%d')}.txt"
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] User: {user_message}\nResponse: {json.dumps(response_obj, ensure_ascii=False)}\n\n"

        def _write():
            with open(mem_path, 'a', encoding='utf-8') as f:
                f.write(entry)

        await asyncio.to_thread(_write)
    except Exception as e:
        log.info("append_conversation_to_memory error: %s", e)


# ── HTTP fallback helper ──────────────────────────────────────────────────────

async def _chat_via_http(msg: str) -> dict:
    headers = {"Content-Type": "application/json", "x-openclaw-agent-id": OPENCLAW_AGENT}
    if OPENCLAW_TOKEN:
        headers["Authorization"] = f"Bearer {OPENCLAW_TOKEN}"
    body = {"model": "openclaw", "messages": [{"role": "user", "content": msg}]}
    async with httpx.AsyncClient() as client:
        r = await client.post(OPENCLAW_CHAT_URL, headers=headers, json=body, timeout=60.0)
        log.info("HTTP chat status: %s", r.status_code)
        if r.status_code != 200:
            return {"error": f"status {r.status_code}", "detail": r.text}
        return {"reply": r.json()}


# ── routes ────────────────────────────────────────────────────────────────────

@app.get("/")
async def get_index():
    return FileResponse("index.html")


@app.post("/chat")
async def chat(request: Request):
    data = await request.json()
    msg = data.get("message", "")
    try:
        if _gw_client and _gw_client.connected:
            log.info("[/chat] using WS gateway")
            events = await _gw_client.chat(msg)
            result = {"reply": events}
        else:
            log.info("[/chat] using HTTP fallback")
            result = await _chat_via_http(msg)
        await append_conversation_to_memory(msg, result)
        return result
    except Exception as e:
        tb = traceback.format_exc()
        log.error("/chat error: %s\n%s", e, tb)
        return {"error": str(e)}


@app.websocket("/ws")
async def websocket_proxy(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            msg = await ws.receive_text()
            log.info("From browser (ws): %s", msg[:120])
            try:
                if _gw_client and _gw_client.connected:
                    log.info("[/ws] using WS gateway")
                    events = await _gw_client.chat(msg)
                    result = {"reply": events}
                else:
                    log.info("[/ws] using HTTP fallback")
                    result = await _chat_via_http(msg)
                await ws.send_text(json.dumps(result, ensure_ascii=False))
                await append_conversation_to_memory(msg, result)
            except Exception as e:
                tb = traceback.format_exc()
                log.error("ws handler error: %s\n%s", e, tb)
                try:
                    await ws.send_text(json.dumps({"error": str(e)}))
                except Exception:
                    pass
    except Exception as e:
        log.info("websocket_proxy loop stopped: %s", e)
    finally:
        try:
            await ws.close()
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
