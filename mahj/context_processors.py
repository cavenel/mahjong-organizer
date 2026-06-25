from django.templatetags.static import static
from django.urls import reverse

from .views.helpers import get_variables


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
