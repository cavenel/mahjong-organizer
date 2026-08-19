"""Re-export every view function so ``from mahj import views`` + ``views.<name>`` continues to work."""

from .helpers import (
    can_access_admin,
    current_membership,
    get_domain,
    get_tenant,
    get_tournament,
    has_role,
    is_tenant_admin,
    superuser_required,
    tenant_admin_required,
    tenant_role_required,
)
from .scoring import (
    player_rounds_rows,
    scores_per_player_rows,
    scores_per_table_grid,
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
    update_seat_penalty,
    update_seats_bulk,
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
    admin_generate_seating,
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
    tenant_create,
    tenant_rename,
    user_create,
    user_delete,
    user_generate_link,
    user_reauth,
    user_remove_from_tenant,
    user_revoke_links,
    user_update_roles,
)
from .restore_admin import (
    restore_pull,
    restore_run,
    restore_status,
)
from .display import (
    check_tournament,
    counter,
    index,
    overview,
    render_scores,
    update_screen_name,
    update_screen_view,
)
from .scan import scan_page, scan_prefill, scan_seats, scan_status
from .print_views import (
    cross_positions,
    player_cards,
    player_names,
    print_schedule,
    print_scores,
    table_posters,
    team_names,
)
