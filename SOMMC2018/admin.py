from django.contrib import admin

from . import models

admin.site.register(models.Player)
admin.site.register(models.Position)
admin.site.register(models.Screen)
admin.site.register(models.Variable)
admin.site.register(models.Player_data)
admin.site.register(models.Hand)
admin.site.register(models.Schedule)
admin.site.register(models.ScreenMode)
admin.site.register(models.Tenant)