"""
OpenClaw Gateway WebSocket client.

Implements the gateway protocol from https://docs.openclaw.ai/gateway/protocol
- Waits for connect.challenge, signs with Ed25519 (v3 payload), sends connect
- Persists device keypair + deviceToken across sessions
- Sends chat.send and collects events until done
"""
import asyncio
import base64
import hashlib
import json
import logging
import os
import pathlib
import time
import uuid
import websockets

try:
    from nacl.signing import SigningKey
    HAS_NACL = True
except ImportError:
    HAS_NACL = False

log = logging.getLogger("openclaw-ws")

# ── configuration (all overridable via env) ───────────────────────────────────
OPENCLAW_WS          = os.getenv("OPENCLAW_WS",          "ws://127.0.0.1:18789")
OPENCLAW_TOKEN       = os.getenv("OPENCLAW_TOKEN",       "")
OPENCLAW_SESSION_KEY = os.getenv("OPENCLAW_SESSION_KEY", "agent:main:main")
CLIENT_VERSION       = "1.0.0"
CLIENT_ID        = "cli"
CLIENT_MODE      = "cli"
CLIENT_PLATFORM  = "web"
SCOPES           = ["operator.read", "operator.write"]

# Events that mark end-of-run for a chat request
DONE_EVENTS = {
    "run.done", "chat.done", "agent.run.done", "chat.run.done",
    "embedded run done", "run.complete", "chat.response.done",
}

KEY_FILE   = pathlib.Path.home() / ".openclaw" / "py_device_key.json"
TOKEN_FILE = pathlib.Path.home() / ".openclaw" / "py_device_token.json"


# ── key + token persistence ───────────────────────────────────────────────────

def _b64url(data: bytes) -> str:
    """base64url with no padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def load_key():
    if not HAS_NACL:
        raise RuntimeError("PyNaCl required:  pip install pynacl")
    if KEY_FILE.exists():
        d  = json.loads(KEY_FILE.read_text(encoding="utf-8"))
        sk = SigningKey(base64.b64decode(d["sk"]))
        pk = sk.verify_key.encode()
        # recalculate id in case old file has truncated value
        dev_id = hashlib.sha256(pk).hexdigest()
        return sk, pk, dev_id
    sk    = SigningKey.generate()
    pk    = sk.verify_key.encode()
    dev_id = hashlib.sha256(pk).hexdigest()   # full 64-char hex
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(json.dumps({
        "id": dev_id,
        "sk": base64.b64encode(sk.encode()).decode(),
        "pk": base64.b64encode(pk).decode(),
    }), encoding="utf-8")
    log.info("Generated new device key  id=%s", dev_id)
    return sk, pk, dev_id


def load_device_token() -> str | None:
    if TOKEN_FILE.exists():
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get("operator")
    return None


def save_device_token(tok: str):
    data: dict = {}
    if TOKEN_FILE.exists():
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    data["operator"] = tok
    TOKEN_FILE.write_text(json.dumps(data), encoding="utf-8")
    log.info("Saved deviceToken")


# ── v2 pipe-delimited signature (confirmed working) ──────────────────────────

def sign_v2(sk, *, nonce: str, signed_at: int, device_id: str,
            client_mode: str, token: str) -> str:
    """
    Pipe-delimited canonical payload (pinchchat buildDeviceAuthPayload v2):
      v2|{deviceId}|{clientId}|{clientMode}|{role}|{scopes_csv}|{signedAtMs}|{token}|{nonce}
    Sign with Ed25519, return base64url (no padding).
    """
    scopes_csv = ",".join(sorted(SCOPES))
    parts = ["v2", device_id, CLIENT_ID, client_mode, "operator",
             scopes_csv, str(signed_at), token or "", nonce]
    body = "|".join(parts).encode("utf-8")
    return _b64url(sk.sign(body).signature)


# ── client ────────────────────────────────────────────────────────────────────

class OpenClawWSClient:
    """
    Authenticated WebSocket client for the OpenClaw gateway.

    Usage:
        client = OpenClawWSClient()
        await client.connect()
        events = await client.chat("hello")
        await client.close()

    Or as async context manager:
        async with OpenClawWSClient() as c:
            events = await c.chat("hello")
    """

    def __init__(self):
        self._ws            = None
        self._sk            = None
        self._pk            = None
        self._device_id     = None
        self.connected      = False
        self._pending: dict[str, asyncio.Future] = {}
        self._event_queues: list[asyncio.Queue]  = []
        self._recv_task     = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *_):
        await self.close()

    # ── connect + handshake ─────────────────────────────────────────────────

    async def connect(self):
        self._sk, self._pk, self._device_id = load_key()
        log.info("Connecting to %s (device=%s)", OPENCLAW_WS, self._device_id)
        self._ws = await websockets.connect(OPENCLAW_WS)
        await self._handshake()
        self._recv_task = asyncio.create_task(self._recv_loop())
        self.connected = True

    async def _handshake(self):
        # Step 1 — receive connect.challenge
        raw  = await asyncio.wait_for(self._ws.recv(), timeout=10)
        chal = json.loads(raw)
        if chal.get("event") != "connect.challenge":
            raise RuntimeError(f"Expected connect.challenge, got: {raw[:200]}")

        chal_payload = chal.get("payload") or {}
        nonce     = chal_payload.get("nonce", "")
        signed_at = int(time.time() * 1000)

        sig    = sign_v2(self._sk, nonce=nonce, signed_at=signed_at,
                         device_id=self._device_id, client_mode=CLIENT_MODE,
                         token=OPENCLAW_TOKEN)
        pk_b64 = _b64url(self._pk)   # base64url, no padding

        auth: dict = {"token": OPENCLAW_TOKEN}
        saved_dt = load_device_token()
        if saved_dt:
            auth["deviceToken"] = saved_dt

        # Step 2 — send connect request
        req = {
            "type": "req",
            "id":   str(uuid.uuid4()),
            "method": "connect",
            "params": {
                "minProtocol": 3,
                "maxProtocol": 3,
                "client": {
                    "id":       CLIENT_ID,
                    "version":  CLIENT_VERSION,
                    "platform": CLIENT_PLATFORM,  # "web"
                    "mode":     CLIENT_MODE,       # "cli"
                },
                "role":        "operator",
                "scopes":      SCOPES,
                "caps":        [],
                "commands":    [],
                "permissions": {},
                "auth":        auth,
                "locale":      "en-US",
                "userAgent":   f"openclaw-py/{CLIENT_VERSION}",
                "device": {
                    "id":        self._device_id,
                    "publicKey": pk_b64,   # base64url, no padding
                    "signature": sig,      # base64url, no padding
                    "signedAt":  signed_at,
                    "nonce":     nonce,
                },
            },
        }
        await self._ws.send(json.dumps(req))

        # Step 3 — receive hello-ok
        res_raw = await asyncio.wait_for(self._ws.recv(), timeout=10)
        res     = json.loads(res_raw)

        if not res.get("ok"):
            err  = res.get("error", {})
            code = err.get("code", "")
            msg  = err.get("message", "")
            raise RuntimeError(f"Connect rejected ({code}): {msg}")

        # Persist deviceToken for reconnects
        dt = ((res.get("payload") or {}).get("auth") or {}).get("deviceToken")
        if dt:
            save_device_token(dt)

        log.info("hello-ok  protocol=%s", (res.get("payload") or {}).get("protocol"))

    # ── receive loop ────────────────────────────────────────────────────────

    async def _recv_loop(self):
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                # Resolve pending RPC futures
                if msg.get("type") == "res":
                    fut = self._pending.pop(msg.get("id"), None)
                    if fut and not fut.done():
                        fut.set_result(msg)
                # Broadcast every message to all active event queues
                for q in list(self._event_queues):
                    try:
                        q.put_nowait(msg)
                    except asyncio.QueueFull:
                        pass
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning("recv_loop ended: %s", e)
        finally:
            self.connected = False
            err = RuntimeError("Connection closed")
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(err)
            self._pending.clear()

    # ── chat ────────────────────────────────────────────────────────────────

    async def chat(self, message: str, timeout: float = 60.0) -> list[dict]:
        """
        Send a user message via chat.send and collect events until a done event
        arrives (or timeout). Returns a list of all received messages.
        """
        if not self.connected or self._ws is None:
            raise RuntimeError("Not connected to gateway")

        req_id  = str(uuid.uuid4())
        res_fut: asyncio.Future = asyncio.get_event_loop().create_future()
        ev_q:   asyncio.Queue  = asyncio.Queue(maxsize=256)

        self._pending[req_id]    = res_fut
        self._event_queues.append(ev_q)

        req = {
            "type": "req",
            "id":   req_id,
            "method": "chat.send",
            "params": {
                "sessionKey":     OPENCLAW_SESSION_KEY,
                "message":        message,
                "deliver":        False,
                "idempotencyKey": str(uuid.uuid4()),
            },
        }
        await self._ws.send(json.dumps(req))

        collected: list[dict] = []
        deadline = time.time() + timeout
        lifecycle_started = False  # track whether the agent run has begun

        try:
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                # Use a generous idle timeout — LLM may take time before first token
                idle_timeout = min(remaining, 60.0)
                try:
                    msg = await asyncio.wait_for(ev_q.get(), timeout=idle_timeout)
                except asyncio.TimeoutError:
                    # Idle timeout: if run never started, just give up; otherwise assume done
                    break

                collected.append(msg)
                event_name = msg.get("event", "")
                payload    = msg.get("payload") or {}

                # Track lifecycle start so we know the run is underway
                if (event_name == "agent" and
                        payload.get("stream") == "lifecycle" and
                        (payload.get("data") or {}).get("phase") == "start"):
                    lifecycle_started = True
                    # Extend deadline from lifecycle start
                    deadline = max(deadline, time.time() + timeout)

                # ── done detection ────────────────────────────────────────
                # 1. lifecycle end = agent run finished
                if (event_name == "agent" and
                        payload.get("stream") == "lifecycle" and
                        (payload.get("data") or {}).get("phase") == "end"):
                    break

                # 2. chat state=final = complete message delivered
                if event_name == "chat" and payload.get("state") == "final":
                    break

                # 3. named done events
                if event_name in DONE_EVENTS:
                    break

                # 4. error response for our request
                if msg.get("type") == "res" and msg.get("id") == req_id and not msg.get("ok"):
                    break
        finally:
            self._event_queues.remove(ev_q)
            self._pending.pop(req_id, None)

        return collected

    # ── close ───────────────────────────────────────────────────────────────

    async def close(self):
        self.connected = False
        if self._recv_task:
            self._recv_task.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
