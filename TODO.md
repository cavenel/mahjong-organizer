- Add Theme variables


# Done
- "Sum of the four Minipoints — must equal 0" - Add an IMPORTANT NOTE here. If there are penalties during a game, the total can be -10, -20 etc. This is not an error. The scorer must check that the difference comes from one or more penalties. The scorer must keep the penalties and the sum different than zero.
- In the score sheets, add a row before total with 4 int forms, for penalties. They must be integers (+ or -). The sum is then total before penalties + penalties. The Table point is computed on sum after penalties.
- Fix score sheet edits on mobile (make table larger, keep space for everything, maybe put QR code and Valid AFTER table if not large enough?)
- Fix score sheet "Entered MP / TP" on two lines instead of one.
- Fix ceremony Teams and Players list on mobile (don't make page wider, use scroll x)
- In docs, remove "This is the MCR head-scorer edition (scorer + publisher + display operator). A scorer who only enters scores can use the shorter scorer guide (MCR). Staff tools (importing/preparing the tournament, creating users, EMA export) and the complete reference live in full_admin.md. For a Riichi tournament, use Riichi_head_scorer.md." and any reference to other roles or other docs. This is a documentation I will give to the given role, so that they know their own features. - for head scorer, remove "1. Roles at a glance". Not their problem. They get the roles and can do these things.
- Remove "(On a Riichi tournament only Minipoints are entered and there's no score sheet — see Riichi_head_scorer.md.)" again this is not relevant for the head scorer, they don't care about other roles or other docs.
- Remove "~2 seconds after you stop typing (and immediately if you navigate away)" this is too technical.
- Remove "If a webhook is configured, a round_published event is sent." this is too technical for the head scorer.
- Remove "How it works (for your peace of mind): the timer is server-authoritative — the server owns the official start instant and every screen renders from it, so screens can't drift apart or be reset by a stray reload. The round length comes from the Total time of a round display setting (below)."
- Remove occurences of "tenant-wide", we know.
- Check table "Top-right buttons (apply to the whole ceremony): | Button | Effect | |---|---| | Blank screens | A clean holding slide (logo / title) on every screen | | End — back to screens | Stop the ceremony; screens return to their normal views. Nothing is published. | | Publish to everyone & end | Reveal the full final results on the public site and all screens, then end. Do this once, at the very end. |" it didn't display correctly in the pdf version.
- In permission recap, remove the long dashes, keep empty if no permission.


# Theme variables:
The core challenge: Tailwind + dynamic colors
Your tailwind.min.css is a static build. Tailwind purges unused classes at build time, so you can't do bg-{{ variables.primary_color }}-500 and expect it to work — those class names won't be in the CSS.

The right approach: CSS custom properties, defined from the model in a <style> block, paired with a small set of hand-written utility classes that use them.

How many variables?
Minimum: 1, but practically 3 to cover the full amber palette you're using:

Variable field	Default (current amber)	Used for
color_primary	#f59e0b	toggle buttons, round buttons active state
color_primary_dark	#b45309	bold text (scores, links hover)
color_primary_light	#fffbeb	row hover bg, header highlight bg
You could get away with 1 field (a base hue in HSL) and compute the shades in CSS with oklch() or hsl() + color-mix(), but that requires modern browser support and is harder to expose in an admin input.

Yes, they'd go in Variable
It's already the right model — one row per tenant, holds all per-tournament config. You'd add:


# models.py — in class Variable
color_primary       = models.CharField(default="#f59e0b", max_length=20)
color_primary_dark  = models.CharField(default="#b45309", max_length=20)
color_primary_light = models.CharField(default="#fffbeb", max_length=20)
Template wiring
In desktop.html (and each modal template), replace the static <style> block with:


<style>
  :root {
    --c-primary:       {{ variables.color_primary|default:"#f59e0b" }};
    --c-primary-dark:  {{ variables.color_primary_dark|default:"#b45309" }};
    --c-primary-light: {{ variables.color_primary_light|default:"#fffbeb" }};
  }

  /* Drop-in replacements for every amber-* Tailwind class you use */
  .t-bg-primary      { background-color: var(--c-primary); }
  .t-bg-primary-light{ background-color: var(--c-primary-light); }
  .t-text-primary    { color: var(--c-primary); }
  .t-text-primary-dk { color: var(--c-primary-dark); }
  .t-border-primary  { border-color: var(--c-primary); }
  .t-border-primary-lt { border-color: color-mix(in srgb, var(--c-primary) 20%, white); }
  /* hover variants */
  .hover\:t-text-primary:hover { color: var(--c-primary); }
  .score-link:hover  { background-color: color-mix(in srgb, var(--c-primary-light) 80%, transparent); }
  /* active toggle button */
  [data-active="true"] { background-color: var(--c-primary); }
</style>
Then in the HTML you'd swap e.g.:

bg-amber-500 text-white → t-bg-primary text-white
text-amber-600 → t-text-primary
hover:bg-amber-50 → hover:t-bg-primary-light (or keep as inline hover via Alpine :class)
The Alpine :class bindings (like :class="seatingMode==='round' ? 'bg-amber-500 text-white' : ...") are the easiest to swap since they're already dynamic — just change the string value to your new class name.

Tradeoff to know
The refactor touches ~30–40 class occurrences across desktop.html and the modal templates. It's mechanical but not trivial. If you want to defer it, a quick shortcut is to only override the few CSS classes Alpine toggles dynamically (the toggle buttons, active tabs) via CSS variables, and leave the static ones as amber for now.