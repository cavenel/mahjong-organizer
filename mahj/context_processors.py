import logging

from django.conf import settings
from django.templatetags.static import static
from django.urls import reverse

from .views.helpers import (
    can_access_admin, get_tenant, get_tournament, has_role, is_tenant_admin,
    public_site_host, public_site_url,
)

logger = logging.getLogger(__name__)

# Each processor falls back to a harmless default rather than failing the whole
# page render — but silently. A bug here renders every page role-less / logo-less
# with no trace, which is nasty to debug at a venue, hence the logging.


def site_logo(request):
    """Expose `site_logo_url` to every template: the tenant's uploaded logo if set,
    otherwise the bundled static mcr_logo. The `?v=` query param cache-busts the
    served URL so projector screens pick up a changed logo. Keeps the fallback and
    cache-busting logic in one place so templates only reference {{ site_logo_url }}.
    """
    try:
        tournament = get_tournament(request)
    except Exception:
        logger.exception("site_logo context processor failed; using the default logo")
        tournament = None
    if tournament is not None and tournament.logo:
        return {"site_logo_url": f"{reverse('logo')}?v={tournament.logo_etag}"}
    return {"site_logo_url": static("images/mcr_logo.png")}


def public_site(request):
    """Expose the spectator-site URL advertised on projector screens and printed
    cards: `public_site_url` (with scheme, for QR/links) and `public_site_host`
    (bare host, for a compact caption). The tenant's configured public_url wins;
    otherwise it's the tenant's <subdomain>.<BASE_DOMAIN>."""
    try:
        tenant = get_tenant(request)
        subdomain = tenant.subdomain if tenant else ''
        tournament = get_tournament(request)
        public_url = tournament.public_url if tournament else ''
    except Exception:
        logger.exception("public_site context processor failed; advertising the base domain")
        subdomain = ''
        public_url = ''
    return {
        "public_site_url": public_site_url(subdomain, public_url),
        "public_site_host": public_site_host(subdomain, public_url),
        "base_domain": settings.BASE_DOMAIN,
    }


def role_flags(request):
    """Expose the current user's tenant-scoped role to every template, so the
    admin shell's nav/menus gate on tenant membership rather than the global
    Django ``is_staff`` flag. ``is_tenant_admin`` is the tier-2 "full admin over
    this tenant" token; the per-role flags already fold admin/superuser in (via
    has_role). ``is_superuser`` (platform ops) stays the Django flag, and
    templates keep using ``user.is_superuser``.
    """
    try:
        return {
            "is_tenant_admin": is_tenant_admin(request),
            "user_is_scorer": has_role(request, 'scorer'),
            "user_is_display_op": has_role(request, 'display_op'),
            "user_is_publisher": has_role(request, 'publisher'),
            "user_can_access_admin": can_access_admin(request),
        }
    except Exception:
        logger.exception("role_flags context processor failed; rendering the page role-less")
        return {
            "is_tenant_admin": False, "user_is_scorer": False,
            "user_is_display_op": False, "user_is_publisher": False,
            "user_can_access_admin": False,
        }
