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
