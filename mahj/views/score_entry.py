import json

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.http import HttpResponse, JsonResponse
from django.template import loader
from django.urls import reverse

from ..models import Hand, ScoreSheet, Seat, PublishedRound
from ..scoring import _attach_players
from ..signals import (
    broadcast_publish_state,
    broadcast_scorer_filled,
    broadcast_scorer_row,
    broadcast_scorer_validation,
    invalidate_leaderboard,
)
from .helpers import get_tenant, get_tournament, tenant_role_required


WINDS = ['E', 'S', 'W', 'N']


def _published_rounds(tenant):
    return sorted(PublishedRound.objects.filter(tenant=tenant).values_list('round_nb', flat=True))


def _unpublish_rounds_from(tenant, round_nb):
    """Unpublishing round N also unpublishes everything after it (no gaps)."""
    qs = PublishedRound.objects.filter(tenant=tenant, round_nb__gte=round_nb)
    deleted = qs.exists()
    qs.delete()
    return deleted


def _round_is_published(tenant, round_nb):
    return PublishedRound.objects.filter(tenant=tenant, round_nb=round_nb).exists()


def _seat(raw):
    """A seat wind entered on the sheet: an int 1-4, or None (blank/0/out of range)."""
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    return v if 1 <= v <= 4 else None


def _parse_hand(points_raw, by_raw, from_raw):
    """Map the raw score-sheet inputs to the stored hand encoding.

    ``win_by`` carries the outcome:
      - a wind 1-4  -> a win (needs both a winning seat and points > 0).
      - 0           -> an explicit draw (scorer typed 0 in the "By" column).
      - NULL        -> an unplayed placeholder / incomplete row.
    Self-draw = a win with no discarder. A NULL row sitting before a later result
    is coerced to a draw at validation (see ``_prune_to_played_hands``), so mid-
    game draws can be left blank; only a draw on the final played hand needs the
    explicit 0 to tell it apart from an unplayed slot.
    """
    try:
        points = int(points_raw)
    except (TypeError, ValueError):
        points = 0
    try:
        by_int = int(by_raw)
    except (TypeError, ValueError):
        by_int = None
    if by_int == 0:
        return {'points': 0, 'win_by': 0, 'win_from': None}  # explicit draw
    win_by = _seat(by_raw)
    if points <= 0 or win_by is None:
        return {'points': 0, 'win_by': None, 'win_from': None}  # unplayed / incomplete
    win_from = _seat(from_raw)
    if win_from == win_by:
        win_from = None  # self-draw
    return {'points': points, 'win_by': win_by, 'win_from': win_from}


def _row_payload(tenant, round_nb, table_nb):
    """Assemble a scorer-row event payload for a single (round, table)."""
    rows = Seat.objects.filter(
        tenant=tenant, round_nb=round_nb, table_nb=table_nb,
    ).order_by('wind')
    return {
        'round_nb': round_nb,
        'table_nb': table_nb,
        'positions': [
            {
                'id': p.id,
                'wind': WINDS[p.wind - 1] if 1 <= p.wind <= 4 else '',
                'mp': p.minipoints,
                'tp': float(p.tablepoints) if p.tablepoints is not None else None,
            }
            for p in rows
        ],
    }


def _sheet_has_content(tenant, round_nb, table_nb):
    """True once any hand on the sheet has a result — a win or a draw (the
    in-progress badge). ``win_by`` is non-NULL for both; NULL rows are unplayed."""
    return Hand.objects.filter(
        tenant=tenant, round_nb=round_nb, table_nb=table_nb, win_by__isnull=False,
    ).exists()


def _scan_qr_svg(request, round_nb, table_nb):
    """Inline SVG QR linking to the pre-filled scan page. Empty string if scan is
    disabled (standalone build) or the pure-Python `segno` dependency isn't
    installed on this host — the template hides the QR when this is empty."""
    if not getattr(settings, 'SCAN_ENABLED', True):
        return ''
    try:
        import segno
    except ImportError:
        return ''
    url = request.build_absolute_uri(
        reverse('scan_prefill_page', args=[round_nb, table_nb])
    )
    return segno.make(url, error='m').svg_inline(scale=3, border=2)


@tenant_role_required('scorer')
def admin_scores_per_hand(request, round_nb, table_nb):
    tenant = get_tenant(request)
    position_vals = _attach_players(tenant, list(
        Seat.objects.filter(tenant=tenant, round_nb=round_nb, table_nb=table_nb).order_by('id')))
    hand_vals = {h.hand_nb: h for h in Hand.objects.filter(
        tenant=tenant, round_nb=round_nb, table_nb=table_nb)}

    # Opening a sheet records it (unvalidated) so the round-completeness and
    # in-progress badges can see it, and materializes the 16 editable hand rows so
    # each cell has a row id + version to drive the per-cell optimistic-lock save.
    sheet, _ = ScoreSheet.objects.get_or_create(
        tenant=tenant, round_nb=round_nb, table_nb=table_nb,
    )

    hands = []
    for i in range(16):
        hand_nb = i + 1
        h = hand_vals.get(hand_nb)
        if h is None:
            # get_or_create (not save()) so two concurrent first-opens of the same
            # fresh table — or an open racing a scan_prefill — don't have the loser
            # 500 on the unique_hand_per_cell constraint.
            h, _ = Hand.objects.get_or_create(
                tenant=tenant, round_nb=round_nb, table_nb=table_nb, hand_nb=hand_nb,
                defaults={'points': 0, 'win_by': None, 'win_from': None},
            )
        # Tint low-confidence (OCR-guessed) cells; manual edits reset confidence to 1.0.
        conf_bg = ''
        if h.confidence is not None and h.confidence < 1.0:
            conf_bg = 'background:rgba(254,202,202,{:.2f});'.format(1.0 - h.confidence)
        hands.append({
            'hand_nb': h.hand_nb,
            'points': h.points,
            'win_by': h.win_by,
            'win_from': h.win_from,
            'id': h.id,
            'version': h.version,
            'conf_bg': conf_bg,
        })

    scores = [None, None, None, None]
    for position_val in position_vals:
        scores[position_val.wind - 1] = position_val

    template = loader.get_template('mahj/admin_scores_per_hand.html')
    context = {
        'hands': hands,
        'validated': sheet.validated,
        'scores': scores,
        'round_nb': round_nb,
        'table_nb': table_nb,
        'qr_svg': _scan_qr_svg(request, round_nb, table_nb),
    }
    return HttpResponse(template.render(context, request))


@tenant_role_required('scorer')
def create_hand_points(request):
    """Bulk-write all 16 hands of a table (admin random-fill tool).

    One transaction, and each hand's version is bumped with F('version')+1 rather
    than written back from a stale read. A plain read-modify-save here would rewind
    the version a concurrent per-cell update_hand_points depends on, silently
    clobbering that scorer's edit; the atomic increment keeps the optimistic lock
    monotonic so no write is lost without a 409.
    """
    tenant = get_tenant(request)
    table_nb = request.POST.get('table_nb')
    round_nb = request.POST.get('round_nb')

    with transaction.atomic():
        for i in range(16):
            hand_nb = i + 1
            fields = _parse_hand(
                request.POST.get('points_' + str(hand_nb)),
                request.POST.get('by_' + str(hand_nb)),
                request.POST.get('from_' + str(hand_nb)),
            )
            fields['confidence'] = 1.0
            updated = Hand.objects.filter(
                tenant=tenant, round_nb=round_nb, table_nb=table_nb, hand_nb=hand_nb,
            ).update(version=F('version') + 1, **fields)
            if not updated:
                Hand.objects.create(
                    tenant=tenant, round_nb=round_nb, table_nb=table_nb, hand_nb=hand_nb,
                    **fields,
                )

    broadcast_scorer_filled(tenant.subdomain, {
        'type': 'scorer.filled',
        'round_nb': int(round_nb),
        'table_nb': int(table_nb),
        'filled': _sheet_has_content(tenant, round_nb, table_nb),
    })
    return HttpResponse("")


@tenant_role_required('scorer')
def update_hand_points(request):
    tenant = get_tenant(request)
    hand_id = request.POST.get('id')
    client_version = int(request.POST.get('version', 0))

    fields = _parse_hand(
        request.POST.get('points'), request.POST.get('by'), request.POST.get('from'),
    )

    updated = Hand.objects.filter(
        tenant=tenant, id=hand_id, version=client_version,
    ).update(
        confidence=1.0,
        version=F('version') + 1,
        **fields,
    )

    if updated == 0:
        try:
            current = Hand.objects.get(tenant=tenant, id=hand_id)
            return JsonResponse({
                'status': 'conflict',
                'current': {
                    'points': current.points,
                    'win_by': current.win_by,
                    'win_from': current.win_from,
                    'version': current.version,
                },
            }, status=409)
        except Hand.DoesNotExist:
            return JsonResponse({'status': 'not_found'}, status=404)

    try:
        updated_hand = Hand.objects.get(tenant=tenant, id=hand_id)
        broadcast_scorer_filled(tenant.subdomain, {
            'type': 'scorer.filled',
            'round_nb': updated_hand.round_nb,
            'table_nb': updated_hand.table_nb,
            'filled': _sheet_has_content(tenant, updated_hand.round_nb, updated_hand.table_nb),
        })
    except Hand.DoesNotExist:
        pass

    return JsonResponse({'status': 'ok', 'version': client_version + 1})


def _prune_to_played_hands(tenant, round_nb, table_nb):
    """Trim a sheet to exactly the hands played, run on validation so the
    validated sheet's row count is its hands played.

    Played hands form a contiguous prefix, so the last row carrying a result (a
    win or an explicit draw, i.e. ``win_by`` not NULL) marks where play ended.
    Any NULL (blank) row *before* it was a played hand that nobody won -> coerce
    to a draw (``win_by`` 0). NULL rows *after* it are unplayed slots -> delete."""
    played_hand_nbs = Hand.objects.filter(
        tenant=tenant, round_nb=round_nb, table_nb=table_nb, win_by__isnull=False,
    ).values_list('hand_nb', flat=True)
    last_played = max(played_hand_nbs, default=0)
    Hand.objects.filter(
        tenant=tenant, round_nb=round_nb, table_nb=table_nb,
        hand_nb__lte=last_played, win_by__isnull=True,
    ).update(win_by=0, win_from=None, points=0)
    Hand.objects.filter(
        tenant=tenant, round_nb=round_nb, table_nb=table_nb, hand_nb__gt=last_played,
    ).delete()


@tenant_role_required('scorer')
def validate_score_sheet(request):
    """Mark a table's score sheet validated (or not). Validating prunes the sheet
    to the hands actually played."""
    tenant = get_tenant(request)
    round_nb = int(request.POST.get('round_nb'))
    table_nb = int(request.POST.get('table_nb'))
    validated = request.POST.get('validated', '1')
    validated = str(validated).lower() in ('1', 'true', 'yes', 'on')

    if validated:
        _prune_to_played_hands(tenant, round_nb, table_nb)
    ScoreSheet.objects.update_or_create(
        tenant=tenant, round_nb=round_nb, table_nb=table_nb,
        defaults={'validated': validated},
    )
    broadcast_scorer_validation(tenant.subdomain, {
        'type': 'scorer.validation',
        'round_nb': round_nb,
        'table_nb': table_nb,
        'valid': validated,
    })
    return JsonResponse({'status': 'ok'})


@tenant_role_required('scorer')
def clear_score_sheet(request):
    """Wipe a table's score sheet: delete all its hands and the ScoreSheet record,
    and reset the four seats' penalties to 0, so the sheet reads as neither filled
    nor validated and carries no leftover penalty.

    Broadcasts validation=False *then* filled=False, in that order, so a remote
    scorer's badge lands grey: validation clears the green/active state, then
    filled clears the amber in-progress state.
    """
    tenant = get_tenant(request)
    round_nb = int(request.POST.get('round_nb'))
    table_nb = int(request.POST.get('table_nb'))
    Hand.objects.filter(tenant=tenant, round_nb=round_nb, table_nb=table_nb).delete()
    ScoreSheet.objects.filter(tenant=tenant, round_nb=round_nb, table_nb=table_nb).delete()
    Seat.objects.filter(
        tenant=tenant, round_nb=round_nb, table_nb=table_nb).update(penalty=0)
    broadcast_scorer_validation(tenant.subdomain, {
        'type': 'scorer.validation', 'round_nb': round_nb, 'table_nb': table_nb, 'valid': False,
    })
    broadcast_scorer_filled(tenant.subdomain, {
        'type': 'scorer.filled', 'round_nb': round_nb, 'table_nb': table_nb, 'filled': False,
    })
    return JsonResponse({'status': 'ok'})


@tenant_role_required('scorer')
def update_position_penalty(request):
    """Set a single seat's penalty (an integer minipoint adjustment, +/-).

    Entered from the MCR score sheet. The penalty is a sheet-balance figure only:
    the player's ranking minipoints already fold it in, so it never feeds the
    standings (it surfaces only on the detailed-scores modal). Unlike the MP/TP
    edits it is therefore *not* publish-locked — like the per-hand detail it can
    still be reconciled after the round is published.
    """
    tenant = get_tenant(request)
    try:
        seat = Seat.objects.get(tenant=tenant, id=request.POST.get('id'))
    except Seat.DoesNotExist:
        return JsonResponse({'status': 'not_found'}, status=404)

    try:
        seat.penalty = int(request.POST.get('penalty'))
    except (TypeError, ValueError):
        seat.penalty = 0
    seat.save(update_fields=['penalty'])

    return JsonResponse({'status': 'ok'})


@tenant_role_required('scorer')
def update_positions_bulk(request):
    """Update all 4 seats of a table row in a single request and transaction."""
    tenant = get_tenant(request)
    try:
        data = json.loads(request.body)
    except (ValueError, KeyError):
        return JsonResponse({'status': 'bad_request'}, status=400)

    entries = data.get('positions', [])
    ids = [int(e['id']) for e in entries]
    positions_map = {p.id: p for p in Seat.objects.filter(tenant=tenant, id__in=ids)}

    to_update = []
    for entry in entries:
        pos = positions_map.get(int(entry['id']))
        if pos is None:
            continue
        try:
            pos.minipoints = int(entry['mp'])
        except (TypeError, ValueError):
            pos.minipoints = None
        try:
            pos.tablepoints = float(entry['tp'])
        except (TypeError, ValueError):
            pos.tablepoints = None
        to_update.append(pos)

    if not to_update:
        return HttpResponse("")

    round_nb = to_update[0].round_nb
    table_nb = to_update[0].table_nb
    subdomain = tenant.subdomain if tenant else ''

    # Published rounds are locked: a score can only change after the round is
    # explicitly unpublished. Reject the edit rather than silently unpublishing,
    # and hand back the current server values so the client can revert the row it
    # tried to change (the lock may have been set by another scorer mid-edit).
    if _round_is_published(tenant, round_nb):
        return JsonResponse({
            'status': 'locked',
            'error': 'round is published',
            'row': _row_payload(tenant, round_nb, table_nb),
        }, status=409)

    with transaction.atomic():
        Seat.objects.bulk_update(to_update, ['minipoints', 'tablepoints'])

    # Scorer-to-scorer live sync: cheap, no cache bust, not sent to public displays.
    broadcast_scorer_row(subdomain, _row_payload(tenant, round_nb, table_nb))

    return HttpResponse("")


@tenant_role_required('publisher')
def set_round_published(request):
    """Publish or unpublish a round. Restricted to staff and the Publisher role:
    publishing locks a round's scores, so plain scorers must not be able to
    publish/unpublish (a stray unpublish would reopen finalized scores for
    editing). Publishing requires all 4 seats of every table in that round to have
    both minipoints and tablepoints set. On success, invalidates the public
    leaderboard cache and broadcasts a refresh to displays.
    """
    if request.method != 'POST':
        return JsonResponse({'status': 'method_not_allowed'}, status=405)

    tenant = get_tenant(request)
    try:
        data = json.loads(request.body) if request.body else {}
    except ValueError:
        return JsonResponse({'status': 'bad_request'}, status=400)

    try:
        round_nb = int(data.get('round_nb', request.POST.get('round_nb')))
    except (TypeError, ValueError):
        return JsonResponse({'status': 'bad_request', 'error': 'round_nb required'}, status=400)

    published = data.get('published', request.POST.get('published'))
    if isinstance(published, str):
        published = published.lower() in ('1', 'true', 'yes', 'on')
    published = bool(published)

    tournament = get_tournament(request)
    is_last_round = (round_nb == tournament.nb_rounds)

    if published:
        qs = Seat.objects.filter(tenant=tenant, round_nb=round_nb)
        if not qs.exists():
            return JsonResponse({'status': 'error', 'error': 'round has no seats'}, status=400)
        if qs.filter(minipoints=None).exists() or qs.filter(tablepoints=None).exists():
            return JsonResponse({'status': 'error', 'error': 'round is incomplete'}, status=400)

        if round_nb > 1:
            previous_published = set(
                PublishedRound.objects.filter(tenant=tenant, round_nb__lt=round_nb)
                    .values_list('round_nb', flat=True)
            )
            missing = [r for r in range(1, round_nb) if r not in previous_published]
            if missing:
                return JsonResponse({
                    'status': 'error',
                    'error': f'Cannot publish round {round_nb} — round(s) {", ".join(map(str, missing))} not published yet',
                }, status=400)

        # The last round is published but withheld from the public until the
        # ceremony reveals it; every other round is public immediately.
        PublishedRound.objects.update_or_create(
            tenant=tenant, round_nb=round_nb,
            defaults={'withheld': is_last_round},
        )
    else:
        # Unpublishing this round also unpublishes any later rounds (no gaps).
        _unpublish_rounds_from(tenant, round_nb)

    subdomain = tenant.subdomain if tenant else ''
    # Announce the round number to the standings screens (they show a 3-2-1
    # countdown before refreshing) only for a normal publish. The last round is
    # withheld for the ceremony (the "waiting for ceremony" holding screen), and
    # unpublish shouldn't count down either — both reload instantly.
    invalidate_leaderboard(
        subdomain,
        published_round=(round_nb if (published and not is_last_round) else None),
    )
    broadcast_publish_state(subdomain, {'published_rounds': _published_rounds(tenant)})

    # Regenerate + upload the static spectator site (no-op unless SFTP configured).
    from ..publish.trigger import fire_static_export
    fire_static_export(subdomain)

    return JsonResponse({'status': 'ok', 'published_rounds': _published_rounds(tenant)})
