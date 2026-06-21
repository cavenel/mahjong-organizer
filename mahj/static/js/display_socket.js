/*
 * Shared display WebSocket client for public screens.
 *
 * Solves two failure modes that previously left a screen silently stale:
 *   1. Missed events while disconnected — on every *re*connection we reload
 *      (or call opts.onReconnect) so we resync whatever was broadcast while
 *      the socket was down (e.g. a server restart/deploy, network blip).
 *   2. Silent half-open sockets — after a sleep/NAT timeout the socket can sit
 *      in readyState OPEN with no `onclose` ever firing, so broadcasts are
 *      dropped forever. A client ping + pong watchdog detects this and forces
 *      a reconnect (which then resyncs via case 1).
 *
 * The server side (mahj/consumers.py TenantConsumer.receive_json) answers
 * {"type":"ping"} with {"event":"pong"} — pongs are swallowed here, never
 * surfaced to onUpdate.
 *
 * Usage:
 *   connectDisplaySocket({
 *     subdomain: 'foo',
 *     path: '/ws/display/',            // optional, this is the default
 *     onUpdate: function(msg) { ... }, // a non-pong message arrived
 *     onReconnect: function() { ... }, // optional; default: location.reload()
 *     onConnect: function() { ... },   // optional; fired on every (re)open
 *     onDisconnect: function() { ... } // optional; fired on every close
 *   });
 */
function connectDisplaySocket(opts) {
  var PING_INTERVAL = 25000;   // how often we probe the connection
  var PONG_TIMEOUT = 10000;    // no pong within this → treat socket as dead
  var MAX_BACKOFF = 30000;

  var path = opts.path || '/ws/display/';
  var subdomain = opts.subdomain;
  // The routing regex requires a non-empty subdomain; bail rather than open a
  // socket that can never match (mirrors the scorer page's guard).
  if (!subdomain) return;

  // --- debug aid -----------------------------------------------------------
  // Toggle from the URL (?dsdebug=1) or the console (localStorage.dsdebug=1)
  // with no redeploy. Turn on DevTools "Preserve log" (Console + Network) so
  // the trace survives each reload. ?dsdebug=hold also SUPPRESSES the reloads,
  // so you can watch the live socket and see whether it's broadcasts
  // (onUpdate) or reconnects flooding the page.
  var dbgFlag = (/[?&]dsdebug=([^&]*)/.exec(location.search) || [])[1];
  if (dbgFlag === undefined && /[?&]dsdebug(&|$)/.test(location.search)) dbgFlag = '1';
  if (dbgFlag === undefined) dbgFlag = (function(){ try { return localStorage.getItem('dsdebug') || undefined; } catch (_) { return undefined; } })();
  var DEBUG = dbgFlag !== undefined && dbgFlag !== '0';
  var HOLD = dbgFlag === 'hold';
  var t0 = Date.now();
  function log() {
    if (!DEBUG) return;
    var dt = ((Date.now() - t0) / 1000).toFixed(1);
    var args = ['[ds +' + dt + 's]'].concat([].slice.call(arguments));
    console.log.apply(console, args);
  }
  log('init: subdomain=' + subdomain + ' path=' + path + (HOLD ? ' (HOLD: reloads suppressed)' : ''));

  var ws = null;
  var everConnected = false;
  var retryDelay = 1000;
  var pingTimer = null;
  var pongTimer = null;

  // --- disconnect banner ---------------------------------------------------
  // A visible, persistent overlay so an unattended projector never sits silently
  // stale: if the socket stays down past a short grace window we show a
  // high-contrast bar. It's attached to <html> (not <body>) because the projector
  // templates zoom <body>, which would otherwise shrink/scale the bar. The ping/
  // pong watchdog turns a dead-but-OPEN socket into an onclose, so this one banner
  // also covers the silently-half-open case. Callers that own their own status UI
  // (the public desktop page) opt out with statusBanner:false.
  var BANNER_GRACE_MS = 4000;
  var bannerEnabled = opts.statusBanner !== false;
  var bannerEl = null;
  var bannerVisible = false;
  var bannerGraceTimer = null;
  var bannerSecTimer = null;

  function ensureBanner() {
    if (bannerEl || !bannerEnabled) return bannerEl;
    if (!document.getElementById('ds-banner-style')) {
      var st = document.createElement('style');
      st.id = 'ds-banner-style';
      st.textContent = '@keyframes dsBannerPulse{0%,100%{opacity:1}50%{opacity:.55}}';
      (document.head || document.documentElement).appendChild(st);
    }
    bannerEl = document.createElement('div');
    bannerEl.id = 'ds-banner';
    bannerEl.style.cssText = [
      'position:fixed', 'top:0', 'left:0', 'right:0', 'z-index:2147483647',
      'background:#dc2626', 'color:#fff', 'font:bold 28px/1.4 sans-serif',
      'text-align:center', 'padding:14px 8px', 'letter-spacing:.5px',
      'box-shadow:0 2px 12px rgba(0,0,0,.5)',
      'animation:dsBannerPulse 1.5s ease-in-out infinite', 'pointer-events:none',
      'display:none'
    ].join(';');
    (document.documentElement || document.body).appendChild(bannerEl);
    return bannerEl;
  }

  function displayBanner() {
    var el = ensureBanner();
    if (!el) return;
    var downSince = Date.now();
    function paint() {
      var secs = Math.round((Date.now() - downSince) / 1000);
      el.textContent = '⚠ DISPLAY DISCONNECTED — reconnecting… (' + secs + 's)';
    }
    paint();
    el.style.display = 'block';
    bannerVisible = true;
    if (bannerSecTimer) clearInterval(bannerSecTimer);
    bannerSecTimer = setInterval(paint, 1000);
    log('disconnect banner shown');
  }

  function hideBanner() {
    if (bannerGraceTimer) { clearTimeout(bannerGraceTimer); bannerGraceTimer = null; }
    if (bannerSecTimer) { clearInterval(bannerSecTimer); bannerSecTimer = null; }
    if (bannerEl) bannerEl.style.display = 'none';
    bannerVisible = false;
  }

  function armBanner() {
    if (!bannerEnabled || bannerVisible || bannerGraceTimer) return;
    bannerGraceTimer = setTimeout(function() {
      bannerGraceTimer = null;
      displayBanner();
    }, BANNER_GRACE_MS);
  }

  function clearTimers() {
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
    if (pongTimer) { clearTimeout(pongTimer); pongTimer = null; }
  }

  function startHeartbeat() {
    clearTimers();
    pingTimer = setInterval(function() {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      try {
        ws.send(JSON.stringify({ type: 'ping' }));
      } catch (e) {
        try { ws.close(); } catch (_) {}
        return;
      }
      log('ping sent');
      if (pongTimer) clearTimeout(pongTimer);
      pongTimer = setTimeout(function() {
        // No pong came back — the socket is dead/half-open. Force a close so
        // onclose schedules a reconnect.
        log('no pong within ' + PONG_TIMEOUT + 'ms → closing (assumed dead)');
        try { ws.close(); } catch (_) {}
      }, PONG_TIMEOUT);
    }, PING_INTERVAL);
  }

  function connect() {
    var scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(scheme + '://' + location.host + path + subdomain + '/');

    ws.onopen = function() {
      var reconnected = everConnected;
      everConnected = true;
      retryDelay = 1000;
      log('open' + (reconnected ? ' (RECONNECT → would reload)' : ' (first connect)'));
      startHeartbeat();
      hideBanner();
      if (opts.onConnect) opts.onConnect();
      if (reconnected && !HOLD) {
        (opts.onReconnect || function() { location.reload(); })();
      }
    };

    ws.onmessage = function(e) {
      var msg;
      try { msg = JSON.parse(e.data); } catch (_) { msg = {}; }
      if (msg && msg.event === 'pong') {
        log('pong received');
        if (pongTimer) { clearTimeout(pongTimer); pongTimer = null; }
        return;
      }
      log('message → onUpdate (would reload):', JSON.stringify(msg));
      if (!HOLD) opts.onUpdate(msg);
    };

    ws.onclose = function(e) {
      log('close (code=' + (e && e.code) + ' wasClean=' + (e && e.wasClean) + ')');
      clearTimers();
      // Show the stale-screen banner if we don't recover within the grace window.
      armBanner();
      if (opts.onDisconnect) opts.onDisconnect();
      // Jittered, capped exponential backoff so a fleet of screens doesn't
      // reconnect (and reload) in lockstep after a restart.
      var delay = retryDelay + Math.random() * 1000;
      retryDelay = Math.min(retryDelay * 2, MAX_BACKOFF);
      log('reconnecting in ' + Math.round(delay) + 'ms');
      setTimeout(connect, delay);
    };

    ws.onerror = function() {
      try { ws.close(); } catch (_) {}
    };
  }

  connect();
}
