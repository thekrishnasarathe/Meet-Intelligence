/*
 * meet-core.js
 *
 * Environment-agnostic Google Meet scraper. It only needs `window` and `document`,
 * so the exact same file runs
 *   - in the Chrome extension (content.js wires emit -> chrome.runtime.sendMessage)
 *   - in a Playwright browser (bot/bot.py wires emit -> page.expose_function)
 *
 * MeetCore.start(emit, opts) -> stop()
 *
 * Events handed to emit(ev). Every event carries meetingId and ts (ISO string):
 *   call_started | call_ended
 *   caption            { id, speaker, text, start_ts, end_ts, final }   upsert by id
 *   participant_join   { id, name, cam, mic }
 *   participant_leave  { id, name }
 *   participant_update { id, name }
 *   cam_on | cam_off | mic_on | mic_off   { id, name }
 *   snapshot           { total, cam_on, cam_off, mic_on, mic_off }
 *
 * Meet's DOM changes without notice and its class names are obfuscated, so this file
 * anchors on structure and ARIA attributes instead. Everything that may need a tweak
 * lives in CFG. To inspect what the scraper sees on a live call, run this in the
 * Meet tab's DevTools console:   window.dispatchEvent(new Event('meetcore-dump'))
 */
(function (root) {
  'use strict';
  if (root.MeetCore) return;

  const CFG = {
    tickMs: 500,               // scan interval
    flushMs: 1000,             // max one caption update per utterance per second
    finalizeAfterMs: 6000,     // an utterance with no change for this long is marked final
    leaveGraceMs: 15000,       // a tile must be missing this long before we call it a leave
    endGraceMs: 4000,          // leave-call button must be gone this long before we call it call_ended
    maxCaptionTries: 12,       // attempts to auto-click "Turn on captions"
    leaveButton: 'button[aria-label*="Leave call" i], button[aria-label*="Leave" i], button[aria-label*="End call" i], button[data-tooltip*="Leave" i]',
    captionRegions: ['div[role="region"][aria-label*="aption" i]', '[aria-live="polite"]', '[aria-live="assertive"]'],
    captionsOnLabel: /turn on (live )?captions|captions \(c\)/i,
    tile: '[data-participant-id], [data-requested-participant-id], [data-allocation-id]',
    assumeMicOnWhenNoIndicator: false,
    noise: /^(jump to bottom|captions?|caption settings|settings|more options|pin|unpin|mute|unmute|remove from call|presentation|keep|devices?|you are presenting)$/i
  };

  // Material icon ligatures (mic_off, more_vert, ...) show up as plain text nodes in Meet.
  const ICON_LIGATURE = /^[a-z0-9]+(_[a-z0-9]+)+$/;
  const isNoise = (t) => CFG.noise.test(t) || ICON_LIGATURE.test(t);

  function isAvatarNode(node) {
    const p = node.parentElement;
    if (!p) return false;
    if (p.closest && p.closest('[aria-hidden="true"], [data-initial], [class*="avatar" i], [class*="photo" i]')) {
      const val = (node.nodeValue || '').trim();
      if (val.length <= 3) return true;
    }
    return false;
  }

  // Visible text of an element, one entry per text node, with UI noise removed.
  function textLines(el) {
    const out = [];
    const walker = el.ownerDocument.createTreeWalker(el, 4 /* SHOW_TEXT */);
    let node;
    while ((node = walker.nextNode())) {
      if (isAvatarNode(node)) continue;
      const t = (node.nodeValue || '').replace(/\s+/g, ' ').trim();
      if (t && !isNoise(t)) out.push(t);
    }
    // If first entry is a single-letter initial of the speaker name (e.g. "A" followed by "Aayushi Gaur")
    if (out.length >= 3 && out[0].length === 1 && out[1].startsWith(out[0])) {
      out.shift();
    }
    return out;
  }

  // Meet grows a caption block word by word, revises recent words, and eventually
  // scrolls old words off the front. Merge so we keep the longest faithful text.
  function mergeText(prev, next) {
    if (!prev || next.indexOf(prev) === 0) return next;          // normal growth
    if (prev.indexOf(next) === 0) return prev;                   // momentary shrink
    const max = Math.min(prev.length, next.length);
    for (let k = max; k >= 4; k--) {                            // window scrolled forward (lowered to 4 for Hindi)
      if (prev.endsWith(next.slice(0, k))) return prev + next.slice(k);
    }
    return next.length >= prev.length * 0.6 ? next : prev + ' ' + next; // revision vs. new text
  }

  function start(emit, opts) {
    opts = opts || {};
    if (opts.cfg) Object.assign(CFG, opts.cfg);
    const doc = root.document;
    const runId = Math.random().toString(36).slice(2, 7);
    let counter = 0, inCall = false, leaveMissingSince = 0;
    let captionTries = 0, lastCaptionTry = 0, tickCount = 0;
    let captionAutoEnabled = false, userTurnedOffCaptions = false;
    const utter = new Map();   // caption block element -> utterance record
    const people = new Map();  // participant id -> { name, cam, mic, missingSince }
    const recentSpeakers = new Map(); // clean name -> timestamp
    let knownSelfName = '';

    // Track user manually clicking captions toggle so we don't fight them
    try {
      doc.addEventListener('click', (e) => {
        try {
          const btn = e.target && e.target.closest ? e.target.closest('button, div[role="button"]') : null;
          if (btn) {
            const label = (btn.getAttribute('aria-label') || '').toLowerCase();
            if (label.includes('turn off captions') || label.includes('turn off live captions')) {
              userTurnedOffCaptions = true;
            } else if (label.includes('turn on captions') || label.includes('turn on live captions')) {
              userTurnedOffCaptions = false;
            }
          }
        } catch (_) { }
      }, true);
    } catch (_) { }

    const meetingId = () => {
      const m = root.location.pathname.match(/\/([a-z]{3}-[a-z]{4}-[a-z]{3})/);
      return m ? m[1] : 'unknown';
    };
    const iso = (t) => new Date(t).toISOString();
    function send(ev, t) {
      ev.meetingId = meetingId();
      ev.ts = iso(t || Date.now());
      try { emit(ev); } catch (e) { /* a failing consumer must not kill the scraper */ }
    }

    // ---------- captions ----------
    function findCaptionRegion() {
      // 1. Direct caption region with 'caption' in aria-label or role='region'
      const explicit = doc.querySelector('div[role="region"][aria-label*="aption" i], div[aria-label*="caption" i], div[role="region"][aria-label*="subtitle" i]');
      if (explicit) return explicit;

      // 2. Look through regions and live areas for caption containers
      const candidates = Array.from(doc.querySelectorAll('div[role="region"], [aria-live="polite"], [aria-live="assertive"]'));
      for (const el of candidates) {
        const label = (el.getAttribute('aria-label') || '').toLowerCase();
        if (label.includes('caption') || label.includes('subtitle')) return el;
        // Or if it contains speaker rows with avatar or text but not video tiles
        if (el.querySelector('img') && (el.textContent || '').trim().length > 3 && !el.querySelector('video')) {
          return el;
        }
      }
      return null;
    }

    // One block per speaker row. Supports images, letter initials, styled circle avatars, or container rows.
    function captionBlocks(region) {
      if (!region) return [];

      // 1. Try finding speaker rows via avatar elements (images, letter avatars, styled circles)
      const avatarSels = 'img, [role="img"], div[style*="border-radius"], div[class*="avatar" i], span[class*="avatar" i]';
      const avatars = region.querySelectorAll(avatarSels);
      if (avatars.length > 0) {
        let sample = avatars[0];
        while (sample.parentElement && sample.parentElement !== region &&
          sample.parentElement.querySelectorAll(avatarSels).length === 1) {
          sample = sample.parentElement;
        }
        if (sample && sample.parentElement) {
          const rowContainer = sample.parentElement;
          const rows = Array.from(rowContainer.children).filter((child) => {
            const lines = textLines(child);
            return lines.length >= 2;
          });
          if (rows.length > 0) return rows;
        }
      }

      // 2. Check if region itself or its container has child rows
      let container = region;
      while (container.children.length === 1 && container.firstElementChild) {
        if (textLines(container.firstElementChild).length >= 2 && container.firstElementChild.children.length <= 4) {
          break;
        }
        container = container.firstElementChild;
      }
      const children = Array.from(container.children);
      const validRows = children.filter((c) => textLines(c).length >= 2);
      if (validRows.length > 0) return validRows;

      return children.length ? children : [region];
    }

    function ensureCaptions(t) {
      if (userTurnedOffCaptions || captionAutoEnabled || captionTries >= 1 || t - lastCaptionTry < 2500) return;
      // If the "Turn on captions" button exists, captions are OFF! Click it once on call join.
      const btn = Array.from(doc.querySelectorAll('button, div[role="button"]'))
        .find((b) => CFG.captionsOnLabel.test(b.getAttribute('aria-label') || ''));
      if (btn) {
        lastCaptionTry = t;
        captionTries++;
        captionAutoEnabled = true;
        btn.click();
        return;
      }
      if (findCaptionRegion()) {
        captionAutoEnabled = true;
        return;
      }
    }

    function emitCaption(u, t) {
      u.dirty = false;
      u.lastEmit = t;
      send({
        type: 'caption', id: u.id, speaker: u.speaker, text: u.text,
        start_ts: iso(u.start), end_ts: iso(u.last), final: u.final
      }, t);
    }

    function finalize(u, t) {
      if (u.final && !u.dirty) return;
      u.final = true;
      emitCaption(u, t);
    }

    function markSpeakerRecent(name, t) {
      if (!name) return;
      const clean = name.replace(/\s*\((?:you|presentation)\)/i, '').trim().toLowerCase();
      recentSpeakers.set(clean, t || Date.now());
    }

    function isRecentSpeaker(name, t) {
      if (!name) return false;
      const clean = name.replace(/\s*\((?:you|presentation)\)/i, '').trim().toLowerCase();
      const last = recentSpeakers.get(clean);
      const windowMs = Math.min(4000, Math.max(100, Math.floor(CFG.leaveGraceMs / 2)));
      return last != null && ((t || Date.now()) - last < windowMs);
    }

    function scanCaptions(t) {
      const region = findCaptionRegion();
      const seen = new Set();
      if (region) {
        for (const block of captionBlocks(region)) {
          const lines = textLines(block);
          if (lines.length < 2) continue;                 // need a name plus some text
          seen.add(block);
          const speaker = lines[0];
          const text = lines.slice(1).join(' ').trim();
          if (!text) continue;
          markSpeakerRecent(speaker, t);
          let u = utter.get(block);
          if (u && u.speaker !== speaker) { finalize(u, t); utter.delete(block); u = null; }
          if (u && u.final) {
            const prevText = u.text;
            if (text.startsWith(prevText)) {
              const delta = text.slice(prevText.length).trim();
              if (delta) {
                u = {
                  id: runId + '-' + (++counter), speaker, text: delta, start: t, last: t, changed: t,
                  dirty: true, final: false, lastEmit: 0
                };
                utter.set(block, u);
              }
            } else if (text !== prevText) {
              u = {
                id: runId + '-' + (++counter), speaker, text: text, start: t, last: t, changed: t,
                dirty: true, final: false, lastEmit: 0
              };
              utter.set(block, u);
            }
          } else if (!u) {
            u = {
              id: runId + '-' + (++counter), speaker, text, start: t, last: t, changed: t,
              dirty: true, final: false, lastEmit: 0
            };
            utter.set(block, u);
          } else if (text !== u.text) {
            const merged = mergeText(u.text, text);
            if (merged !== u.text) {
              u.text = merged; u.last = t; u.changed = t; u.dirty = true; u.final = false;
            }
          }
        }
      }
      for (const [block, u] of Array.from(utter)) {
        if (!seen.has(block) || !block.isConnected) { finalize(u, t); utter.delete(block); }
        else if (!u.final && t - u.changed > CFG.finalizeAfterMs) finalize(u, t);
        else if (u.dirty && t - u.lastEmit >= CFG.flushMs) emitCaption(u, t);
      }
    }

    // ---------- participants: presence, camera, mic ----------
    function hasIconLigature(el, iconName) {
      if (!el) return false;
      const text = el.textContent || '';
      if (text.includes(iconName)) {
        if (el.nodeType === 1 && (el.textContent || '').trim() === iconName) return true;
        const nodes = el.querySelectorAll ? el.querySelectorAll('span, i, div, mat-icon, [class*="icon" i], [class*="symbol" i]') : [];
        for (let i = 0; i < nodes.length; i++) {
          if ((nodes[i].textContent || '').trim() === iconName) return true;
        }
      }
      const dashName = iconName.replace('_', '-');
      if (el.matches && (el.matches(`[data-icon="${iconName}"], [data-icon="${dashName}"], .${iconName}, .${dashName}`))) return true;
      if (el.querySelector && el.querySelector(`[data-icon="${iconName}"], [data-icon="${dashName}"], .${iconName}, .${dashName}`)) return true;
      return false;
    }

    function getSelfMic() {
      const btns = doc.querySelectorAll(
        'button[aria-label*="microphone" i], div[role="button"][aria-label*="microphone" i], ' +
        'button[aria-label*="mic" i], div[role="button"][aria-label*="mic" i], ' +
        'button[data-is-muted], div[role="button"][data-is-muted]'
      );
      for (let i = 0; i < btns.length; i++) {
        const b = btns[i];
        const label = (b.getAttribute('aria-label') || '').toLowerCase();
        // Ignore auxiliary menus (e.g. "Microphone settings", "More microphone options")
        if (/settings|more options|device|select a mic|arrow|chevron/i.test(label) &&
          !/turn (on|off)|unmute|mute/i.test(label)) {
          continue;
        }

        const isMutedAttr = b.getAttribute('data-is-muted');
        if (isMutedAttr != null) return isMutedAttr !== 'true';

        if (label.includes('turn off microphone') || label.includes('mute microphone') || label.includes('mute (')) return true;
        if (label.includes('turn on microphone') || label.includes('unmute microphone') || label.includes('unmute (')) return false;

        const txt = (b.textContent || '').trim();
        if (txt.includes('mic_off')) return false;
        if (txt === 'mic' || (txt.includes('mic') && !txt.includes('mic_off') && !/settings|options/i.test(label))) return true;
      }
      return null;
    }

    function getSelfCam() {
      const btns = doc.querySelectorAll(
        'button[aria-label*="camera" i], div[role="button"][aria-label*="camera" i], ' +
        'button[aria-label*="video" i], div[role="button"][aria-label*="video" i]'
      );
      for (let i = 0; i < btns.length; i++) {
        const b = btns[i];
        const label = (b.getAttribute('aria-label') || '').toLowerCase();
        // Ignore visual effects, background blur, settings dropdowns
        if (/effects|background|settings|more options|device|select a camera|arrow|chevron/i.test(label) &&
          !/turn (on|off)/i.test(label)) {
          continue;
        }

        if (label.includes('turn off camera') || label.includes('turn off video') || label.includes('turn off camera (')) return true;
        if (label.includes('turn on camera') || label.includes('turn on video') || label.includes('turn on camera (')) return false;

        const txt = (b.textContent || '').trim();
        if (txt.includes('videocam_off') || txt.includes('video_off')) return false;
        if (txt === 'videocam' || (txt.includes('videocam') && !/effects|background|settings/i.test(label))) return true;
      }
      return null;
    }

    function isPresentationTile(tile) {
      if (!tile) return false;
      if (tile.matches && tile.matches('[data-is-presentation="true"], [data-is-screenshare="true"], [data-is-presentation]')) return true;
      if (tile.querySelector && tile.querySelector('[data-is-presentation="true"], [data-is-screenshare="true"], [data-is-presentation]')) return true;

      const pid = ((tile.getAttribute && tile.getAttribute('data-participant-id')) || '') + ' ' +
        ((tile.getAttribute && tile.getAttribute('data-requested-participant-id')) || '') + ' ' +
        ((tile.getAttribute && tile.getAttribute('data-allocation-id')) || '');
      if (/presentation/i.test(pid)) return true;

      const aria = ((tile.getAttribute && tile.getAttribute('aria-label')) || '').toLowerCase();
      if (/\bpresentation\b|\bis presenting\b|\byou are presenting\b|\byou're presenting\b/i.test(aria)) return true;

      const lines = textLines(tile);
      for (let i = 0; i < lines.length; i++) {
        if (/^(presentation|you are presenting|you're presenting to everyone)$/i.test(lines[i])) return true;
        if (/\b(is presenting|presentation)\b/i.test(lines[i])) return true;
      }
      return false;
    }

    function detectTileMic(tile, name, t) {
      const flag = (tile.matches && tile.matches('[data-is-muted]')) ? tile : tile.querySelector('[data-is-muted]');
      if (flag) {
        const val = flag.getAttribute('data-is-muted');
        if (val === 'true') return false;
        if (val === 'false') return true;
      }
      if (hasIconLigature(tile, 'mic_off') || (tile.textContent || '').includes('mic_off')) return false;

      const ariaEls = Array.from(tile.querySelectorAll('[aria-label]'));
      if (tile.hasAttribute && tile.hasAttribute('aria-label')) ariaEls.unshift(tile);
      for (let i = 0; i < ariaEls.length; i++) {
        const label = (ariaEls[i].getAttribute('aria-label') || '').toLowerCase();
        if (/\b(muted|microphone is off|microphone off|mic is off|mic off|turned off)\b/i.test(label)) return false;
        if (/\b(microphone|mic)\b.*(off|muted|turn on)/i.test(label)) return false;
        if (/\b(speaking|talking|active speaker|unmuted|microphone is on|microphone on)\b/i.test(label)) return true;
        if (/\b(microphone|mic)\b.*(on|unmuted|active)/i.test(label)) return true;
      }

      if (tile.querySelector('[data-visual-indicator], [data-active-speaker], .speaking-indicator, [data-is-speaking="true"], [data-is-talking="true"]')) {
        return true;
      }

      if (name && isRecentSpeaker(name, t)) {
        return true;
      }

      if (hasIconLigature(tile, 'mic') && !(tile.textContent || '').includes('mic_off')) {
        return true;
      }

      return CFG.assumeMicOnWhenNoIndicator ? true : false;
    }

    function detectTileCam(tile) {
      if (hasIconLigature(tile, 'videocam_off') || hasIconLigature(tile, 'video_off')) return false;
      if ((tile.textContent || '').includes('videocam_off') || (tile.textContent || '').includes('video_off')) return false;

      const ariaEls = Array.from(tile.querySelectorAll('[aria-label]'));
      if (tile.hasAttribute && tile.hasAttribute('aria-label')) ariaEls.unshift(tile);
      for (let i = 0; i < ariaEls.length; i++) {
        const label = (ariaEls[i].getAttribute('aria-label') || '').toLowerCase();
        if (/\b(camera|video)\b.*(off|disabled|turned off|is off)|turn on (camera|video)/i.test(label)) return false;
      }

      if (tile.matches && (tile.matches('[data-is-camera-off="true"], [data-video-off="true"], [data-is-video-muted="true"], [data-has-video="false"]'))) return false;
      if (tile.querySelector('[data-is-camera-off="true"], [data-video-off="true"], [data-is-video-muted="true"], [data-has-video="false"]')) return false;

      const videos = Array.from(tile.querySelectorAll('video'));
      if (!videos.length) return false;

      return videos.some((v) => {
        if (v.paused) return false;
        const cs = root.getComputedStyle ? root.getComputedStyle(v) : null;
        if (cs && (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0')) return false;
        if (v.readyState != null && v.readyState < 2) return false;
        if (v.videoWidth != null && v.videoWidth === 0) return false;
        if (v.srcObject && typeof v.srcObject.getVideoTracks === 'function') {
          const tracks = v.srcObject.getVideoTracks();
          if (!tracks.length || !tracks[0].enabled || tracks[0].readyState !== 'live' || tracks[0].muted) {
            return false;
          }
        }
        return true;
      });
    }

    function isSelfTile(tile) {
      if (!tile) return false;
      if (tile.matches && tile.matches('[data-is-self="true"], [data-self-name]')) return true;
      if (tile.querySelector && tile.querySelector('[data-is-self="true"], [data-self-name]')) return true;
      const video = tile.querySelector ? tile.querySelector('video') : null;
      if (video) {
        const style = video.getAttribute('style') || '';
        if (style.includes('scaleX(-1)')) return true;
      }
      let p = tile.parentElement;
      while (p && p !== doc.body) {
        const aria = ((p.getAttribute && p.getAttribute('aria-label')) || '').toLowerCase();
        if (aria.includes('your self view') || aria.includes('your video') || aria.includes('self-view')) return true;
        p = p.parentElement;
      }
      return false;
    }

    function getSelfDevice() {
      const ua = (root.navigator && root.navigator.userAgent) || '';
      const isMobileUA = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini|Mobile/i.test(ua);
      const isIpadOS = !!(root.navigator && root.navigator.maxTouchPoints > 1 && /Macintosh/.test(ua));
      return (isMobileUA || isIpadOS) ? 'mobile' : 'desktop';
    }

    function isPhoneNumber(str) {
      if (!str) return false;
      const trimmed = str.trim();
      const digits = trimmed.replace(/\D/g, '');
      if (digits.length >= 7 && digits.length <= 15) {
        if (/^[\s+]*\+?\d{1,4}?[-.\s]?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}[\s]*$/.test(trimmed)) return true;
        if (/^\+?\d{7,15}$/.test(trimmed.replace(/[-.\s()]/g, ''))) return true;
      }
      return false;
    }

    function cleanParticipantName(raw) {
      if (!raw) return '';
      let str = raw.replace(/\s+/g, ' ').trim();

      // Extract real name from action phrases:
      // e.g. "Pin Krishna Sarathe to your main screen", "Pin Krishna to screen", "Pin Alice"
      const mPin = str.match(/^(?:pin|unpin)\s+(.+?)(?:\s+to\s+(?:your\s+)?(?:main\s+)?screen)?$/i);
      if (mPin && mPin[1]) str = mPin[1].trim();

      // "More options for [Name]", "Actions for [Name]", "Action menu for [Name]"
      const mAction = str.match(/^(?:more\s+options|actions|action\s+menu)\s+for\s+(.+)$/i);
      if (mAction && mAction[1]) str = mAction[1].trim();

      // "Mute [Name]", "Unmute [Name]"
      const mMute = str.match(/^(?:mute|unmute)\s+(.+)$/i);
      if (mMute && mMute[1]) str = mMute[1].trim();

      // "[Name] is speaking", "[Name] is talking", "[Name] is muted", "[Name] is unmuted"
      const mState = str.match(/^(.+?)\s+is\s+(?:speaking|talking|muted|unmuted)$/i);
      if (mState && mState[1]) str = mState[1].trim();

      // Remove role and status suffixes:
      // " (You)", " (Meeting host)", " Meeting host", " (Host)", " Host", " (Co-host)", " Co-host", " (Presentation)", " (Presenting)", " (Guest)", " (External)"
      str = str.replace(/\s*\((?:you|presentation|presenting|meeting host|host|co-host|guest|external)\)$/i, '');
      str = str.replace(/\s+(?:meeting host|host|co-host)$/i, '');
      return str.trim();
    }

    function isNoiseName(str) {
      if (!str || str.length <= 1) return true;
      // Camera framing / auto-frame status
      if (/^(you're|you are)?\s*(continuously\s+framed|framed|framing)/i.test(str)) return true;
      if (/^framing\s+(is\s+)?(on|off)$/i.test(str)) return true;
      // Mic / camera / audio status
      if (/^microphones?\s+(is\s+)?(on|off|muted|unmuted)/i.test(str)) return true;
      if (/^(camera|video|audio)\s+(is\s+)?(on|off)/i.test(str)) return true;
      if (/^turn\s+(on|off)\s+(microphone|camera|video)/i.test(str)) return true;
      // UI actions & buttons
      if (/^(pin|unpin|pin\s+to\s+(main\s+)?screen|unpin\s+from\s+(main\s+)?screen)$/i.test(str)) return true;
      if (/^(jump to bottom|captions?|caption settings|settings|more options|actions|mute|unmute|remove from call|keep|devices?|people|contributors?|search for people)$/i.test(str)) return true;
      if (/^(participant|tile|video|grid|screen|presentation|presentation tile)$/i.test(str)) return true;
      if (/^(\d+\s*(people|contributors?|in the call)?)$/i.test(str)) return true;
      if (/^waiting for (host|others|a call)/i.test(str)) return true;
      if (/^(minimize|expand self view|show in a tile|change layout|picture in picture)$/i.test(str)) return true;
      return false;
    }

    function hasPhoneOrMobileIndicator(el, name) {
      if (!el && !name) return false;
      if (name && isPhoneNumber(name)) return true;
      if (!el) return false;

      // 1. Text or ligature
      const mobileIcons = ['smartphone', 'phone_android', 'phone_iphone', 'phone', 'call', 'stay_current_portrait', 'mobile_friendly', 'tablet_android', 'tablet_mac'];
      for (let i = 0; i < mobileIcons.length; i++) {
        if (hasIconLigature(el, mobileIcons[i])) return true;
      }
      // 2. SVG paths: Material 2 & Material 3 / Symbols icons for phone & smartphone
      const svgs = el.querySelectorAll ? Array.from(el.querySelectorAll('svg')) : [];
      if (el.tagName && el.tagName.toLowerCase() === 'svg') svgs.push(el);
      for (let i = 0; i < svgs.length; i++) {
        const s = svgs[i];
        const aria = (s.getAttribute('aria-label') || s.getAttribute('title') || '').toLowerCase();
        if (/\b(phone|mobile|smartphone|cellular|android|iphone|dial-in|call|handset)\b/i.test(aria)) return true;
        const paths = Array.from(s.querySelectorAll('path'));
        for (let j = 0; j < paths.length; j++) {
          const d = paths[j].getAttribute('d') || '';
          // Smartphone / phone_android / stay_current_portrait (Material 2 & 3)
          if (/M17\s*1(\.01)?|M16\s*1H8|M15\.5\s*1h-8|M17\s*2|M18\s*1\.01/i.test(d)) return true;
          if (/M5\s*23q|M7\s*23q|M4\s*20V4|M6\s*22q|M5\s*21V3/i.test(d)) return true;
          // Phone / call / dial-in (Material 2 & 3)
          if (/M6\.62\s*10\.79|M20\.01\s*15\.38|M21\s*16\.5|M19\.23\s*15\.26/i.test(d)) return true;
          if (/M19\.95\s*21q|M19\.95\s*21|M20\s*21|M7\.4\s*16\.6/i.test(d)) return true;
        }
      }
      // 3. Accessibility labels & tooltips
      const ariaEls = el.querySelectorAll ? Array.from(el.querySelectorAll('[aria-label], [title], [data-tooltip]')) : [];
      if (el.hasAttribute && el.hasAttribute('aria-label')) ariaEls.unshift(el);
      for (let i = 0; i < ariaEls.length; i++) {
        const l = (ariaEls[i].getAttribute('aria-label') || ariaEls[i].getAttribute('title') || ariaEls[i].getAttribute('data-tooltip') || '').toLowerCase();
        if (/\b(joined from (a )?phone|joined on mobile|mobile device|phone participant|dial-in|using a phone|smartphone|iphone|android phone|calling in|companion mode|companion)\b/i.test(l)) return true;
      }
      // 4. Subtitle / text
      const txt = (el.textContent || '').toLowerCase();
      if (/\b(joined by phone|joined from phone|phone call|dial-in|companion mode)\b/i.test(txt)) return true;
      if (isPhoneNumber(txt)) return true;

      return false;
    }

    function detectDevice(tile, isSelf, prevDevice, name) {
      if (isSelf) return getSelfDevice();
      // Sticky preservation of positive identification
      if (prevDevice === 'mobile') return 'mobile';
      if (name && isPhoneNumber(name)) return 'mobile';
      if (!tile) return prevDevice || 'unknown';

      if (hasPhoneOrMobileIndicator(tile, name)) return 'mobile';

      // Check tile attributes or ID
      const pid = ((tile.getAttribute && tile.getAttribute('data-participant-id')) || '') + ' ' +
        ((tile.getAttribute && tile.getAttribute('data-requested-participant-id')) || '') + ' ' +
        ((tile.getAttribute && tile.getAttribute('data-client-type')) || '') + ' ' +
        ((tile.getAttribute && tile.getAttribute('data-device-type')) || '');
      if (/\b(phoneusers|mobile|phone|android|ios|companion)\b/i.test(pid)) return 'mobile';

      // Check video stream / element dimensions
      const video = tile.querySelector ? tile.querySelector('video') : null;
      if (video) {
        // 1. WebRTC MediaStreamTrack settings (exact native capture resolution)
        if (video.srcObject && typeof video.srcObject.getVideoTracks === 'function') {
          try {
            const tracks = video.srcObject.getVideoTracks();
            if (tracks && tracks.length && typeof tracks[0].getSettings === 'function') {
              const s = tracks[0].getSettings();
              if (s) {
                if ((s.height && s.width && s.height > s.width * 1.05) || (s.aspectRatio && s.aspectRatio < 0.95)) {
                  return 'mobile';
                }
                if (s.width && s.height && s.width >= s.height * 1.15) {
                  return 'desktop';
                }
              }
            }
          } catch (e) { }
        }
        // 2. Video element internal resolution (portrait video: mobile camera)
        if (video.videoWidth > 0 && video.videoHeight > 0) {
          if (video.videoHeight > video.videoWidth * 1.05) return 'mobile';
          if (video.videoWidth >= video.videoHeight * 1.15) return 'desktop';
        }
        // 3. Rendered / layout dimensions (e.g. pillarboxed mobile view)
        if (video.clientHeight > 0 && video.clientWidth > 0) {
          if (video.clientHeight > video.clientWidth * 1.05) return 'mobile';
          if (video.clientWidth >= video.clientHeight * 1.15) return 'desktop';
        }
        if (video.getBoundingClientRect) {
          try {
            const r = video.getBoundingClientRect();
            if (r.height > 0 && r.width > 0) {
              if (r.height > r.width * 1.05) return 'mobile';
              if (r.width >= r.height * 1.15) return 'desktop';
            }
          } catch (e) { }
        }
      }

      // Check CSS classes / attributes for portrait or mobile
      if (tile.matches && tile.matches('.portrait-video, [data-is-portrait="true"], [data-device-type="mobile"], [data-device="mobile"], [data-is-phone="true"], [data-companion="true"]')) return 'mobile';
      if (tile.querySelector && tile.querySelector('.portrait-video, [data-is-portrait="true"], [data-device-type="mobile"], [data-device="mobile"], [data-is-phone="true"], [data-companion="true"]')) return 'mobile';

      // If remote participant has the exact same name as the local user (isSelf), this is their companion / mobile join
      if (knownSelfName && name && name.toLowerCase() === knownSelfName.toLowerCase() && !isSelf) {
        return 'mobile';
      }

      // If previously observed as desktop when camera was active, preserve desktop
      if (prevDevice === 'desktop') return 'desktop';

      // Unobservable when camera is off: do not invent desktop!
      return prevDevice || 'unknown';
    }

    function readTile(tile, id, t, prevDevice) {
      const selfEl = (tile.matches && tile.matches('[data-self-name]')) ? tile : tile.querySelector('[data-self-name]');
      let name = selfEl && selfEl.getAttribute('data-self-name');
      if (name) name = cleanParticipantName(name);

      if (!name || isNoiseName(name)) {
        const nameEl = tile.querySelector('[data-name], [data-participant-name]');
        if (nameEl && nameEl.textContent) name = cleanParticipantName(nameEl.textContent);
      }
      // Prioritize visible text lines of the tile over child button aria-labels
      if (!name || isNoiseName(name)) {
        const lines = textLines(tile);
        for (let i = 0; i < lines.length; i++) {
          const candidate = cleanParticipantName(lines[i]);
          if (candidate && !isNoiseName(candidate)) {
            name = candidate;
            break;
          }
        }
      }
      // Fallback to tile's own aria-label if no visible text line exists
      if (!name || isNoiseName(name)) {
        const tileAria = tile.getAttribute ? (tile.getAttribute('aria-label') || tile.getAttribute('title') || '') : '';
        const candidate = cleanParticipantName(tileAria);
        if (candidate && !isNoiseName(candidate)) {
          name = candidate;
        }
      }

      // If name is noise or empty, this tile is not a valid participant (e.g. framing notification overlay or pin button)
      if (isNoiseName(name)) {
        return null;
      }
      if (!name) name = 'Participant ' + id.slice(-4);

      const tileAria = ((tile.getAttribute && tile.getAttribute('aria-label')) || '').toLowerCase();
      const isSelf = id === 'self' || name === 'You' || name.endsWith(' (You)') || !!selfEl ||
        (tile.matches && tile.matches('[data-is-self="true"]')) ||
        !!tile.querySelector('[data-is-self="true"]') ||
        /\(you\)/i.test(tileAria) || tileAria === 'you' ||
        isSelfTile(tile);

      if (isSelf && knownSelfName && name === 'You') {
        name = knownSelfName;
      }

      const selfMic = isSelf ? getSelfMic() : null;
      const selfCam = isSelf ? getSelfCam() : null;

      const mic = (selfMic !== null) ? selfMic : detectTileMic(tile, name, t);
      const cam = (selfCam !== null) ? selfCam : detectTileCam(tile);
      const device = detectDevice(tile, isSelf, prevDevice, name);

      return { name, cam, mic, device, isSelf };
    }

    function getOfficialHeadcount() {
      const candidates = doc.querySelectorAll(
        'button[aria-label*="everyone" i], button[aria-label*="people" i], button[aria-label*="participant" i], ' +
        '[role="button"][aria-label*="everyone" i], [role="button"][aria-label*="people" i], [role="button"][aria-label*="participant" i]'
      );
      for (let i = 0; i < candidates.length; i++) {
        const btn = candidates[i];
        const label = (btn.getAttribute('aria-label') || '');
        const m = label.match(/\((\d+)\)/) || label.match(/(\d+)\s*(?:people|participants|everyone)/i) || label.match(/(?:everyone|people|participants)[^\d]*(\d+)/i);
        if (m) {
          const n = parseInt(m[1], 10);
          if (n > 0 && n < 1000) return n;
        }
        const badge = btn.querySelector('[class*="count" i], [class*="badge" i], span');
        if (badge && /^\d+$/.test(badge.textContent.trim())) {
          const n = parseInt(badge.textContent.trim(), 10);
          if (n > 0 && n < 1000) return n;
        }
      }
      return null;
    }

    function snapshot(t) {
      let cam = 0, mic = 0, desktop = 0, mobile = 0;
      people.forEach((p) => {
        if (p.cam) cam++;
        if (p.mic) mic++;
        if (p.device === 'mobile') mobile++; else desktop++;
      });
      const officialCount = getOfficialHeadcount();
      const total = officialCount !== null ? Math.max(people.size, officialCount) : people.size;
      send({
        type: 'snapshot', total, cam_on: cam, cam_off: Math.max(0, total - cam),
        mic_on: mic, mic_off: Math.max(0, total - mic),
        desktop_count: desktop, mobile_count: mobile
      }, t);
    }

    function getParticipantTiles() {
      const candidates = Array.from(doc.querySelectorAll(CFG.tile));
      return candidates.filter((el) => {
        if (isPresentationTile(el)) return false;
        let p = el.parentElement;
        while (p && p !== doc.body) {
          if (candidates.indexOf(p) !== -1) return false;
          p = p.parentElement;
        }
        return true;
      });
    }

    function scanPeoplePanel(current) {
      const items = doc.querySelectorAll('[role="listitem"][data-participant-id], [role="tabpanel"] [role="listitem"]');
      items.forEach((item, idx) => {
        if (isPresentationTile(item)) return;
        const id = item.getAttribute('data-participant-id') || ('panel-p-' + idx);
        let rawName = '';
        const nameEl = item.querySelector('[data-name], span');
        if (nameEl) rawName = (nameEl.textContent || '').trim();
        if (!rawName) return;
        const name = cleanParticipantName(rawName);
        if (!name || isNoiseName(name)) return;
        const isSelf = rawName.includes('(You)') || name === 'You' || id === 'self';
        if (isSelf && name !== 'You') {
          knownSelfName = name;
        }

        let mic = false;
        const micOff = hasIconLigature(item, 'mic_off') || (item.textContent || '').includes('mic_off') ||
          item.querySelector('[data-is-muted="true"]') ||
          /\b(muted|microphone is off|mic off)\b/i.test(item.getAttribute('aria-label') || '');
        if (micOff) {
          mic = false;
        } else if (hasIconLigature(item, 'mic') || item.querySelector('[data-is-muted="false"]') ||
          /\b(unmuted|speaking|talking)\b/i.test(item.getAttribute('aria-label') || '')) {
          mic = true;
        }

        const prevP = people.get(id);
        const dev = detectDevice(item, isSelf, prevP ? prevP.device : null, name);

        const existingKey = Array.from(current.keys()).find((k) => {
          const val = current.get(k);
          return val && (val.name.toLowerCase() === name.toLowerCase() || (isSelf && val.isSelf));
        });
        if (!existingKey) {
          current.set(id, { name, cam: false, mic, device: dev, isSelf });
        } else {
          const existing = current.get(existingKey);
          if (existing) {
            if (dev === 'mobile') existing.device = 'mobile';
            else if (dev === 'desktop' && existing.device === 'unknown') existing.device = 'desktop';
            if (isSelf) existing.isSelf = true;
            if (name !== 'You' && existing.name === 'You') existing.name = name;
          }
        }
      });
    }

    function scanPeople(t) {
      const current = new Map();
      const tiles = getParticipantTiles();
      tiles.forEach((tile) => {
        const id = tile.getAttribute('data-participant-id') ||
          tile.getAttribute('data-requested-participant-id') ||
          tile.getAttribute('data-allocation-id');
        if (!id || current.has(id)) return;
        const prev = people.get(id);
        const data = readTile(tile, id, t, prev ? prev.device : null);
        if (data && data.name && !isNoiseName(data.name)) {
          current.set(id, data);
        }
      });

      const selfTileEntry = Array.from(current.entries()).find(([k, p]) => p.isSelf);
      const selfMic = getSelfMic();
      const selfCam = getSelfCam();
      const inCallControls = (selfMic !== null || selfCam !== null);

      if (selfTileEntry) {
        if (selfMic !== null) selfTileEntry[1].mic = selfMic;
        if (selfCam !== null) selfTileEntry[1].cam = selfCam;
        if (knownSelfName && selfTileEntry[1].name === 'You') {
          selfTileEntry[1].name = knownSelfName;
        }
        if (people.has('self') && selfTileEntry[0] !== 'self') {
          people.delete('self');
        }
      } else if (inCallControls) {
        const selfNameEl = doc.querySelector('[data-self-name]');
        const rawSelfName = (selfNameEl && selfNameEl.getAttribute('data-self-name')) || knownSelfName || 'You';
        const selfName = cleanParticipantName(rawSelfName) || 'You';
        current.set('self', {
          name: selfName,
          cam: selfCam != null ? selfCam : false,
          mic: selfMic != null ? selfMic : false,
          device: getSelfDevice(),
          isSelf: true
        });
      }

      scanPeoplePanel(current);

      let changed = false;
      current.forEach((s, id) => {
        const p = people.get(id);
        if (!p) {
          const dev = s.device || 'unknown';
          people.set(id, { name: s.name, cam: s.cam, mic: s.mic, device: dev, isSelf: s.isSelf, missingSince: 0 });
          send({ type: 'participant_join', id, name: s.name, cam: s.cam, mic: s.mic, device: dev }, t);
          changed = true;
          return;
        }
        p.missingSince = 0;
        if (s.name !== p.name && !p.isSelf && !isNoiseName(s.name)) {
          p.name = s.name;
          send({ type: 'participant_update', id, name: s.name, device: s.device || p.device }, t);
        }
        if (s.device && s.device !== p.device) {
          if (s.device !== 'unknown' || !p.device) {
            p.device = s.device;
            send({ type: 'participant_update', id, name: p.name, device: p.device }, t);
            changed = true;
          }
        }
        if (s.cam !== p.cam) { p.cam = s.cam; send({ type: s.cam ? 'cam_on' : 'cam_off', id, name: p.name }, t); changed = true; }
        if (s.mic !== p.mic) { p.mic = s.mic; send({ type: s.mic ? 'mic_on' : 'mic_off', id, name: p.name }, t); changed = true; }
      });

      const officialCount = getOfficialHeadcount();
      people.forEach((p, id) => {
        if (current.has(id)) return;
        if (isRecentSpeaker(p.name, t)) {
          p.missingSince = 0;
          return;
        }
        if (!p.missingSince) { p.missingSince = t; return; }
        if (officialCount !== null && people.size <= officialCount) {
          return;
        }
        if (t - p.missingSince > CFG.leaveGraceMs) {
          send({ type: 'participant_leave', id, name: p.name }, t);
          people.delete(id);
          changed = true;
        }
      });
      if (changed) snapshot(t);
    }

    // ---------- call lifecycle ----------
    function endCall(t) {
      utter.forEach((u) => finalize(u, t));
      utter.clear();
      people.forEach((p, id) => send({ type: 'participant_leave', id, name: p.name }, t));
      people.clear();
      send({ type: 'call_ended' }, t);
      inCall = false;
      leaveMissingSince = 0;
    }

    function tick() {
      const t = Date.now();
      tickCount++;
      if (doc.querySelector(CFG.leaveButton)) {
        leaveMissingSince = 0;
        if (!inCall) { inCall = true; captionTries = 0; send({ type: 'call_started' }, t); }
      } else if (inCall) {
        if (!leaveMissingSince) leaveMissingSince = t;
        else if (t - leaveMissingSince > CFG.endGraceMs) { endCall(t); return; }
      }
      if (!inCall) return;
      ensureCaptions(t);
      scanCaptions(t);
      if (tickCount % 2 === 0) scanPeople(t);
    }

    function dump() {
      const region = findCaptionRegion();
      console.group('[MeetCore] dump');
      console.log('in call:', inCall, '| meeting:', meetingId());
      console.log('caption region:', region);
      if (region) {
        console.log('parsed blocks:', captionBlocks(region).map((b) => textLines(b)));
        console.log(region.outerHTML.slice(0, 3000));
      }
      console.table(Array.from(doc.querySelectorAll(CFG.tile)).map((tile) => {
        const id = tile.getAttribute('data-participant-id');
        return Object.assign({ id }, readTile(tile, id));
      }));
      console.groupEnd();
    }

    const timer = root.setInterval(tick, CFG.tickMs);
    root.addEventListener('meetcore-dump', dump);
    tick();

    return function stop() {
      root.clearInterval(timer);
      root.removeEventListener('meetcore-dump', dump);
    };
  }

  root.MeetCore = { start, _mergeText: mergeText, _textLines: textLines };
})(typeof window !== 'undefined' ? window : globalThis);
