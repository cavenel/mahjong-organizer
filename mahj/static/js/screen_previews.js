/* Behaviour for the screen-preview grid rendered by _screen_previews.html:
   show/hide the thumbnails, and enlarge one into the modal.

   Every preview is a live display page in an iframe, so the `src` is set only
   while the panel is open — a dozen collapsed thumbnails must not sit there
   polling. Re-showing re-sets `src`, which also refreshes a stale thumbnail. */
$(function () {
  $("#toggle-previews").on('click', function () {
    var $container = $("#previews-container");
    var hidden = $container.css('display') === 'none';
    if (hidden) {
      $(".preview-iframe").each(function () { this.src = $(this).attr('data-src'); });
      $container.css('display', 'flex');
      $("#toggle-previews-label").text('Hide preview');
    } else {
      $container.css('display', 'none');
      $(".preview-iframe").each(function () { this.removeAttribute('src'); });
      $("#toggle-previews-label").text('Show preview');
    }
  });

  function closePreviewModal() {
    $("#preview-modal").css('display', 'none');
    document.getElementById('preview-modal-iframe').removeAttribute('src');
  }

  $(".enlarge-preview").on('click', function () {
    var src = $(this).attr('data-src');
    // Fit a 1920×1080 frame inside the viewport (leaving room for the modal chrome).
    var scale = Math.min((window.innerWidth - 64) / 1920, (window.innerHeight - 120) / 1080);
    scale = Math.min(scale, 1);
    var iframe = document.getElementById('preview-modal-iframe');
    iframe.style.transform = 'scale(' + scale + ')';
    iframe.src = src;
    $("#preview-modal-frame").css({ width: (1920 * scale) + 'px', height: (1080 * scale) + 'px' });
    $("#preview-modal-title").text($(this).attr('data-title'));
    $("#preview-modal").css('display', 'flex');
  });

  $("#preview-modal-close").on('click', closePreviewModal);
  // Close when clicking the dark backdrop (but not the modal box itself).
  $("#preview-modal").on('click', function (e) { if (e.target === this) closePreviewModal(); });
  $(document).on('keydown', function (e) { if (e.key === 'Escape') closePreviewModal(); });
});
