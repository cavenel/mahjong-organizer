"""Render the public spectator page (desktop + detail modals) to static files.

This reuses the *live* Django views and templates: we call the same view
functions a browser would hit, as an anonymous user, and write their HTML to
files a dumb web host can serve. There is no separate frontend.

Because the render is anonymous, the views apply their normal reveal masking
(withheld / unpublished rounds stay hidden), so a static snapshot can never leak
pre-ceremony data. The one dynamic dependency the static site can't satisfy — the
live leaderboard WebSocket — is swapped for `static_poll.js`, which polls the
`version.json` we emit here and surfaces the page's existing "Refresh" prompt when
a newer export is uploaded.
"""
import hashlib
import json
import re
import shutil
import urllib.parse
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.templatetags.static import static as static_url
from django.test import RequestFactory

from ..models import Player, Seat, Tenant, TournamentSettings
from ..signals import leaderboard_gen
from ..views import public, public_modals

# Top-level static/ subtrees the public spectator page never loads — excluded
# from the export (see _copy_static). Admin-console assets, doc PDFs, and the
# projector gong sounds aren't referenced by the public pages.
_SKIP_STATIC_DIRS = {'admin', 'docs', 'sounds'}


def _make_request(subdomain, tenant):
    """An anonymous GET bound to `tenant`.

    We attach `_tenant` directly so get_tenant() short-circuits — bypassing host
    parsing and the DEBUG/LOCAL_TENANT logic, which don't apply to an offline
    render. AnonymousUser → is_staff/is_authenticated are False, so the views take
    the public (reveal-masked) path.
    """
    request = RequestFactory().get('/', HTTP_HOST=f'{subdomain}.mahj.ovh')
    request.user = AnonymousUser()
    request._tenant = tenant
    # Signals the desktop view to drop the auth menu (no login on a static host)
    # and skip the shared anon HTML cache.
    request._static_export = True
    return request


def _team_slug(team_name):
    """Stable ASCII filename for a team modal.

    Team names are free-form (spaces / unicode), which makes them fragile as URL
    path segments and filenames. Hash them so the exported filename and the
    rewritten href always agree regardless of host URL-decoding. Mirrors the
    hashing details_team() already uses for its cache key.
    """
    return hashlib.md5(team_name.encode('utf-8')).hexdigest()


def _rewrite_html(html, logo_replacement, poll_url):
    """Make one rendered page servable from a dumb static host.

    The desktop opens modals by fetching their relative href into an iframe (and
    the same links appear inside the modals), so every modal link must resolve to
    an exported `.html` file.
    """
    # Literal player / detailed-score links → add the .html the static file has.
    html = re.sub(
        r'(href=")(details_player_\d+|detailed_scores_\d+_\d+)(")',
        r'\1\2.html\3', html)

    # Team links carry a url-encoded, free-form name; swap it for the md5 slug the
    # exported file is named with, and add .html.
    def _team_sub(m):
        team = urllib.parse.unquote(m.group(1))
        return f'href="details_team_{_team_slug(team)}.html"'
    html = re.sub(r'href="details_team_([^"]*)"', _team_sub, html)

    # Alpine hrefs built client-side at runtime → append .html to the expression.
    html = html.replace(
        ":href=\"'detailed_scores_' + entry.round_nb + '_' + entry.table_nb\"",
        ":href=\"'detailed_scores_' + entry.round_nb + '_' + entry.table_nb + '.html'\"")
    html = html.replace(
        ":href=\"'details_player_' + seat.player.id\"",
        ":href=\"'details_player_' + seat.player.id + '.html'\"")

    # Live WebSocket client → the static poller (no /ws/ endpoint on a dumb host).
    html = re.sub(r'/static/js/display_socket[^"\']*\.js', poll_url, html)

    # DB-served tenant logo (/logo?v=…) → the exported logo.png. No-op when the
    # tenant uses the bundled static mcr_logo fallback.
    if logo_replacement:
        html = re.sub(r'/logo\?v=[^"\'&\s]*', logo_replacement, html)

    # Make static URLs relative (/static/… → static/…) so the site can be hosted
    # in a subfolder, not just at the domain root. Everything else the page loads
    # (modals, logo.png, version.json) is already relative and lives in the same
    # flat dir, so a relative static/ path resolves the same from index.html and
    # from any modal opened in the iframe.
    html = re.sub(r'''(=|\()(["']?)/static/''', r'\1\2static/', html)

    return html


def _copy_static(out):
    """Copy the static files the public page needs into the export.

    Skips assets the spectator page never loads so the export — and the SFTP
    upload — isn't dominated by hundreds of unused files: the Django admin
    console assets, the doc PDFs, the projector gong sounds, and WhiteNoise's
    pre-compressed .gz/.br variants (a plain host serves the normal file). This
    cuts the file count ~4x, which is what makes the upload slow (SFTP does a
    round-trip per file).
    """
    static_root = getattr(settings, 'STATIC_ROOT', None)
    if not (static_root and Path(static_root).is_dir() and any(Path(static_root).iterdir())):
        raise RuntimeError(
            'STATIC_ROOT is empty — run `manage.py collectstatic` before exporting.')
    root = Path(static_root)

    def _ignore(src, names):
        skip = {n for n in names if n.endswith('.gz') or n.endswith('.br')}
        if Path(src) == root:  # prune whole unused trees at the top level only
            skip |= {n for n in names if n in _SKIP_STATIC_DIRS}
        return skip

    dest = out / 'static'
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(root, dest, ignore=_ignore)


def export_public(subdomain, out_dir, copy_static=True):
    """Render the public site for `subdomain` into `out_dir`. Returns the Path.

    Everything is anonymous and read-only; nothing is written to the database.
    `copy_static=False` skips copying STATIC_ROOT (used by tests, which don't run
    collectstatic).
    """
    tenant = Tenant.objects.get(subdomain=subdomain)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    poll_url = static_url('js/static_poll.js')

    # Tenant logo: dump the DB BLOB to a file and repoint the HTML at it.
    variables = TournamentSettings.objects.filter(tenant=tenant).first()
    logo_replacement = ''
    if variables and variables.logo:
        (out / 'logo.png').write_bytes(bytes(variables.logo))
        logo_replacement = 'logo.png'

    def render(view_fn, *args):
        resp = view_fn(_make_request(subdomain, tenant), *args)
        return _rewrite_html(resp.content.decode('utf-8'), logo_replacement, poll_url)

    def write(name, content):
        (out / name).write_text(content, encoding='utf-8')

    # The spectator landing page.
    write('index.html', render(public.desktop))

    # Player detail modals.
    for pid in Player.objects.filter(tenant=tenant).values_list('id', flat=True):
        write(f'details_player_{pid}.html', render(public_modals.details_player, pid))

    # Team detail modals (distinct non-empty team names).
    teams = (Player.objects.filter(tenant=tenant)
             .exclude(team='').values_list('team', flat=True).distinct())
    for team in teams:
        write(f'details_team_{_team_slug(team)}.html',
              render(public_modals.details_team, team))

    # Per-table hand-by-hand modals, for every (round, table) that exists.
    pairs = (Seat.objects.filter(tenant=tenant)
             .values_list('round_nb', 'table_nb').distinct())
    for round_nb, table_nb in pairs:
        write(f'detailed_scores_{round_nb}_{table_nb}.html',
              render(public_modals.detailed_scores, round_nb, table_nb))

    # The stats tab's "Download stats" button links here. stats_xlsx returns a
    # binary workbook (not HTML), so it bypasses render()/write(); the anonymous
    # request gives it the same reveal-masked public data as every other page.
    stats_resp = public.stats_xlsx(_make_request(subdomain, tenant))
    (out / 'stats.xlsx').write_bytes(stats_resp.content)

    if copy_static:
        _copy_static(out)

    # version.json drives the client-side refresh prompt; write it LAST so a
    # client never sees a new version before the files it points at exist.
    write('version.json', json.dumps({'version': leaderboard_gen(subdomain)}))

    return out
