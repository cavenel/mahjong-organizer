/* Autosave for the tournament-settings controls, shared by the Tournament
 * settings page and the Player card design page.
 *
 * Contract: every control carries class "tournament-input" and an id of the form
 * "tournament-<field>". A change on any of them posts *all* of them in one call
 * to the settings page's set_tournament action, which writes the fields on its
 * allowlist (see admin_views.TOURNAMENT_SETTINGS_FIELDS) and answers 400 with a
 * readable reason when a value is rejected.
 *
 * Status is reported inside the ".settings-card" being edited, not once at the
 * top of the page: cards sit below the fold, so a page-level status was invisible
 * exactly when it was wanted. Only one card shows a status at a time — a single
 * edit posts every field in one call, so two at once would imply two saves.
 *
 * On success a "tournament-saved" event fires on `document`, which the card
 * design page uses to reload its preview.
 */
window.initTournamentAutosave = function (options) {
  var csrfToken = options.csrfToken;
  var saveTimer = null;

  function saveState(text, $card) {
    clearTimeout(saveTimer);
    $(".settings-save-state").text('');
    var $slot = ($card && $card.length ? $card : $(".settings-card").first())
      .find(".settings-save-state");
    $slot.text(text);
    if (text === 'Saved') {
      saveTimer = setTimeout(function () { $slot.text(''); }, 1500);
    }
  }

  $(".tournament-input").on('change', function () {
    // Sent in the POST body, not the query string: custom card CSS runs to
    // kilobytes, well past what a URL can carry reliably.
    var payload = { 'csrfmiddlewaretoken': csrfToken };
    $(".tournament-input").each(function () {
      // A checkbox's .value is always "on"; send its checked state instead so
      // the boolean coercion server-side sees true/false.
      payload[this.id] = this.type === 'checkbox' ? (this.checked ? 'true' : 'false')
                                                  : this.value;
    });
    var $card = $(this).closest(".settings-card");
    saveState('Saving…', $card);
    $.ajax({
      type: "POST",
      url: "/admin?page=settings&action=set_tournament",
      data: payload,
      success: function () {
        saveState('Saved', $card);
        document.dispatchEvent(new CustomEvent('tournament-saved'));
      },
      error: function (xhr) {
        // Clear the status rather than leaving "Saving…" up — the dialog below
        // is what reports the failure.
        saveState('', $card);
        // set_tournament returns a short plain-text reason on a 400; a 500 is an
        // HTML error page with no useful text, so fall back to a generic line.
        var msg = (xhr && xhr.responseText) || '';
        if (!msg || xhr.status >= 500 || /<html/i.test(msg)) {
          msg = 'The server rejected the change (status ' + ((xhr && xhr.status) || '?') + ').';
        }
        window.alertAction({ title: 'Couldn’t save settings', body: msg });
      }
    });
  });
};
