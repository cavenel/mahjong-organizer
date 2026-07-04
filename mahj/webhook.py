"""Optional webhook: POST leaderboard JSON to an external URL on publish/unpublish.

Configured via the WEBHOOK_URL environment variable. If unset, no webhook fires.
An optional WEBHOOK_SECRET can be set to authenticate requests (sent as
X-Webhook-Secret header).

WEBHOOK_TENANT restricts the webhook to a single tenant: only publishes from
that tenant's subdomain are sent. This is important because every tenant shares
the same WEBHOOK_URL — without this gate a test tenant's publishes would post to
the live site. If WEBHOOK_TENANT is unset, every tenant fires (legacy). Holds a
subdomain, mirroring LOCAL_TENANT.
"""
import logging
import os
import threading

import requests

logger = logging.getLogger(__name__)

WEBHOOK_URL = os.environ.get('WEBHOOK_URL', '')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', '')
WEBHOOK_TENANT = os.environ.get('WEBHOOK_TENANT', '')


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
        from .scoring import _assign_ranks, _standings_rank_key, _standings_sort_key
        team_rows = sorted(by_team.values(), key=_standings_sort_key(variables))
        # Tied teams share a position, mirroring the players: level on both TP and
        # MP under MCR, or level on MP alone under the MP-ranked rules.
        _assign_ranks(team_rows, _standings_rank_key(variables), field='pos')
        for tr in team_rows:
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


def fire_webhook(payload, subdomain=None):
    """Send payload to the configured webhook URL in a background thread.

    Does nothing if WEBHOOK_URL is not set, or if WEBHOOK_TENANT is set and
    `subdomain` (the publishing tenant) does not match it — so only the live
    tournament's tenant reaches the webhook, not test tenants.
    """
    if not WEBHOOK_URL:
        return

    if WEBHOOK_TENANT and subdomain != WEBHOOK_TENANT:
        logger.info(
            'Webhook skipped: tenant %r is not the configured WEBHOOK_TENANT %r',
            subdomain, WEBHOOK_TENANT)
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
