"""Re-export every view function so ``from mahj import views`` + ``views.<name>`` continues to work."""

from .helpers import (
    PositionForm,
    get_domain,
    get_podium,
    can_access_admin,
    get_tenant,
    get_variables,
    is_display_op,
    is_publisher,
    is_scorer,
    is_scorer_or_display_op,
    player_statistics,
)
from .scoring import (
    player_rounds_json,
    scores_per_player_json,
    scores_per_table_json,
    stat_all_rounds,
    stat_rounds,
    table_stats,
    table_stats_rounds,
    tournament_seating,
)
from .score_entry import (
    admin_scores_per_hand,
    clear_score_sheet,
    create_hand_points,
    set_round_published,
    update_hand_points,
    validate_score_sheet,
    update_position_penalty,
    update_positions_bulk,
)
from .ceremony import ceremony_control, ceremony_data
from .public import desktop, stats_xlsx
from .public_modals import (
    detailed_scores,
    details_player,
    details_team,
)
from .admin_views import (
    admin_print_EMA,
    admin_reset,
    admin_upload_from_template,
    admin_export_to_template,
    counter_start,
    logo,
    options,
    publish_web,
    publish_status,
    publish_target_save,
    publish_target_test,
    admin_team_draw,
    admin_team_draw_save,
    admin_player_draw,
    admin_player_draw_assign,
    player_editor_save,
    update_logo,
)
from .user_admin import (
    user_create,
    user_delete,
    user_generate_link,
    user_reauth,
    user_revoke_links,
    user_update_roles,
)
from .restore_admin import (
    restore_pull,
    restore_run,
    restore_status,
)
from .display import (
    check_variables,
    counter,
    index,
    overview,
    render_scores,
    scores_per_table,
    update_screen_name,
    update_screen_view,
)
from .scan import scan_page, scan_positions, scan_prefill, scan_status
from .print_views import (
    cross_positions,
    player_cards,
    player_names,
    print_schedule,
    print_scores,
    table_posters,
    team_names,
)
