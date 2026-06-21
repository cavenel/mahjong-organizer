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
