"""The Django admin site, narrowed to the platform operator.

``/admin_db/`` is mounted on every tenant subdomain and its registered models are
unscoped — a Player changelist there lists every tournament's competitors. Django's
default gate is ``is_active and is_staff``, which is one flag wider than this
deployment ever intends: ``docs/dev/access-control.md`` reserves ``is_staff`` for
the admin site alone and states that no access decision may key on it, because a
tenant admin is a ``Membership`` row and nothing else.

Requiring ``is_superuser`` is strictly narrower and costs the operator nothing —
``createsuperuser`` sets both flags — while making the staff flag grant nothing on
its own. Wired in through ``MahjAdminConfig.default_site`` (the documented hook)
rather than by reassigning ``admin.site``, so there is one admin site and
``@admin.register`` keeps working unchanged.
"""
from django.contrib.admin import AdminSite
from django.contrib.admin.apps import AdminConfig


class SuperuserAdminSite(AdminSite):
    def has_permission(self, request):
        return request.user.is_active and request.user.is_superuser


class MahjAdminConfig(AdminConfig):
    default_site = 'mahj.admin_site.SuperuserAdminSite'
