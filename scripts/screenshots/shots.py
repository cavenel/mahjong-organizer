"""Recapture the admin-console guide's screenshots against a local instance.

Drives a throwaway single-process instance (sqlite + in-memory channels, tenant
'test') with one browser context per role, and writes element shots into
docs/admin-console/screenshots/ under the filenames guide.md references.
See README.md in this directory for the full recipe (environment, server,
browser libraries).

Usage:
    shots.py [stage ...]     stages, in their natural order:
        bootstrap   create the tenant and the four role users (idempotent)
        seed        import the click-through fixture, fill scores + sheets
        admin       every admin-visible page (the bulk of the shots)
        scorer      the scorer's sidebar
        login       the logged-out login page
        mobile      the phone scan page
        post        crop the tall captures to guide-friendly heights
Run with no arguments for all stages. Stages are re-runnable; `seed` re-imports
(wiping the test tenant) and can be re-run if sqlite lock contention from the
parallel fill drops a save or two.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
OUT = REPO / 'docs/admin-console/screenshots'
FIXTURE = REPO / 'docs/dev/clickthrough-fixtures/click-through-MCR-16p-3r.xlsx'
BASE = os.environ.get('SHOTS_BASE', 'http://127.0.0.1:8123')
PW = 'shots-pw'
PYTHON = str(REPO / '.venv/bin/python')

# The environment the server and every ORM helper below run under.
ENV = dict(os.environ,
           DJANGO_SETTINGS_MODULE='shots_settings', PYTHONPATH=str(HERE),
           LOCAL_TENANT='test',
           MAHJ_DB_PATH=os.environ.get('MAHJ_DB_PATH', str(HERE / '.local/shots.sqlite3')))

done, failed = [], []


def shoot(target, name, **kw):
    try:
        target.screenshot(path=str(OUT / name), **kw)
        done.append(name)
        print(f'  ✓ {name}')
    except Exception as e:
        failed.append(name)
        print(f'  ✗ {name}: {str(e)[:160]}')


def clear_reauth(page):
    """Get past the sudo gate on a page that has one, and wait until it is gone.

    The Continue button POSTs and then reloads, so a fixed pause races the reload:
    when it lost, the shot was of the password prompt rather than the page. Waiting
    for the field to disappear is what actually says the gate is behind us.
    """
    main = page.locator('#admin-maincol')
    field = main.locator('input[type=password]')
    if not field.count():
        return
    field.fill(PW)
    main.locator('button[type=submit]').first.click()
    page.wait_for_selector('#admin-maincol input[type=password]',
                           state='detached', timeout=15000)
    page.wait_for_load_state('networkidle')


def login(ctx, user):
    page = ctx.new_page()
    page.goto(f'{BASE}/admin')
    page.fill('#id_username', user)
    page.fill('#id_password', PW)
    page.click('button[type=submit]')
    page.wait_for_load_state('networkidle')
    return page


def orm(code):
    subprocess.run([PYTHON, '-c', 'import django; django.setup()\n' + code],
                   check=True, cwd=REPO, env=ENV)


def stage_bootstrap():
    """Migrate the throwaway DB and create the tenant + one user per role."""
    Path(ENV['MAHJ_DB_PATH']).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([PYTHON, 'manage.py', 'migrate', '--noinput'],
                   check=True, cwd=REPO, env=ENV)
    orm('''
from django.contrib.auth.models import User
from mahj.models import Tenant, Membership
tenant, _ = Tenant.objects.get_or_create(subdomain="test",
                                         defaults={"name": "Rehearsal tournament"})
for name, roles in (("anna.admin", {"is_tenant_admin": True}),
                    ("sam.scorer", {"is_scorer": True}),
                    ("pia.publisher", {"is_publisher": True}),
                    ("dana.display", {"is_display_op": True})):
    user, created = User.objects.get_or_create(username=name)
    if created:
        user.set_password("''' + PW + '''")
        user.save()
    Membership.objects.get_or_create(user=user, tenant=tenant, defaults=roles)
print("tenant + role users ready")
''')


def orm_fixups():
    """Rename the fixtures' deliberately-hostile names for presentable shots,
    align two round-3 sheets with their grids, and paint confidence tints."""
    orm('''
from mahj.models import Tenant, Player, Seat, Hand, ScoreSheet
t = Tenant.objects.get(subdomain="test")

for draw, first, last in ((1, "Robert", "Nystrom"), (2, "Aoife", "O'Brien")):
    p = Player.objects.get(tenant=t, draw_number=draw)
    p.first_name, p.last_name = first, last
    p.full_name = f"{first} {last}"
    p.short_name = first
    p.save()

def align(table_nb):
    """Grid MP/TP := the sheet's computed totals, so the cross-check is green."""
    totals = {1: 0, 2: 0, 3: 0, 4: 0}
    for h in Hand.objects.filter(tenant=t, round_nb=3, table_nb=table_nb):
        if not h.win_by:
            continue
        pts, by, frm = h.points, h.win_by, h.win_from
        for seat in (1, 2, 3, 4):
            if frm is None:
                totals[seat] += 3 * (8 + pts) if seat == by else -(8 + pts)
            elif seat == by:
                totals[seat] += 3 * 8 + pts
            elif seat == frm:
                totals[seat] += -(8 + pts)
            else:
                totals[seat] += -8
    mps = [totals[w] for w in (1, 2, 3, 4)]
    order = sorted(range(4), key=lambda i: -mps[i])
    tps = [0.0] * 4
    for rank, i in enumerate(order):
        tps[i] = [4.0, 2.0, 1.0, 0.0][rank]
    for w in (1, 2, 3, 4):
        Seat.objects.filter(tenant=t, round_nb=3, table_nb=table_nb, wind=w).update(
            minipoints=mps[w - 1], tablepoints=tps[w - 1], penalty=0)

align(1)
align(2)
# Table 2: an editable sheet with green cross-checks (shot 12).
ScoreSheet.objects.filter(tenant=t, round_nb=3, table_nb=2).update(validated=False)
# Table 1: an unvalidated sheet fresh from the scanner (shot 13) — pink tints.
ScoreSheet.objects.filter(tenant=t, round_nb=3, table_nb=1).update(validated=False)
for h in Hand.objects.filter(tenant=t, round_nb=3, table_nb=1).order_by("hand_nb"):
    h.confidence = {1: 0.3, 2: 0.55, 5: 0.3, 8: 0.7, 11: 0.55, 14: 0.3}.get(h.hand_nb, 1.0)
    h.save(update_fields=["confidence"])
print("fixups done")
''')


def stage_seed(ctx):
    print('== seed: import fixture, fill data ==')
    page = login(ctx, 'anna.admin')

    page.goto(f'{BASE}/admin?page=import_template')
    shoot(page.locator('#admin-maincol main > div').first, '40-import-template.png')
    page.set_input_files('input[name=myfile]', str(FIXTURE))
    page.click('form[action="admin_upload_from_template"] button[type=submit]')
    page.wait_for_timeout(800)
    # Re-imports over an existing tournament confirm through the styled dialog.
    confirm = page.locator('button:has-text("Erase and import")')
    if confirm.count():
        confirm.click()
    page.wait_for_load_state('networkidle', timeout=120000)
    page.wait_for_timeout(2000)

    page.goto(f'{BASE}/admin?page=scoring')
    page.wait_for_selector('.table_row', state='attached')
    page.click('text=Fill all rounds — scores')
    page.wait_for_function(
        "document.querySelectorAll('.publish-status.published').length >= 2",
        timeout=60000)
    page.click('text=Fill all rounds — score sheets')
    try:
        page.wait_for_function(
            "document.querySelectorAll('.valid-badge.active').length >= 12",
            timeout=60000)
    except Exception:
        # The 12 parallel 16-hand transactions can trip sqlite's lock on a slow
        # disk, dropping a validate or two; the hands themselves land. Finish
        # the validation marks via ORM — the toolbar would have set them all.
        orm('''
from mahj.models import Tenant, ScoreSheet
t = Tenant.objects.get(subdomain="test")
for rn in (1, 2, 3):
    for tn in (1, 2, 3, 4):
        ScoreSheet.objects.update_or_create(
            tenant=t, round_nb=rn, table_nb=tn, defaults={"validated": True})
print("sheet validation completed via ORM")
''')
    time.sleep(2)
    orm_fixups()
    print('seeded')


def stage_admin(ctx):
    print('== admin shots ==')
    page = login(ctx, 'anna.admin')

    # Dashboard content (00, bottom-trimmed in stage_post) and the whole shell —
    # sidebar beside the dashboard (03): a viewport shot, so the admin sidebar
    # reads in context instead of as a tall bare strip.
    page.goto(f'{BASE}/admin?page=welcome')
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(800)
    shoot(page.locator('#admin-maincol main'), '00-welcome-dashboard.png')
    shoot(page, '03-sidebar-staff.png')

    # Print modal (player cards), scrolled to the cards — leave via goto
    page.click('button:has-text("Print / Export")')
    page.click('button:has-text("Player cards")')
    page.wait_for_timeout(3000)
    frame = page.frame(name='modal-iframe')
    if frame:
        frame.evaluate('window.scrollTo(0, 380)')
        page.wait_for_timeout(500)
    shoot(page.locator('#modal-iframe').locator('xpath=ancestor::div[contains(@class,"relative")][1]'),
          '05-print-modal.png')

    # User management (behind the password re-check)
    page.goto(f'{BASE}/admin?page=users')
    page.wait_for_load_state('networkidle')
    clear_reauth(page)
    page.wait_for_timeout(500)
    shoot(page.locator('#admin-maincol main'), '02-assign-role.png')

    # Backup & restore — also behind the password re-check.
    page.goto(f'{BASE}/admin?page=backup')
    page.wait_for_load_state('networkidle')
    clear_reauth(page)
    page.wait_for_timeout(500)
    shoot(page.locator('#admin-maincol main > div').first, '41-backup-restore.png')

    def goto_scoring(round_nb=None):
        page.goto(f'{BASE}/admin?page=scoring')
        page.wait_for_selector('.table_row', state='attached')
        page.wait_for_timeout(800)
        if round_nb:
            page.click(f'button[role=tab]:has-text("Round {round_nb}")')
            page.wait_for_timeout(500)

    # Toolbar + round 3 grid shots (filled and empty rows side by side). The
    # test toolbar gets its own shot (41) and is hidden for the page shot (10),
    # which illustrates the Scoring page as it looks on a real tenant.
    goto_scoring(3)
    shoot(page.locator('div.border-dashed.border-amber-400'), '41-test-toolbar.png')
    page.evaluate("document.querySelector('div.border-dashed.border-amber-400').style.display = 'none'")
    shoot(page.locator('#admin-maincol main'), '10-scoring-page.png')
    page.evaluate("document.querySelector('div.border-dashed.border-amber-400').style.display = ''")
    shoot(page.locator('#table_row_r3_t1'), '11-filled-row.png')
    shoot(page.locator('.tab-pane[data-round-nb="3"] .flex.items-center.gap-3').first,
          '33-last-round-hint.png')

    # Score sheet modal, table 2 (green cross-check) + the QR side column
    page.locator('#table_row_r3_t2 .show_hands').click()
    page.wait_for_timeout(3000)
    shoot(page.locator('#modalDetails-iframe').locator('xpath=ancestor::div[contains(@class,"relative")][1]'),
          '12-score-sheet.png')
    # 14: a wide strip of the sheet's top rows with the QR beside them — the
    # bare .sheet-side column is mostly empty stretch. Clip in page coordinates
    # computed from the QR block's box (frame boxes are page-relative).
    frame = page.frame(name='modalDetails-iframe')
    if frame:
        qr = frame.locator('div.flex.flex-col.items-center', has=frame.locator('svg')).first
        box = qr.bounding_box()
        if box:
            left = max(0, box['x'] - 740)
            clip = {'x': left, 'y': max(0, box['y'] - 14),
                    'width': box['x'] + box['width'] + 18 - left,
                    'height': box['height'] + 34}
            try:
                page.screenshot(path=str(OUT / '14-scan-qr.png'), clip=clip)
                done.append('14-scan-qr.png')
                print('  ✓ 14-scan-qr.png')
            except Exception as e:
                failed.append('14-scan-qr.png')
                print(f'  ✗ 14-scan-qr.png: {str(e)[:160]}')

    # Score sheet modal, table 1 (confidence tints)
    goto_scoring(3)
    page.locator('#table_row_r3_t1 .show_hands').click()
    page.wait_for_timeout(3000)
    shoot(page.locator('#modalDetails-iframe').locator('xpath=ancestor::div[contains(@class,"relative")][1]'),
          '13-scan-confidence.png')

    # Publish shots
    goto_scoring(1)
    shoot(page.locator('.tab-pane[data-round-nb="1"]'), '31-published-round.png')
    goto_scoring(2)
    pane2 = page.locator('.tab-pane[data-round-nb="2"]')
    pane2.locator('.publish-toggle').click()   # unpublish (no confirm on this page)
    page.wait_for_timeout(1500)
    shoot(pane2.locator('.flex.items-center.gap-3').first, '30-publish-bar.png')
    pane2.locator('.publish-toggle').click()   # re-publish
    page.wait_for_timeout(1500)

    # Publisher overview — the intro + table card, not the whole (mostly empty) page
    page.goto(f'{BASE}/admin?page=publisher_overview')
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(800)
    shoot(page.locator('.po-wrap'), '32-publisher-overview.png')
    shoot(page.locator('.po-wrap'), '42-filled-data.png')

    # Display page: screens/views/modes are seeded via ORM (the UI keeps them in
    # collapsed <details> panels); open the panels before shooting.
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
ScreenMode.objects.create(tenant=t, name="Play", views=["scores:detailed", "counter", "schedule"])
ScreenMode.objects.create(tenant=t, name="Break", views=["welcome", "welcome", "schedule"])
print("screens ready")
''')
    page.goto(f'{BASE}/admin?page=display')
    page.wait_for_load_state('networkidle')
    page.evaluate("document.getElementById('configure-screens').open = true;"
                  "document.getElementById('display-settings').open = true;")
    page.wait_for_timeout(500)
    shoot(page.locator('.select-output').first.locator('xpath=ancestor::div[contains(@class,"rounded-xl")][1]'),
          '21-screen-card.png')
    shoot(page.locator('h2:has-text("Display modes")').locator('xpath=following-sibling::*[1] | ..').first,
          '22-display-modes.png')
    page.locator('#toggle-previews').click()
    page.wait_for_timeout(5000)
    shoot(page.locator('#previews-container'), '23-previews.png')
    start = page.locator('button:has-text("Start timer")')
    if start.count():
        page.once('dialog', lambda d: d.accept())
        start.click()
        page.wait_for_timeout(6000)
    shoot(page.locator('h2:has-text("Timer control")').locator('..'), '24-timer.png')
    shoot(page.locator('#display-settings'), '25-display-settings.png')
    page.evaluate("document.getElementById('configure-screens').open = true;")
    page.wait_for_timeout(300)
    shoot(page.locator('#admin-maincol main'), '20-display-page.png')

    # Ceremony console
    page.goto(f'{BASE}/admin?page=ceremony')
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(1500)
    shoot(page.locator('#admin-maincol main'), '26-ceremony-console.png')


def stage_scorer(ctx):
    print('== scorer shots ==')
    page = login(ctx, 'sam.scorer')
    page.wait_for_selector('#admin-sidebar')
    shoot(page.locator('#admin-sidebar'), '04-sidebar-scorer.png')


def stage_login(ctx):
    print('== login shot ==')
    page = ctx.new_page()
    page.goto(f'{BASE}/admin')
    page.wait_for_selector('#id_username')
    shoot(page.locator('form[action*="login"]').locator('xpath=ancestor::div[1]'), '01-login.png')


def stage_mobile(pw_browser):
    print('== mobile scan shot ==')
    ctx = pw_browser.new_context(viewport={'width': 390, 'height': 740},
                                 device_scale_factor=2)
    page = ctx.new_page()
    page.goto(f'{BASE}/scan_3_4')
    page.wait_for_timeout(2000)
    shoot(page, '15-scan-page.png', full_page=True)
    ctx.close()


def stage_post():
    """Crop the tall captures: the PDF caps image height, so a skyscraper shot
    renders unreadably small. Keep each page shot's informative top, and trim
    trailing background from element shots that stretch past their content."""
    from PIL import Image
    print('== post-crops ==')
    for name, keep in (('20-display-page.png', 2300),
                       ('26-ceremony-console.png', 1500),
                       ('04-sidebar-scorer.png', 470)):
        p = OUT / name
        im = Image.open(p)
        w, h = im.size
        if h > keep:
            im.crop((0, 0, w, keep)).save(p)
        print(f'  ✓ {name} -> {w}x{min(h, keep)}')
    # Page-content shots are taller than their content; trim rows matching the
    # page background (sampled bottom-left), leaving a small margin.
    for name in ('00-welcome-dashboard.png', '10-scoring-page.png',
                 '02-assign-role.png'):
        p = OUT / name
        im = Image.open(p).convert('RGB')
        w, h = im.size
        px = im.load()
        bg = px[2, h - 2]
        last = h - 1
        for y in range(h - 1, 0, -1):
            if any(abs(px[x, y][c] - bg[c]) > 8 for x in range(0, w, 8) for c in (0, 1, 2)):
                last = y
                break
        im.crop((0, 0, w, min(h, last + 40))).save(p)
        print(f'  ✓ {name} -> {w}x{min(h, last + 40)} (trimmed)')


def main():
    stages = sys.argv[1:] or ['bootstrap', 'seed', 'admin', 'scorer', 'login',
                              'mobile', 'post']
    if 'bootstrap' in stages:
        stage_bootstrap()
    browser_stages = [s for s in stages if s != 'bootstrap' and s != 'post']
    if browser_stages:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            def fresh():
                return browser.new_context(viewport={'width': 1150, 'height': 800},
                                           device_scale_factor=2)
            if 'seed' in stages:
                stage_seed(fresh())
            if 'admin' in stages:
                stage_admin(fresh())
            if 'scorer' in stages:
                stage_scorer(fresh())
            if 'login' in stages:
                stage_login(fresh())
            if 'mobile' in stages:
                stage_mobile(browser)
            browser.close()
    if 'post' in stages:
        stage_post()
    print(f'\n{len(done)} captured, {len(failed)} failed: {failed}')


if __name__ == '__main__':
    main()
