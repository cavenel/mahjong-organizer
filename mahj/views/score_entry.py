import json

from django.contrib.auth.decorators import user_passes_test
from django.db import transaction
from django.db.models import F
from django.http import HttpResponse, JsonResponse
from django.template import loader
from django.urls import reverse

from ..models import Hand, Position, PublishedRound
from ..signals import (
    broadcast_publish_state,
    broadcast_scorer_filled,
    broadcast_scorer_row,
    broadcast_scorer_validation,
    invalidate_leaderboard,
)
from .helpers import get_tenant, get_variables, is_publisher, is_scorer


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


def _row_payload(tenant, round_nb, table_nb):
    """Assemble a scorer-row event payload for a single (round, table)."""
    rows = Position.objects.filter(
        tenant=tenant, round_nb=round_nb, table_nb=table_nb,
    ).order_by('position')
    return {
        'round_nb': round_nb,
        'table_nb': table_nb,
        'positions': [
            {
                'id': p.id,
                'wind': WINDS[p.position - 1] if 1 <= p.position <= 4 else '',
                'mp': p.minipoints,
                'tp': float(p.tablepoints) if p.tablepoints is not None else None,
            }
            for p in rows
        ],
    }


def _scan_qr_svg(request, round_nb, table_nb):
    """Inline SVG QR linking to the pre-filled scan page. Empty string if the
    pure-Python `segno` dependency isn't installed on this host."""
    try:
        import segno
    except ImportError:
        return ''
    url = request.build_absolute_uri(
        reverse('scan_prefill_page', args=[round_nb, table_nb])
    )
    return segno.make(url, error='m').svg_inline(scale=3, border=2)


@user_passes_test(is_scorer)
def admin_scores_per_hand(request, round_nb, table_nb):
    tenant = get_tenant(request)
    position_vals = Position.objects.filter(tenant=tenant).order_by('id').filter(round_nb=round_nb, table_nb=table_nb)
    hand_vals = Hand.objects.filter(tenant=tenant).order_by('id').filter(round_nb=round_nb, table_nb=table_nb)

    all_hands = [None for _ in range(17)]
    for hand_val in hand_vals:
        all_hands[hand_val.hand_nb - 1] = hand_val

    if all_hands[16] is None:
        h = Hand(tenant=tenant, round_nb=round_nb, table_nb=table_nb, hand_nb=17, pts=0, win_by=0, win_from=0)
        h.save()
        all_hands[16] = h

    hands = []
    for i in range(16):
        h = all_hands[i]
        if h is None:
            h = Hand(tenant=tenant, round_nb=round_nb, table_nb=table_nb, hand_nb=i + 1, pts=0, win_by=0, win_from=0)
            h.save()
        # Tint low-confidence (OCR-guessed) cells; manual edits reset confidence to 1.0.
        conf_bg = ''
        if h.confidence is not None and h.confidence < 1.0:
            conf_bg = 'background:rgba(254,202,202,{:.2f});'.format(1.0 - h.confidence)
        hands.append({
            'hand_nb': h.hand_nb,
            'pts': h.pts,
            'win_by': h.win_by,
            'win_from': h.win_from,
            'id': h.id,
            'version': h.version,
            'conf_bg': conf_bg,
        })

    scores = [None, None, None, None]
    for position_val in position_vals:
        scores[position_val.position - 1] = position_val

    template = loader.get_template('mahj/admin_scores_per_hand.html')
    context = {
        'hands': hands,
        'completed': all_hands[16],
        'scores': scores,
        'round_nb': round_nb,
        'table_nb': table_nb,
        'qr_svg': _scan_qr_svg(request, round_nb, table_nb),
    }
    return HttpResponse(template.render(context, request))


@user_passes_test(is_scorer)
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

    def _int(name):
        try:
            return int(request.POST.get(name))
        except (TypeError, ValueError):
            return 0

    with transaction.atomic():
        for i in range(16):
            hand_nb = i + 1
            fields = {
                'pts': _int('pts_' + str(hand_nb)),
                'win_by': _int('by_' + str(hand_nb)),
                'win_from': _int('from_' + str(hand_nb)),
                'confidence': 1.0,
            }
            updated = Hand.objects.filter(
                tenant=tenant, round_nb=round_nb, table_nb=table_nb, hand_nb=hand_nb,
            ).update(version=F('version') + 1, **fields)
            if not updated:
                Hand.objects.create(
                    tenant=tenant, round_nb=round_nb, table_nb=table_nb, hand_nb=hand_nb,
                    **fields,
                )

    filled = Hand.objects.filter(
        tenant=tenant, round_nb=round_nb, table_nb=table_nb, pts__gt=0,
    ).exclude(hand_nb=17).exists()
    broadcast_scorer_filled(tenant.subdomain, {
        'type': 'scorer.filled',
        'round_nb': int(round_nb),
        'table_nb': int(table_nb),
        'filled': filled,
    })
    return HttpResponse("")


@user_passes_test(is_scorer)
def update_hand_points(request):
    tenant = get_tenant(request)
    hand_id = request.POST.get('id')
    client_version = int(request.POST.get('version', 0))

    try:
        pts = int(request.POST.get('pts'))
    except (TypeError, ValueError):
        pts = 0
    try:
        win_from = int(request.POST.get('from'))
    except (TypeError, ValueError):
        win_from = 0
    try:
        win_by = int(request.POST.get('by'))
    except (TypeError, ValueError):
        win_by = 0

    updated = Hand.objects.filter(
        tenant=tenant, id=hand_id, version=client_version,
    ).update(
        pts=pts, win_by=win_by, win_from=win_from,
        confidence=1.0,
        version=F('version') + 1,
    )

    if updated == 0:
        try:
            current = Hand.objects.get(tenant=tenant, id=hand_id)
            return JsonResponse({
                'status': 'conflict',
                'current': {
                    'pts': current.pts,
                    'win_by': current.win_by,
                    'win_from': current.win_from,
                    'version': current.version,
                },
            }, status=409)
        except Hand.DoesNotExist:
            return JsonResponse({'status': 'not_found'}, status=404)

    try:
        updated_hand = Hand.objects.get(tenant=tenant, id=hand_id)
        if updated_hand.hand_nb == 17:
            broadcast_scorer_validation(tenant.subdomain, {
                'type': 'scorer.validation',
                'round_nb': updated_hand.round_nb,
                'table_nb': updated_hand.table_nb,
                'valid': updated_hand.pts == 1,
            })
        else:
            filled = Hand.objects.filter(
                tenant=tenant, round_nb=updated_hand.round_nb,
                table_nb=updated_hand.table_nb, pts__gt=0,
            ).exclude(hand_nb=17).exists()
            broadcast_scorer_filled(tenant.subdomain, {
                'type': 'scorer.filled',
                'round_nb': updated_hand.round_nb,
                'table_nb': updated_hand.table_nb,
                'filled': filled,
            })
    except Hand.DoesNotExist:
        pass

    return JsonResponse({'status': 'ok', 'version': client_version + 1})


@user_passes_test(is_scorer)
def validate_score_sheet(request):
    """Set hand_nb=17 pts=1 (Valid) for a given (round_nb, table_nb)."""
    tenant = get_tenant(request)
    round_nb = int(request.POST.get('round_nb'))
    table_nb = int(request.POST.get('table_nb'))
    hand, _ = Hand.objects.get_or_create(
        tenant=tenant, round_nb=round_nb, table_nb=table_nb, hand_nb=17,
        defaults={'pts': 0, 'win_by': 0, 'win_from': 0},
    )
    hand.pts = 1
    hand.save()
    broadcast_scorer_validation(tenant.subdomain, {
        'type': 'scorer.validation',
        'round_nb': round_nb,
        'table_nb': table_nb,
        'valid': True,
    })
    return JsonResponse({'status': 'ok'})


@user_passes_test(is_scorer)
def update_position_points(request):
    tenant = get_tenant(request)
    position = Position.objects.get(tenant=tenant, id=request.GET.get('id'))

    # Published rounds are locked: a score can only change after the round is
    # explicitly unpublished. Reject the edit rather than silently unpublishing.
    if _round_is_published(tenant, position.round_nb):
        return JsonResponse({'status': 'locked', 'error': 'round is published'}, status=409)

    try:
        position.minipoints = int(request.GET.get('mp'))
    except (TypeError, ValueError):
        position.minipoints = None
    try:
        position.tablepoints = float(request.GET.get('tp'))
    except (TypeError, ValueError):
        position.tablepoints = None

    position.save()

    subdomain = tenant.subdomain if tenant else ''
    broadcast_scorer_row(subdomain, _row_payload(tenant, position.round_nb, position.table_nb))
    return HttpResponse("")


@user_passes_test(is_scorer)
def update_positions_bulk(request):
    """Update all 4 positions of a table row in a single request and transaction."""
    tenant = get_tenant(request)
    try:
        data = json.loads(request.body)
    except (ValueError, KeyError):
        return JsonResponse({'status': 'bad_request'}, status=400)

    entries = data.get('positions', [])
    ids = [int(e['id']) for e in entries]
    positions_map = {p.id: p for p in Position.objects.filter(tenant=tenant, id__in=ids)}

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
    # explicitly unpublished. Reject the edit rather than silently unpublishing.
    if _round_is_published(tenant, round_nb):
        return JsonResponse({'status': 'locked', 'error': 'round is published'}, status=409)

    with transaction.atomic():
        Position.objects.bulk_update(to_update, ['minipoints', 'tablepoints'])

    # Scorer-to-scorer live sync: cheap, no cache bust, not sent to public displays.
    broadcast_scorer_row(subdomain, _row_payload(tenant, round_nb, table_nb))

    return HttpResponse("")


@user_passes_test(is_publisher)
def set_round_published(request):
    """Publish or unpublish a round. Restricted to staff and the Publisher role:
    publishing locks a round's scores, so plain scorers must not be able to
    publish/unpublish (a stray unpublish would reopen finalized scores for
    editing). Publishing requires all 4 positions of every
    table in that round to have both minipoints and tablepoints set. On success,
    invalidates the public leaderboard cache and broadcasts a refresh to displays.
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

    variables = get_variables(request)
    is_last_round = (round_nb == variables.nb_rounds)

    if published:
        qs = Position.objects.filter(tenant=tenant, round_nb=round_nb)
        if not qs.exists():
            return JsonResponse({'status': 'error', 'error': 'round has no positions'}, status=400)
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

        reveal = 0 if is_last_round else 100
        PublishedRound.objects.update_or_create(
            tenant=tenant, round_nb=round_nb,
            defaults={'reveal_level': reveal},
        )
    else:
        # Unpublishing this round also unpublishes any later rounds (no gaps).
        _unpublish_rounds_from(tenant, round_nb)

    subdomain = tenant.subdomain if tenant else ''
    invalidate_leaderboard(subdomain)
    broadcast_publish_state(subdomain, {'published_rounds': _published_rounds(tenant)})

    from ..webhook import fire_webhook, leaderboard_payload
    from .scoring import scores_per_player_json
    standings = scores_per_player_json(request, check_final=True)
    event = 'round_published' if published else 'round_unpublished'
    fire_webhook(leaderboard_payload(event, standings, variables, round_nb))

    return JsonResponse({'status': 'ok', 'published_rounds': _published_rounds(tenant)})
