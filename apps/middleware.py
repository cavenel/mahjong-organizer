from django.http import JsonResponse

from mahj.views.helpers import FieldError


class FieldErrorMiddleware:
    """Render a :class:`mahj.views.helpers.FieldError` as a JSON 400 naming the field.

    Field validation is not an exceptional condition — a scorer mistyping a cell is
    an expected outcome of a human typing — so the coercion helpers raise this
    instead of ``BadRequest``. That matters twice over: Django logs a ``BadRequest``
    with a full traceback (a stack trace per typo) and renders a generic 400 page
    that drops the message, leaving the score grid with a red pip and nothing to
    tell the scorer which cell is wrong.

    Middleware rather than a view decorator so there is nothing to forget: a view
    using the helpers without the decorator would 500. ``BadRequest`` is left alone
    — a body that isn't a JSON object really is a malformed request and owes no
    friendlier answer.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if isinstance(exception, FieldError):
            return JsonResponse(
                {'status': 'bad_request', 'field': exception.field,
                 'error': f'{exception.field} {exception.message}'},
                status=400)
        return None


class AuthCookieMiddleware:
    """Sets a non-HttpOnly `auth=1` cookie when the session is authenticated.

    nginx reads this cookie to decide whether to bypass its `/` microcache:
    anonymous viewers (no cookie) get cached responses; logged-in staff/scorers
    always see live data. Reading the session is cheap — anonymous requests have
    no session cookie so the (DB) session store is never queried, and only the
    <=20 staff sessions incur an indexed lookup; we never touch request.user here.
    """

    COOKIE = 'auth'

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        is_auth = bool(getattr(request, 'session', None) and request.session.get('_auth_user_id'))
        has_cookie = request.COOKIES.get(self.COOKIE) == '1'
        if is_auth and not has_cookie:
            response.set_cookie(
                self.COOKIE, '1',
                max_age=60 * 60 * 24 * 14,
                # Secure follows the request's own scheme. Hardcoding it meant the
                # cookie was silently dropped over plain HTTP — which is how the
                # standalone build and dev always run — so nginx's cache-bypass
                # signal never arrived and logged-in staff could be served a cached
                # anonymous page. It carries no secret; the session cookie is what
                # authenticates, and that has its own Secure flag in prod.
                secure=request.is_secure(), httponly=False, samesite='Lax',
            )
        elif not is_auth and has_cookie:
            response.delete_cookie(self.COOKIE)
        return response
