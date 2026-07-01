"""Staff-only database restore console.

Lists the local Postgres dumps (written by ``backup_db.sh`` / pulled by
``pull_backups.sh``), pulls fresh dumps from the off-host remote, and enqueues a
restore of a chosen dump. The destructive work runs in ``restore_worker`` (a
request can't tear down its own container); these endpoints only enqueue + poll.

Gated exactly like User management — ``@staff_and_reauthed`` (staff + a recent
password re-confirmation) — plus a typed-confirmation of the DB name on restore,
mirroring the typed-name prompt in ``scripts/restore_db.sh``.
"""
import datetime
import os

from django.conf import settings
from django.http import JsonResponse

from .. import restore_queue
from .user_admin import staff_and_reauthed

# Newest N dumps shown per source before the "show older" expander — a box holds
# hundreds (one dump every few minutes over multi-day retention), so we never
# render the full list.
RECENT_PER_SOURCE = 20


def _human_size(n):
    size = float(n)
    for unit in ('B', 'K', 'M', 'G'):
        if size < 1024 or unit == 'G':
            return f"{size:.0f}{unit}" if unit == 'B' else f"{size:.1f}{unit}"
        size /= 1024


def _ago(when):
    """Compact relative age of a UTC datetime, so freshness reads the same
    regardless of the viewer's timezone (the stamp itself is UTC)."""
    if when is None:
        return ''
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    secs = (now - when).total_seconds()
    if secs < 90:
        return 'just now'
    if secs < 5400:
        return f"{round(secs / 60)}m ago"
    if secs < 172800:
        return f"{round(secs / 3600)}h ago"
    return f"{round(secs / 86400)}d ago"


def _parse_name(filename):
    """``mahj_<source>_<stamp>.dump`` → (source, datetime|None). ``source`` may
    itself contain underscores (it's a filename-safe token), so split off the
    trailing stamp, not the first underscore."""
    stem = filename[len('mahj_'):-len('.dump')]
    source, _, stamp = stem.rpartition('_')
    when = None
    try:
        when = datetime.datetime.strptime(stamp, '%Y%m%dT%H%M%SZ')
    except ValueError:
        pass
    return (source or '?'), when


def list_backups():
    """Group local dumps by origin (cloud / venue / …), newest first.

    Returns a list of groups; each has the recent slice plus a total count and
    time span, so the page can surface the latest (and the latest *venue* dump on
    failback) in one click without shipping an 800-row table."""
    backups = {}
    if restore_queue.BACKUPS_DIR.is_dir():
        for path in restore_queue.BACKUPS_DIR.glob('mahj_*.dump'):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            source, when = _parse_name(path.name)
            backups.setdefault(source, []).append({
                'name': path.name,
                'source': source,
                'size_h': _human_size(path.stat().st_size),
                # Stamp is UTC; label it as such and add a tz-independent age so
                # "is this current?" doesn't depend on the viewer's clock.
                'when': (when.strftime('%d %b %H:%M') + ' UTC') if when else '?',
                'ago': _ago(when),
                # Order by the embedded UTC stamp (the true backup time, and what
                # rsync -a preserves as mtime anyway); fall back to mtime if the
                # name doesn't parse.
                'sort': when.timestamp() if when else mtime,
            })

    groups = []
    for source, dumps in backups.items():
        dumps.sort(key=lambda d: d['sort'], reverse=True)
        spans = [d['when'] for d in dumps if d['when'] != '?']
        groups.append({
            'source': source,
            'count': len(dumps),
            'newest': dumps[0]['name'],
            'span': f"{spans[-1]} → {spans[0]}" if len(spans) > 1 else (spans[0] if spans else ''),
            'recent': dumps[:RECENT_PER_SOURCE],
            'has_more': len(dumps) > RECENT_PER_SOURCE,
        })
    # Surface 'venue' first when present (the failback case), else newest activity.
    groups.sort(key=lambda g: (g['source'] != 'venue', -g['recent'][0]['sort']))
    return groups


def _validate_dump(name):
    """Basename only (no path traversal), present in the backups dir, carrying the
    pg_dump custom-format magic header. Returns the Path or None."""
    if not name or name != os.path.basename(name) or not name.endswith('.dump'):
        return None
    path = restore_queue.BACKUPS_DIR / name
    if not path.is_file():
        return None
    try:
        with open(path, 'rb') as fh:
            if fh.read(5) != b'PGDMP':
                return None
    except OSError:
        return None
    return path


@staff_and_reauthed
def restore_pull(request):
    """Enqueue a pull of the off-host dumps down into the local backups dir."""
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    job_id = restore_queue.new_job_id()
    restore_queue.enqueue({'job_id': job_id, 'action': 'pull'})
    return JsonResponse({'status': 'ok', 'job_id': job_id})


@staff_and_reauthed
def restore_run(request):
    """Enqueue a restore of a chosen dump — gated by a typed DB-name confirmation."""
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)
    import json
    try:
        data = json.loads(request.body) if request.body else {}
    except ValueError:
        return JsonResponse({'status': 'bad_request'}, status=400)

    db_name = settings.DATABASES['default']['NAME']
    if (data.get('confirm') or '') != db_name:
        return JsonResponse({'status': 'error',
                             'error': f"Type the database name ('{db_name}') to confirm."},
                            status=400)

    name = data.get('dump') or ''
    if _validate_dump(name) is None:
        return JsonResponse({'status': 'error', 'error': 'Unknown or invalid dump.'},
                            status=400)

    job_id = restore_queue.new_job_id()
    restore_queue.enqueue({'job_id': job_id, 'action': 'restore', 'dump': name})
    return JsonResponse({'status': 'ok', 'job_id': job_id})


@staff_and_reauthed
def restore_status(request):
    """Poll a queued restore/pull job."""
    job_id = request.GET.get('job_id', '')
    if not job_id:
        return JsonResponse({'status': 'error', 'error': 'job_id required'}, status=400)
    result = restore_queue.get_result(job_id)
    if result is None:
        return JsonResponse({'status': 'expired',
                             'error': 'Job timed out or was lost.'})
    return JsonResponse(result)
