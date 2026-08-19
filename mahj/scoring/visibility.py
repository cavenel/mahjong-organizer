"""The single source of truth for how much of a tournament a viewer may see.

Two things drive visibility: how many rounds are *complete* (every seat scored),
and the *publish* state (which rounds are published, and whether the final round
is published-but-withheld for the podium ceremony). Every surface — standings,
seating grid, per-round/overall stat cards, the modal cards — derives its round
cutoff from the helpers here, so the end-of-tournament "hold the final round for
the ceremony" rule is expressed in exactly one place.

A single ``full_view`` flag on the callers picks the mode:
  - public  (``full_view=False``, the default): clamp to the last published round,
    and drop the final round while it is withheld for the ceremony. The display
    screen shows a holding message during that window.
  - full    (``full_view=True``, admin / ceremony / print): see every scored round,
    no masking.
"""

from ..models import Seat, PublishedRound
from ._common import unscored_seats_q


def _last_complete_round(tenant, tournament):
    first_incomplete = (
        Seat.objects.filter(tenant=tenant)
        .filter(unscored_seats_q(tournament))
        .order_by('round_nb')
        .values('round_nb')
        .first()
    )
    return (first_incomplete['round_nb'] - 1) if first_incomplete else tournament.nb_rounds


def _final_round_withheld(tenant, nb_rounds):
    """Publish state of the last round, read from its PublishedRound row:
      None  → last round not published at all
      True  → published but withheld from the public for the ceremony
      False → published and visible to everyone
    """
    row = PublishedRound.objects.filter(tenant=tenant, round_nb=nb_rounds).first()
    return row.withheld if row else None


def publish_state(tenant, tournament):
    """``(last_published, final_round_withheld)`` for this tenant in one query.

    ``last_published`` is the highest published round_nb (0 if none);
    ``final_round_withheld`` is True only when the final round is published *and*
    withheld for the ceremony.
    """
    pub = {r.round_nb: r.withheld for r in PublishedRound.objects.filter(tenant=tenant)}
    last_published = max(pub) if pub else 0
    return last_published, bool(pub.get(tournament.nb_rounds))


def final_withheld_now(complete_round, tournament, final_withheld):
    """True in the end-of-tournament suspense window: every round is scored but the
    final one is published-and-withheld, so its results are held for the ceremony."""
    return complete_round == tournament.nb_rounds and final_withheld


def public_round_max(tenant, tournament, full_view=False):
    """Highest round a public viewer may see: clamped to the last published round,
    with a withheld final round dropped during the ceremony-suspense window.
    ``full_view=True`` (admin/ceremony) sees every scored round. Use this to cap
    auxiliary surfaces — e.g. the modal's placement/hand cards — to the same rounds
    the standings expose.
    """
    if full_view:
        return tournament.nb_rounds
    last_published, final_withheld = publish_state(tenant, tournament)
    round_max = min(_last_complete_round(tenant, tournament), last_published)
    if final_withheld_now(round_max, tournament, final_withheld):
        round_max = max(0, round_max - 1)
    return round_max
