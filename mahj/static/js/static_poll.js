/*
 * Static-export drop-in replacement for display_socket.js.
 *
 * The exported public site is served by a dumb web host with no Django and no
 * WebSocket endpoint, so the live socket client can't run there (it would loop
 * forever on failed /ws/ connects and flash the "DISPLAY DISCONNECTED" banner).
 *
 * This file exposes the SAME `connectDisplaySocket(opts)` entry point the
 * desktop page already calls, but instead of opening a socket it polls a small
 * `version.json` sitting next to the page. The export writes a fresh
 * version.json on every publish; when the version changes we fire
 * opts.onUpdate({event:'leaderboard_update'}) — which the page turns into its
 * existing "Refresh" prompt (updateAvailable = true), identical to the live UX.
 *
 * The export's URL-rewrite pass swaps the <script src="display_socket.js"> tag
 * for this file, so the page's own connectWS() code is untouched.
 */
function connectDisplaySocket(opts) {
  var POLL_INTERVAL = 30000;   // how often we check for a new publish
  var VERSION_URL = 'version.json';

  // The page treats "connected" as its healthy state (hides any disconnected
  // hint). A static site is always "reachable", so report connected once.
  if (opts.onConnect) opts.onConnect();

  var baseline = null;         // version seen on first successful poll
  var notified = false;        // fire the refresh prompt only once

  function poll() {
    // Cache-bust so neither the browser nor the host serves a stale file.
    fetch(VERSION_URL + '?t=' + Date.now(), { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        var v = String(data.version == null ? '' : data.version);
        if (baseline === null) {
          baseline = v;                 // establish the version this page was built at
        } else if (v !== baseline && !notified) {
          notified = true;              // a newer export was uploaded
          if (opts.onUpdate) opts.onUpdate({ event: 'leaderboard_update' });
        }
      })
      .catch(function () { /* transient network error — try again next tick */ });
  }

  poll();
  setInterval(poll, POLL_INTERVAL);
}
