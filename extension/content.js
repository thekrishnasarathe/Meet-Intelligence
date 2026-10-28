// Adapter: runs the shared scraper in the Meet tab, adds 1-click in-page bot deploy pill, and forwards events to background.js.
(function () {
  // If an older instance was running before an extension reload, clean it up cleanly
  if (window.__meetIntelCleanup) {
    try {
      window.__meetIntelCleanup();
    } catch (_) {}
  }

  const DEBUG = true;

  // Track if current extension context is valid
  function isExtensionValid() {
    try {
      return !!(typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.id);
    } catch (_) {
      return false;
    }
  }

  function handleContextInvalidated() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    if (pillBtn) {
      pillBtn.disabled = false;
      pillBtn.style.borderColor = 'rgba(239, 68, 68, 0.6)';
      pillBtn.style.background = 'linear-gradient(135deg, rgba(69, 10, 10, 0.94), rgba(15, 23, 42, 0.94))';
      pillBtn.innerHTML = '<span>🔄</span><span>Extension Reloaded (Click to Refresh)</span>';
      pillBtn.title = 'The extension was reloaded in Chrome. Click to refresh this tab.';
      pillBtn.onclick = () => window.location.reload();
    }
  }

  function safeSendMessage(msg, callback) {
    if (!isExtensionValid()) {
      handleContextInvalidated();
      return false;
    }
    try {
      if (!callback) {
        // One-way fire-and-forget: do not pass callback so Chrome never opens an unused response port
        const p = chrome.runtime.sendMessage(msg);
        if (p && p.catch) p.catch(() => {});
        return true;
      }
      chrome.runtime.sendMessage(msg, (resp) => {
        const lastErr = chrome.runtime.lastError;
        if (lastErr) {
          if (lastErr.message && lastErr.message.includes('Extension context invalidated')) {
            handleContextInvalidated();
            return;
          }
          if (lastErr.message && lastErr.message.includes('message port closed')) {
            return; // Normal Chrome Manifest V3 teardown artifact; ignore silently
          }
        }
        if (callback) callback(resp);
      });
      return true;
    } catch (err) {
      if (err.message && err.message.includes('Extension context invalidated')) {
        handleContextInvalidated();
      }
      return false;
    }
  }

  function getMeetingId() {
    const m = window.location.pathname.match(/(?:meet\/)?([a-z]{3}-[a-z]{4}-[a-z]{3})/i);
    return m ? m[1].toLowerCase() : '';
  }

  // ---------- In-Page 1-Click Bot Deploy Pill ----------
  let botRunning = false;
  let pillBtn = null;
  let pollTimer = null;

  function injectBotPill() {
    const existingWrap = document.getElementById('meet-intel-pill-wrap');
    if (existingWrap) existingWrap.remove();

    const wrap = document.createElement('div');
    wrap.id = 'meet-intel-pill-wrap';
    wrap.style.cssText = `
      position: fixed;
      top: 14px;
      left: 78px;
      z-index: 999999;
      display: flex;
      align-items: center;
      gap: 6px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    `;

    pillBtn = document.createElement('button');
    pillBtn.id = 'meet-intel-pill-btn';
    pillBtn.style.cssText = `
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 6px 13px;
      background: linear-gradient(135deg, rgba(24, 32, 54, 0.94), rgba(15, 23, 42, 0.94));
      color: #f1f5f9;
      border: 1px solid rgba(99, 102, 241, 0.45);
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      box-shadow: 0 4px 15px rgba(0, 0, 0, 0.4);
      transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
    `;
    pillBtn.innerHTML = '<span>🤖</span><span>Deploy Notes Bot</span>';

    pillBtn.addEventListener('mouseenter', () => {
      pillBtn.style.borderColor = '#818cf8';
      pillBtn.style.transform = 'translateY(-1px)';
    });
    pillBtn.addEventListener('mouseleave', () => {
      pillBtn.style.borderColor = botRunning ? 'rgba(16, 185, 129, 0.6)' : 'rgba(99, 102, 241, 0.45)';
      pillBtn.style.transform = 'translateY(0)';
    });

    pillBtn.addEventListener('click', () => {
      if (!isExtensionValid()) {
        handleContextInvalidated();
        return;
      }
      const mid = getMeetingId();
      if (!mid) return;

      if (botRunning) {
        pillBtn.disabled = true;
        pillBtn.innerHTML = '<span>🛑</span><span>Stopping Bot...</span>';
        safeSendMessage({ kind: 'bot_stop', meetingId: mid }, (resp) => {
          pillBtn.disabled = false;
          if (resp && resp.ok) {
            updatePillUI(false);
          } else {
            console.warn('[MeetIntel] Stop bot response:', resp);
            updatePillUI(false);
          }
        });
      } else {
        pillBtn.disabled = true;
        pillBtn.innerHTML = '<span>🚀</span><span>Launching Bot...</span>';
        safeSendMessage(
          { kind: 'bot_launch', url: window.location.href, name: 'Notes Bot' },
          (resp) => {
            pillBtn.disabled = false;
            if (resp && resp.ok) {
              updatePillUI(true);
            } else {
              const detail = (resp && (resp.error || (resp.data && resp.data.detail))) || 'Backend offline';
              console.warn('[MeetIntel] Bot launch error:', detail);
              pillBtn.innerHTML = '<span>⚠️</span><span>' + (detail.length > 20 ? 'Launch Failed' : detail) + '</span>';
              setTimeout(() => updatePillUI(false), 3000);
            }
          }
        );
      }
    });

    wrap.appendChild(pillBtn);
    document.body.appendChild(wrap);

    // Initial bot status check
    checkBotStatus();
  }

  function updatePillUI(running) {
    if (!pillBtn) return;
    botRunning = running;
    if (running) {
      pillBtn.style.borderColor = 'rgba(16, 185, 129, 0.6)';
      pillBtn.style.background = 'linear-gradient(135deg, rgba(6, 44, 30, 0.94), rgba(15, 23, 42, 0.94))';
      pillBtn.innerHTML = '<span style="color: #34d399">●</span><span>Notes Bot Active (Click to Stop)</span>';
    } else {
      pillBtn.style.borderColor = 'rgba(99, 102, 241, 0.45)';
      pillBtn.style.background = 'linear-gradient(135deg, rgba(24, 32, 54, 0.94), rgba(15, 23, 42, 0.94))';
      pillBtn.innerHTML = '<span>🤖</span><span>Deploy Notes Bot</span>';
    }
  }

  function checkBotStatus() {
    const mid = getMeetingId();
    if (!mid) return;
    safeSendMessage({ kind: 'bot_status', meetingId: mid }, (resp) => {
      if (resp && resp.ok && resp.data) {
        updatePillUI(!!resp.data.running);
      }
    });
  }

  // Inject pill on DOM ready
  if (document.body) injectBotPill();
  else window.addEventListener('DOMContentLoaded', injectBotPill);

  // Periodically poll bot status every 3s
  pollTimer = setInterval(checkBotStatus, 3000);

  // ---------- Auto-Deploy Check & Core Scraper ----------
  let autoDeployTriggered = false;

  let stopScraper = null;
  if (window.MeetCore && window.MeetCore.start) {
    stopScraper = window.MeetCore.start((ev) => {
      if (DEBUG && ev.type !== 'snapshot') {
        console.debug('[MeetIntel]', ev.type, ev.speaker || ev.name || '', (ev.text || '').slice(-80));
      }

      // Auto-deploy bot only if explicitly enabled by user
      if (ev.type === 'call_started' && !autoDeployTriggered) {
        autoDeployTriggered = true;
        if (isExtensionValid() && chrome.storage && chrome.storage.local) {
          try {
            chrome.storage.local.get(['autoDeployBot']).then((r) => {
              if (r && r.autoDeployBot === true && !botRunning) {
                console.info('[MeetIntel] User opted into auto-deploy: launching bot...');
                safeSendMessage(
                  { kind: 'bot_launch', url: window.location.href, name: 'Notes Bot' },
                  (resp) => {
                    if (resp && resp.ok) updatePillUI(true);
                  }
                );
              }
            }).catch(() => {});
          } catch (_) {}
        }
      }

      safeSendMessage({ kind: 'meet_event', ev });
    });
  }

  const onPageHide = () => {
    const mid = getMeetingId() || 'unknown';
    safeSendMessage({
      kind: 'meet_event',
      ev: { type: 'call_ended', meetingId: mid, ts: new Date().toISOString() }
    });
  };
  window.addEventListener('pagehide', onPageHide);

  // Store cleanup function on window so future reloads can cleanly clean up
  window.__meetIntelCleanup = () => {
    if (pollTimer) clearInterval(pollTimer);
    if (stopScraper) {
      try { stopScraper(); } catch (_) {}
    }
    window.removeEventListener('pagehide', onPageHide);
    const wrap = document.getElementById('meet-intel-pill-wrap');
    if (wrap) wrap.remove();
  };
})();
