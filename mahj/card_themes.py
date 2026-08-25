"""Default CSS for each printed-player-card theme, plus the colour presets.

A theme is a bundle of rules in the card stylesheet (``body.theme-<name>`` in
mahj/templates/mahj/cards/sheet.html) *and* a default variable block, which is
what an organiser sees in the card-design editor and edits. Keeping the variable
block here as text — rather than as model fields — means a new knob is a new line
in these strings, with no migration and no UI change: the design page builds its
colour pickers by parsing whatever ``--name: value;`` lines the text holds.

``TournamentSettings.card_css`` blank means "the theme's default block"; once the
organiser touches anything, the page saves the full text.
"""

# Comments in these blocks are shown to organisers in the editor, so they name
# what each variable paints rather than restating the variable name.
_CLASSIC = """:root {
  --accent:       #006AA7;  /* header bars, table numbers, S/W/N wind chips */
  --accent-2:     #FECC02;  /* rule under the header, East wind chip */
  --paper:        #FFFFFF;  /* card background */
  --ink:          #1A2530;  /* player name and headings */
  --ink-soft:     #2A3540;  /* opponent names on the back */
  --muted:        #6E6E6E;  /* column labels, round numbers */
  --line:         #C0C0C0;  /* footer rule, day-band rule */
  --line-soft:    #E0E0E0;  /* row separators on the back */
  --write-border: #9A9A9A;  /* the boxes players write scores in */
  --wind-ink:     #FFFFFF;  /* letters on the S/W/N wind chips */
  --wind-east-ink:#006AA7;  /* letter on the East wind chip */
  --font-display: 'Fraunces', 'Times New Roman', serif;
  --font-body:    'Geist', system-ui, sans-serif;
  --font-mono:    'Geist Mono', ui-monospace, monospace;
}"""

# Ink-on-paper: no colour bars, hairlines and outlined wind chips instead.
_MINIMAL = """:root {
  --accent:       #2A3540;  /* hairlines, table numbers, wind chip outlines */
  --accent-2:     #8A6D1F;  /* East wind chip outline, day labels */
  --paper:        #FFFFFF;  /* card background */
  --ink:          #14181C;  /* player name and headings */
  --ink-soft:     #333A40;  /* opponent names on the back */
  --muted:        #7A7F84;  /* column labels, round numbers */
  --line:         #D2D5D8;  /* footer rule, day-band rule */
  --line-soft:    #EBEDEF;  /* row separators on the back */
  --write-border: #AAAEB2;  /* the boxes players write scores in */
  --wind-ink:     #2A3540;  /* letters on the S/W/N wind chips */
  --wind-east-ink:#8A6D1F;  /* letter on the East wind chip */
  --font-display: 'Geist', system-ui, sans-serif;
  --font-body:    'Geist', system-ui, sans-serif;
  --font-mono:    'Geist Mono', ui-monospace, monospace;
}"""

# Filled header band, high contrast — reads across a room.
_BOLD = """:root {
  --accent:       #123B63;  /* filled header band, totals border */
  --accent-2:     #F2B233;  /* header band under-rule, East wind chip */
  --paper:        #FFFFFF;  /* card background */
  --ink:          #10161C;  /* player name and headings */
  --ink-soft:     #263038;  /* opponent names on the back */
  --muted:        #5F676E;  /* column labels, round numbers */
  --line:         #B4BAC0;  /* footer rule, day-band rule */
  --line-soft:    #DCE0E4;  /* row separators on the back */
  --write-border: #8A9098;  /* the boxes players write scores in */
  --wind-ink:     #FFFFFF;  /* letters on the S/W/N wind chips */
  --wind-east-ink:#10161C;  /* letter on the East wind chip */
  --font-display: 'Fraunces', 'Times New Roman', serif;
  --font-body:    'Geist', system-ui, sans-serif;
  --font-mono:    'Geist Mono', ui-monospace, monospace;
}"""

CARD_THEME_DEFAULT_CSS = {
    "classic": _CLASSIC,
    "minimal": _MINIMAL,
    "bold": _BOLD,
}

# Editor presets: picking one rewrites the --accent / --accent-2 lines only, so
# the rest of the organiser's block (fonts, custom rules) survives.
CARD_PALETTES = [
    {"name": "Sweden", "accent": "#006AA7", "accent_2": "#FECC02"},
    {"name": "Ink & gold", "accent": "#1A2530", "accent_2": "#C9A227"},
    {"name": "Forest", "accent": "#1F5F3A", "accent_2": "#E0B84C"},
    {"name": "Slate", "accent": "#37474F", "accent_2": "#90A4AE"},
    {"name": "Crimson", "accent": "#8B1E2D", "accent_2": "#E8C547"},
    {"name": "Ocean", "accent": "#0B5C8E", "accent_2": "#4FC3F7"},
]


def effective_card_css(tournament):
    """The CSS to render for ``tournament``'s cards.

    Blank ``card_css`` means the organiser never touched the design, so the
    active theme's default block stands in. An unknown stored theme falls back to
    classic rather than raising — the same tolerance the print view applies to an
    unknown card_format.
    """
    if tournament.card_css:
        return tournament.card_css
    return CARD_THEME_DEFAULT_CSS.get(tournament.card_theme, _CLASSIC)
