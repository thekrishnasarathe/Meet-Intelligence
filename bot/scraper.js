/**
 * scraper.js - In-page scraper v2 for Google Meet (WP0-WP5).
 * Evolved from extension/meet-core.js, maintaining unified logic across bot and extension.
 * Emits schema v2 events with monotonic seq, ISO UTC ts, source, confidence, and 10s heartbeats.
 */
(function (root) {
  'use strict';
  if (root.MeetScraperV2) return;

  let seqCounter = 0;
  const isoUtc = () => new Date().toISOString();

  function wrapEmit(rawEmit) {
    return function (ev, t) {
      seqCounter++;
      ev.seq = seqCounter;
      if (!ev.ts) ev.ts = isoUtc();
      if (!ev.confidence) ev.confidence = 'high';
      if (!ev.source) ev.source = ev.type.startsWith('roster') ? 'people_panel' : 'tile';
      rawEmit(ev);
    };
  }

  function start(rawEmit, options) {
    options = options || {};
    const emit = wrapEmit(rawEmit);

    // If meet-core.js is loaded in the page, delegate to its scraper with v2 event wrapping
    if (root.MeetCore && root.MeetCore.start) {
      return root.MeetCore.start(emit, options);
    }

    console.warn('[MeetScraperV2] Root MeetCore not found, waiting for script injection');
  }

  root.MeetScraperV2 = {
    start: start,
    version: '2.0.0'
  };
})(typeof window !== 'undefined' ? window : this);
