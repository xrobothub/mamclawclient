import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { getSDK, CbEvents } from '@openim/client-sdk';

function loadDotEnvLikeFile() {
  try {
    const __dirname = path.dirname(fileURLToPath(import.meta.url));
    const envPath = path.join(__dirname, '.env');
    if (!fs.existsSync(envPath)) return;
    const raw = fs.readFileSync(envPath, 'utf8');
    for (const line of raw.split(/\r?\n/)) {
      const s = line.trim();
      if (!s || s.startsWith('#')) continue;
      const i = s.indexOf('=');
      if (i <= 0) continue;
      const k = s.slice(0, i).trim();
      let v = s.slice(i + 1).trim();
      if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
        v = v.slice(1, -1);
      }
      if (!(k in process.env)) {
        process.env[k] = v;
      }
    }
  } catch {
    // ignore dotenv parse failures
  }
}

loadDotEnvLikeFile();

const sdk = getSDK();

const HOST = process.env.OPENIM_BRIDGE_HOST || '127.0.0.1';
const PORT = Number(process.env.OPENIM_BRIDGE_PORT || 8788);

const IM_API = (process.env.OPENIM_BRIDGE_IM_API || process.env.IM_API_ADDR || 'http://47.239.0.170:10002').trim();
const IM_WS = (process.env.OPENIM_BRIDGE_IM_WS || 'ws://47.239.0.170:10001').trim();
const PLATFORM_ID = Number(process.env.OPENIM_BRIDGE_PLATFORM_ID || process.env.IM_PLATFORM_ID || 5);
const PULL_INTERVAL_MS = Number(process.env.OPENIM_BRIDGE_PULL_INTERVAL_MS || 3000);
const ACTIVE_POLL = process.env.OPENIM_BRIDGE_ACTIVE_POLL === '1';
const SDK_LOG_LEVEL = Number(process.env.OPENIM_BRIDGE_SDK_LOG_LEVEL || 5); // 5=Silent
const LOG_RECV = process.env.OPENIM_BRIDGE_LOG_RECV === '1';
const CHAT_API = (
  process.env.OPENIM_BRIDGE_CHAT_API
  || process.env.IM_CHAT_ADDR
  || IM_API.replace(/:\d+$/, ':10008')
).trim();

const LOGIN_EMAIL = (process.env.OPENIM_BRIDGE_EMAIL_OR_PHONE || '').trim();
const LOGIN_PASSWORD = (process.env.OPENIM_BRIDGE_PASSWORD || '').trim();

let sdkUserId = (process.env.OPENIM_BRIDGE_USER_ID || '').trim();
let sdkToken = (process.env.OPENIM_BRIDGE_TOKEN || '').trim();

let ready = false;
let connecting = null;
let lastError = '';
let pullTimer = null;

// conversationID -> msgInfo-like shape used by openim_client.py
const latestByConversation = new Map();

function writeJson(res, statusCode, payload) {
  res.writeHead(statusCode, {
    'Content-Type': 'application/json; charset=utf-8',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
  });
  res.end(JSON.stringify(payload));
}

function stringifyErr(e) {
  if (!e) return 'unknown error';
  if (typeof e === 'string') return e;
  if (e?.message && typeof e.message === 'string') return e.message;
  if (typeof e?.errMsg === 'string' && e.errMsg) {
    const code = e?.errCode ?? '';
    return code !== '' ? `[${code}] ${e.errMsg}` : e.errMsg;
  }
  try {
    return JSON.stringify(e);
  } catch {
    return String(e);
  }
}

function toConvID(msg) {
  const gid = String(msg?.groupID || '');
  if (gid) return `sg_${gid}`;

  const sendID = String(msg?.sendID || '');
  const recvID = String(msg?.recvID || '');
  const peer = sendID === sdkUserId ? recvID : sendID;
  return `si_${sdkUserId}_${peer}`;
}

function toMsgInfo(msg) {
  const text = String(msg?.textElem?.content || '').trim();
  const content = text ? JSON.stringify({ content: text }) : String(msg?.content || '');
  return {
    clientMsgID: msg?.clientMsgID || '',
    sendID: msg?.sendID || '',
    senderName: msg?.senderNickname || msg?.senderName || msg?.sendID || '',
    groupID: msg?.groupID || '',
    sessionType: msg?.groupID ? 3 : (msg?.sessionType || 1),
    contentType: msg?.contentType || 101,
    content,
    LatestMsgRecvTime: msg?.sendTime || Date.now(),
  };
}

function onIncoming(msg) {
  if (!msg) return;
  if (String(msg?.sendID || '') === String(sdkUserId || '')) {
    // Ignore self-sent messages to avoid echo loops into main.
    return;
  }
  const convID = toConvID(msg);
  const next = toMsgInfo(msg);
  const nextId = String(next?.clientMsgID || '');
  const prevId = String(latestByConversation.get(convID)?.clientMsgID || '');
  if (!nextId || nextId === prevId) return;
  latestByConversation.set(convID, next);
  const text = String(msg?.textElem?.content || '').slice(0, 80);
  if (LOG_RECV) {
    console.log(`[bridge] recv ${convID} msg=${nextId.slice(0, 8)} from=${msg?.sendID || '-'} text=${text}`);
  }
}

function toMsgInfoFromConversation(conv) {
  const raw = String(conv?.latestMsg || '').trim();
  if (!raw) return null;

  let msg = null;
  try {
    msg = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!msg || typeof msg !== 'object') return null;
  if (String(msg?.sendID || '') === String(sdkUserId || '')) return null;

  const content = String(msg?.content || '');
  const contentType = Number(msg?.contentType || 0);
  return {
    clientMsgID: String(msg?.clientMsgID || ''),
    sendID: String(msg?.sendID || ''),
    senderName: String(msg?.senderNickname || msg?.senderName || msg?.sendID || ''),
    groupID: String(msg?.groupID || conv?.groupID || ''),
    sessionType: Number(msg?.sessionType || conv?.conversationType || (conv?.groupID ? 3 : 1)),
    contentType,
    content,
    LatestMsgRecvTime: Number(msg?.sendTime || conv?.latestMsgSendTime || Date.now()),
  };
}

async function refreshConversations(reason = 'pull') {
  await ensureLogin();

  let offset = 0;
  const count = 100;
  let loops = 0;

  while (loops < 10) {
    loops += 1;
    const resp = await sdk.getConversationListSplit({ offset, count });
    const list = Array.isArray(resp?.data) ? resp.data : [];
    if (!list.length) break;

    for (const conv of list) {
      const convID = String(conv?.conversationID || '');
      if (!convID) continue;
      const mi = toMsgInfoFromConversation(conv);
      if (!mi) continue;

      const prev = latestByConversation.get(convID);
      const nextId = String(mi.clientMsgID || '');
      const prevId = String(prev?.clientMsgID || '');
      if (!nextId || nextId === prevId) continue;

      latestByConversation.set(convID, mi);
      const text = (() => {
        try {
          const obj = JSON.parse(String(mi.content || '{}'));
          return String(obj?.content || '').slice(0, 80);
        } catch {
          return '';
        }
      })();
      if (LOG_RECV) {
        console.log(`[bridge] recv ${convID} msg=${nextId.slice(0, 8)} from=${mi.sendID || '-'} text=${text}`);
      }
    }

    if (list.length < count) break;
    offset += count;
  }
}

function startActivePulling() {
  if (pullTimer) return;
  pullTimer = setInterval(() => {
    refreshConversations('tick').catch(e => {
      lastError = e?.message || String(e);
      // Keep idle mode quiet: do not spam logs on background pull failures.
    });
  }, Math.max(1000, PULL_INTERVAL_MS));
}

async function ensureLogin() {
  if (ready) return;
  if (connecting) {
    await connecting;
    return;
  }
  if (!sdkUserId || !sdkToken) {
    if (!LOGIN_EMAIL || !LOGIN_PASSWORD) {
      throw new Error('Missing token and login credentials. Set OPENIM_BRIDGE_TOKEN or OPENIM_BRIDGE_EMAIL_OR_PHONE/OPENIM_BRIDGE_PASSWORD');
    }

    const opid = `bridge${Date.now()}`;
    const loginResp = await fetch(`${CHAT_API}/account/login`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        operationID: opid,
      },
      body: JSON.stringify({
        email: LOGIN_EMAIL,
        password: LOGIN_PASSWORD,
        areaCode: '',
        platform: PLATFORM_ID,
      }),
    });

    const loginJson = await loginResp.json();
    if (!loginResp.ok || loginJson?.errCode !== 0) {
      throw new Error(`Chat login failed [${loginJson?.errCode ?? loginResp.status}] ${loginJson?.errMsg || loginResp.statusText || ''}`);
    }

    sdkUserId = String(loginJson?.data?.userID || sdkUserId || '');
    sdkToken = String(loginJson?.data?.imToken || '');
    if (!sdkUserId || !sdkToken) {
      throw new Error('Chat login succeeded but userID/imToken missing');
    }
  }

  connecting = (async () => {
    const loginRes = await sdk.login({
      userID: sdkUserId,
      token: sdkToken,
      apiAddr: IM_API,
      wsAddr: IM_WS,
      platformID: PLATFORM_ID,
      logLevel: SDK_LOG_LEVEL,
    });

    if (loginRes?.errCode && loginRes.errCode !== 0) {
      throw new Error(`SDK login failed (${loginRes.errCode}): ${loginRes.errMsg || ''}`);
    }

    sdk.on(CbEvents.OnRecvNewMessage, (evt) => onIncoming(evt?.data ?? evt));
    sdk.on(CbEvents.OnRecvNewMessages, (evt) => {
      const data = evt?.data ?? evt;
      const list = Array.isArray(data) ? data : [data];
      list.forEach(onIncoming);
    });

    ready = true;
    lastError = '';
    console.log(`[bridge] OpenIM SDK login OK userID=${sdkUserId}`);
    if (ACTIVE_POLL) {
      startActivePulling();
      await refreshConversations('init');
    }
  })();

  try {
    await connecting;
  } catch (e) {
    lastError = e?.message || String(e);
    throw e;
  } finally {
    connecting = null;
  }
}

async function sendText({ recvID = '', groupID = '', text = '' }) {
  const plain = String(text || '').trim();
  if (!plain) throw new Error('text is required');
  if (!recvID && !groupID) throw new Error('recvID or groupID is required');

  await ensureLogin();

  const { data } = await sdk.createTextMessage(plain);
  await sdk.sendMessage({ recvID, groupID, message: data });

  return { ok: true };
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let raw = '';
    req.setEncoding('utf8');
    req.on('data', chunk => {
      raw += chunk;
      if (raw.length > 1024 * 1024) reject(new Error('Payload too large'));
    });
    req.on('end', () => {
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw));
      } catch {
        reject(new Error('Invalid JSON body'));
      }
    });
    req.on('error', reject);
  });
}

const server = http.createServer(async (req, res) => {
  try {
    if (req.method === 'OPTIONS') {
      writeJson(res, 200, { ok: true });
      return;
    }

    if (req.method === 'GET' && req.url === '/health') {
      try {
        await ensureLogin();
        writeJson(res, 200, { ok: true, userID: sdkUserId, ready, error: '' });
      } catch (e) {
        const msg = e?.message || String(e);
        lastError = msg;
        writeJson(res, 200, { ok: false, userID: sdkUserId || null, ready, error: msg });
      }
      return;
    }

    if (req.method === 'POST' && req.url === '/poll') {
      if (ACTIVE_POLL) {
        try {
          await refreshConversations('poll');
        } catch (e) {
          lastError = stringifyErr(e);
          const conversations = Object.fromEntries(latestByConversation.entries());
          // Return 200 to keep main loop stable even if one refresh fails.
          writeJson(res, 200, {
            ok: false,
            errCode: -1,
            errMsg: lastError,
            userID: sdkUserId || null,
            count: latestByConversation.size,
            conversations,
          });
          return;
        }
      }
      const conversations = Object.fromEntries(latestByConversation.entries());
      writeJson(res, 200, { ok: true, userID: sdkUserId, count: latestByConversation.size, conversations });
      return;
    }

    if (req.method === 'POST' && req.url === '/send') {
      const body = await readJsonBody(req);
      const out = await sendText({
        recvID: body.recvID || '',
        groupID: body.groupID || '',
        text: body.text || '',
      });
      writeJson(res, 200, out);
      return;
    }

    writeJson(res, 404, { ok: false, error: 'Not found' });
  } catch (e) {
    const msg = stringifyErr(e);
    lastError = msg;
    writeJson(res, 500, { ok: false, errCode: -1, errMsg: msg, errDlt: '' });
  }
});

server.listen(PORT, HOST, () => {
  console.log(`[bridge] listening on http://${HOST}:${PORT}`);
  // Eager warmup: login and start pulling immediately after startup.
  ensureLogin().catch(e => {
    lastError = e?.message || String(e);
  });
});

process.on('SIGINT', async () => {
  try {
    if (pullTimer) {
      clearInterval(pullTimer);
      pullTimer = null;
    }
    if (ready) {
      await sdk.logout();
    }
  } catch {
    // ignore
  }
  process.exit(0);
});
