"""What a tenant scans with: its API key, and its score-sheet template.

Score-sheet OCR is the app's only paid external call, and it used to run on one
platform-wide ``ANTHROPIC_API_KEY`` — so on a multi-tenant host every tenant's
scans landed on the operator's bill, with no attribution and no ceiling, from an
endpoint that is anonymous by design. Scanning is now strictly bring-your-own-key:
no key on the tenant, no scanning, and there is no code path that reaches a
credential the tenant's own row didn't supply.

A tenant also scans against **its own score sheet**, with no fallback to a
built-in one, for a different reason: a sheet nobody chose fails every photo
silently and forever, and the failure reads as bad photography rather than as
configuration. So "can this tournament scan?" means key *and* sheet.

The one rule this module exists to keep: **the gate on the request path and the
lookup in the worker must be the same function.** Two predicates — "does a row
exist" for the request, "decrypt and use" for the worker — drift, and every drift
is a player who took a photo, waited on a spinner, and got an error after the
money was already spent. So ``is_configured`` is literally
``resolve_setup(...).can_scan``, cached. Do not later "optimise" it into an EXISTS
query: that reintroduces the divergence for a row whose ciphertext no longer
decrypts.
"""
import logging

from django.core.cache import cache
from django.utils import timezone

from .secrets import make_codec

logger = logging.getLogger(__name__)

# Its own purpose-derived key, so scan ciphertext is not substitutable for the
# publish target's (and vice versa) even though both derive from SECRET_KEY.
encrypt, decrypt, decrypt_or_blank = make_codec(
    salt=b'mahj-scan-key',
    info=b'anthropic-api-key',
    purpose='scanning API key',
)

CACHE_KEY = 'scan_key_ok:{}'
TTL = 300          # matches TOURNAMENT_TTL; busted on write by signals anyway


class ScanSetup:
    """One tenant's scanning setup, as the worker needs it.

    `key` is plaintext and must never be cached, logged, put in a job payload or
    rendered. `template`/`bbox` are not secret, just per-tenant.

    Both halves are required and neither falls back. A missing key would spend
    somebody else's money; a missing sheet is worse in its own way, because
    guessing one fails every photo silently and permanently while telling the
    player their photography is at fault.
    """
    __slots__ = ('key', 'template', 'etag', 'bbox')

    def __init__(self, key='', template=None, etag='', bbox=None):
        self.key = key
        self.template = template
        self.etag = etag
        self.bbox = bbox

    @property
    def can_scan(self):
        """Everything a paid, aligned scan needs. The single predicate — see the
        module docstring on why the gate and the worker must not diverge."""
        return bool(self.key) and self.template is not None


def resolve_setup(subdomain):
    """This tenant's key + sheet template, in one query. Never cached.

    Uncached because it returns plaintext: putting an API key in the LRU Redis
    cache would seed a secret into a store nothing else treats as secret-bearing,
    and the template bytes are 100-300 KB of blob that has no business sitting
    beside the sessions. A scan job already costs a vision call plus an OpenCV
    alignment, so one indexed row read per job is free — and it buys immediate
    revocation.
    """
    if not subdomain:
        return ScanSetup()
    from .models import ScanConfig
    cfg = (ScanConfig.objects
           .filter(tenant__subdomain=subdomain)
           .order_by('id').first())
    if cfg is None:
        return ScanSetup()
    return ScanSetup(
        key=decrypt_or_blank(cfg.api_key_enc),
        template=bytes(cfg.template_img) if cfg.has_template else None,
        etag=cfg.template_etag if cfg.has_template else '',
        bbox=cfg.bbox if cfg.has_template else None,
    )


def resolve_key(subdomain):
    """The key this tenant scans with; '' means it cannot scan."""
    return resolve_setup(subdomain).key


def is_configured(subdomain):
    """Can this tenant scan at all — key *and* sheet? Cached for TTL, the boolean
    only, never the key.

    A cache miss does the DB read. It must never fail *open*: an unreachable
    cache returns None from cache.get, which is indistinguishable from "not
    cached", and answering True there would spend money we have no mandate for.
    That is the exact opposite of the upload rate limiter in views.scan, which
    fails open on purpose so a Redis blip can't stop scanning at the venue. The
    asymmetry is deliberate: one protects the venue, this one protects the bill.
    """
    if not subdomain:
        return False
    key = CACHE_KEY.format(subdomain)
    try:
        cached = cache.get(key)
    except Exception:
        cached = None
    # Ints, not bools: a cached False would be indistinguishable from a miss.
    if cached is not None:
        return bool(cached)
    ok = resolve_setup(subdomain).can_scan
    try:
        cache.set(key, 1 if ok else 0, TTL)
    except Exception:
        pass          # an unusable cache costs a query per request, nothing more
    return ok


def forget(subdomain):
    """Drop the cached availability for a tenant.

    Called from the ScanConfig signal, and by the worker the moment the API
    rejects a key — that closes the revocation loop within one job instead of
    five minutes of uploads accepted and then failed.
    """
    if subdomain:
        try:
            cache.delete(CACHE_KEY.format(subdomain))
        except Exception:
            pass


def stamp_error(subdomain, message):
    """Record why this tenant's last scan failed, for its admin page.

    An organiser is not watching the worker log. A revoked key, a rate-limited
    new account or a sheet that no photo matches are all invisible to them
    otherwise — this is the whole diagnosis path, so it must never raise into
    the worker loop that is trying to finish a job.
    """
    if not subdomain:
        return
    try:
        from .models import ScanConfig
        ScanConfig.objects.filter(tenant__subdomain=subdomain).update(
            last_error=message[:200], last_error_at=timezone.now())
    except Exception:
        logger.exception("could not record the scan error for %r", subdomain)
