"""Re-export every view function so ``from mahj import views`` + ``views.<name>`` continues to work."""

from .helpers import (
    PositionForm,
    get_domain,
    get_podium,
    get_tenant,
    get_variables,
    is_display_op,
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
    tournament_seating,
)
from .score_entry import (
    admin_scores_per_hand,
    create_hand_points,
    set_round_published,
    update_hand_points,
    validate_score_sheet,
    update_position_points,
    update_positions_bulk,
)
from .public import desktop
from .public_modals import (
    detailed_scores,
    details_player,
    details_player_ema,
    details_team,
)
from .admin_views import (
    admin_print_EMA,
    admin_upload_from_template,
    check_final,
    counter_start,
    options,
    randomize,
    timer_options,
    update_variables,
    update_welcome,
    welcome_options,
)
from .display import (
    check_page,
    check_round,
    check_variables,
    counter,
    index,
    overview,
    scores_per_player,
    scores_per_table,
    update_screen_view,
)
from .print_views import (
    cross_positions,
    player_cards,
    player_names,
    print_schedule,
    print_scores,
    table_posters,
)
