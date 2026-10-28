// Smoke test: loads the real sidepanel.html/js in jsdom with a fake chrome.storage and a fake Gemini.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const { JSDOM } = require('jsdom');

const ext = path.join(__dirname, '..', 'extension');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let passed = 0;
const ok = (c, m) => { assert.ok(c, m); passed++; console.log('  ok  ' + m); };

(async () => {
  const t = (s) => new Date(Date.UTC(2026, 9, 24, 12, 0, s)).toISOString();
  const data = {
    lastMeeting: 'abc-defg-hij',
    settings: { apiKey: 'test-key', liveModel: 'live-model', finalModel: 'final-model' },
    'session:abc-defg-hij': {
      meetingId: 'abc-defg-hij', startedAt: t(0), endedAt: t(120),
      order: ['a', 'b'],
      segments: {
        a: { id: 'a', speaker: 'Asha', text: '<b>Let us ship Friday</b>', start_ts: t(3), end_ts: t(6), final: true },
        b: { id: 'b', speaker: 'Rahul', text: 'I will prepare the release notes', start_ts: t(10), end_ts: t(14), final: true }
      },
      media: [
        { type: 'participant_join', id: 'p1', name: 'Asha', cam: true, mic: true, ts: t(0) },
        { type: 'participant_join', id: 'p2', name: 'Rahul', cam: false, mic: true, ts: t(1) }
      ]
    }
  };
  const listeners = [];
  const chrome = {
    storage: {
      local: {
        get: async (keys) => { const ks = Array.isArray(keys) ? keys : [keys]; const o = {}; ks.forEach((k) => { if (k in data) o[k] = data[k]; }); return o; },
        set: async (o) => { Object.assign(data, o); listeners.forEach((l) => l(Object.fromEntries(Object.keys(o).map((k) => [k, {}])), 'local')); },
        remove: async (ks) => { (Array.isArray(ks) ? ks : [ks]).forEach((k) => delete data[k]); }
      },
      onChanged: { addListener: (fn) => listeners.push(fn) }
    }
  };

  const calls = [];
  const fakeFetch = async (url, init) => {
    const body = JSON.parse(init.body);
    calls.push({ url, key: init.headers['Authorization'], body });
    const asJson = body.response_format && body.response_format.type === 'json_object';
    const text = asJson
      ? JSON.stringify({ current_topic: 'Release timing', key_points: [{ speaker: 'Asha', point: 'Ship on Friday' }], decisions: ['Ship Friday'], action_items: [{ owner: 'Rahul', task: 'Release notes', due: null }], open_questions: [] })
      : '## Executive summary\nThey agreed to ship on Friday.\n\n## Action items\n| Owner | Task | Due |\n| --- | --- | --- |\n| Rahul | Release notes | - |';
    return { ok: true, status: 200, json: async () => ({ choices: [{ message: { content: text } }] }) };
  };

  const html = fs.readFileSync(path.join(ext, 'sidepanel.html'), 'utf8').replace(/<script[^>]*><\/script>/g, '');
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true });
  const w = dom.window;
  w.chrome = chrome; w.fetch = fakeFetch; w.confirm = () => true;
  w.eval(fs.readFileSync(path.join(ext, 'lib.js'), 'utf8'));
  w.eval(fs.readFileSync(path.join(ext, 'sidepanel.js'), 'utf8'));
  w.document.dispatchEvent(new w.Event('DOMContentLoaded'));
  await sleep(100);
  const $ = (s) => w.document.querySelector(s);

  ok($('#live').hidden === false && $('#empty').hidden === true, 'panel shows the live view when a session exists');
  ok($('#stat-people').textContent === '2' && $('#stat-cam').textContent === '1' && $('#stat-mic').textContent === '2', 'headcount shows 2 people, 1 camera, 2 mics');
  ok($('#stat-devices').textContent === '2🖥️ 0📱', 'headcount shows device breakdown (2 desktop, 0 mobile)');
  ok($('#status').textContent.includes('Ended') && $('#status').textContent.includes('abc-defg-hij'), 'status line shows meeting id and elapsed time');
  ok($('#transcript').textContent.includes('Asha') && $('#transcript').textContent.includes('Rahul'), 'transcript lists both speakers');
  ok(!$('#transcript').querySelector('b'), 'HTML in caption text is shown as text, not injected');
  ok($('#engagement').querySelectorAll('tbody tr').length === 2, 'participation table has one row per person');
  ok($('#engagement').querySelector('.badge-device') !== null, 'participation table renders device badge');

  // Test interactive device toggle (click Rahul's device badge to toggle from Desktop to Mobile)
  const rahulBadge = $('#engagement').querySelectorAll('tbody tr')[1].querySelector('.badge-device');
  rahulBadge.click();
  await sleep(100);
  ok($('#stat-devices').textContent === '1🖥️ 1📱', 'clicking device badge toggles participant to mobile and updates stat counter');
  ok($('#engagement').querySelectorAll('tbody tr')[1].querySelector('.badge-device').textContent.includes('Mobile'), 'table reflects toggled mobile device');

  $('#btn-live').click();
  await sleep(150);
  ok(calls.length === 1 && calls[0].key === 'Bearer test-key' && calls[0].body.model === 'live-model', 'catch-up calls the chosen live model on Groq with Bearer key');
  ok(calls[0].body.messages[1].content.includes('NEW_TRANSCRIPT') && calls[0].body.messages[1].content.includes('[00:03] Asha'), 'prompt carries previous state and timestamped transcript');
  ok($('#sec-live').hidden === false && $('#live-summary').textContent.includes('Release timing'), 'catch-up renders the current topic');
  ok(data['summary:abc-defg-hij'].cursor === 2, 'cursor advanced past the summarized segments');

  $('#btn-live').click();
  await sleep(100);
  ok(calls.length === 1 && $('#msg').textContent.includes('Nothing new'), 'second catch-up with no new speech makes no API call');

  $('#btn-final').click();
  await sleep(150);
  ok(calls.length === 2 && calls[1].body.model === 'final-model', 'final report uses the final model on Groq');
  const report = $('#final-report').textContent;
  ok($('#sec-final').hidden === false && report.includes('# Meeting report') && report.includes('Executive summary') && report.includes('## Participation'), 'final report has header, AI sections and participation table');
  ok($('#final-report-rich').querySelector('.report-summary-header') !== null, 'rich report renders executive summary header card');
  ok($('#final-report-rich').querySelector('.sec-summary') !== null, 'rich report renders summary section card');

  $('#btn-clear').click();
  await sleep(100);
  ok(!data['session:abc-defg-hij'], 'clear removes the stored meeting');
  ok($('#live').hidden === true && $('#empty').hidden === false, 'panel returns to empty state after clearing meeting');

  // Test rejoining same meeting link with fresh session
  data['session:abc-defg-hij'] = {
    meetingId: 'abc-defg-hij', startedAt: t(200), endedAt: null,
    order: ['new_seg'],
    segments: {
      new_seg: { id: 'new_seg', speaker: 'Asha', text: 'Fresh meeting started', start_ts: t(202), end_ts: t(205), final: true }
    },
    media: [
      { type: 'participant_join', id: 'p1', name: 'Asha', cam: true, mic: true, ts: t(200) }
    ]
  };
  data.lastMeeting = 'abc-defg-hij';
  // Notify sidepanel of storage update
  listeners.forEach((l) => l({ 'session:abc-defg-hij': {}, lastMeeting: {} }, 'local'));
  await sleep(300);
  ok($('#live').hidden === false, 'panel updates when fresh session for same link begins');
  ok($('#transcript').textContent.includes('Fresh meeting started'), 'transcript reflects fresh call without old history');
  ok(!$('#transcript').textContent.includes('Let us ship Friday'), 'old meeting history is absent from fresh call');

  // Test btn-reset button
  $('#btn-reset').click();
  await sleep(100);
  ok(!data['session:abc-defg-hij'], 'btn-reset clears the meeting from storage');
  ok($('#live').hidden === true && $('#empty').hidden === false, 'btn-reset returns panel to empty state');

  console.log('\n' + passed + ' checks passed');
  process.exit(0);
})().catch((e) => { console.error('\nFAILED:', e.message); process.exit(1); });
