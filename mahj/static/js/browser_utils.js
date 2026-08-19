/* Small helpers for pages that build HTML strings client-side or POST to the
   API. Loaded with a plain <script src>, so both live on `window`. */

/* Escape a value for interpolation into an HTML string — both text nodes and
   quoted attribute values. Player and team names are operator-entered (Excel
   import, player editor), so every name that reaches innerHTML goes through
   here; without it a name containing `<` or `"` injects markup. */
window.escapeHtml = function (value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
};

/* The CSRF token from the cookie, for the `X-CSRFToken` header on fetch/ajax
   writes. Pages rendered fresh can use `{{ csrf_token }}` instead; this is for
   the ones that can't (cached HTML) or that post from a static JS file. */
window.csrfCookie = function () {
  var m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]*)/);
  return m ? decodeURIComponent(m[1]) : '';
};
