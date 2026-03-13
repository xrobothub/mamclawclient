"""

OpenClaw Hub Client

───────────────────

Background WebSocket client that connects main.py to the OpenClaw Hub as a

headless "lobster" peer.



Features

  -?Registers with the hub using OPENCLAW_HUB_NAME / OPENCLAW_HUB_AVATAR

  -?Auto-accepts all incoming friend requests

  -?When a friend sends a message, calls the `on_message` callback;

    if the callback returns a non-empty string, sends it back as a reply

  -?Reconnects automatically with exponential back-off



Usage (from main.py)

  hub = HubClient(

      hub_ws_url="ws://hub-host:9000/ws",

      name="My Lobster",

      avatar="🦞",

      on_message=async_handler,   # async (from_id, from_name, content) -> str|None

  )

  await hub.start()   # fire-and-forget background task

  ...

  await hub.stop()

"""



from __future__ import annotations

import asyncio

import hashlib

import json

import logging

import os

import time

import uuid

from typing import Callable, Awaitable



import websockets



log = logging.getLogger("hub-client")





def _load_or_create_peer_id() -> str:

    """Return a stable 16-char hex ID, persisted to ~/.openclaw/hub_peer_id."""

    path = os.path.join(os.path.expanduser("~"), ".openclaw", "hub_peer_id")

    try:

        os.makedirs(os.path.dirname(path), exist_ok=True)

        if os.path.exists(path):

            saved = open(path).read().strip()

            if len(saved) == 16 and all(c in '0123456789abcdef' for c in saved):

                return saved

        raw = str(time.time_ns()).encode()

        new_id = hashlib.md5(raw).hexdigest()[:16]

        open(path, "w").write(new_id)

        log.info("Hub: created stable peer_id %s -?%s", new_id, path)

        return new_id

    except Exception as e:

        log.warning("Hub: cannot persist peer_id (%s), using ephemeral id", e)

        return hashlib.md5(str(time.time_ns()).encode()).hexdigest()[:16]





class HubClient:

    def __init__(

        self,

        hub_ws_url: str,

        name: str,

        avatar: str = "🦞",

        on_message: Callable[[str, str, str], Awaitable[str | None]] | None = None,

    ):

        """

        hub_ws_url  -?e.g. "ws://hub.example.com:9000/ws"

        name        -?display name shown to other peers

        avatar      -?emoji avatar

        on_message  -?async (from_id, from_name, content) -> reply_str | None

                      Return a string to auto-reply, None to stay silent.

        """

        self.url        = hub_ws_url

        self.name       = name

        self.avatar     = avatar

        self.on_message = on_message



        # Stable 16-char hex peer ID -?MD5(time_ns), persisted to ~/.openclaw/hub_peer_id

        self.peer_id: str          = _load_or_create_peer_id()

        self.connected: bool       = False

        self._ws                   = None

        self._task: asyncio.Task | None = None

        self._groups: dict[str, dict]   = {}  # group_id -> {id,name,members}



    # ── Public API ─────────────────────────────────────────────────────────────



    async def start(self):

        """Start the background reconnect loop."""

        self._task = asyncio.create_task(self._run(), name="hub-client")



    async def stop(self):

        """Gracefully shut down."""

        self.connected = False

        if self._task:

            self._task.cancel()

            try:

                await self._task

            except asyncio.CancelledError:

                pass

        if self._ws:

            try:

                await self._ws.close()

            except Exception:

                pass



    # ── Internals ──────────────────────────────────────────────────────────────



    async def _run(self):

        retry = 0

        while True:

            try:

                async with websockets.connect(self.url) as ws:

                    self._ws = ws

                    await ws.send(json.dumps({

                        "type":   "register",

                        "id":     self.peer_id,

                        "name":   self.name,

                        "avatar": self.avatar,

                    }))



                    # Expect registered confirmation

                    raw = await asyncio.wait_for(ws.recv(), timeout=10)

                    msg = json.loads(raw)

                    if msg.get("type") != "registered":

                        log.warning("Hub: unexpected first message: %s", msg.get("type"))

                        await ws.close()

                    else:

                        self.peer_id   = msg["peer_id"]

                        self.connected = True

                        retry = 0

                        log.info("Hub: connected as %s (id=%s)", self.name, self.peer_id)



                        async for raw in ws:

                            try:

                                msg = json.loads(raw)

                            except Exception:

                                continue

                            await self._handle(msg, ws)



            except asyncio.CancelledError:

                return

            except Exception as e:

                self.connected = False

                self._ws = None

                wait = min(60, 2 ** min(retry, 6))

                log.warning("Hub: disconnected (%s) -?retry in %ds", e, wait)

                await asyncio.sleep(wait)

                retry += 1



    async def _handle(self, msg: dict, ws):

        t = msg.get("type", "")



        if t == "friend_request":

            # Auto-accept every incoming friend request

            log.info("Hub: friend request from %s (%s) -?auto-accepting",

                     msg.get("from_name"), msg.get("from"))

            try:

                await ws.send(json.dumps({

                    "type": "friend_accept",

                    "from": msg["from"],

                }))

            except Exception as e:

                log.warning("Hub: failed to send friend_accept: %s", e)



        elif t == "message":

            from_id   = msg.get("from", "")

            from_name = msg.get("from_name", "?")

            content   = msg.get("content", "")

            log.info("Hub: message from %s: %s", from_name, content[:80])



            if self.on_message:

                try:

                    reply = await self.on_message(from_id, from_name, content)

                    if reply and self._ws:

                        await self._ws.send(json.dumps({

                            "type":    "message",

                            "to":      from_id,

                            "content": reply,

                        }))

                except Exception as e:

                    log.warning("Hub: on_message callback error: %s", e)



        elif t == "group_created":

            g = msg.get("group") or {}

            self._groups[g.get("id", "")] = g

            log.info("Hub: joined group '%s' (%s)", g.get("name"), g.get("id", ""))



        elif t == "group_message":

            group_id  = msg.get("group_id", "")

            from_id   = msg.get("from", "")

            from_name = msg.get("from_name", "?")

            content   = msg.get("content", "")

            group_name = (self._groups.get(group_id) or {}).get("name", group_id)

            log.info("Hub: group '%s' msg from %s: %s", group_name, from_name, content[:80])



            if self.on_message:

                try:

                    reply = await self.on_message(from_id, from_name, content)

                    if reply and self._ws:

                        await self._ws.send(json.dumps({

                            "type":     "group_message",

                            "group_id": group_id,

                            "content":  reply,

                        }))

                except Exception as e:

                    log.warning("Hub: group on_message callback error: %s", e)



        elif t == "pong":

            pass



        elif t in ("peer_joined", "peer_left", "friend_accepted",

                   "friend_rejected", "friend_request_sent"):

            log.debug("Hub event: %s", t)



        else:

            log.debug("Hub: unhandled msg type=%s", t)

