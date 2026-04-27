class AuthCookieMiddleware:
    """Sets a non-HttpOnly `auth=1` cookie when the session is authenticated.

    nginx reads this cookie to decide whether to bypass its `/` microcache:
    anonymous viewers (no cookie) get cached responses; logged-in staff/scorers
    always see live data. Reading session is cheap (Redis-backed); we never
    touch request.user here.
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
                secure=True, httponly=False, samesite='Lax',
            )
        elif not is_auth and has_cookie:
            response.delete_cookie(self.COOKIE)
        return response
