// Service worker: opens the side panel and keeps one session record per meeting.
// Events are applied one at a time (queue) so concurrent messages never overwrite each other.

function enableSidePanelOnClick() {
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => { });
}

async function injectOpenTabs() {
  if (!chrome.scripting || !chrome.tabs) return;
  try {
    const tabs = await chrome.tabs.query({ url: 'https://meet.google.com/*' });
    for (const tab of tabs) {
      if (tab.id) {
        chrome.scripting.executeScript({
          target: { tabId: tab.id },
          files: ['meet-core.js', 'content.js']
        }).catch(() => { });
      }
    }
  } catch (e) { }
}

chrome.runtime.onInstalled.addListener(() => {
  enableSidePanelOnClick();
  injectOpenTabs();
});
chrome.runtime.onStartup.addListener(() => {
  enableSidePanelOnClick();
  injectOpenTabs();
});

let queue = Promise.resolve();

async function callBackend(path, options = {}) {
  const hosts = ['http://127.0.0.1:8000', 'http://localhost:8000'];
  let lastErr = null;
  for (const host of hosts) {
    try {
      const res = await fetch(host + path, options);
      const data = await res.json().catch(() => ({}));
      return { ok: res.ok, status: res.status, data };
    } catch (err) {
      lastErr = err;
    }
  }
  return { ok: false, error: lastErr ? (lastErr.message || String(lastErr)) : 'Backend unreachable' };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.kind === 'meet_event' && msg.ev) {
    queue = queue.then(() => apply(msg.ev)).catch((e) => console.error('[MeetIntel]', e));
    sendResponse({ ok: true });
    return false;
  } else if (msg && msg.kind === 'device_override') {
    queue = queue.then(async () => {
      const key = 'session:' + msg.meetingId;
      const stored = await chrome.storage.local.get(key);
      const s = stored[key];
      if (s) {
        s.deviceOverrides = s.deviceOverrides || {};
        s.deviceOverrides[msg.name.toLowerCase()] = msg.device;
        s.media.push({
          type: 'participant_update',
          name: msg.name,
          device: msg.device,
          ts: new Date().toISOString()
        });
        await chrome.storage.local.set({ [key]: s });
      }
    }).catch((e) => console.error('[MeetIntel]', e));
    sendResponse({ ok: true });
    return false;
  } else if (msg && msg.kind === 'bot_launch') {
    callBackend('/bot/launch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: msg.url, name: msg.name || 'Notes Bot' })
    }).then(sendResponse).catch((err) => sendResponse({ ok: false, error: err.message }));
    return true; // asynchronous response
  } else if (msg && msg.kind === 'bot_stop') {
    callBackend(`/bot/${msg.meetingId}/stop`, { method: 'POST' })
      .then(sendResponse).catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  } else if (msg && msg.kind === 'bot_status') {
    callBackend(`/bot/${msg.meetingId}/status`)
      .then(sendResponse).catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  }
  sendResponse({ ok: false });
  return false;
});

const meetTabs = new Map();

function parseMeetUrl(url) {
  if (!url) return null;
  const m = url.match(/meet\.google\.com\/(?:meet\/)?([a-z]{3}-[a-z]{4}-[a-z]{3})/i);
  return m ? m[1].toLowerCase() : null;
}

if (chrome.tabs && chrome.tabs.onUpdated) {
  chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    if (tab && tab.url) {
      const mid = parseMeetUrl(tab.url);
      if (mid) {
        meetTabs.set(tabId, mid);
        chrome.storage.local.set({ lastMeeting: mid }).catch(() => { });
      } else if (meetTabs.has(tabId)) {
        const oldMid = meetTabs.get(tabId);
        meetTabs.delete(tabId);
        closeMeetingIfNoTabs(oldMid);
      }
    }
  });
}

if (chrome.tabs && chrome.tabs.onActivated) {
  chrome.tabs.onActivated.addListener(async (activeInfo) => {
    try {
      const tab = await chrome.tabs.get(activeInfo.tabId);
      if (tab && tab.url) {
        const mid = parseMeetUrl(tab.url);
        if (mid) {
          meetTabs.set(activeInfo.tabId, mid);
          await chrome.storage.local.set({ lastMeeting: mid });
        }
      }
    } catch (_) { }
  });
}

if (chrome.tabs && chrome.tabs.onRemoved) {
  chrome.tabs.onRemoved.addListener((tabId) => {
    if (meetTabs.has(tabId)) {
      const mid = meetTabs.get(tabId);
      meetTabs.delete(tabId);
      closeMeetingIfNoTabs(mid);
    }
  });
}

async function closeMeetingIfNoTabs(mid) {
  try {
    const tabs = await chrome.tabs.query({ url: '*://meet.google.com/*' });
    const hasTab = (tabs || []).some((t) => parseMeetUrl(t.url) === mid);
    if (!hasTab) {
      const key = 'session:' + mid;
      const stored = await chrome.storage.local.get(key);
      const s = stored[key];
      if (s && !s.endedAt) {
        s.endedAt = new Date().toISOString();
        await chrome.storage.local.set({ [key]: s });
      }
    }
  } catch (e) { }
}

async function apply(ev) {
  const key = 'session:' + ev.meetingId;
  const stored = await chrome.storage.local.get(key);
  let s = stored[key] || {
    meetingId: ev.meetingId, startedAt: null, endedAt: null,
    segments: {}, order: [], media: []
  };

  switch (ev.type) {
    case 'call_started': {
      const isPreviousCallEnded = !!s.endedAt;
      const lastEventTs = s.media.length ? s.media[s.media.length - 1].ts : (s.startedAt || null);
      const gapMs = lastEventTs ? (Date.parse(ev.ts) - Date.parse(lastEventTs)) : 0;

      // Rejoining same meeting link after a call ended or after gap (>60s):
      // Archive the old call to history and start fresh with empty segments and media.
      if (isPreviousCallEnded || (s.startedAt && gapMs > 60000)) {
        if (s.startedAt && (s.order.length || s.media.length)) {
          const archiveKey = 'history:' + ev.meetingId + ':' + s.startedAt.replace(/[:.]/g, '-');
          await chrome.storage.local.set({ [archiveKey]: s }).catch(() => { });
        }
        s = {
          meetingId: ev.meetingId,
          startedAt: ev.ts,
          endedAt: null,
          segments: {},
          order: [],
          media: []
        };
        await chrome.storage.local.remove('summary:' + ev.meetingId).catch(() => { });
      } else {
        s.startedAt = s.startedAt || ev.ts;
        s.endedAt = null;
      }
      break;
    }
    case 'call_ended':
      s.endedAt = ev.ts;
      break;
    case 'caption':
      if (!s.startedAt) s.startedAt = ev.start_ts;
      if (!s.segments[ev.id]) s.order.push(ev.id);
      s.segments[ev.id] = {
        id: ev.id, speaker: ev.speaker, text: ev.text,
        start_ts: ev.start_ts, end_ts: ev.end_ts, final: ev.final
      };
      break;
    case 'participant_join':
    case 'participant_leave':
    case 'participant_update':
    case 'cam_on':
    case 'cam_off':
    case 'mic_on':
    case 'mic_off':
      s.media.push({ type: ev.type, id: ev.id, name: ev.name, cam: ev.cam, mic: ev.mic, device: ev.device, ts: ev.ts });
      break;
    case 'snapshot':
      s.lastSnapshot = { total: ev.total, cam: ev.cam_on, mic: ev.mic_on, ts: ev.ts };
      break;
    default:
      return;
  }
  await chrome.storage.local.set({ [key]: s, lastMeeting: ev.meetingId });
}
