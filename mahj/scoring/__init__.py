"""Pure scoring/stats helpers — no request/view dependencies.

Split into submodules but presented as one flat namespace: import as
``from mahj import scoring`` and call ``scoring.player_standings(...)`` etc., or
``from mahj.scoring import player_standings``. Golden-file tests in
tests/test_scoring_golden.py lock the output shapes.

  - visibility — the single source of truth for what round a viewer may see.
  - standings  — player/team standings and the seating grid.
  - stats      — per-round / per-player / per-team statistics and the modals'
                 per-player rounds.
  - _common    — low-level helpers shared by the above (no inter-module deps).
"""
from ._common import (
    WINDS,
    WIND_LETTERS,
    _attach_players,
    _country_flag,
    _group_by,
    completed_tables,
    player_schedule,
    seat_is_scored,
    unscored_seats_q,
)
from .visibility import (
    _final_round_withheld,
    _last_complete_round,
    final_withheld_now,
    public_round_max,
    publish_state,
)
from .standings import (
    _assign_ranks,
    pad_scores,
    player_standings,
    rounds_played,
    team_standings,
    tournament_seating,
)
from .stats import (
    all_slot_rounds,
    overall_winners,
    player_extra_stats,
    player_rounds,
    round_winners,
    scores_per_table,
    stats_export,
    table_stats,
    table_stats_rounds,
    team_extra_stats,
)
