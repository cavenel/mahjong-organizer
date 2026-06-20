from django.contrib import admin
from django.urls import path, include
from mahj import views


urlpatterns = [
    path('', views.desktop, name='home'),
    path('admin', views.options),
    path('options', views.options, name='options'),
    path('admin_db/', admin.site.urls),
    path('accounts/', include('django.contrib.auth.urls')),

    # Display screens
    path('<int:screen_id>', views.index, name='index'),
    path('update_screen_view', views.update_screen_view, name='update_screen_view'),
    path('overview', views.overview, name='overview'),

    # Admin actions
    path('admin_upload_from_template', views.admin_upload_from_template, name='admin_upload_from_template'),
    path('randomize', views.randomize, name='randomize'),
    path('admin_team_draw', views.admin_team_draw, name='admin_team_draw'),
    path('admin_team_draw_save', views.admin_team_draw_save, name='admin_team_draw_save'),
    path('update_variables', views.update_variables, name='update_variables'),
    path('update_welcome', views.update_welcome, name='update_welcome'),
    path('welcome_options', views.welcome_options, name='welcome_options'),
    path('timer_options', views.timer_options, name='timer_options'),
    path('counter_start', views.counter_start, name='counter_start'),

    # Score entry
    path('scores_per_hand_<int:round_nb>_<int:table_nb>', views.admin_scores_per_hand, name='admin_scores_per_hand'),
    path('update_position_points', views.update_position_points, name='update_position_points'),
    path('update_positions_bulk', views.update_positions_bulk, name='update_positions_bulk'),
    path('set_round_published', views.set_round_published, name='set_round_published'),
    path('create_hand_points', views.create_hand_points, name='create_hand_points'),
    path('update_hand_points', views.update_hand_points, name='update_hand_points'),
    path('validate_score_sheet', views.validate_score_sheet, name='validate_score_sheet'),

    # Desktop modal endpoints
    path('details_player_<int:id>', views.details_player, name='details_player'),
    path('details_player_ema_<int:id>', views.details_player_ema, name='details_player_ema'),
    path('detailed_scores_<int:round_nb>_<int:table_nb>', views.detailed_scores, name='detailed_scores'),
    path('details_team_<str:team_name>', views.details_team, name='details_team'),

    # Display-screen polling
    path('check_page', views.check_page, name='check_page'),
    path('check_round', views.check_round, name='check_round'),
    path('check_final', views.check_final, name='check_final'),
    path('check_variables', views.check_variables, name='check_variables'),

    # Scan
    path('scan', views.scan_page, name='scan'),
    path('scan_<int:round_nb>_<int:table_nb>', views.scan_page, name='scan_prefill_page'),
    path('scan_positions', views.scan_positions, name='scan_positions'),
    path('scan_prefill', views.scan_prefill, name='scan_prefill'),

    # Print / export
    path('scores_per_player.<str:ext>', views.scores_per_player, name='scores_per_player'),
    path('EMA_report.xlsx', views.admin_print_EMA),
    path('print_scores', views.print_scores, name='print_scores'),
    path('player_cards', views.player_cards, name='player_cards'),
    path('player_names', views.player_names, name='player_names'),
    path('print_schedule', views.print_schedule, name='print_schedule'),
    path('table_posters', views.table_posters, name='table_posters'),
    path('cross_positions', views.cross_positions, name='cross_positions'),
]
