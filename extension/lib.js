// Pure helpers shared by the side panel and the tests (no DOM, no chrome.* APIs).
(function (root) {
  'use strict';

  const pad = (n) => String(n).padStart(2, '0');

  function fmtClock(ms) {
    if (ms == null || isNaN(ms)) return '00:00';
    const total = Math.max(0, Math.floor(Number(ms) / 1000));
    const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
    return (h ? pad(h) + ':' : '') + pad(m) + ':' + pad(s);
  }

  function fmtDur(ms) {
    if (ms == null || isNaN(ms)) return '0s';
    const s = Math.max(0, Math.round(Number(ms) / 1000));
    if (s < 60) return s + 's';
    const m = Math.floor(s / 60);
    if (m < 60) return m + 'm ' + pad(s % 60) + 's';
    return Math.floor(m / 60) + 'h ' + pad(m % 60) + 'm';
  }

  // Merge consecutive segments from the same speaker into turns.
  function buildTurns(session, from, to) {
    const ids = session.order.slice(from || 0, to == null ? undefined : to);
    let t0 = session.startedAt ? Date.parse(session.startedAt) : null;
    if (isNaN(t0)) t0 = null;
    if (t0 == null && ids.length) {
      const firstSeg = session.segments[ids[0]];
      if (firstSeg && firstSeg.start_ts) {
        const parsed = Date.parse(firstSeg.start_ts);
        if (!isNaN(parsed)) t0 = parsed;
      }
    }
    const turns = [];
    ids.forEach((id) => {
      const seg = session.segments[id];
      if (!seg || !seg.text) return;
      const start = Date.parse(seg.start_ts);
      const end = Date.parse(seg.end_ts);
      const safeStart = isNaN(start) ? (t0 || Date.now()) : start;
      const safeEnd = isNaN(end) ? safeStart : end;
      const offsetMs = t0 == null ? 0 : Math.max(0, safeStart - t0);
      const last = turns[turns.length - 1];
      if (last && last.speaker === seg.speaker && (safeStart - last.endMs) < 20000) {
        last.text += ' ' + seg.text;
        last.endMs = safeEnd;
      } else {
        turns.push({ speaker: seg.speaker, text: seg.text, startMs: safeStart, endMs: safeEnd,
                     offsetMs: offsetMs });
      }
    });
    return turns;
  }

  function transcriptText(session, from, to) {
    return buildTurns(session, from, to)
      .map((t) => '[' + fmtClock(t.offsetMs) + '] ' + t.speaker + ': ' + t.text)
      .join('\n');
  }

  function cleanParticipantName(raw) {
    if (!raw) return '';
    let str = raw.replace(/\s+/g, ' ').trim();
    const mPin = str.match(/^(?:pin|unpin)\s+(.+?)(?:\s+to\s+(?:your\s+)?(?:main\s+)?screen)?$/i);
    if (mPin && mPin[1]) str = mPin[1].trim();
    const mAction = str.match(/^(?:more\s+options|actions|action\s+menu)\s+for\s+(.+)$/i);
    if (mAction && mAction[1]) str = mAction[1].trim();
    const mMute = str.match(/^(?:mute|unmute)\s+(.+)$/i);
    if (mMute && mMute[1]) str = mMute[1].trim();
    const mState = str.match(/^(.+?)\s+is\s+(?:speaking|talking|muted|unmuted)$/i);
    if (mState && mState[1]) str = mState[1].trim();
    str = str.replace(/\s*\((?:you|presentation|presenting|meeting host|host|co-host|guest|external)\)$/i, '');
    str = str.replace(/\s+(?:meeting host|host|co-host)$/i, '');
    return str.trim();
  }

  function isNoiseName(str) {
    if (!str || str.length <= 1) return true;
    if (/^(you're|you are)?\s*(continuously\s+framed|framed|framing)/i.test(str)) return true;
    if (/^framing\s+(is\s+)?(on|off)$/i.test(str)) return true;
    if (/^microphones?\s+(is\s+)?(on|off|muted|unmuted)/i.test(str)) return true;
    if (/^(camera|video|audio)\s+(is\s+)?(on|off)/i.test(str)) return true;
    if (/^turn\s+(on|off)\s+(microphone|camera|video)/i.test(str)) return true;
    if (/^(pin|unpin|pin\s+to\s+(main\s+)?screen|unpin\s+from\s+(main\s+)?screen)$/i.test(str)) return true;
    if (/^(jump to bottom|captions?|caption settings|settings|more options|actions|mute|unmute|remove from call|keep|devices?|people|contributors?|search for people)$/i.test(str)) return true;
    if (/^(participant|tile|video|grid|screen|presentation|presentation tile)$/i.test(str)) return true;
    if (/^(\d+\s*(people|contributors?|in the call)?)$/i.test(str)) return true;
    if (/^waiting for (host|others|a call)/i.test(str)) return true;
    if (/^(minimize|expand self view|show in a tile|change layout|picture in picture)$/i.test(str)) return true;
    return false;
  }

  // Replay media events to get who is here right now and their camera/mic state.
  function currentState(session) {
    const map = new Map();
    session.media.forEach((e) => {
      let p = map.get(e.id);
      if (!p) {
        const initDev = (e.device !== undefined) ? e.device : 'desktop';
        p = { id: e.id, name: e.name, present: false, cam: false, mic: false, device: initDev };
        map.set(e.id, p);
      }
      if (e.name) p.name = e.name;
      if (e.device !== undefined) p.device = e.device;
      if (e.type === 'participant_join') {
        p.present = true;
        p.cam = !!e.cam;
        p.mic = !!e.mic;
        if (e.device !== undefined) p.device = e.device;
      }
      else if (e.type === 'participant_leave') p.present = false;
      else if (e.type === 'participant_update') { if (e.device !== undefined) p.device = e.device; }
      else if (e.type === 'cam_on') p.cam = true;
      else if (e.type === 'cam_off') p.cam = false;
      else if (e.type === 'mic_on') p.mic = true;
      else if (e.type === 'mic_off') p.mic = false;
    });

    const presentList = Array.from(map.values()).filter((p) => p.present);
    const unique = [];
    const seen = new Set();
    const overrides = session.deviceOverrides || {};

    presentList.forEach((p) => {
      const clean = cleanParticipantName(p.name);
      if (isNoiseName(clean)) return;
      const isSelf = p.id === 'self' || clean === 'You';
      const key = isSelf ? '__self__' : clean.toLowerCase();

      // Check device override from session
      const override = overrides[clean.toLowerCase()] || overrides[p.id] || (isSelf ? overrides['__self__'] : null);
      const effectiveDev = override || p.device || 'unknown';

      if (seen.has(key)) {
        const existing = unique.find((u) => {
          const uClean = cleanParticipantName(u.name);
          return isSelf ? (u.id === 'self' || uClean === 'You') : uClean.toLowerCase() === key;
        });
        if (existing) {
          if (p.cam) existing.cam = true;
          if (p.mic) existing.mic = true;
          if (effectiveDev === 'mobile' || p.device === 'mobile') existing.device = 'mobile';
          else if (effectiveDev === 'desktop' || p.device === 'desktop') {
            if (existing.device !== 'mobile') existing.device = 'desktop';
          }
          if (override) existing.device = override;
        }
        return;
      }
      seen.add(key);
      unique.push({ ...p, name: clean, device: effectiveDev });
    });

    const total = (session.lastSnapshot && session.lastSnapshot.total > unique.length)
      ? session.lastSnapshot.total
      : unique.length;

    const desktopCount = unique.filter((p) => p.device === 'desktop').length;
    const mobileCount = unique.filter((p) => p.device === 'mobile' || p.device === 'phone_dialin').length;
    const unknownCount = unique.filter((p) => !p.device || p.device === 'unknown').length;

    return {
      participants: unique,
      total: total,
      cam: unique.filter((p) => p.cam).length,
      mic: unique.filter((p) => p.mic).length,
      desktop: desktopCount,
      mobile: mobileCount,
      unknown: unknownCount
    };
  }

  // Per-participant durations: present, camera on, mic unmuted.
  function engagement(session, nowMs) {
    const end = session.endedAt ? Date.parse(session.endedAt) : nowMs;
    const events = session.media.slice().sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts));
    const people = new Map();
    const overrides = session.deviceOverrides || {};

    const accrue = (p, t) => {
      if (p.present && p.last != null && t > p.last) {
        const d = t - p.last;
        p.presentMs += d;
        if (p.cam) p.camMs += d;
        if (p.mic) p.micMs += d;
      }
      p.last = t;
    };
    events.forEach((e) => {
      const t = Date.parse(e.ts);
      let p = people.get(e.id);
      if (!p) {
        const initDev = (e.device !== undefined) ? e.device : 'desktop';
        p = { id: e.id, name: e.name, present: false, cam: false, mic: false, device: initDev,
              presentMs: 0, camMs: 0, micMs: 0, last: null };
        people.set(e.id, p);
      }
      accrue(p, t);
      if (e.name) p.name = e.name;
      if (e.device !== undefined) p.device = e.device;
      if (e.type === 'participant_join') {
        p.present = true;
        p.cam = !!e.cam;
        p.mic = !!e.mic;
        if (e.device !== undefined) p.device = e.device;
      }
      else if (e.type === 'participant_leave') p.present = false;
      else if (e.type === 'participant_update') { if (e.device !== undefined) p.device = e.device; }
      else if (e.type === 'cam_on') p.cam = true;
      else if (e.type === 'cam_off') p.cam = false;
      else if (e.type === 'mic_on') p.mic = true;
      else if (e.type === 'mic_off') p.mic = false;
    });
    people.forEach((p) => accrue(p, end));

    const merged = new Map();
    Array.from(people.values()).forEach((p) => {
      const cleanName = cleanParticipantName(p.name || 'Participant');
      if (isNoiseName(cleanName)) return;
      const isSelf = p.id === 'self' || cleanName === 'You';
      const key = isSelf ? '__self__' : cleanName.toLowerCase();

      const override = overrides[cleanName.toLowerCase()] || overrides[p.id] || (isSelf ? overrides['__self__'] : null);
      const effectiveDev = override || p.device || 'unknown';

      const existing = merged.get(key);
      if (!existing) {
        merged.set(key, {
          name: cleanName,
          device: effectiveDev,
          presentMs: p.presentMs,
          camMs: p.camMs,
          micMs: p.micMs
        });
      } else {
        if (effectiveDev === 'mobile' || p.device === 'mobile') existing.device = 'mobile';
        else if (effectiveDev === 'desktop' || p.device === 'desktop') {
          if (existing.device !== 'mobile') existing.device = 'desktop';
        }
        if (override) existing.device = override;
        existing.presentMs = Math.max(existing.presentMs, p.presentMs);
        existing.camMs = Math.max(existing.camMs, p.camMs);
        existing.micMs = Math.max(existing.micMs, p.micMs);
      }
    });

    return Array.from(merged.values()).map((p) => ({
      name: p.name, device: p.device || 'desktop', presentMs: p.presentMs, camMs: p.camMs, micMs: p.micMs,
      camPct: p.presentMs ? Math.round((100 * p.camMs) / p.presentMs) : 0,
      micPct: p.presentMs ? Math.round((100 * p.micMs) / p.presentMs) : 0
    }));
  }

  function engagementMarkdown(rows) {
    if (!rows.length) return '## Participation\n\nNo camera or mic events were captured.';
    const lines = ['## Participation', '',
      '| Participant | Present | Camera on | Mic unmuted | Device |',
      '| --- | --- | --- | --- | --- |'];
    rows.forEach((r) => lines.push('| ' + r.name + ' | ' + fmtDur(r.presentMs) + ' | ' +
      fmtDur(r.camMs) + ' (' + r.camPct + '%) | ' + fmtDur(r.micMs) + ' (' + r.micPct + '%) | ' +
      (r.device === 'mobile' ? 'Mobile' : 'Desktop') + ' |'));
    lines.push('', 'Mic unmuted means the microphone was not muted, not that the person was speaking.');
    return lines.join('\n');
  }

  // Replay a JSONL string (one event per line) into a session object.
  // Same logic as background.js apply(), so bot output and extension output are interchangeable.
  function replayEvents(jsonlText) {
    var session = {
      meetingId: 'imported', startedAt: null, endedAt: null,
      segments: {}, order: [], media: []
    };
    jsonlText.split('\n').forEach(function (line) {
      line = line.trim();
      if (!line) return;
      var ev;
      try { ev = JSON.parse(line); } catch (e) { return; }
      if (ev.meetingId) session.meetingId = ev.meetingId;
      var t = ev.type;
      if (t === 'call_started') {
        if (session.endedAt) {
          session.startedAt = ev.ts;
          session.endedAt = null;
          session.segments = {};
          session.order = [];
          session.media = [];
        } else {
          session.startedAt = session.startedAt || ev.ts;
          session.endedAt = null;
        }
      } else if (t === 'call_ended') {
        session.endedAt = ev.ts;
      } else if (t === 'caption') {
        if (!session.startedAt) session.startedAt = ev.start_ts || ev.ts;
        if (!session.segments[ev.id]) session.order.push(ev.id);
        session.segments[ev.id] = {
          id: ev.id, speaker: ev.speaker || 'Unknown', text: ev.text || '',
          start_ts: ev.start_ts || ev.ts, end_ts: ev.end_ts || ev.ts, final: !!ev.final
        };
      } else if (['participant_join', 'participant_leave', 'participant_update',
                   'cam_on', 'cam_off', 'mic_on', 'mic_off'].indexOf(t) !== -1) {
        session.media.push({
          type: t, id: ev.id || 'unknown', name: ev.name || 'Participant',
          cam: ev.cam, mic: ev.mic, device: ev.device, ts: ev.ts
        });
      } else if (t === 'snapshot') {
        session.lastSnapshot = {
          total: ev.total, cam: ev.cam_on, mic: ev.mic_on, ts: ev.ts
        };
      }
    });
    return session;
  }

  const lib = { fmtClock, fmtDur, buildTurns, transcriptText, currentState, engagement, engagementMarkdown, replayEvents, cleanParticipantName, isNoiseName };
  root.MeetLib = lib;
  if (typeof module !== 'undefined' && module.exports) module.exports = lib;
})(typeof window !== 'undefined' ? window : globalThis);
