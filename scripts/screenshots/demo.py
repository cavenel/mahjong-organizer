"""Record a narrated screen-capture tour of the app and render it to mp4 + gif.

Same throwaway instance as shots.py (standalone profile, sqlite, tenant 'test'):
this script only drives the browser and stitches the result. Each scene is one
Playwright context recording its own video segment; the segments are then padded
onto a 1280x720 canvas and concatenated, so the phone scene shows up as a
portrait panel between the landscape ones.

A caption pill and a synthetic mouse cursor are injected into every page (real
cursors are not part of a headless recording), which is what makes the capture
readable as a demo rather than as a screen dump.

Usage:
    demo.py [--lang en|fr] [--no-gif] [--keep] [scene ...]

Scenes, in their natural order (each re-runnable on its own; the ones that need
data assume the earlier ones have run at least once):

    reset       wipe the test tournament so the tour starts from a blank one
    login       the login page, then the run dashboard
    setup       the setup checklist and the player import
    print       the printable player cards
    scoring     a score sheet filled hand by hand, then validated
    publish     publishing round 1, and the publisher's pre-flight overview
    fill        (not recorded) fill the remaining rounds via the test toolbar
    display     the display console: outputs, previews, round timer
    screen_scores / screen_counter / screen_schedule
                the projector screens themselves, full-bleed
    phone       the public app on a phone, portrait
    ceremony    the prize-giving console revealing the podium
    screen_podium   the podium on the projector
    render      pad, concatenate and encode everything captured so far

Run with no scene names for all of them. Output goes to docs/screenshots/
(demo.mp4 + demo.gif, suffixed with the language for anything but English).
See README.md in this directory for the environment and the server.
"""
import json
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

from playwright.sync_api import sync_playwright

from shots import BASE, ENV, HERE, PW, PYTHON, REPO, FIXTURE, orm, stage_bootstrap

RAW = HERE / '.local/demo'
OUT = REPO / 'docs/screenshots'
DESKTOP = {'width': 1280, 'height': 720}
PHONE = {'width': 390, 'height': 844}
CANVAS = (1280, 720)
# Every segment opens on a blank frame while the first page paints; drop it.
TRIM = float(os.environ.get('DEMO_TRIM', '0.6'))
# The tour is a fast overview, not a walk-through: every pacing pause goes
# through beat(), so DEMO_PACE re-times the whole film at once. Waits for
# something to actually load stay unscaled.
PACE = float(os.environ.get('DEMO_PACE', '1.0'))
LANG = 'en'

# ---------------------------------------------------------------- overlay ----

# Caption pill + fake cursor + click ripple, installed in every frame. The
# caption is kept in sessionStorage so it survives the navigations inside a
# scene, and only the top frame draws it (the score-sheet iframe would
# otherwise echo it in the middle of the modal).
OVERLAY_JS = r"""
(() => {
  if (window.__demoInstalled) return;
  window.__demoInstalled = true;
  const KEY = '__demo_caption';
  const CSS = `
#__demo_layer{position:fixed;inset:0;pointer-events:none;z-index:2147483647;
  font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
#__demo_cap{position:absolute;left:50%;bottom:24px;transform:translateX(-50%);max-width:80%;
  background:rgba(15,23,42,.9);color:#fff;padding:9px 22px;border-radius:9999px;
  font-size:19px;font-weight:600;line-height:1.35;text-align:center;
  box-shadow:0 8px 28px rgba(0,0,0,.35);opacity:0;transition:opacity .3s}
#__demo_cap.on{opacity:1}
#__demo_cur{position:absolute;left:0;top:0;width:24px;height:24px;opacity:0;
  transition:opacity .2s;filter:drop-shadow(0 2px 3px rgba(0,0,0,.45))}
#__demo_cur.on{opacity:1}
.__demo_ring{position:absolute;width:14px;height:14px;margin:-7px 0 0 -7px;border-radius:50%;
  border:2px solid rgba(245,158,11,.95);background:rgba(245,158,11,.25);
  animation:__demo_pop .5s ease-out forwards}
@keyframes __demo_pop{from{transform:scale(.4);opacity:1}to{transform:scale(3.4);opacity:0}}
`;
  function build() {
    if (!document.body || document.getElementById('__demo_layer')) return;
    const st = document.createElement('style');
    st.textContent = CSS;
    (document.head || document.documentElement).appendChild(st);
    const layer = document.createElement('div');
    layer.id = '__demo_layer';
    layer.innerHTML =
      '<div id="__demo_cap"></div>' +
      '<svg id="__demo_cur" viewBox="0 0 24 24"><path d="M4 2 L4 20 L9 15.5 L12 22 L15 20.5 L12 14.5 L19 14 Z"' +
      ' fill="#111827" stroke="#fff" stroke-width="1.4"/></svg>';
    document.body.appendChild(layer);
    if (window.top === window) render(sessionStorage.getItem(KEY) || '');
  }
  function render(text) {
    const el = document.getElementById('__demo_cap');
    if (!el) return;
    el.textContent = text;
    el.classList.toggle('on', !!text);
  }
  window.__demoCaption = (text) => {
    if (window.top !== window) return;
    try { sessionStorage.setItem(KEY, text); } catch (e) {}
    build();
    render(text);
  };
  const move = (e) => {
    build();
    const cur = document.getElementById('__demo_cur');
    if (!cur) return;
    cur.classList.add('on');
    cur.style.transform = `translate(${e.clientX}px, ${e.clientY}px)`;
  };
  const ring = (e) => {
    build();
    const layer = document.getElementById('__demo_layer');
    if (!layer) return;
    const r = document.createElement('div');
    r.className = '__demo_ring';
    r.style.left = e.clientX + 'px';
    r.style.top = e.clientY + 'px';
    layer.appendChild(r);
    setTimeout(() => r.remove(), 600);
  };
  document.addEventListener('mousemove', move, true);
  document.addEventListener('mousedown', ring, true);
  if (document.readyState === 'loading')
    document.addEventListener('DOMContentLoaded', build);
  else
    build();
})();
"""

# --------------------------------------------------------------- captions ----

TEXT = {
    'login':        ("Sign in — one account per role",
                     "Connexion — un compte par rôle"),
    'console':      ("The console — the whole tournament from one place",
                     "La console — tout le tournoi au même endroit"),
    'dashboard':    ("The run dashboard: everything at a glance",
                     "Le tableau de bord : tout d'un coup d'œil"),
    'setup':        ("A checklist walks you through the setup",
                     "Une checklist guide la préparation"),
    'import':       ("Import the players from a spreadsheet",
                     "Importez les joueurs depuis un tableur"),
    'imported':     ("Players, teams and seating are in",
                     "Joueurs, équipes et placement sont chargés"),
    'card_design':  ("Player cards: pick a format, restyle them",
                     "Cartons joueur : choisissez un format, restylez-les"),
    'print':        ("Print player cards, table posters, schedules",
                     "Imprimez cartons, affiches de table, plannings"),
    'scoring':      ("Scoring: one row per table, one tab per round",
                     "Saisie : une ligne par table, un onglet par tour"),
    'sheet':        ("The score sheet computes as you type",
                     "La feuille de score calcule à la volée"),
    'validate':     ("Cross-checks turn green, then you validate",
                     "Les contrôles passent au vert, puis on valide"),
    'publish':      ("Publishing a round pushes it everywhere at once",
                     "Publier un tour l'envoie partout d'un coup"),
    'overview':     ("One page to check before you publish",
                     "Une page de contrôle avant publication"),
    'display':      ("The display console drives every screen in the room",
                     "La console pilote tous les écrans de la salle"),
    'previews':     ("Live previews of what each screen shows",
                     "Aperçu en direct de chaque écran"),
    'timer':        ("Start the round timer from here",
                     "Lancez le chrono du tour d'ici"),
    'screen_scores': ("Projector: live standings",
                      "Vidéoprojecteur : classement en direct"),
    'screen_counter': ("Projector: the round countdown",
                       "Vidéoprojecteur : le compte à rebours"),
    'screen_schedule': ("Projector: seating for the round",
                        "Vidéoprojecteur : le placement du tour"),
    'phone':        ("Players follow along on their phone",
                     "Les joueurs suivent sur leur téléphone"),
    'phone_player': ("Every player's own results, hand by hand",
                     "Les résultats de chacun, main par main"),
    'phone_seating': ("Where they sit, next round",
                      "Où ils s'assoient, au tour suivant"),
    'ceremony':     ("Prize-giving: reveal the podium place by place",
                     "Remise des prix : révélez le podium place par place"),
    'podium':       ("…on the big screen, in front of the room",
                     "…sur grand écran, devant la salle"),
}


def beat(page, ms):
    """A pacing pause, scaled by DEMO_PACE."""
    page.wait_for_timeout(max(120, int(ms * PACE)))


def say(page, key, hold=700):
    text = TEXT[key][1 if LANG == 'fr' else 0]
    try:
        page.evaluate("t => window.__demoCaption && window.__demoCaption(t)", text)
    except Exception:
        pass
    beat(page, hold)


# ---------------------------------------------------------------- driving ----

def cursor_to(page, locator, steps=14):
    """Walk the pointer to an element so the click reads as a movement."""
    try:
        box = locator.bounding_box(timeout=5000)
    except Exception:
        return
    if not box:
        return
    page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2,
                    steps=steps)
    beat(page, 120)


def click(page, locator, after=400):
    """Move to an element, click it, and let the result settle on camera."""
    cursor_to(page, locator)
    locator.click()
    beat(page, after)


def type_into(page, locator, text, after=200):
    cursor_to(page, locator)
    locator.click()
    page.keyboard.type(text, delay=max(20, int(55 * PACE)))
    beat(page, after)


def scroll(page, dy, steps=10):
    """Wheel-scroll in small steps — one big jump is unreadable on video."""
    for _ in range(steps):
        page.mouse.wheel(0, dy / steps)
        beat(page, 30)


def goto(page, path, wait=600):
    # Drop the caption before navigating: it is restored per page from
    # sessionStorage, so without this the previous line hangs over the new page
    # until the scene says something else.
    try:
        page.evaluate("() => window.__demoCaption && window.__demoCaption('')")
    except Exception:
        pass
    page.goto(f'{BASE}{path}')
    page.wait_for_load_state('networkidle')
    beat(page, wait)


# ------------------------------------------------------------- data setup ----

def stage_reset(browser):
    """Blank the test tournament so the tour opens on an empty instance.

    Driven through the app's own danger zone rather than the ORM: the running
    server caches the settings row for five minutes and busts that cache from
    its own signals, so an out-of-process wipe leaves it serving a row that no
    longer exists — and the next import 500s on the stale primary key.
    """
    print('== reset (not recorded) ==')
    ctx = browser.new_context(viewport=DESKTOP,
                              storage_state=str(state_path('anna.admin')))
    page = ctx.new_page()
    page.goto(f'{BASE}/admin?page=settings')
    page.wait_for_load_state('networkidle')
    if page.locator('#reset-confirm').count():
        page.fill('#reset-confirm', 'RESET')
        page.fill('#reset-password', PW)
        page.click('#reset-tournament')
        page.click('button:has-text("Delete everything")')
        page.wait_for_url('**/admin', timeout=60000)
        page.wait_for_load_state('networkidle')
    # The wipe restores defaults, so mark it a test tournament again — that is
    # what keeps the fill toolbar on the Scoring page for the `fill` stage.
    page.goto(f'{BASE}/admin?page=settings')
    page.wait_for_load_state('networkidle')
    page.check('#tournament-is_test')
    page.wait_for_timeout(1500)
    ctx.close()
    print('  ✓ tournament blanked')


def state_path(user):
    return RAW / f'state-{user}.json'


def stage_auth(browser):
    """Log each role in once, off camera, and keep the cookies on disk: a
    recorded scene then opens straight on its page instead of on a login form."""
    for user in ('anna.admin',):
        ctx = browser.new_context(viewport=DESKTOP)
        page = ctx.new_page()
        page.goto(f'{BASE}/admin')
        page.fill('#id_username', user)
        page.fill('#id_password', PW)
        page.click('button[type=submit]')
        page.wait_for_load_state('networkidle')
        ctx.storage_state(path=str(state_path(user)))
        ctx.close()


def enable_wal():
    """Put the throwaway DB in WAL mode (a persistent, one-off property of the
    file).

    Without it sqlite's rollback journal makes a reader block writers, so the
    row polling below was itself provoking the `database is locked` 500s that
    left tables unfilled. WAL lets the polls read while the app writes.
    The server must be (re)started afterwards to pick it up.
    """
    orm('''
from django.db import connection
with connection.cursor() as c:
    c.execute("PRAGMA journal_mode=WAL")
    print("journal mode:", c.fetchone()[0])
''')


def db_count(expr):
    """Count rows in the throwaway DB — the fill toolbar writes asynchronously,
    and the page's own badges are a less reliable signal than the rows."""
    code = ('import django; django.setup()\n'
            'from mahj.models import *\n'
            't = Tenant.objects.get(subdomain="test")\n'
            f'print({expr})')
    out = subprocess.run([PYTHON, '-c', code], capture_output=True, text=True,
                         cwd=REPO, env=ENV)
    try:
        return int(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return -1


def wait_rows(page, expr, target, timeout=120000, compare='>='):
    waited = 0
    while waited < timeout:
        got = db_count(expr)
        if (got >= target) if compare == '>=' else (0 <= got <= target):
            return True
        page.wait_for_timeout(3000)
        waited += 3000
    print(f'  ! timed out waiting for {expr} {compare} {target}')
    return False


def stage_fill(browser):
    """Fill and publish every round through the test toolbar — off camera,
    because it is a fixture shortcut, not something anyone does at a tournament.

    Round by round rather than "Fill all rounds": that button deliberately leaves
    the last round's final two tables blank (it exists to exercise the
    incomplete-round path), and the tour wants a tournament that finished.
    """
    ctx = browser.new_context(viewport=DESKTOP,
                              storage_state=str(state_path('anna.admin')))
    page = ctx.new_page()
    page.goto(f'{BASE}/admin?page=scoring')
    page.wait_for_selector('.table_row', state='attached')
    rounds = page.locator('button[role=tab]').count() or 3
    for rn in range(1, rounds + 1):
        page.click(f'button[role=tab]:has-text("Round {rn}")')
        page.wait_for_timeout(500)
        unscored = (f'Seat.objects.filter(tenant=t, round_nb={rn}, '
                    'tablepoints=None).count()')
        # sqlite serialises writers, and the toolbar fires one save per table a
        # few milliseconds apart: under the lock a table's save can 500 and its
        # row stays blank, which keeps the round incomplete and unpublishable.
        # Clicking again only rewrites the rows, so just retry until it is whole.
        for attempt in range(4):
            page.click('button:text-is("Fill — scores")')
            if wait_rows(page, unscored, 0, timeout=30000, compare='<='):
                break
        else:
            print(f'  ! round {rn} still has unscored seats')
        sheets = page.locator('button:text-is("Fill — score sheets")')
        if sheets.count():
            validated = ('ScoreSheet.objects.filter(tenant=t, '
                         f'round_nb={rn}, validated=True).count()')
            for attempt in range(3):
                sheets.click()
                if wait_rows(page, validated, 4, timeout=30000):
                    break
            else:
                # The hands themselves land even when a validate is lost to the
                # lock; finish the marks the toolbar would have set.
                orm(f'''
from mahj.models import Tenant, ScoreSheet, Seat
t = Tenant.objects.get(subdomain="test")
for tn in sorted(set(Seat.objects.filter(tenant=t, round_nb={rn})
                     .values_list("table_nb", flat=True))):
    ScoreSheet.objects.update_or_create(
        tenant=t, round_nb={rn}, table_nb=tn, defaults={{"validated": True}})
print("sheet validation completed via ORM")
''')
    # Publish every round from its own tab: the toggles run through the app, so
    # the leaderboard cache and the live screens are refreshed with them.
    for rn in range(1, rounds + 1):
        page.goto(f'{BASE}/admin?page=scoring')
        page.wait_for_selector('.table_row', state='attached')
        page.click(f'button[role=tab]:has-text("Round {rn}")')
        page.wait_for_timeout(600)
        toggle = page.locator(f'.tab-pane[data-round-nb="{rn}"] .publish-toggle').first
        if toggle.count() and toggle.is_enabled() and not toggle.is_checked():
            toggle.click()
            page.wait_for_timeout(2500)
    ctx.close()


def presentable_names():
    """Rename the fixture's two XSS-probe competitors.

    The click-through fixture deliberately carries hostile names to prove they
    are escaped everywhere; on camera they read as a broken tournament, and they
    show up on the cards, the sheets, the standings and the phone alike.
    """
    orm('''
from mahj.models import Tenant, Player
t = Tenant.objects.get(subdomain="test")
for draw, first, last in ((1, "Robert", "Nystrom"), (2, "Aoife", "O\'Brien")):
    p = Player.objects.filter(tenant=t, draw_number=draw).first()
    if p is None:
        continue
    p.first_name, p.last_name = first, last
    p.full_name = f"{first} {last}"
    p.short_name = first
    p.save()
print("names made presentable")
''')


def seed_screens():
    """Give the room three named screens, as a real venue would have."""
    orm('''
from mahj.models import Tenant, Screen, ScreenMode
t = Tenant.objects.get(subdomain="test")
have = list(Screen.objects.filter(tenant=t).order_by("id"))
while len(have) < 3:
    have.append(Screen.objects.create(tenant=t, name="", view="black"))
for sc, (name, view) in zip(have, [("Main hall", "scores:detailed"),
                                   ("Timer wall", "counter"),
                                   ("Entrance", "schedule")]):
    sc.name, sc.view = name, view
    sc.save()
ScreenMode.objects.filter(tenant=t).delete()
ScreenMode.objects.create(tenant=t, name="Play",
                          views=["scores:detailed", "counter", "schedule"])
ScreenMode.objects.create(tenant=t, name="Break",
                          views=["welcome", "welcome", "schedule"])
print("screens ready")
''')


# ----------------------------------------------------------------- scenes ----

def scene_login(page):
    goto(page, '/admin')
    say(page, 'login', 500)
    type_into(page, page.locator('#id_username'), 'anna.admin')
    type_into(page, page.locator('#id_password'), PW)
    click(page, page.locator('button[type=submit]'), after=1400)
    say(page, 'console', 1400)
    scroll(page, 500)
    beat(page, 500)


def scene_setup(page):
    goto(page, '/admin?page=setup')
    say(page, 'setup', 1400)
    scroll(page, 450)
    beat(page, 400)

    goto(page, '/admin?page=import_template')
    say(page, 'import', 900)
    cursor_to(page, page.locator('input[name=myfile]'))
    page.set_input_files('input[name=myfile]', str(FIXTURE))
    beat(page, 500)
    click(page, page.locator(
        'form[action="admin_upload_from_template"] button[type=submit]'), after=600)
    confirm = page.locator('button:has-text("Erase and import")')
    if confirm.count():
        click(page, confirm, after=500)
    page.wait_for_load_state('networkidle', timeout=180000)
    presentable_names()
    beat(page, 600)
    say(page, 'imported', 1200)

    goto(page, '/admin?page=player_editor', wait=900)
    scroll(page, 700)
    beat(page, 600)


def scene_print(page):
    # A7 landscape first: the default A6 sheet prints its top row upside down
    # (so a sheet cut in half reads the same way up), which looks like a bug in
    # a preview nobody is about to cut, and a three-round card fits A7 easily.
    goto(page, '/admin?page=card_design', wait=800)
    say(page, 'card_design', 900)
    a7 = page.locator('button:has-text("A7 landscape")').first
    if a7.count():
        click(page, a7, after=1800)

    goto(page, '/admin?page=print_materials')
    say(page, 'print', 800)
    card = page.locator('div:has(> h3:text-is("Player cards")) >> button').first
    click(page, card, after=300)
    page.wait_for_timeout(2500)          # the preview is a print page in an iframe
    frame = page.frame(name='modal-iframe')
    if frame:
        for offset in (240, 520):
            frame.evaluate(f'window.scrollTo({{top: {offset}, behavior: "smooth"}})')
            beat(page, 900)
    beat(page, 400)


def scene_scoring(page):
    goto(page, '/admin?page=scoring', wait=800)
    say(page, 'scoring', 1300)

    click(page, page.locator('#table_row_r1_t1 .show_hands'), after=300)
    page.wait_for_timeout(2500)          # the sheet is an iframe of its own
    say(page, 'sheet', 700)
    sheet = page.frame_locator('#modalDetails-iframe')
    # Three hands, entered the way a scorer would: value, winner, discarder
    # (blank = self-draw), then Tab to let the sheet recompute.
    for hand, points, by, frm in ((1, '18', '2', '4'), (2, '26', '3', ''),
                                  (3, '12', '1', '3')):
        type_into(page, sheet.locator(f'#pts_{hand}'), points, after=80)
        type_into(page, sheet.locator(f'#by_{hand}'), by, after=80)
        if frm:
            type_into(page, sheet.locator(f'#from_{hand}'), frm, after=80)
        page.keyboard.press('Tab')
        beat(page, 450)
    say(page, 'validate', 700)
    click(page, sheet.locator('#valid_17'), after=800)
    # Only three hands were entered, so validating asks about the blank rows
    # that follow — the sheet's own guard against a lost final draw.
    proceed = page.locator('button:has-text("Record as not played")')
    if proceed.count():
        click(page, proceed, after=1000)
    click(page, page.locator('div:has(> #modalDetails-iframe) button').last, after=900)


def scene_publish(page):
    """Recorded after the off-camera fill, because a round only publishes once
    every table in it is in."""
    goto(page, '/admin?page=scoring', wait=800)
    say(page, 'publish', 700)
    page.click('button[role=tab]:has-text("Round 1")')
    beat(page, 400)
    toggle = page.locator('.tab-pane[data-round-nb="1"] .publish-toggle').first
    if toggle.count() and toggle.is_enabled():
        if toggle.is_checked():          # a re-run: unpublish first, then republish
            click(page, toggle, after=1000)
        click(page, toggle, after=1400)
    goto(page, '/admin?page=publisher_overview', wait=800)
    say(page, 'overview', 1400)
    scroll(page, 400)
    beat(page, 400)

    goto(page, '/admin?page=welcome', wait=800)
    say(page, 'dashboard', 1600)


def scene_display(page):
    seed_screens()
    goto(page, '/admin?page=display', wait=800)
    say(page, 'display', 1100)
    page.evaluate("var d = document.getElementById('configure-screens');"
                  "if (d) d.open = true;")
    beat(page, 400)
    scroll(page, 500)
    select = page.locator('.select-output').first
    cursor_to(page, select)
    select.select_option('scores:totals')
    beat(page, 800)
    select.select_option('scores:detailed')
    beat(page, 600)

    say(page, 'previews', 400)
    click(page, page.locator('#toggle-previews'), after=200)
    page.wait_for_timeout(7000)          # each preview is a live screen loading
    scroll(page, 400)
    beat(page, 700)

    say(page, 'timer', 400)
    start = page.locator('button:has-text("Start timer")')
    if start.count():
        page.once('dialog', lambda d: d.accept())
        click(page, start, after=600)
        ok = page.locator('button:has-text("Start")').last
        if ok.count() and ok.is_visible():
            click(page, ok, after=300)
        beat(page, 2500)


def _screen(path, key, hold=3000):
    def run(page):
        goto(page, path, wait=300)
        page.wait_for_timeout(2500)      # screens fill themselves over the socket
        say(page, key, hold)
    return run


def scene_phone(page):
    goto(page, '/', wait=1000)
    say(page, 'phone', 1200)
    scores = page.locator('button:visible:has-text("Scores")').last
    if scores.count():
        click(page, scores, after=800)
    scroll(page, 700, steps=12)
    beat(page, 500)

    say(page, 'phone_player', 400)
    # :visible — the other tabs' panels are in the DOM too, just hidden.
    row = page.locator('a[href^="details_player_"]:visible').first
    if row.count():
        click(page, row, after=300)
        page.wait_for_timeout(1500)      # the modal fetches the player's hands
        scroll(page, 400)
        beat(page, 700)
        close = page.locator('button:visible:has-text("×")').last
        if close.count():
            click(page, close, after=500)

    seating = page.locator('button:visible:has-text("Seating")').last
    if seating.count():
        say(page, 'phone_seating', 300)
        click(page, seating, after=800)
        scroll(page, 600, steps=12)
        beat(page, 700)


def scene_ceremony(page):
    goto(page, '/admin?page=ceremony', wait=1000)
    say(page, 'ceremony', 1100)
    reveal = page.locator('button:has-text("Reveal next")').first
    for _ in range(4):
        if not (reveal.count() and reveal.is_enabled()):
            break
        click(page, reveal, after=1300)
    beat(page, 700)


SCENES = [
    ('login', scene_login, DESKTOP, False),
    ('setup', scene_setup, DESKTOP, True),
    ('print', scene_print, DESKTOP, True),
    ('scoring', scene_scoring, DESKTOP, True),
    ('publish', scene_publish, DESKTOP, True),
    ('display', scene_display, DESKTOP, True),
    ('screen_scores', _screen('/1', 'screen_scores'), DESKTOP, False),
    ('screen_counter', _screen('/2', 'screen_counter'), DESKTOP, False),
    ('screen_schedule', _screen('/3', 'screen_schedule'), DESKTOP, False),
    ('phone', scene_phone, PHONE, False),
    ('ceremony', scene_ceremony, DESKTOP, True),
    ('screen_podium', _screen('/1', 'podium', 6000), DESKTOP, False),
]
SCENE_ORDER = {name: i for i, (name, *_rest) in enumerate(SCENES)}


def run_fill(browser):
    print('== fill (not recorded) ==')
    try:
        stage_fill(browser)
    except Exception:
        print('  ! fill stopped early — later scenes may be short of data:')
        traceback.print_exc(limit=3)


def record(browser, name, fn, viewport, authed):
    print(f'== scene {name} ==')
    ctx = browser.new_context(
        viewport=viewport, record_video_dir=str(RAW), record_video_size=viewport,
        storage_state=str(state_path('anna.admin')) if authed else None)
    ctx.add_init_script(OVERLAY_JS)
    page = ctx.new_page()
    try:
        fn(page)
    except Exception:
        print(f'  ! {name} stopped early:')
        traceback.print_exc(limit=3)
    video = page.video
    ctx.close()
    dest = RAW / f'{SCENE_ORDER[name]:02d}-{name}.webm'
    dest.unlink(missing_ok=True)
    shutil.move(video.path(), dest)
    print(f'  ✓ {dest.name}')


# ---------------------------------------------------------------- encoding ----

def ffmpeg_bin():
    pixi = HERE / '.pixi/envs/default/bin/ffmpeg'
    if pixi.exists():
        return str(pixi)
    found = shutil.which('ffmpeg')
    if not found:
        sys.exit('ffmpeg not found — `cd scripts/screenshots && pixi install` '
                 '(it is in pixi.toml) or install it system-wide.')
    return found


def stage_render(make_gif=True):
    """Pad every segment onto one 1280x720 canvas and concatenate.

    Padding rather than cropping is what lets the portrait phone segment sit in
    the same film as the landscape ones, as a centred panel on the dark ground.
    """
    segments = sorted(RAW.glob('[0-9][0-9]-*.webm'))
    if not segments:
        sys.exit('no recorded segments in %s — run the scenes first' % RAW)
    w, h = CANVAS
    chains, labels = [], []
    for i, seg in enumerate(segments):
        chains.append(
            f'[{i}:v]trim=start={TRIM},setpts=PTS-STARTPTS,fps=25,'
            f'scale={w}:{h}:force_original_aspect_ratio=decrease,'
            f'pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x0f172a,setsar=1[v{i}]')
        labels.append(f'[v{i}]')
    graph = ';'.join(chains) + ';' + ''.join(labels) + \
        f'concat=n={len(segments)}:v=1:a=0[out]'

    OUT.mkdir(parents=True, exist_ok=True)
    suffix = '' if LANG == 'en' else f'-{LANG}'
    mp4 = OUT / f'demo{suffix}.mp4'
    ff = ffmpeg_bin()
    cmd = [ff, '-y', '-loglevel', 'error', '-stats']
    for seg in segments:
        cmd += ['-i', str(seg)]
    cmd += ['-filter_complex', graph, '-map', '[out]',
            '-c:v', 'libx264', '-preset', 'medium', '-crf', '23',
            '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(mp4)]
    print(f'== render: {len(segments)} segments -> {mp4.name} ==')
    subprocess.run(cmd, check=True)
    print(f'  ✓ {mp4} ({mp4.stat().st_size / 1e6:.1f} MB)')

    if not make_gif:
        return
    gif = OUT / f'demo{suffix}.gif'
    print(f'== render: {gif.name} ==')
    subprocess.run(
        [ff, '-y', '-loglevel', 'error', '-stats', '-i', str(mp4),
         '-filter_complex',
         'fps=8,scale=720:-1:flags=lanczos,split[a][b];'
         '[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=3',
         '-loop', '0', str(gif)], check=True)
    size = gif.stat().st_size / 1e6
    print(f'  ✓ {gif} ({size:.1f} MB)')
    if size > 10:
        print('  ! large for a README — link the mp4 instead, or cut scenes')


# ------------------------------------------------------------------- main ----

def main():
    global LANG
    args = sys.argv[1:]
    make_gif = '--no-gif' not in args
    keep = '--keep' in args
    if '--lang' in args:
        i = args.index('--lang')
        LANG = args[i + 1]
        del args[i:i + 2]
    args = [a for a in args if not a.startswith('--')]

    all_stages = ['reset', 'bootstrap'] + [n for n, *_ in SCENES] + ['fill', 'render']
    if args:
        unknown = [a for a in args if a not in all_stages]
        if unknown:
            sys.exit(f'unknown stage(s): {unknown}\nknown: {all_stages}')
        stages = args
    else:
        # `fill` has to happen after `scoring` (it would otherwise overwrite the
        # sheet the scene fills by hand) and before anything showing standings.
        stages = ['bootstrap', 'reset', 'login', 'setup', 'print', 'scoring',
                  'fill', 'publish', 'display', 'screen_scores', 'screen_counter',
                  'screen_schedule', 'phone', 'ceremony', 'screen_podium',
                  'render']

    RAW.mkdir(parents=True, exist_ok=True)
    if 'bootstrap' in stages:
        stage_bootstrap()
        enable_wal()
    scene_stages = [s for s in stages if s in SCENE_ORDER or s in ('fill', 'reset')]
    if scene_stages:
        if not keep and not args:
            for old in RAW.glob('*.webm'):
                old.unlink()
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            stage_auth(browser)
            if 'reset' in stages:
                stage_reset(browser)
            for name, fn, viewport, authed in SCENES:
                if name in stages:
                    record(browser, name, fn, viewport, authed)
                if name == 'scoring' and 'fill' in stages:
                    run_fill(browser)
            if 'fill' in stages and 'scoring' not in stages:
                run_fill(browser)
            browser.close()
    if 'render' in stages:
        stage_render(make_gif)


if __name__ == '__main__':
    main()
