from django.contrib import admin

from . import models

admin.site.register(models.Player)
admin.site.register(models.Seat)
admin.site.register(models.Screen)
admin.site.register(models.TournamentSettings)
admin.site.register(models.ScoreSheet)
admin.site.register(models.Hand)
admin.site.register(models.Schedule)
admin.site.register(models.ScreenMode)
admin.site.register(models.Tenant)


@admin.register(models.Membership)
class MembershipAdmin(admin.ModelAdmin):
    """Superuser surface for per-tenant access: create tenants (above) then grant
    users their roles here. The bespoke in-app console is the day-to-day tool;
    this stays as the platform-operator fallback."""
    list_display = ('user', 'tenant', 'is_tenant_admin', 'is_scorer', 'is_display_op', 'is_publisher')
    list_filter = ('tenant', 'is_tenant_admin', 'is_scorer', 'is_display_op', 'is_publisher')
    search_fields = ('user__username', 'tenant__subdomain', 'tenant__name')
    list_select_related = ('user', 'tenant')