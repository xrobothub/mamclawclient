"""
Zulip Client for MamClaw
────────────────────────
Connects to a Zulip server as a regular user account, listens for private
messages and @mentions, calls the same on_message callback as HubClient, and
sends replies back.

Usage in main.py:
    from zulip_client import ZulipClient
    zulip = ZulipClient(
        site     = "https://chat.mamclaw.com",
        email    = "mamclaw@mamclaw.com",
        api_key  = "xxxxxxxxxxxx",
        name     = "龙虾妈妈",
        on_message = on_friend_message,   # same callback as HubClient
    )
    await zulip.start()

.env keys:
    ZULIP_SITE      = https://chat.mamclaw.com
    ZULIP_EMAIL     = mamclaw@mamclaw.com
    ZULIP_API_KEY   = xxxxxxxxxxxx
    ZULIP_LISTEN    = pm,mention    # comma-separated: pm / mention / all
    ZULIP_PRESENCE  = 1             # set 0 to disable presence keep-alive
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Awaitable

log = logging.getLogger("zulip-client")


class ZulipClient:
    """Async wrapper around the synchronous Zulip Python library."""

    def __init__(
        self,
        site: str,
        email: str,
        api_key: str,
        name: str = "MamClaw",
        listen: str = "pm,mention",         # "pm", "mention", "channel:频道名", "all"
        presence: bool = True,
        on_message: Callable[[str, str, str], Awaitable[str | None]] | None = None,
    ):
        self.site       = site.rstrip("/")
        self.email      = email
        self.api_key    = api_key
        self.name       = name
        self.presence   = presence
        self.on_message = on_message

        # Parse listen modes and channel subscriptions
        # e.g. "pm,mention,channel:general,channel:帮助"
        self._listen_modes: set[str] = set()
        self._listen_channels: set[str] = set()  # channel names (lower)
        for part in listen.split(","):
            part = part.strip()
            if part.lower().startswith("channel:"):
                ch = part[8:].strip()
                if ch:
                    self._listen_channels.add(ch.lower())
                    self._listen_modes.add("channel")
            elif part:
                self._listen_modes.add(part.lower())

        self._client    = None   # zulip.Client instance (created on start)
        self._my_id: int | None = None
        self._task: asyncio.Task | None = None
        self._presence_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    # ── Public API ─────────────────────────────────────────────────────────────

    async def start(self):
        """Start listening loop in the background."""
        try:
            import zulip  # noqa: F401 – check import early
        except ImportError:
            raise RuntimeError("zulip package not installed – run: pip install zulip")

        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="zulip-client")
        if self.presence:
            self._presence_task = asyncio.create_task(
                self._keep_presence(), name="zulip-presence"
            )
        log.info("ZulipClient: starting for %s @ %s", self.email, self.site)

    async def stop(self):
        """Gracefully stop."""
        self._stop_event.set()
        for t in [self._task, self._presence_task]:
            if t:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        log.info("ZulipClient: stopped")

    # ── Internals ──────────────────────────────────────────────────────────────

    def _make_client(self):
        import zulip
        return zulip.Client(
            site    = self.site,
            email   = self.email,
            api_key = self.api_key,
        )

    def _get_my_id(self, client) -> int | None:
        try:
            res = client.get_profile()
            if res.get("result") == "success":
                return res["user_id"]
        except Exception as e:
            log.warning("ZulipClient: could not get own user_id: %s", e)
        return None

    async def _run(self):
        loop = asyncio.get_running_loop()
        retry = 0
        while not self._stop_event.is_set():
            try:
                client = await loop.run_in_executor(None, self._make_client)
                self._client = client
                self._my_id  = await loop.run_in_executor(None, self._get_my_id, client)
                log.info("ZulipClient: connected – my user_id=%s", self._my_id)
                retry = 0

                # Build narrow filter
                event_types = ["message"]
                narrow: list[list[str]] = []
                modes = self._listen_modes
                if "all" not in modes:
                    if "pm" in modes and modes <= {"pm"}:
                        # only pm, no mention/channel
                        narrow = [["is", "private"]]
                    elif "mention" in modes and modes <= {"mention"}:
                        # only mention
                        narrow = [["is", "mentioned"]]
                    # pm+mention, channel, or combined: no server-side narrow,
                    # filter in _handle_event instead

                def _event_handler(event: dict):
                    if self._stop_event.is_set():
                        return
                    asyncio.run_coroutine_threadsafe(
                        self._handle_event(event), loop
                    ).result()   # block the Zulip thread until handled

                await loop.run_in_executor(
                    None,
                    lambda: client.call_on_each_event(
                        _event_handler,
                        event_types=event_types,
                        narrow=narrow,
                    ),
                )

            except asyncio.CancelledError:
                return
            except Exception as e:
                self._client = None
                wait = min(60, 2 ** min(retry, 6))
                log.warning("ZulipClient: error (%s) – retry in %ds", e, wait)
                try:
                    await asyncio.wait_for(
                        asyncio.shield(asyncio.sleep(wait)),
                        timeout=wait + 1,
                    )
                except (asyncio.CancelledError, Exception):
                    return
                retry += 1

    async def _handle_event(self, event: dict):
        if event.get("type") != "message":
            return
        msg      = event.get("message", {})
        sender_id   = msg.get("sender_id")
        sender_email = msg.get("sender_email", "")
        sender_name  = msg.get("sender_full_name", sender_email)
        content      = msg.get("content", "").strip()
        msg_type     = msg.get("type", "")        # "private" or "stream"

        # Ignore own messages
        if sender_id == self._my_id or sender_email == self.email:
            return

        # Filter by listen mode
        modes = self._listen_modes
        if "all" not in modes:
            is_pm      = msg_type == "private"
            is_mention = f"@**{self.name}**" in content or f"@{self.email}" in content
            is_channel = (msg_type == "stream" and
                          (not self._listen_channels or
                           msg.get("display_recipient", "").lower() in self._listen_channels))

            want = False
            if "pm" in modes and is_pm:
                want = True
            if "mention" in modes and is_mention:
                want = True
            if "channel" in modes and is_channel:
                want = True
            if not want:
                return

        # Strip @mention prefix from content if present
        import re
        content = re.sub(r"@\*\*[^*]+\*\*\s*", "", content).strip()
        if not content:
            return

        channel_info = ""
        if msg_type == "stream":
            channel_info = f" [#{msg.get('display_recipient','?')}/{msg.get('subject','?')}]"
        log.info("ZulipClient: message from %s (%s)%s: %s",
                 sender_name, sender_id, channel_info, content[:80])

        reply_text = None
        if self.on_message:
            try:
                reply_text = await self.on_message(
                    str(sender_id), sender_name, content
                )
            except Exception as e:
                log.warning("ZulipClient: on_message error: %s", e, exc_info=True)

        if reply_text and self._client:
            await self._send_reply(msg, reply_text)

    async def _send_reply(self, orig_msg: dict, text: str):
        loop = asyncio.get_running_loop()
        client = self._client
        if not client:
            return

        if orig_msg.get("type") == "private":
            payload = {
                "type":    "private",
                "to":      orig_msg["sender_email"],
                "content": text,
            }
        else:  # stream
            payload = {
                "type":    "stream",
                "to":      orig_msg["display_recipient"],
                "topic":   orig_msg["subject"],
                "content": text,
            }

        try:
            res = await loop.run_in_executor(
                None, lambda: client.send_message(payload)
            )
            if res.get("result") != "success":
                log.warning("ZulipClient: send_message failed: %s", res)
        except Exception as e:
            log.warning("ZulipClient: send_message error: %s", e)

    async def _keep_presence(self):
        """Send presence heartbeat every 60 s so this account appears online."""
        loop = asyncio.get_running_loop()
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(60)
                if self._client:
                    await loop.run_in_executor(
                        None,
                        lambda: self._client.call_endpoint(
                            "users/me/presence",
                            method="POST",
                            request={"status": "active", "new_user_input": True,
                                     "slim_presence": True},
                        ),
                    )
                    log.debug("ZulipClient: presence heartbeat sent")
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.debug("ZulipClient: presence error: %s", e)
