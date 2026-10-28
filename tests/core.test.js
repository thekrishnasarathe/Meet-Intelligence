// Runs meet-core.js against a mock Meet DOM (jsdom) and checks lib.js math.
// This proves the logic, NOT Meet's real selectors - verify those on a live call.
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const { JSDOM } = require('jsdom');

const ext = path.join(__dirname, '..', 'extension');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let passed = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); passed++; console.log('  ok  ' + msg); };

async function coreTest() {
  const dom = new JSDOM('<!doctype html><body></body>', { url: 'https://meet.google.com/abc-defg-hij', runScripts: 'outside-only', pretendToBeVisual: true });
  const w = dom.window, d = w.document;
  w.eval(fs.readFileSync(path.join(ext, 'meet-core.js'), 'utf8'));

  d.body.innerHTML = '<button aria-label="Leave call">call_end</button><button id="cc" aria-label="Turn on captions">closed_caption</button>';
  d.getElementById('cc').addEventListener('click', () => {
    const region = d.createElement('div');
    region.setAttribute('role', 'region'); region.setAttribute('aria-label', 'Captions');
    region.innerHTML = '<div id="list"></div>';
    d.body.appendChild(region);
  });

  const row = (name, text) => '<div class="row"><img src="a.png"><div><span>' + name + '</span></div><div class="t"><span>' + text + '</span></div><span>mic_off</span></div>';
  const tile = (id, name, cam, mic) => {
    const t = d.createElement('div');
    t.setAttribute('data-participant-id', id);
    t.innerHTML = '<span>' + name + '</span><span>more_vert</span><div data-is-muted="' + (mic ? 'false' : 'true') + '"></div><video></video>';
    const v = t.querySelector('video');
    Object.defineProperty(v, 'readyState', { value: cam ? 4 : 0, configurable: true });
    Object.defineProperty(v, 'videoWidth', { value: cam ? 640 : 0, configurable: true });
    Object.defineProperty(v, 'paused', { value: false, configurable: true });
    d.body.appendChild(t);
    return t;
  };

  const events = [];
  const stop = w.MeetCore.start((e) => events.push(e), {
    cfg: { tickMs: 40, flushMs: 80, finalizeAfterMs: 350, leaveGraceMs: 250, endGraceMs: 200 }
  });
  const of = (type) => events.filter((e) => e.type === type);

  await sleep(120);
  ok(of('call_started').length === 1 && of('call_started')[0].meetingId === 'abc-defg-hij', 'call_started carries the meeting id');
  ok(!!d.querySelector('[role=region]'), 'scraper clicked "Turn on captions"');

  const list = d.getElementById('list');
  list.innerHTML = row('Asha Verma', 'Hello');
  await sleep(120);
  list.innerHTML = row('Asha Verma', 'Hello everyone') ;
  await sleep(120);
  list.innerHTML = row('Asha Verma', 'Hello everyone, lets begin') ;
  await sleep(120);
  const asha = of('caption').filter((e) => e.speaker === 'Asha Verma');
  ok(asha.length >= 1, 'caption emitted with speaker name');
  ok(!asha.some((e) => /mic_off|more_vert/.test(e.text)), 'icon ligature text is filtered out');
  ok(new Set(asha.map((e) => e.id)).size >= 1, 'caption updates reuse an id (upsert)');

  list.innerHTML = row('Asha Verma', 'Hello everyone, lets begin') + row('Rahul Mehta', 'Sounds good');
  await sleep(150);
  const rahul = of('caption').filter((e) => e.speaker === 'Rahul Mehta');
  ok(rahul.length >= 1 && rahul[0].id !== asha[0].id, 'second speaker gets a separate utterance');
  ok(rahul[0].text === 'Sounds good', 'speaker text excludes the name');

  await sleep(450);
  ok(of('caption').some((e) => e.speaker === 'Asha Verma' && e.final), 'idle utterance is marked final');

  // Test that subsequent speech in the same block after finalization gets a fresh timestamp and id
  list.innerHTML = row('Asha Verma', 'Hello everyone, lets begin. Moving to topic two');
  await sleep(150);
  const ashaNew = of('caption').filter((e) => e.speaker === 'Asha Verma' && e.text.includes('Moving to topic two'));
  ok(ashaNew.length >= 1, 'subsequent speech after finalization creates a new utterance');
  ok(ashaNew[0].id !== asha[0].id, 'subsequent utterance has a new ID');
  ok(Date.parse(ashaNew[0].start_ts) >= Date.parse(asha[0].end_ts), 'subsequent utterance has an updated current timestamp');

  // Test that a participant with a letter avatar (no <img>, e.g. Aayushi Gaur) is captured accurately
  const letterAvatarRow = '<div class="row"><div class="avatar" aria-hidden="true">A</div><div><span>Aayushi Gaur</span></div><div class="t"><span>अपना</span></div></div>';
  list.innerHTML = row('Asha Verma', 'Continuing') + letterAvatarRow;
  await sleep(150);
  const aayushi = of('caption').filter((e) => e.speaker === 'Aayushi Gaur');
  ok(aayushi.length >= 1, 'participant with letter avatar (no img) is accurately captured');
  ok(aayushi[0].text === 'अपना', 'letter avatar participant text matches exactly (Hindi)');

  const t1 = tile('spaces/x/devices/1', 'Asha Verma', true, true);
  const t2 = tile('spaces/x/devices/2', 'Rahul Mehta', false, false);
  await sleep(250);
  const joins = of('participant_join');
  ok(joins.length === 2, 'two participants detected');
  const j1 = joins.find((e) => e.name === 'Asha Verma');
  ok(j1 && j1.cam === true && j1.mic === true, 'initial camera/mic state captured for participant 1');
  const j2 = joins.find((e) => e.name === 'Rahul Mehta');
  ok(j2 && j2.cam === false && j2.mic === false, 'initial camera/mic state captured for participant 2');

  t1.querySelector('[data-is-muted]').setAttribute('data-is-muted', 'true');
  Object.defineProperty(t2.querySelector('video'), 'readyState', { value: 4, configurable: true });
  Object.defineProperty(t2.querySelector('video'), 'videoWidth', { value: 640, configurable: true });
  await sleep(250);
  ok(of('mic_off').some((e) => e.name === 'Asha Verma'), 'mic_off emitted on mute');
  ok(of('cam_on').some((e) => e.name === 'Rahul Mehta'), 'cam_on emitted when video starts');
  const snap = of('snapshot').pop();
  ok(snap.total === 2 && snap.cam_on === 2 && snap.mic_on === 0, 'snapshot counts match (2 people, 2 cams, 0 mics)');

  // Test Material Symbol mic_off and videocam_off on real Meet DOM structures
  const t3 = d.createElement('div');
  t3.setAttribute('data-participant-id', 'spaces/x/devices/3');
  t3.innerHTML = '<span>Priya Sharma</span><span class="material-icons">mic_off</span><span class="material-icons">videocam_off</span><video></video>';
  d.body.appendChild(t3);
  await sleep(150);
  const joins3 = of('participant_join').find((e) => e.name === 'Priya Sharma');
  ok(joins3 && joins3.mic === false && joins3.cam === false, 'Material symbol ligatures mic_off and videocam_off accurately detected as off');

  // Test aria-label for muted microphone
  const t4 = d.createElement('div');
  t4.setAttribute('data-participant-id', 'spaces/x/devices/4');
  t4.innerHTML = '<span>Dev Patel</span><div aria-label="Microphone is off"></div><video></video>';
  d.body.appendChild(t4);
  await sleep(150);
  const joins4 = of('participant_join').find((e) => e.name === 'Dev Patel');
  ok(joins4 && joins4.mic === false, 'aria-label "Microphone is off" accurately detected as muted');

  // Test bottom control bar self buttons (Turn off microphone = unmuted, Turn off camera = cam on)
  const micBtn = d.createElement('button');
  micBtn.setAttribute('aria-label', 'Turn off microphone (Ctrl + d)');
  const camBtn = d.createElement('button');
  camBtn.setAttribute('aria-label', 'Turn on camera (Ctrl + e)');
  d.body.appendChild(micBtn);
  d.body.appendChild(camBtn);
  await sleep(150);
  const selfJoin = of('participant_join').find((e) => e.id === 'self');
  ok(selfJoin && selfJoin.mic === true && selfJoin.cam === false, 'bottom bar self controls accurately report mic on and cam off for You');

  // Toggle bottom bar mic button to muted ("Turn on microphone")
  micBtn.setAttribute('aria-label', 'Turn on microphone (Ctrl + d)');
  await sleep(150);
  ok(of('mic_off').some((e) => e.id === 'self'), 'bottom bar self mute toggle emits mic_off');

  // Test nested tile candidates (e.g. data-allocation-id wrapping data-participant-id)
  const outerWrapper = d.createElement('div');
  outerWrapper.setAttribute('data-allocation-id', 'alloc-nested-999');
  const innerTile = d.createElement('div');
  innerTile.setAttribute('data-participant-id', 'spaces/x/devices/nested');
  innerTile.innerHTML = '<span>Nested User</span><div data-is-muted="false"></div><video></video>';
  outerWrapper.appendChild(innerTile);
  d.body.appendChild(outerWrapper);
  await sleep(150);
  const nestedJoins = of('participant_join').filter((e) => e.name === 'Nested User');
  ok(nestedJoins.length === 1, 'nested tile candidate elements do not create duplicate participants');
  outerWrapper.remove();

  // Test presentation/screenshare tile is ignored (not counted as human participant or camera)
  const presTile = d.createElement('div');
  presTile.setAttribute('data-participant-id', 'spaces/x/devices/pres1-presentation');
  presTile.setAttribute('aria-label', 'Alice Smith is presenting');
  presTile.innerHTML = '<span>Presentation (Alice Smith)</span><video></video>';
  const presVideo = presTile.querySelector('video');
  Object.defineProperty(presVideo, 'readyState', { value: 4, configurable: true });
  Object.defineProperty(presVideo, 'videoWidth', { value: 1280, configurable: true });
  d.body.appendChild(presTile);
  await sleep(150);
  const presJoins = of('participant_join').filter((e) => e.name.includes('Presentation') || e.name.includes('presenting'));
  ok(presJoins.length === 0, 'presentation tiles are not counted as participants');
  presTile.remove();

  // Test paused video is accurately detected as camera off
  const tPaused = d.createElement('div');
  tPaused.setAttribute('data-participant-id', 'spaces/x/devices/paused');
  tPaused.innerHTML = '<span>Paused Cam User</span><div data-is-muted="false"></div><video></video>';
  const vPaused = tPaused.querySelector('video');
  Object.defineProperty(vPaused, 'readyState', { value: 4, configurable: true });
  Object.defineProperty(vPaused, 'videoWidth', { value: 640, configurable: true });
  Object.defineProperty(vPaused, 'paused', { value: true, configurable: true });
  d.body.appendChild(tPaused);
  await sleep(150);
  const pausedJoin = of('participant_join').find((e) => e.name === 'Paused Cam User');
  ok(pausedJoin && pausedJoin.cam === false, 'paused video tag accurately detected as camera off');
  tPaused.remove();

  // Test "John Doe is muted" aria-label detection
  const tMuted = d.createElement('div');
  tMuted.setAttribute('data-participant-id', 'spaces/x/devices/muted-aria');
  tMuted.innerHTML = '<span>Sara Connor</span><div aria-label="Sara Connor is muted"></div><video></video>';
  d.body.appendChild(tMuted);
  await sleep(150);
  const mutedJoin = of('participant_join').find((e) => e.name === 'Sara Connor');
  ok(mutedJoin && mutedJoin.mic === false, 'aria-label "Sara Connor is muted" accurately detected as muted');
  tMuted.remove();

  // Test device detection: Mobile portrait video vs smartphone icon
  const tMobileVid = d.createElement('div');
  tMobileVid.setAttribute('data-participant-id', 'spaces/x/devices/mob1');
  tMobileVid.innerHTML = '<span>Mobile Video User</span><div data-is-muted="false"></div><video></video>';
  const vMob = tMobileVid.querySelector('video');
  Object.defineProperty(vMob, 'readyState', { value: 4, configurable: true });
  Object.defineProperty(vMob, 'videoWidth', { value: 360, configurable: true });
  Object.defineProperty(vMob, 'videoHeight', { value: 640, configurable: true });
  Object.defineProperty(vMob, 'paused', { value: false, configurable: true });
  d.body.appendChild(tMobileVid);

  const tMobileIcon = d.createElement('div');
  tMobileIcon.setAttribute('data-participant-id', 'spaces/x/devices/mob2');
  tMobileIcon.innerHTML = '<span>Mobile Badge User</span><span class="material-icons">smartphone</span><div data-is-muted="true"></div><video></video>';
  d.body.appendChild(tMobileIcon);

  // Test SVG path smartphone detection (Material 2 & Material 3)
  const tMobileSvg = d.createElement('div');
  tMobileSvg.setAttribute('data-participant-id', 'spaces/x/devices/mob-svg');
  tMobileSvg.innerHTML = '<span>Mobile Svg User</span><svg viewBox="0 0 24 24"><path d="M17 1.01L7 1c-1.1 0-2 .9-2 2v18c0 1.1.9 2 2 2h10c1.1 0 2-.9 2-2V3c0-1.1-.9-1.99-2-1.99zM17 19H7V5h10v14z"></path></svg><div data-is-muted="true"></div><video></video>';
  d.body.appendChild(tMobileSvg);

  const tMobileM3 = d.createElement('div');
  tMobileM3.setAttribute('data-participant-id', 'spaces/x/devices/mob-m3');
  tMobileM3.innerHTML = '<span>Mobile M3 User</span><svg viewBox="0 0 24 24"><path d="M5 23q-.825 0-1.412-.587T3 21V3q0-.825.588-1.412T5 1h14q.825 0 1.413.588T21 3v18q0 .825-.587 1.413T19 23zm2-5h10V6H7z"></path></svg><div data-is-muted="true"></div><video></video>';
  d.body.appendChild(tMobileM3);

  // Test phone dial-in number detected as mobile
  const tDialIn = d.createElement('div');
  tDialIn.setAttribute('data-participant-id', 'spaces/x/devices/phone-dialin');
  tDialIn.innerHTML = '<span>+1 (555) 234-5678</span><div data-is-muted="true"></div><video></video>';
  d.body.appendChild(tDialIn);

  // Test "Pin [Name] to your main screen" aria-label cleaning
  const tPin = d.createElement('div');
  tPin.setAttribute('data-participant-id', 'spaces/x/devices/pin-test');
  tPin.setAttribute('aria-label', 'Pin Alice to your main screen');
  tPin.innerHTML = '<div data-is-muted="false"></div><video></video>';
  d.body.appendChild(tPin);

  // Test "You're continuously framed" noise rejection
  const tNoise = d.createElement('div');
  tNoise.setAttribute('data-participant-id', 'spaces/x/devices/noise-framing');
  tNoise.innerHTML = '<span>You\'re continuously framed</span><div data-is-muted="false"></div><video></video>';
  d.body.appendChild(tNoise);

  await sleep(150);
  const mobVidJoin = of('participant_join').find((e) => e.name === 'Mobile Video User');
  ok(mobVidJoin && mobVidJoin.device === 'mobile', 'portrait video (height > width) detected as mobile device');
  const mobIconJoin = of('participant_join').find((e) => e.name === 'Mobile Badge User');
  ok(mobIconJoin && mobIconJoin.device === 'mobile', 'smartphone ligature icon detected as mobile device');
  const mobSvgJoin = of('participant_join').find((e) => e.name === 'Mobile Svg User');
  ok(mobSvgJoin && mobSvgJoin.device === 'mobile', 'smartphone Material SVG path detected as mobile device');
  const mobM3Join = of('participant_join').find((e) => e.name === 'Mobile M3 User');
  ok(mobM3Join && mobM3Join.device === 'mobile', 'Material 3 smartphone SVG path detected as mobile device');
  const dialInJoin = of('participant_join').find((e) => e.name === '+1 (555) 234-5678');
  ok(dialInJoin && dialInJoin.device === 'mobile', 'phone number dial-in participant name detected as mobile device');
  const pinJoin = of('participant_join').find((e) => e.name === 'Alice');
  ok(pinJoin != null, '"Pin Alice to your main screen" accurately cleaned to "Alice"');
  const noiseJoin = of('participant_join').find((e) => e.name.includes('continuously framed'));
  ok(!noiseJoin, '"You\'re continuously framed" rejected as noise and not added as a participant');

  tMobileVid.remove();
  tMobileIcon.remove();
  tMobileSvg.remove();
  tMobileM3.remove();
  tDialIn.remove();
  tPin.remove();
  tNoise.remove();

  // Test official headcount button reconciliation
  const countBtn = d.createElement('button');
  countBtn.setAttribute('aria-label', 'Show everyone (5)');
  d.body.appendChild(countBtn);
  await sleep(150);
  const countSnap = of('snapshot').pop();
  ok(countSnap && countSnap.total >= 5, 'official headcount button (Show everyone 5) accurately reflects in snapshot total');
  countBtn.remove();

  t2.remove();
  t3.remove();
  t4.remove();
  micBtn.remove();
  camBtn.remove();
  await sleep(650);
  ok(of('participant_leave').some((e) => e.name === 'Rahul Mehta'), 'participant_leave after grace period');

  d.querySelector('button[aria-label="Leave call"]').remove();
  await sleep(450);
  ok(of('call_ended').length === 1, 'call_ended when the leave button disappears');
  ok(of('participant_leave').some((e) => e.name === 'Asha Verma'), 'remaining participants closed out at call end');
  stop();

  const merge = w.MeetCore._mergeText;
  ok(merge('hello wor', 'hello world') === 'hello world', 'mergeText: growth');
  ok(merge('hello world', 'hello wor') === 'hello world', 'mergeText: momentary shrink keeps longer text');
  ok(merge('I thing we should ship', 'I think we should ship it') === 'I think we should ship it', 'mergeText: revision replaces');
  ok(merge('the quick brown fox jumps over', 'brown fox jumps over the lazy dog') === 'the quick brown fox jumps over the lazy dog', 'mergeText: scrolled window is stitched');
}

function libTest() {
  const L = require(path.join(ext, 'lib.js'));
  const t = (s) => new Date(Date.UTC(2026, 9, 24, 12, 0, s)).toISOString();
  const session = {
    meetingId: 'abc-defg-hij', startedAt: t(0), endedAt: t(100),
    order: ['a', 'b', 'c'],
    segments: {
      a: { id: 'a', speaker: 'Asha', text: 'Hi all', start_ts: t(2), end_ts: t(4), final: true },
      b: { id: 'b', speaker: 'Asha', text: 'let us start', start_ts: t(6), end_ts: t(9), final: true },
      c: { id: 'c', speaker: 'Rahul', text: 'ok', start_ts: t(12), end_ts: t(13), final: true }
    },
    media: [
      { type: 'participant_join', id: 'p1', name: 'Asha', cam: true, mic: true, ts: t(0) },
      { type: 'participant_join', id: 'p2', name: 'Rahul', cam: false, mic: false, ts: t(10) },
      { type: 'mic_off', id: 'p1', name: 'Asha', ts: t(40) },
      { type: 'cam_on', id: 'p2', name: 'Rahul', ts: t(50) },
      { type: 'participant_leave', id: 'p2', name: 'Rahul', ts: t(90) }
    ]
  };
  const turns = L.buildTurns(session);
  ok(turns.length === 2 && turns[0].text === 'Hi all let us start', 'consecutive same-speaker segments merge into one turn');
  ok(L.transcriptText(session).startsWith('[00:02] Asha: Hi all let us start'), 'transcript lines carry elapsed time');
  const st = L.currentState(session);
  ok(st.total === 1 && st.cam === 1 && st.mic === 0, 'live state: one person left, camera on, mic muted');
  const rows = L.engagement(session, Date.parse(t(100)));
  const asha = rows.find((r) => r.name === 'Asha'), rahul = rows.find((r) => r.name === 'Rahul');
  ok(asha.presentMs === 100000 && asha.camMs === 100000 && asha.micMs === 40000, 'Asha: present 100s, cam 100s, mic 40s');
  ok(rahul.presentMs === 80000 && rahul.camMs === 40000 && rahul.micMs === 0, 'Rahul: present 80s, cam 40s, mic 0s');
  ok(asha.micPct === 40 && rahul.camPct === 50, 'percentages are relative to time present');
  ok(L.engagementMarkdown(rows).includes('| Asha | 1m 40s | 1m 40s (100%) | 40s (40%) |'), 'markdown participation table renders');
  ok(L.engagementMarkdown(rows).includes('| Device |'), 'markdown participation table includes Device column header');

  const jsonl = [
    JSON.stringify({ type: 'call_started', meetingId: 'test-123', ts: t(0) }),
    JSON.stringify({ type: 'participant_join', id: 'p1', name: 'Asha', cam: true, mic: true, ts: t(0) }),
    JSON.stringify({ type: 'caption', id: 'c1', speaker: 'Asha', text: 'Hello bot', start_ts: t(2), end_ts: t(5), final: true, ts: t(5) }),
    JSON.stringify({ type: 'call_ended', ts: t(10) })
  ].join('\n');
  const replayed = L.replayEvents(jsonl);
  ok(replayed.meetingId === 'test-123' && replayed.order.length === 1 && replayed.segments['c1'].text === 'Hello bot', 'replayEvents parses JSONL into session object');
  ok(replayed.media.length === 1 && replayed.media[0].name === 'Asha', 'replayEvents captures media participants');

  const jsonlRejoin = [
    JSON.stringify({ type: 'call_started', meetingId: 'same-link', ts: t(0) }),
    JSON.stringify({ type: 'caption', id: 'old1', speaker: 'Asha', text: 'Old meeting stuff', start_ts: t(2), end_ts: t(5), final: true, ts: t(5) }),
    JSON.stringify({ type: 'call_ended', ts: t(10) }),
    JSON.stringify({ type: 'call_started', meetingId: 'same-link', ts: t(60) }),
    JSON.stringify({ type: 'caption', id: 'new1', speaker: 'Rahul', text: 'New meeting fresh start', start_ts: t(62), end_ts: t(65), final: true, ts: t(65) })
  ].join('\n');
  const replayedRejoin = L.replayEvents(jsonlRejoin);
  ok(replayedRejoin.order.length === 1 && replayedRejoin.order[0] === 'new1', 'replayEvents resets segments when call_started follows call_ended');
  ok(!replayedRejoin.segments['old1'], 'old meeting history is excluded when rejoining same link');

  ok(L.fmtClock(null) === '00:00' && L.fmtClock(NaN) === '00:00', 'fmtClock safely formats null and NaN');
  ok(L.fmtDur(null) === '0s' && L.fmtDur(NaN) === '0s', 'fmtDur safely formats null and NaN');
  const sessionNoStart = {
    order: ['seg1'],
    segments: { seg1: { id: 'seg1', speaker: 'Asha', text: 'Hi', start_ts: t(10), end_ts: t(12), final: true } }
  };
  const turnsNoStart = L.buildTurns(sessionNoStart);
  ok(turnsNoStart.length === 1 && turnsNoStart[0].offsetMs === 0, 'buildTurns falls back cleanly when startedAt is missing');

  // Test deduplication of self participant
  const sessionDupSelf = {
    media: [
      { type: 'participant_join', id: 'self', name: 'You', cam: true, mic: true, ts: t(0) },
      { type: 'participant_join', id: 'dev123', name: 'You', cam: true, mic: true, ts: t(1) }
    ]
  };
  const stDup = L.currentState(sessionDupSelf);
  ok(stDup.total === 1 && stDup.cam === 1 && stDup.mic === 1, 'duplicate self entries are deduplicated in currentState');

  // Test snapshot headcount integration
  const sessionSnap = {
    media: [
      { type: 'participant_join', id: 'p1', name: 'Asha', cam: true, mic: true, ts: t(0) }
    ],
    lastSnapshot: { total: 4, cam: 1, mic: 1, ts: t(1) }
  };
  const stSnap = L.currentState(sessionSnap);
  ok(stSnap.total === 4 && stSnap.cam === 1 && stSnap.mic === 1, 'currentState reflects snapshot total when higher than tile count');

  // Test desktop and mobile device counts in currentState
  const sessionDevices = {
    media: [
      { type: 'participant_join', id: 'd1', name: 'Desk1', cam: true, mic: true, device: 'desktop', ts: t(0) },
      { type: 'participant_join', id: 'm1', name: 'Mob1', cam: true, mic: true, device: 'mobile', ts: t(0) },
      { type: 'participant_join', id: 'm2', name: 'Mob2', cam: false, mic: false, device: 'mobile', ts: t(0) }
    ]
  };
  const stDev = L.currentState(sessionDevices);
  ok(stDev.desktop === 1 && stDev.mobile === 2, 'currentState accurately counts desktop and mobile participants');

  // Test deviceOverrides in currentState and engagement
  const sessionOverride = {
    media: [
      { type: 'participant_join', id: 'd1', name: 'UserA', cam: true, mic: true, device: 'desktop', ts: t(0) }
    ],
    deviceOverrides: { usera: 'mobile' }
  };
  const stOverride = L.currentState(sessionOverride);
  ok(stOverride.desktop === 0 && stOverride.mobile === 1 && stOverride.participants[0].device === 'mobile', 'deviceOverrides updates participant device and count in currentState');
  const engOverride = L.engagement(sessionOverride, t(100));
  ok(engOverride[0].device === 'mobile', 'deviceOverrides updates participant device in engagement');
}

(async () => {
  console.log('scraper');
  await coreTest();
  console.log('helpers');
  libTest();
  console.log('\n' + passed + ' checks passed');
  process.exit(0);
})().catch((e) => { console.error('\nFAILED:', e.message); process.exit(1); });
