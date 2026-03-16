"""
OpenIM REST polling client for mamclawclient
────────────────────────────────────────────
Connects to the OpenIM HTTP API as a regular user, polls for new messages,
and sends text replies.

Auto-login (recommended):
  Set IM_EMAIL + IM_PASSWORD and leave IM_TOKEN empty.  The client will
  call the Chat API to obtain a fresh imToken on startup automatically.

Env vars (all optional, can also be passed as constructor args):
  IM_EMAIL          Account email (for auto-login)
  IM_PASSWORD       Account password (for auto-login)
  IM_CHAT_ADDR      Chat API address, e.g. http://47.239.0.170:10008
                    (defaults to IM_API_ADDR with port replaced to 10008)
  IM_USER_ID        UserID of this virtual user (resolved via login if empty)
  IM_TOKEN          imToken override (skip auto-login if set)
  IM_API_ADDR       OpenIM IM API address, e.g. http://47.239.0.170:10002
    IM_PLATFORM_ID    Platform ID (default 3 = Windows)
  IM_POLL_INTERVAL  Poll interval in seconds (default 3)
  IM_ENABLED        Set to 0 to disable (default 1)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Awaitable, Callable

import httpx

log = logging.getLogger("openim")


class _TokenExpired(Exception):
    pass


class OpenIMClient:
    """
    Polls OpenIM REST API for new messages and dispatches them to an
    async callback.  Replies from the callback are sent back automatically.

    on_message signature:
        async def handler(from_id, from_name, content, *,
                          session_type, group_id, conv_id) -> str | None
        Return a string to reply, None to stay silent.
    """

    def __init__(
        self,
        *,
        user_id: str = "",
        im_token: str = "",
        api_addr: str = "http://47.239.0.170:10002",
        poll_interval: float = 3.0,
        on_message: Callable[..., Awaitable[str | None]] | None = None,
    ):
        self.user_id       = user_id or os.getenv("IM_USER_ID", "")
        self.token         = im_token or os.getenv("IM_TOKEN", "")
        self.base          = (api_addr or os.getenv("IM_API_ADDR", "http://47.239.0.170:10002")).rstrip("/")
        self.poll_interval = poll_interval
        self.on_message    = on_message
        self.platform_id   = int(os.getenv("IM_PLATFORM_ID", "3"))  # 3=Windows, avoid conflict with browser (5=Web)
        self.use_bridge    = os.getenv("OPENIM_USE_BRIDGE", "1") != "0"
        self.bridge_url    = os.getenv("OPENIM_BRIDGE_URL", "http://127.0.0.1:8788").rstrip("/")

        # Auto-login credentials (used when IM_TOKEN is not set)
        self._email    = os.getenv("IM_EMAIL", "")
        self._password = os.getenv("IM_PASSWORD", "")
        # Chat API address (for login); derives from IM_API_ADDR if not set
        _chat_default = self.base.rsplit(":", 1)[0] + ":10008"
        self._chat_base = os.getenv("IM_CHAT_ADDR", _chat_default).rstrip("/")

        self._seen: dict[str, str] = {}        # conv_id → last seen msgInfo.clientMsgID
        self._conv_meta: dict[str, dict] = {}  # conv_id → full conversation element
        self._ready      = False
        self._start_time = 0.0
        self._task: asyncio.Task | None = None

    # ── public API ────────────────────────────────────────────────────────────

    async def _login(self) -> bool:
        """Login via Chat API to obtain a fresh imToken.  Returns True on success."""
        if not self._email or not self._password:
            return False
        body = {
            "email":      self._email,
            "password":   self._password,
            "areaCode":   "",
            "platform":   self.platform_id,
        }
        headers = {
            "Content-Type": "application/json",
            "operationID":  f"py{int(time.time() * 1000)}",
        }
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(f"{self._chat_base}/account/login", json=body, headers=headers)
                data = r.json()
        except Exception as e:
            log.error("OpenIM login request failed: %s", e)
            return False
        if data.get("errCode", -1) != 0:
            log.error("OpenIM login failed: [%s] %s", data.get("errCode"), data.get("errMsg"))
            return False
        d = data.get("data") or {}
        self.token    = d.get("imToken", "")
        self.user_id  = self.user_id or d.get("userID", "")
        log.info("OpenIM login OK  userID=%s  token=%.20s…", self.user_id, self.token)
        return bool(self.token)

    async def start(self):
        if self.use_bridge:
            ok = await self._bridge_health()
            if not ok:
                log.error("OpenIM bridge unavailable: %s", self.bridge_url)
                return
            self._start_time = time.time()
            self._task = asyncio.create_task(self._run(), name="openim-poll")
            log.info("OpenIM client started via bridge  bridge=%s  user=%s  poll=%.1fs",
                     self.bridge_url, self.user_id or "-", self.poll_interval)
            return

        # Try auto-login if no static token
        if not self.token and self._email:
            if not await self._login():
                log.error("OpenIM client failed to login — not starting")
                return
        if not self.user_id or not self.token:
            log.warning("OpenIM client disabled: IM_USER_ID or IM_TOKEN not set")
            return
        self._start_time = time.time()
        self._task = asyncio.create_task(self._run(), name="openim-poll")
        log.info("OpenIM client started  user=%s  api=%s  poll=%.1fs",
                 self.user_id, self.base, self.poll_interval)

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def send_text(
        self,
        recv_id: str,
        text: str,
        *,
        group_id: str = "",
        session_type: int = 1,
    ) -> dict:
        """Send a text message.  session_type: 1=C2C, 3=group."""
        if self.use_bridge:
            return await self._bridge_send_text(
                recv_id=recv_id,
                text=text,
                group_id=group_id,
                session_type=session_type,
            )

        # In this deployment, groupID is the most reliable signal for group replies.
        if group_id:
            session_type = 3

        body = {
            "sendID":           self.user_id,
            "recvID":           recv_id if session_type != 3 else "",
            "groupID":          group_id,
            "senderPlatformID": self.platform_id,
            "content":          {"content": text},
            "contentType":      101,          # text
            "sessionType":      session_type,
            "isOnlineOnly":     False,
            "notOfflinePush":   False,
            "sendTime":         0,
            "offlinePushInfo": {
                "title": "新消息", "desc": "", "ex": "",
                "iOSPushSound": "default", "iOSBadgeCount": True,
            },
        }
        r = await self._post("/msg/send_msg", body)
        if r.get("errCode", 0) != 0:
            code = r.get("errCode")
            msg  = r.get("errMsg")
            dlt  = r.get("errDlt", "")
            if code == 1002:
                log.error("send_msg denied [%s] %s - %s", code, msg, dlt)
                log.error("OpenIM server requires app manager privileges for /msg/send_msg in this deployment")
            else:
                log.warning("send_msg error [%s] %s - %s", code, msg, dlt)
        return r

    # ── internals ─────────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "operationID":  f"py{int(time.time() * 1000)}",
            "token":        self.token,
        }

    async def _post(self, path: str, body: dict, *, read_timeout: float = 10) -> dict:
        timeout = httpx.Timeout(connect=5.0, read=read_timeout, write=5.0, pool=5.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(f"{self.base}{path}", json=body, headers=self._headers())
                if r.status_code != 200:
                    log.warning("POST %s → HTTP %d: %s", path, r.status_code, r.text[:200])
                    return {}
                return r.json()
        except Exception as e:
            log.warning("POST %s error: %s", path, e)
            return {}

    async def _bridge_health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{self.bridge_url}/health")
            if r.status_code != 200:
                log.warning("OpenIM bridge health HTTP %s: %s", r.status_code, r.text[:200])
                return False
            d = r.json()
            if not d.get("ok"):
                log.warning("OpenIM bridge health not ok: %s", d.get("error") or d)
                return False
            bridge_uid = d.get("userID") or d.get("recvUserID")
            if bridge_uid and not self.user_id:
                self.user_id = str(bridge_uid)
            return True
        except Exception as e:
            log.warning("OpenIM bridge health check failed: %s", e)
            return False

    async def _bridge_poll(self) -> dict[str, dict] | None:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5, read=20, write=5, pool=5)) as c:
                r = await c.post(f"{self.bridge_url}/poll", json={})
            if r.status_code != 200:
                log.warning("bridge /poll HTTP %s: %s", r.status_code, r.text[:200])
                return {}
            d = r.json()
            if not d.get("ok"):
                log.warning("bridge /poll error: %s", d)
                return {}
            bridge_uid = d.get("userID") or d.get("recvUserID")
            if bridge_uid and not self.user_id:
                self.user_id = str(bridge_uid)
            return d.get("conversations") or {}
        except Exception as e:
            log.warning("bridge /poll error: %s", e)
            return {}

    async def _bridge_send_text(
        self,
        *,
        recv_id: str,
        text: str,
        group_id: str = "",
        session_type: int = 1,
    ) -> dict:
        payload = {
            "recvID": recv_id if session_type != 3 else "",
            "groupID": group_id,
            "text": text,
            "sessionType": session_type,
        }
        if group_id:
            payload["sessionType"] = 3

        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(f"{self.bridge_url}/send", json=payload)
            if r.status_code != 200:
                log.warning("bridge /send HTTP %s: %s", r.status_code, r.text[:200])
                return {"errCode": -1, "errMsg": f"bridge_http_{r.status_code}"}
            d = r.json()
            if d.get("ok"):
                return {"errCode": 0, "errMsg": ""}
            code = int(d.get("errCode") or -1)
            msg = d.get("errMsg") or d.get("error") or "bridge_send_failed"
            dlt = d.get("errDlt", "")
            if code == 1002:
                log.error("bridge send denied [%s] %s - %s", code, msg, dlt)
            else:
                log.warning("bridge send error [%s] %s - %s", code, msg, dlt)
            return {"errCode": code, "errMsg": msg, "errDlt": dlt}
        except Exception as e:
            log.warning("bridge /send error: %s", e)
            return {"errCode": -1, "errMsg": str(e)}

    async def _get_sorted_conversations(self) -> dict[str, dict] | None:
        """Return conversationID -> msgInfo map using get_sorted_conversation_list."""
        if self.use_bridge:
            return await self._bridge_poll()

        r = await self._post("/conversation/get_sorted_conversation_list",
                             {"userID": self.user_id,
                              "pagination": {"pageNumber": 1, "showNumber": 200}},
                             read_timeout=15)
        code = r.get("errCode", 0)
        if code in (1501, 1502, 1503, 1504, 1505, 1506):
            raise _TokenExpired(r.get("errMsg"))
        if code != 0:
            log.warning("get_sorted_conversation_list error %s: %s", code, r.get("errMsg"))
            return {}

        convs = (r.get("data") or {}).get("conversationElems") or []
        self._conv_meta = {c["conversationID"]: c for c in convs if c.get("conversationID")}

        out: dict[str, dict] = {}
        for c in convs:
            cid = c.get("conversationID")
            if cid:
                out[cid] = c.get("msgInfo") or {}
        return out

    async def _poll_once(self):
        conv_map = await self._get_sorted_conversations()
        if conv_map is None:
            return

        for cid, msg_info in conv_map.items():
            cur_mid = str(msg_info.get("clientMsgID") or "")

            if not self._ready:
                self._seen[cid] = cur_mid
                continue

            last = self._seen.get(cid)
            if last is None:
                self._seen[cid] = cur_mid
                if cur_mid and (int(msg_info.get("LatestMsgRecvTime") or 0) / 1000) >= self._start_time:
                    await self._dispatch_msg_info(cid, msg_info)
                continue

            if not cur_mid or cur_mid == last:
                continue

            self._seen[cid] = cur_mid
            if (int(msg_info.get("LatestMsgRecvTime") or 0) / 1000) >= self._start_time:
                await self._dispatch_msg_info(cid, msg_info)

    async def _dispatch_msg_info(self, conv_id: str, msg_info: dict):
        """Process one latest-message snapshot from sorted conversation list."""
        send_id = msg_info.get("sendID", "")
        if send_id == self.user_id:
            return

        content_type = int(msg_info.get("contentType") or 0)
        if content_type != 101:
            return

        raw_content = msg_info.get("content", "")
        text = raw_content if isinstance(raw_content, str) else ""
        if isinstance(raw_content, str):
            try:
                content_obj = json.loads(raw_content)
                if isinstance(content_obj, dict):
                    text = content_obj.get("content") or content_obj.get("text") or raw_content
            except Exception:
                text = raw_content

        if not str(text).strip():
            return

        nickname  = msg_info.get("senderName") or send_id
        group_id  = msg_info.get("groupID", "")
        sess_type = 3 if group_id else int(msg_info.get("sessionType") or 1)

        log.info("IM ← [%s] %s: %s", conv_id, nickname, str(text)[:100])

        if self.on_message:
            try:
                reply = await self.on_message(
                    send_id, nickname, str(text),
                    session_type=sess_type,
                    group_id=group_id,
                    conv_id=conv_id,
                )
                if reply:
                    log.info("IM → [%s] replying (session=%s, group=%s, recv=%s): %s",
                             conv_id, sess_type, group_id or "-", send_id or "-", reply[:80])
                    await self.send_text(
                        send_id, reply,
                        group_id=group_id,
                        session_type=sess_type,
                    )
            except Exception as e:
                log.warning("on_message handler error: %s", e, exc_info=True)

    async def _dispatch(self, msg: dict, conv: dict):
        """Process one incoming message."""
        if msg.get("sendID") == self.user_id:
            return   # ignore own messages
        if msg.get("contentType", 0) != 101:
            return   # text only

        # textElem is the standard SDK field; some REST responses use content dict
        text = (
            (msg.get("textElem") or {}).get("content")
            or (msg.get("content") or {}).get("content")
            or ""
        )
        if not text.strip():
            return

        send_id    = msg.get("sendID", "")
        nickname   = msg.get("senderNickname") or send_id
        sess_type  = conv.get("conversationType", 1)
        group_id   = conv.get("groupID", "") or msg.get("groupID", "")
        conv_id    = conv.get("conversationID", "")

        log.info("IM ← [%s] %s: %s", conv_id, nickname, text[:100])

        if self.on_message:
            try:
                reply = await self.on_message(
                    send_id, nickname, text,
                    session_type=sess_type,
                    group_id=group_id,
                    conv_id=conv_id,
                )
                if reply:
                    log.info("IM → [%s] replying: %s", conv_id, reply[:80])
                    await self.send_text(
                        send_id, reply,
                        group_id=group_id,
                        session_type=sess_type,
                    )
            except Exception as e:
                log.warning("on_message handler error: %s", e, exc_info=True)

    async def _run(self):
        while True:
            had_error = False
            try:
                await self._poll_once()
            except _TokenExpired:
                log.warning("OpenIM token expired — re-logging in")
                if not await self._login():
                    log.error("Re-login failed; pausing 30s before retry")
                    await asyncio.sleep(30)
                    continue
                self._ready = False
                self._seen.clear()
                self._conv_meta.clear()
                had_error = True
            except Exception as e:
                log.warning("openim poll error: %s", e, exc_info=True)
                had_error = True

            if not self._ready:
                self._ready = True
                log.info("OpenIM client ready (user=%s, %d conversations tracked)",
                         self.user_id, len(self._seen))

            # Always sleep between polls to avoid hammering the server
            await asyncio.sleep(self.poll_interval if had_error else max(self.poll_interval, 1))
