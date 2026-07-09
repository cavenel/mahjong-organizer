from django.conf import settings
from django.templatetags.static import static
from django.urls import reverse

from .views.helpers import get_tenant, get_variables, public_site_host, public_site_url


def site_logo(request):
    """Expose `site_logo_url` to every template: the tenant's uploaded logo if set,
    otherwise the bundled static mcr_logo. The `?v=` query param cache-busts the
    served URL so projector screens pick up a changed logo. Keeps the fallback and
    cache-busting logic in one place so templates only reference {{ site_logo_url }}.
    """
    try:
        variables = get_variables(request)
    except Exception:
        variables = None
    if variables is not None and variables.logo:
        return {"site_logo_url": f"{reverse('logo')}?v={variables.logo_etag}"}
    return {"site_logo_url": static("images/mcr_logo.png")}


def public_site(request):
    """Expose the spectator-site URL advertised on projector screens and printed
    cards: `public_site_url` (with scheme, for QR/links) and `public_site_host`
    (bare host, for a compact caption). The tenant's configured public_url wins;
    otherwise it's the tenant's <subdomain>.<BASE_DOMAIN>."""
    try:
        tenant = get_tenant(request)
        subdomain = tenant.subdomain if tenant else ''
        variables = get_variables(request)
        public_url = variables.public_url if variables else ''
    except Exception:
        subdomain = ''
        public_url = ''
    return {
        "public_site_url": public_site_url(subdomain, public_url),
        "public_site_host": public_site_host(subdomain, public_url),
        "base_domain": settings.BASE_DOMAIN,
    }
