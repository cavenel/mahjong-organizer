"""Optional webhook: POST leaderboard JSON to an external URL on publish/unpublish.

Configured via the WEBHOOK_URL environment variable. If unset, no webhook fires.
An optional WEBHOOK_SECRET can be set to authenticate requests (sent as
X-Webhook-Secret header).
"""
import logging
import os
import threading

import requests

logger = logging.getLogger(__name__)

WEBHOOK_URL = os.environ.get('WEBHOOK_URL', '')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', '')


def leaderboard_payload(event, standings, variables, round_nb=None):
    """Build a leaderboard webhook payload from player standings.

    Aggregates per-team standings the same way for every event so consumers see
    a consistent shape. `round_nb` defaults to the last round.
    """
    uses_teams = any(s.get('team') for s in standings)
    team_rows = []
    if uses_teams:
        by_team = {}
        for s in standings:
            t = s.get('team') or ''
            if not t:
                continue
            slot = by_team.setdefault(t, {
                'team': t,
                'flag': '',
                '_flags': set(),
                'total': {'tp': 0.0, 'mp': 0},
                'scores': [{'tp': None, 'mp': None} for _ in range(variables.nb_rounds)],
            })
            slot['_flags'].add(s.get('flag') or '')
            slot['total']['tp'] += s['total'].get('tp') or 0
            slot['total']['mp'] += s['total'].get('mp') or 0
            for r_idx, sc in enumerate(s.get('scores', [])):
                if r_idx < len(slot['scores']) and sc.get('tp') is not None:
                    rslot = slot['scores'][r_idx]
                    rslot['tp'] = (rslot['tp'] or 0) + sc['tp']
                    rslot['mp'] = (rslot['mp'] or 0) + (sc.get('mp') or 0)
        sort_key = (lambda x: -x['total']['tp']) if variables.rules == 'MCR' else (lambda x: -x['total']['mp'])
        team_rows = sorted(by_team.values(), key=sort_key)
        for i, tr in enumerate(team_rows, 1):
            tr['pos'] = i
            flags = tr.pop('_flags')
            tr['flag'] = next(iter(flags)) if len(flags) == 1 else ''

    return {
        'event': event,
        'round_nb': variables.nb_rounds if round_nb is None else round_nb,
        'rules': variables.rules,
        'nb_rounds': variables.nb_rounds,
        'standings': standings,
        'team_standings': team_rows if uses_teams else None,
    }


def fire_webhook(payload):
    """Send payload to the configured webhook URL in a background thread.

    Does nothing if WEBHOOK_URL is not set.
    """
    if not WEBHOOK_URL:
        return

    def _send():
        headers = {'Content-Type': 'application/json'}
        if WEBHOOK_SECRET:
            headers['X-Webhook-Secret'] = WEBHOOK_SECRET
        try:
            resp = requests.post(WEBHOOK_URL, json=payload, headers=headers, timeout=10)
            if resp.status_code >= 400:
                logger.warning('Webhook POST to %s returned %s', WEBHOOK_URL, resp.status_code)
        except Exception as e:
            logger.warning('Webhook POST to %s failed: %s', WEBHOOK_URL, e)

    threading.Thread(target=_send, daemon=True).start()
