from urllib.parse import urlencode

from django.http import HttpResponseRedirect
from django.urls import reverse
from django.views.csrf import csrf_failure as default_csrf_failure


def csrf_failure(request, reason="", template_name="403_csrf.html"):
    """Project-wide CSRF-failure handler (wired via settings.CSRF_FAILURE_VIEW).

    A standalone home-screen app (Android) often *resumes* a stale login page
    rather than refetching it. Django rotates the CSRF secret on every
    successful login, so that stale form's one-time token no longer matches the
    (persistent, 1-year) csrftoken cookie — producing a dead-end 403 the moment
    the user signs in. For the login POST specifically, bounce them to a fresh
    login GET, which re-renders a matching token and re-sets the cookie, with an
    `expired` flag the template turns into a friendly notice. The GET can't fail
    CSRF, so there is no redirect loop.

    Every other path keeps Django's default 403, so genuine CSRF failures
    elsewhere are never masked.
    """
    login_path = reverse("login")
    if request.method == "POST" and request.path == login_path:
        params = {"expired": "1"}
        nxt = request.GET.get("next")
        if nxt:
            params["next"] = nxt
        return HttpResponseRedirect(f"{login_path}?{urlencode(params)}")
    return default_csrf_failure(request, reason=reason, template_name=template_name)
