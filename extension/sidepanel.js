/* Side panel: live transcript, headcount, Groq summaries, report export. */
(function () {
  'use strict';
  const L = window.MeetLib;
  const $ = (sel) => document.querySelector(sel);

  const DEFAULTS = {
    apiKey: '',
    liveModel: 'llama-3.1-8b-instant',
    finalModel: 'llama-3.3-70b-versatile'
  };
  const API = 'https://api.groq.com/openai/v1';
  const FALLBACK_MODELS = ['llama-3.1-8b-instant', 'llama-3.3-70b-versatile', 'qwen/qwen3.8-27b', 'openai/gpt-oss-20b'];

  const SYSTEM_LIVE =
    'You maintain a rolling summary of a live meeting. You receive PREVIOUS_STATE (JSON) and NEW_TRANSCRIPT. ' +
    'The meeting speech may be in English, Hindi, or Hinglish (Hindi words in Latin or Devanagari script). ' +
    'Accurately interpret Hindi/Hinglish idioms, context, and terminology, and output clear English summaries. ' +
    'Return ONLY a JSON object: {"current_topic": string, "key_points": [{"speaker": string, "point": string}], ' +
    '"decisions": [string], "action_items": [{"owner": string, "task": string, "due": string|null}], ' +
    '"open_questions": [string]}. Merge the new transcript into the previous state and compress older points so the ' +
    'whole state stays under about 400 tokens. Never invent facts, names or dates. The speaker "You" is the person ' +
    'running the extension. Captions are machine generated and can contain recognition errors.';

  const SYSTEM_FINAL =
    'You write meeting reports from speaker-attributed, timestamped transcripts. Use only what the transcript says. ' +
    'The transcript may contain speech in English, Hindi, or Hinglish. ' +
    'Accurately understand Hindi/Hinglish discussions, synthesizing decisions and actions into professional English. ' +
    'Output Markdown with exactly these sections: "## Executive summary", "## Topic timeline" (bullets like ' +
    '"[mm:ss] topic - one line"), "## Speaker contributions" (per speaker: main points and stance), "## Decisions", ' +
    '"## Action items" (a table with columns Owner | Task | Due), "## Open questions". Write "None recorded." for an ' +
    'empty section. Captions are machine generated; do not over-interpret odd words. The speaker "You" is the person ' +
    'who recorded the meeting.';

  let settings = Object.assign({}, DEFAULTS);
  let meetingId = null, session = null, summary = null, busy = false, loadTimer = null;

  // ---------- storage ----------
  async function loadAll() {
    const base = await chrome.storage.local.get(['lastMeeting', 'settings']);
    settings = Object.assign({}, DEFAULTS, base.settings || {});
    meetingId = base.lastMeeting || null;
    session = null; summary = null;
    if (meetingId) {
      const r = await chrome.storage.local.get(['session:' + meetingId, 'summary:' + meetingId]);
      session = r['session:' + meetingId] || null;
      summary = r['summary:' + meetingId] || null;
    }
    render();
  }

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'local' || (changes.settings && Object.keys(changes).length === 1)) return;
    clearTimeout(loadTimer);
    loadTimer = setTimeout(loadAll, 250);
  });

  // ---------- small DOM helper (textContent only, so names and LLM output can never inject HTML) ----------
  function el(tag, props, children) {
    const node = document.createElement(tag);
    Object.entries(props || {}).forEach(([k, v]) => {
      if (k === 'text') node.textContent = v;
      else if (k === 'style') node.setAttribute('style', v);
      else node.setAttribute(k, v);
    });
    (children || []).forEach((c) => node.appendChild(c));
    return node;
  }
  const hueOf = (name) => {
    let h = 0;
    for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 360;
    return h;
  };

  // ---------- render ----------
  function render() {
    const has = !!(session && (session.order.length || session.media.length || session.startedAt));
    $('#empty').hidden = has;
    $('#live').hidden = !has;
    renderStatus();
    if (!has) return;

    const st = L.currentState(session);
    $('#stat-people').textContent = st.total;
    $('#stat-cam').textContent = st.cam;
    $('#stat-mic').textContent = st.mic;
    const statDev = $('#stat-devices');
    if (statDev) {
      let devText = (st.desktop || 0) + '🖥️ ' + (st.mobile || 0) + '📱';
      if (st.unknown && st.unknown > 0) {
        devText += ' ' + st.unknown + '❓';
      }
      statDev.textContent = devText;
      statDev.title = (st.desktop || 0) + ' Desktop, ' + (st.mobile || 0) + ' Mobile' + (st.unknown ? ', ' + st.unknown + ' Unknown (camera off/no signal)' : '') + ' (Click participant badge below to change)';
    }

    renderTranscript();
    renderEngagement();
    renderSummary();
    $('#btn-live').disabled = busy;
    $('#btn-final').disabled = busy;
  }

  function renderStatus() {
    const s = $('#status');
    if (!session || !session.startedAt) {
      s.className = 'status';
      s.textContent = 'Waiting for a call';
      return;
    }
    const start = Date.parse(session.startedAt);
    if (isNaN(start)) {
      s.className = 'status';
      s.textContent = 'In call ' + (session.meetingId || '');
      return;
    }
    const live = !session.endedAt;
    const endRaw = live ? Date.now() : Date.parse(session.endedAt);
    const end = isNaN(endRaw) ? Date.now() : endRaw;
    const elapsed = Math.max(0, end - start);
    s.className = live ? 'status live' : 'status';
    s.textContent = (live ? 'Live, ' : 'Ended, ') + L.fmtClock(elapsed) + ' in ' + session.meetingId;
  }
  setInterval(renderStatus, 1000);

  function renderTranscript() {
    const box = $('#transcript');
    const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    box.textContent = '';
    const turns = L.buildTurns(session);
    if (!turns.length) {
      box.appendChild(el('p', { class: 'placeholder', text: 'Nothing said yet. Captions are switched on automatically.' }));
      return;
    }
    turns.forEach((t) => {
      const hue = hueOf(t.speaker);
      const clockTitle = t.startMs && !isNaN(t.startMs)
        ? new Date(t.startMs).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
        : '';
      const turn = el('div', { class: 'turn', style: '--rail:hsl(' + hue + ' 55% 48%)' }, [
        el('div', {}, [
          el('span', { class: 'who', text: t.speaker }),
          el('time', { text: L.fmtClock(t.offsetMs), title: clockTitle })
        ]),
        el('p', { text: t.text })
      ]);
      box.appendChild(turn);
    });
    if (nearBottom) box.scrollTop = box.scrollHeight;
  }

  async function toggleDevice(rawName) {
    if (!session) return;
    const clean = L.cleanParticipantName(rawName);
    const key = clean.toLowerCase();
    session.deviceOverrides = session.deviceOverrides || {};

    const curRows = L.engagement(session, Date.now());
    const row = curRows.find((r) => r.name.toLowerCase() === key || (clean === 'You' && r.name === 'You'));
    const curDev = session.deviceOverrides[key] || (row ? row.device : 'desktop');
    const nextDev = (curDev === 'mobile') ? 'desktop' : 'mobile';

    session.deviceOverrides[key] = nextDev;
    if (clean === 'You') session.deviceOverrides['__self__'] = nextDev;

    // Record an update event in media log
    session.media.push({
      type: 'participant_update',
      id: row ? (row.id || ('p-' + key)) : ('p-' + key),
      name: clean,
      device: nextDev,
      ts: new Date().toISOString()
    });

    // Save updated session to storage
    if (window.chrome && chrome.storage && chrome.storage.local) {
      const keyName = 'session:' + session.meetingId;
      await chrome.storage.local.set({ [keyName]: session }).catch(() => {});
    }

    if (window.chrome && chrome.runtime && chrome.runtime.sendMessage) {
      try {
        chrome.runtime.sendMessage({
          kind: 'device_override',
          meetingId: session.meetingId,
          name: clean,
          device: nextDev
        });
      } catch (e) {}
    }

    try {
      backendFetch('/meetings/' + session.meetingId + '/events', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          type: 'participant_update',
          id: row ? (row.id || ('p-' + key)) : ('p-' + key),
          name: clean,
          device: nextDev,
          ts: new Date().toISOString()
        })
      }).catch(() => {});
    } catch (_) {}

    setMsg('Switched ' + clean + ' to ' + (nextDev === 'mobile' ? '📱 Mobile' : '🖥️ Desktop'), false);
    setTimeout(() => {
      const m = $('#msg');
      if (m && m.textContent.includes('Switched ' + clean)) m.textContent = '';
    }, 2500);

    render();
  }

  function renderEngagement() {
    const host = $('#engagement');
    host.textContent = '';
    const rows = L.engagement(session, Date.now());
    if (!rows.length) {
      host.appendChild(el('p', { class: 'muted', text: 'No camera or mic events yet.' }));
      return;
    }
    const head = el('tr', {}, ['Participant', 'Device', 'Present', 'Camera on', 'Mic unmuted'].map((h) => el('th', { text: h })));
    const body = rows.map((r) => {
      const dev = r.device || 'desktop';
      let icon = '🖥️';
      let label = ' Desktop';
      let devClass = 'desktop';
      if (dev === 'mobile') {
        icon = '📱';
        label = ' Mobile';
        devClass = 'mobile';
      } else if (dev === 'unknown') {
        icon = '❓';
        label = ' Unknown';
        devClass = 'unknown';
      }

      const devBadge = el('button', {
        class: 'badge-device ' + devClass + ' clickable',
        type: 'button',
        title: 'Click to switch ' + r.name + ' device (currently ' + label.trim() + ')',
        'aria-label': 'Switch device for ' + r.name + ' (currently ' + label.trim() + ')'
      }, [
        el('span', { class: 'badge-device-icon', text: icon }),
        el('span', { class: 'badge-device-text', text: label }),
        el('span', { class: 'badge-device-arrow', text: ' ▾' })
      ]);
      devBadge.addEventListener('click', (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        toggleDevice(r.name);
      });
      return el('tr', {}, [
        el('td', { text: r.name }),
        el('td', {}, [devBadge]),
        el('td', { text: L.fmtDur(r.presentMs) }),
        el('td', { text: L.fmtDur(r.camMs) + ' (' + r.camPct + '%)' }),
        el('td', { text: L.fmtDur(r.micMs) + ' (' + r.micPct + '%)' })
      ]);
    });
    host.appendChild(el('table', {}, [el('thead', {}, [head]), el('tbody', {}, body)]));
  }

  function list(items, fmt) {
    return el('ul', {}, items.map((i) => el('li', { text: fmt ? fmt(i) : String(i) })));
  }

  function renderRichReport(markdown, container) {
    container.textContent = '';
    if (!markdown) return;

    const lines = markdown.split('\n');
    let title = 'Meeting Report';
    let metaItems = [];
    let currentSection = null;
    const sections = [];

    lines.forEach((line) => {
      const trim = line.trim();
      if (trim.startsWith('# ')) {
        title = trim.replace(/^#\s*/, '').trim();
      } else if (trim.startsWith('## ')) {
        if (currentSection) sections.push(currentSection);
        currentSection = { title: trim.replace(/^##\s*/, '').trim(), lines: [] };
      } else if (currentSection) {
        currentSection.lines.push(line);
      } else if (trim.startsWith('- ')) {
        metaItems.push(trim.replace(/^-\s*/, '').trim());
      }
    });
    if (currentSection) sections.push(currentSection);

    // Meta chips card
    const headerCard = el('div', { class: 'report-summary-header' });
    headerCard.appendChild(el('h3', { text: title, class: 'report-title' }));
    if (metaItems.length) {
      const metaRow = el('div', { class: 'report-meta-chips' });
      metaItems.forEach((m) => {
        const parts = m.split(':');
        const k = parts[0].trim();
        const v = parts.slice(1).join(':').trim();
        let icon = '📌';
        if (/meeting/i.test(k)) icon = '🔗';
        else if (/duration/i.test(k)) icon = '⏱️';
        else if (/participant/i.test(k)) icon = '👥';
        metaRow.appendChild(el('span', { class: 'meta-chip' }, [
          el('span', { class: 'meta-chip-label', text: icon + ' ' + k + ': ' }),
          el('b', { text: v || k })
        ]));
      });
      headerCard.appendChild(metaRow);
    }
    container.appendChild(headerCard);

    // Render cards per section
    sections.forEach((sec) => {
      const secTitle = sec.title;
      const lower = secTitle.toLowerCase();
      const card = el('div', { class: 'report-section-card' });

      let icon = '📄';
      let secClass = 'general';
      if (lower.includes('executive summary')) { icon = '📋'; secClass = 'summary'; }
      else if (lower.includes('topic') || lower.includes('timeline')) { icon = '⏱️'; secClass = 'timeline'; }
      else if (lower.includes('decision')) { icon = '🎯'; secClass = 'decisions'; }
      else if (lower.includes('action item')) { icon = '⚡'; secClass = 'actions'; }
      else if (lower.includes('speaker') || lower.includes('contribution')) { icon = '🗣️'; secClass = 'speakers'; }
      else if (lower.includes('open question') || lower.includes('question')) { icon = '❓'; secClass = 'questions'; }
      else if (lower.includes('participation')) { icon = '📊'; secClass = 'participation'; }

      card.classList.add('sec-' + secClass);
      card.appendChild(el('div', { class: 'sec-head' }, [
        el('span', { class: 'sec-icon', text: icon }),
        el('h4', { text: secTitle })
      ]));

      const content = el('div', { class: 'sec-body' });
      const tableLines = sec.lines.filter((l) => l.trim().startsWith('|') && l.trim().endsWith('|'));

      if (tableLines.length >= 2) {
        const parseRow = (line) => line.split('|').map((c) => c.trim()).filter((c, idx, arr) => idx > 0 && idx < arr.length - 1);
        const headers = parseRow(tableLines[0]);
        const dataRows = tableLines.slice(2).map(parseRow);
        const isActionTable = lower.includes('action');
        const isPartTable = lower.includes('participation');

        const table = el('table', { class: 'rich-table' });
        const thead = el('thead', {}, [
          el('tr', {}, (isActionTable ? ['Done', ...headers] : headers).map((h) => el('th', { text: h })))
        ]);
        const tbody = el('tbody');
        dataRows.forEach((rowVals) => {
          if (!rowVals.length) return;
          const tr = el('tr');
          if (isActionTable) {
            const checkTd = el('td', { class: 'col-check' });
            const chk = el('input', { type: 'checkbox', class: 'task-check' });
            chk.addEventListener('change', () => tr.classList.toggle('task-done', chk.checked));
            checkTd.appendChild(chk);
            tr.appendChild(checkTd);
          }
          rowVals.forEach((val, cIdx) => {
            const td = el('td');
            if (isPartTable && headers[cIdx] && headers[cIdx].toLowerCase().includes('device')) {
              const isMob = /mobile/i.test(val);
              td.appendChild(el('span', { class: 'badge-device ' + (isMob ? 'mobile' : 'desktop') }, [
                el('span', { text: isMob ? '📱 Mobile' : '🖥️ Desktop' })
              ]));
            } else if (isActionTable && headers[cIdx] && headers[cIdx].toLowerCase().includes('owner')) {
              td.appendChild(el('span', { class: 'owner-badge', text: '👤 ' + val }));
            } else {
              td.textContent = val;
            }
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
        });
        table.appendChild(thead);
        table.appendChild(tbody);
        content.appendChild(table);
      } else if (lower.includes('topic') || lower.includes('timeline')) {
        const tl = el('div', { class: 'report-timeline' });
        sec.lines.forEach((l) => {
          const t = l.trim().replace(/^[-*]\s*/, '').trim();
          if (!t) return;
          const matchTime = t.match(/^\[?(\d{1,2}:\d{2})\]?\s*(?:-\s*)?(.*)$/);
          const item = el('div', { class: 'tl-item' });
          if (matchTime) {
            item.appendChild(el('span', { class: 'tl-time', text: matchTime[1] }));
            item.appendChild(el('span', { class: 'tl-text', text: matchTime[2] || '' }));
          } else {
            item.appendChild(el('span', { class: 'tl-dot', text: '•' }));
            item.appendChild(el('span', { class: 'tl-text', text: t }));
          }
          tl.appendChild(item);
        });
        content.appendChild(tl);
      } else if (lower.includes('decision')) {
        const listDec = el('div', { class: 'decisions-list' });
        sec.lines.forEach((l) => {
          const t = l.trim().replace(/^[-*]\s*/, '').trim();
          if (!t || t.toLowerCase() === 'none recorded.') return;
          listDec.appendChild(el('div', { class: 'decision-pill' }, [
            el('span', { class: 'dec-check', text: '✓' }),
            el('span', { class: 'dec-text', text: t })
          ]));
        });
        if (!listDec.children.length) listDec.appendChild(el('p', { class: 'muted', text: 'None recorded.' }));
        content.appendChild(listDec);
      } else if (lower.includes('speaker') || lower.includes('contribution')) {
        const spkGrid = el('div', { class: 'speakers-grid' });
        sec.lines.forEach((l) => {
          const t = l.trim().replace(/^[-*]\s*/, '').trim();
          if (!t || t.toLowerCase() === 'none recorded.') return;
          const colonIdx = t.indexOf(':');
          const item = el('div', { class: 'speaker-card' });
          if (colonIdx !== -1) {
            item.appendChild(el('div', { class: 'speaker-name', text: '👤 ' + t.slice(0, colonIdx).trim() }));
            item.appendChild(el('div', { class: 'speaker-detail', text: t.slice(colonIdx + 1).trim() }));
          } else {
            item.appendChild(el('div', { class: 'speaker-detail', text: t }));
          }
          spkGrid.appendChild(item);
        });
        if (!spkGrid.children.length) spkGrid.appendChild(el('p', { class: 'muted', text: 'None recorded.' }));
        content.appendChild(spkGrid);
      } else {
        sec.lines.forEach((l) => {
          const t = l.trim();
          if (!t) return;
          if (t.startsWith('- ') || t.startsWith('* ')) {
            content.appendChild(el('div', { class: 'bullet-item' }, [
              el('span', { class: 'bullet-dot', text: '•' }),
              el('span', { text: t.slice(2).trim() })
            ]));
          } else {
            content.appendChild(el('p', { class: 'report-p', text: t }));
          }
        });
      }

      card.appendChild(content);
      container.appendChild(card);
    });
  }

  function renderSummary() {
    const liveSec = $('#sec-live'), finalSec = $('#sec-final');
    const state = summary && summary.state;
    liveSec.hidden = !state;
    if (state) {
      const box = $('#live-summary');
      box.textContent = '';
      const d = el('div', { class: 'digest' });
      d.appendChild(el('h3', { text: 'Current topic' }));
      d.appendChild(el('p', { text: state.current_topic || 'Not clear yet' }));
      const kp = state.key_points || [];
      if (kp.length) { d.appendChild(el('h3', { text: 'Key points' })); d.appendChild(list(kp, (k) => k.speaker + ': ' + k.point)); }
      const dec = state.decisions || [];
      if (dec.length) { d.appendChild(el('h3', { text: 'Decisions so far' })); d.appendChild(list(dec)); }
      const ai = state.action_items || [];
      if (ai.length) { d.appendChild(el('h3', { text: 'Action items' })); d.appendChild(list(ai, (a) => a.owner + ': ' + a.task + (a.due ? ' (by ' + a.due + ')' : ''))); }
      const oq = state.open_questions || [];
      if (oq.length) { d.appendChild(el('h3', { text: 'Open questions' })); d.appendChild(list(oq)); }
      box.appendChild(d);
    }
    const fin = summary && summary.final;
    finalSec.hidden = !fin;
    if (fin) {
      $('#final-report').textContent = fin.markdown;
      const richBox = $('#final-report-rich');
      if (richBox) renderRichReport(fin.markdown, richBox);
    }
  }

  // ---------- Groq API (with automatic model fallback on 429/404) ----------
  function setMsg(text, isError) {
    const m = $('#msg');
    m.textContent = text || '';
    m.className = isError ? 'msg error' : 'msg';
  }

  async function callGroqSingle(model, system, prompt, asJson) {
    const body = {
      model: model,
      messages: [
        { role: 'system', content: system },
        { role: 'user', content: prompt }
      ],
      temperature: asJson ? 0.2 : 0.3
    };
    if (asJson) body.response_format = { type: 'json_object' };

    const res = await fetch(API + '/chat/completions', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + settings.apiKey
      },
      body: JSON.stringify(body)
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const reason = data && data.error && data.error.message ? data.error.message : 'HTTP ' + res.status;
      const err = new Error(reason);
      err.status = res.status;
      throw err;
    }
    const choice = data.choices && data.choices[0];
    const text = (choice && choice.message && choice.message.content) || '';
    if (!text) throw new Error('Empty response from Groq');
    return text;
  }

  async function groq(preferredModel, system, prompt, asJson) {
    const candidates = [preferredModel].concat(FALLBACK_MODELS.filter((m) => m !== preferredModel));
    let lastErr = null;
    for (let i = 0; i < candidates.length; i++) {
      try {
        return await callGroqSingle(candidates[i], system, prompt, asJson);
      } catch (e) {
        lastErr = e;
        if (e.status !== 429 && e.status !== 404 && e.status !== 503) throw e;
      }
    }
    throw lastErr;
  }

  const parseJson = (raw) => JSON.parse(raw.replace(/^```(?:json)?\s*|\s*```$/g, '').trim());

  async function guarded(fn) {
    if (busy) return;
    if (!settings.apiKey) { setMsg('Add your Groq API key under Groq API settings first.', true); $('.settings').open = true; return; }
    if (!session || !session.order.length) { setMsg('No transcript yet. Wait until someone speaks.', true); return; }
    busy = true; setMsg('Working...'); render();
    try { setMsg((await fn()) || ''); }
    catch (e) { setMsg(e.message || String(e), true); }
    busy = false; render();
  }

  async function summarizeLive() {
    await guarded(async () => {
      const cursor = (summary && summary.cursor) || 0;
      const lastSeg = session.segments[session.order[session.order.length - 1]];
      const upto = session.order.length - (lastSeg && !lastSeg.final ? 1 : 0);
      if (upto <= cursor && summary && summary.state) return 'Nothing new since the last catch-up.';
      if (upto <= 0) return 'Wait for the current sentence to finish.';
      const prompt = 'PREVIOUS_STATE:\n' + JSON.stringify((summary && summary.state) || {}) +
                     '\n\nNEW_TRANSCRIPT:\n' + L.transcriptText(session, cursor, upto);
      const state = parseJson(await groq(settings.liveModel, SYSTEM_LIVE, prompt, true));
      summary = Object.assign({}, summary || {}, { state, cursor: upto, updatedAt: Date.now() });
      await chrome.storage.local.set({ ['summary:' + meetingId]: summary });
    });
  }

  async function finalReport() {
    await guarded(async () => {
      const end = session.endedAt ? Date.parse(session.endedAt) : Date.now();
      const start = Date.parse(session.startedAt);
      const rows = L.engagement(session, Date.now());
      const names = rows.map((r) => r.name).join(', ') || 'unknown';
      const prompt = 'Meeting ' + session.meetingId + ', duration ' + L.fmtDur(end - start) +
                     '. Participants: ' + names + '.\n\nTRANSCRIPT:\n' + L.transcriptText(session);
      const body = await groq(settings.finalModel, SYSTEM_FINAL, prompt, false);
      const header = '# Meeting report\n\n- Meeting: ' + session.meetingId +
                     '\n- Started: ' + new Date(start).toLocaleString() +
                     '\n- Duration: ' + L.fmtDur(end - start) +
                     '\n- Participants seen: ' + names + '\n\n';
      const markdown = header + body.trim() + '\n\n' + L.engagementMarkdown(rows) + '\n';
      summary = Object.assign({}, summary || {}, { final: { markdown, at: Date.now() } });
      await chrome.storage.local.set({ ['summary:' + meetingId]: summary });
    });
  }

  // ---------- export, clear ----------
  function download(name, text, type) {
    const url = URL.createObjectURL(new Blob([text], { type }));
    const a = el('a', { href: url, download: name });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

  // ---------- settings ----------
  async function saveSettings() {
    settings = { apiKey: $('#set-key').value.trim(), liveModel: $('#set-live').value.trim() || DEFAULTS.liveModel,
                 finalModel: $('#set-final').value.trim() || DEFAULTS.finalModel };
    await chrome.storage.local.set({ settings });
  }

  async function loadModels() {
    if (!$('#set-key').value.trim()) { setMsg('Paste your Groq API key first.', true); return; }
    await saveSettings();
    try {
      const res = await fetch(API + '/models', { headers: { 'Authorization': 'Bearer ' + settings.apiKey } });
      const data = await res.json();
      if (!res.ok) throw new Error((data.error && data.error.message) || 'HTTP ' + res.status);
      const names = (data.data || [])
        .map((m) => m.id)
        .filter((id) => !/whisper|tts|guard|playai/i.test(id));
      const dl = $('#model-list');
      dl.textContent = '';
      names.forEach((n) => dl.appendChild(el('option', { value: n })));
      setMsg(names.length + ' Groq chat models loaded. Click a model field to choose one.');
    } catch (e) { setMsg(e.message || String(e), true); }
  }

  // ---------- wire up ----------
  document.addEventListener('DOMContentLoaded', async () => {
    await loadAll();
    $('#set-key').value = settings.apiKey;
    $('#set-live').value = settings.liveModel;
    $('#set-final').value = settings.finalModel;
    ['#set-key', '#set-live', '#set-final'].forEach((s) => $(s).addEventListener('change', saveSettings));
    $('#btn-live').addEventListener('click', summarizeLive);
    $('#btn-final').addEventListener('click', finalReport);
    $('#btn-models').addEventListener('click', loadModels);
    $('#btn-download-report').addEventListener('click', () => {
      if (summary && summary.final) download('meeting-report-' + meetingId + '.md', summary.final.markdown, 'text/markdown');
    });

    const btnRich = $('#btn-view-rich');
    const btnRaw = $('#btn-view-raw');
    const richEl = $('#final-report-rich');
    const rawEl = $('#final-report');
    if (btnRich && btnRaw) {
      btnRich.addEventListener('click', () => {
        btnRich.classList.add('active');
        btnRaw.classList.remove('active');
        if (richEl) richEl.hidden = false;
        if (rawEl) rawEl.hidden = true;
      });
      btnRaw.addEventListener('click', () => {
        btnRaw.classList.add('active');
        btnRich.classList.remove('active');
        if (richEl) richEl.hidden = true;
        if (rawEl) rawEl.hidden = false;
      });
    }

    const btnCopy = $('#btn-copy-report');
    if (btnCopy) {
      btnCopy.addEventListener('click', async () => {
        if (!summary || !summary.final) return;
        try {
          if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(summary.final.markdown);
          } else {
            const ta = document.createElement('textarea');
            ta.value = summary.final.markdown;
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            ta.remove();
          }
          setMsg('Copied Markdown to clipboard! ✨');
        } catch (e) {
          setMsg('Failed to copy: ' + e.message, true);
        }
      });
    }

    $('#btn-raw').addEventListener('click', () => {
      if (session) download('meeting-' + meetingId + '.json', JSON.stringify(session, null, 2), 'application/json');
    });
    const handleReset = async () => {
      if (!meetingId) return;
      await chrome.storage.local.remove(['session:' + meetingId, 'summary:' + meetingId, 'lastMeeting']);
      session = null;
      summary = null;
      meetingId = null;
      render();
      setMsg('');
    };
    const resetBtn = $('#btn-reset');
    if (resetBtn) resetBtn.addEventListener('click', handleReset);

    $('#btn-clear').addEventListener('click', async () => {
      if (!meetingId || !window.confirm('Delete the transcript and summaries for ' + meetingId + ' from this browser?')) return;
      await handleReset();
    });
    const triggerImport = () => $('#file-import').click();
    $('#btn-import-empty').addEventListener('click', triggerImport);
    $('#btn-import').addEventListener('click', triggerImport);
    $('#file-import').addEventListener('change', async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      try {
        const text = await file.text();
        let imported;
        if (file.name.endsWith('.jsonl')) {
          imported = L.replayEvents(text);
        } else {
          imported = JSON.parse(text);
        }
        if (!imported || (!imported.order || !imported.order.length) && !imported.media.length) {
          setMsg('File contained no meeting data.', true); return;
        }
        const mid = imported.meetingId || 'imported';
        await chrome.storage.local.set({ ['session:' + mid]: imported, lastMeeting: mid });
        setMsg('Imported ' + imported.order.length + ' segments from ' + file.name);
      } catch (err) {
        setMsg('Import failed: ' + (err.message || String(err)), true);
      }
      e.target.value = '';
    });

    // ---------- Bot Controller & Live Sync ----------
    const chkAuto = $('#chk-auto-deploy');
    if (chkAuto) {
      chkAuto.checked = false; // Always default to OFF (no automatic joins without permission)
      if (chrome.storage && chrome.storage.local && chrome.storage.local.get) {
        chrome.storage.local.get(['autoDeployBot']).then((r) => {
          chkAuto.checked = (r && r.autoDeployBot === true);
        }).catch(() => {});
      }
      chkAuto.addEventListener('change', () => {
        if (chrome.storage && chrome.storage.local && chrome.storage.local.set) {
          chrome.storage.local.set({ autoDeployBot: chkAuto.checked }).catch(() => {});
        }
      });
    }

    async function backendFetch(path, options = {}) {
      const hosts = ['http://127.0.0.1:8000', 'http://localhost:8000'];
      let lastErr = null;
      for (const host of hosts) {
        try {
          return await fetch(host + path, options);
        } catch (err) {
          lastErr = err;
        }
      }
      throw lastErr || new Error('Backend unreachable');
    }

    async function getActiveMeetUrl() {
      // 1. Query the actively focused tab FIRST so any current meeting is captured
      if (typeof chrome !== 'undefined' && chrome.tabs && chrome.tabs.query) {
        try {
          const activeTabs = await chrome.tabs.query({ active: true, currentWindow: true });
          if (activeTabs && activeTabs[0] && activeTabs[0].url) {
            const m = activeTabs[0].url.match(/(?:meet\/)?([a-z]{3}-[a-z]{4}-[a-z]{3})/i);
            if (m) return 'https://meet.google.com/' + m[1].toLowerCase();
          }
          const allTabs = await chrome.tabs.query({ url: '*://meet.google.com/*' });
          for (const tab of allTabs) {
            const m = (tab.url || '').match(/(?:meet\/)?([a-z]{3}-[a-z]{4}-[a-z]{3})/i);
            if (m) return 'https://meet.google.com/' + m[1].toLowerCase();
          }
        } catch (_) {}
      }
      if (meetingId) return 'https://meet.google.com/' + meetingId;
      return '';
    }

    const btnLaunch = $('#btn-side-launch-bot');
    const btnStop = $('#btn-side-stop-bot');
    const sideDot = $('#side-bot-dot');
    const sideLabel = $('#side-bot-label');
    const dashLink = $('#btn-open-dash');

    if (btnLaunch) {
      btnLaunch.addEventListener('click', async () => {
        const url = await getActiveMeetUrl();
        if (!url) {
          setMsg('Please open a Google Meet tab first!', true);
          return;
        }
        btnLaunch.disabled = true;
        btnLaunch.textContent = '🚀 Launching Bot...';
        try {
          const res = await backendFetch('/bot/launch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url, name: 'Notes Bot' })
          });
          const d = await res.json();
          if (res.ok) {
            setMsg('Bot launched successfully into ' + url);
            syncBotState();
          } else {
            setMsg('Launch error: ' + (d.detail || 'Failed to start bot'), true);
          }
        } catch (e) {
          setMsg('Cannot connect to backend (http://localhost:8000). Is it running?', true);
        } finally {
          btnLaunch.disabled = false;
          btnLaunch.textContent = '🚀 1-Click Deploy Bot';
        }
      });
    }

    if (btnStop) {
      btnStop.addEventListener('click', async () => {
        const targetUrl = await getActiveMeetUrl();
        const mid = meetingId || (targetUrl ? targetUrl.split('/').pop() : '');
        if (!mid) return;
        btnStop.disabled = true;
        try {
          await backendFetch('/bot/' + mid + '/stop', { method: 'POST' });
          setMsg('Bot stopped.');
          syncBotState();
        } catch (e) {
          setMsg('Failed to stop bot: ' + e.message, true);
        } finally {
          btnStop.disabled = false;
        }
      });
    }

    async function syncBotState() {
      const targetUrl = await getActiveMeetUrl();
      const mid = meetingId || (targetUrl ? targetUrl.split('/').pop() : '');
      if (!mid) return;

      if (dashLink) {
        dashLink.href = 'http://localhost:8000/dashboard?mid=' + mid;
      }

      try {
        const res = await backendFetch('/meetings/' + mid + '/live');
        if (!res.ok) {
          if (sideDot) sideDot.className = 'bot-pulse-dot';
          if (sideLabel) sideLabel.textContent = 'Bot: Offline';
          if (btnLaunch) btnLaunch.style.display = 'inline-block';
          if (btnStop) btnStop.style.display = 'none';
          return;
        }
        const data = await res.json();
        const botRunning = data.bot && data.bot.running;

        if (sideDot) sideDot.className = 'bot-pulse-dot' + (botRunning ? ' live' : '');
        if (sideLabel) sideLabel.textContent = botRunning ? 'Bot: In Call 🟢' : 'Bot: Offline';
        if (btnLaunch) btnLaunch.style.display = botRunning ? 'none' : 'inline-block';
        if (btnStop) btnStop.style.display = botRunning ? 'inline-block' : 'none';
      } catch (_) {}
    }

    setInterval(syncBotState, 2000);
    syncBotState();
  });
})();
