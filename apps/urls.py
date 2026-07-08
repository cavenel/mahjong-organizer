from django.contrib import admin
from django.urls import path, include
from mahj import views


urlpatterns = [
    path('', views.desktop, name='home'),
    path('stats.xlsx', views.stats_xlsx, name='stats_xlsx'),
    path('admin', views.options),
    path('options', views.options, name='options'),
    path('admin_db/', admin.site.urls),
    path('accounts/', include('django.contrib.auth.urls')),

    # Display screens
    path('<int:screen_id>', views.index, name='index'),
    path('update_screen_view', views.update_screen_view, name='update_screen_view'),
    path('update_screen_name', views.update_screen_name, name='update_screen_name'),
    path('overview', views.overview, name='overview'),

    # Prize-giving ceremony
    path('ceremony_control', views.ceremony_control, name='ceremony_control'),
    path('ceremony_data', views.ceremony_data, name='ceremony_data'),

    # Admin actions
    path('admin_upload_from_template', views.admin_upload_from_template, name='admin_upload_from_template'),
    path('randomize', views.randomize, name='randomize'),
    path('admin_team_draw', views.admin_team_draw, name='admin_team_draw'),
    path('admin_team_draw_save', views.admin_team_draw_save, name='admin_team_draw_save'),
    path('player_editor_save', views.player_editor_save, name='player_editor_save'),
    path('logo', views.logo, name='logo'),
    path('update_logo', views.update_logo, name='update_logo'),
    path('publish_web', views.publish_web, name='publish_web'),
    path('publish_status', views.publish_status, name='publish_status'),
    path('counter_start', views.counter_start, name='counter_start'),

    # User management (staff only)
    path('user_reauth', views.user_reauth, name='user_reauth'),
    path('user_create', views.user_create, name='user_create'),
    path('user_update_roles', views.user_update_roles, name='user_update_roles'),
    path('user_generate_link', views.user_generate_link, name='user_generate_link'),
    path('user_revoke_links', views.user_revoke_links, name='user_revoke_links'),
    path('user_delete', views.user_delete, name='user_delete'),

    # Database restore (staff only)
    path('restore_pull', views.restore_pull, name='restore_pull'),
    path('restore_run', views.restore_run, name='restore_run'),
    path('restore_status', views.restore_status, name='restore_status'),

    # Score entry
    path('scores_per_hand_<int:round_nb>_<int:table_nb>', views.admin_scores_per_hand, name='admin_scores_per_hand'),
    path('update_position_penalty', views.update_position_penalty, name='update_position_penalty'),
    path('update_positions_bulk', views.update_positions_bulk, name='update_positions_bulk'),
    path('set_round_published', views.set_round_published, name='set_round_published'),
    path('create_hand_points', views.create_hand_points, name='create_hand_points'),
    path('update_hand_points', views.update_hand_points, name='update_hand_points'),
    path('validate_score_sheet', views.validate_score_sheet, name='validate_score_sheet'),
    path('clear_score_sheet', views.clear_score_sheet, name='clear_score_sheet'),

    # Desktop modal endpoints
    path('details_player_<int:id>', views.details_player, name='details_player'),
    path('detailed_scores_<int:round_nb>_<int:table_nb>', views.detailed_scores, name='detailed_scores'),
    # <path:> (not <str:>) so team names containing "/" (e.g. "France/Italy/Spain") match.
    path('details_team_<path:team_name>', views.details_team, name='details_team'),

    # Display-screen polling
    path('check_variables', views.check_variables, name='check_variables'),

    # Scan
    path('scan', views.scan_page, name='scan'),
    path('scan_<int:round_nb>_<int:table_nb>', views.scan_page, name='scan_prefill_page'),
    path('scan_status', views.scan_status, name='scan_status'),
    path('scan_positions', views.scan_positions, name='scan_positions'),
    path('scan_prefill', views.scan_prefill, name='scan_prefill'),

    # Print / export
    path('EMA_report.xlsx', views.admin_print_EMA),
    path('print_scores', views.print_scores, name='print_scores'),
    path('player_cards', views.player_cards, name='player_cards'),
    path('player_names', views.player_names, name='player_names'),
    path('team_names', views.team_names, name='team_names'),
    path('print_schedule', views.print_schedule, name='print_schedule'),
    path('table_posters', views.table_posters, name='table_posters'),
    path('cross_positions', views.cross_positions, name='cross_positions'),
]
