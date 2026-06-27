/*
 * Live-page WebSocket helper for the admin shell (Scoring, Publisher overview,
 * Display console).
 *
 * Wraps connectDisplaySocket — so the admin pages inherit its heartbeat
 * watchdog (catches a silently half-open socket after sleep/NAT timeout) and
 * jittered auto-reconnect — and surfaces an impossible-to-miss banner when the
 * page can no longer be trusted to match the server. Unlike the public screens
 * we never auto-reload: a scorer may be mid entry, so the banner offers a
 * manual Refresh instead.
 *
 * Two states:
 *   reconnecting (amber) — the socket is down right now, so live updates aren't
 *     arriving. Shown only after a short grace so a sub-second blip doesn't
 *     flash a warning; clears itself if we reconnect cleanly.
 *   out of sync (red, sticky) — we reconnected after a gap, so a broadcast
 *     (publish, validation, a completed table) may have been missed while we
 *     were down. Only a reload can be trusted, so this stays until the user
 *     refreshes.
 *
 * Usage:
 *   connectScorerSocket({ subdomain: '...', onUpdate: function (msg) { ... } });
 */
function connectScorerSocket(opts) {
  if (!opts.subdomain) return;

  var GRACE_MS = 4000;
  var state = 'hidden';           // 'hidden' | 'reconnecting' | 'stale'
  var graceTimer = null;
  var bannerEl = null, textEl = null;

  function ensureBanner() {
    if (bannerEl) return bannerEl;
    bannerEl = document.createElement('div');
    bannerEl.id = 'admin-sync-banner';
    bannerEl.style.cssText = [
      'position:fixed', 'top:0', 'left:0', 'right:0', 'z-index:2147483647',
      'display:none', 'align-items:center', 'justify-content:center', 'gap:16px',
      'font:600 15px/1.4 sans-serif', 'color:#fff', 'text-align:center',
      'padding:12px 16px', 'box-shadow:0 2px 12px rgba(0,0,0,.35)'
    ].join(';');
    textEl = document.createElement('span');
    var refreshBtn = document.createElement('button');
    refreshBtn.type = 'button';
    refreshBtn.textContent = 'Refresh';
    refreshBtn.style.cssText = [
      'background:#fff', 'color:#111', 'border:0', 'border-radius:6px',
      'font:600 14px/1 sans-serif', 'padding:8px 14px', 'cursor:pointer'
    ].join(';');
    refreshBtn.addEventListener('click', function () { location.reload(); });
    bannerEl.appendChild(textEl);
    bannerEl.appendChild(refreshBtn);
    (document.body || document.documentElement).appendChild(bannerEl);
    return bannerEl;
  }

  function paint() {
    var el = ensureBanner();
    if (state === 'hidden') { el.style.display = 'none'; return; }
    if (state === 'stale') {
      el.style.background = '#dc2626';
      textEl.textContent = '⚠ This page is out of sync with the server.';
    } else {
      el.style.background = '#d97706';
      textEl.textContent = '⚠ Connection lost — reconnecting…';
    }
    el.style.display = 'flex';
  }

  connectDisplaySocket({
    subdomain: opts.subdomain,
    path: '/ws/scorers/',
    statusBanner: false,          // we draw our own admin-flavoured banner
    onUpdate: opts.onUpdate,
    // Socket dropped: warn (after a grace) that updates aren't arriving.
    onDisconnect: function () {
      if (state === 'stale' || graceTimer) return;
      graceTimer = setTimeout(function () {
        graceTimer = null;
        state = 'reconnecting';
        paint();
      }, GRACE_MS);
    },
    // Re-opened after a gap → we may have missed a broadcast. Stick the red
    // out-of-sync banner; from here only a manual refresh can be trusted.
    onReconnect: function () {
      if (graceTimer) { clearTimeout(graceTimer); graceTimer = null; }
      state = 'stale';
      paint();
    },
    // Fires on every open. On the first connect this is a no-op; on a clean
    // reconnect it clears the transient "reconnecting" banner (onReconnect then
    // upgrades it to the sticky stale banner straight after).
    onConnect: function () {
      if (graceTimer) { clearTimeout(graceTimer); graceTimer = null; }
      if (state === 'reconnecting') { state = 'hidden'; paint(); }
    },
  });
}
