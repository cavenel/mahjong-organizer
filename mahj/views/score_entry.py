import json

from django.contrib.auth.decorators import user_passes_test
from django.db import transaction
from django.db.models import F
from django.http import HttpResponse, JsonResponse
from django.template import loader

from ..models import Hand, Position, PublishedRound
from ..signals import (
    broadcast_publish_state,
    broadcast_scorer_row,
    broadcast_scorer_validation,
    invalidate_leaderboard,
)
from .helpers import get_tenant, get_variables, is_scorer


WINDS = ['E', 'S', 'W', 'N']


def _published_rounds(tenant):
    return sorted(PublishedRound.objects.filter(tenant=tenant).values_list('round_nb', flat=True))


def _unpublish_rounds_from(tenant, round_nb):
    """Edit to round N invalidates publication of N and everything after it."""
    qs = PublishedRound.objects.filter(tenant=tenant, round_nb__gte=round_nb)
    deleted = qs.exists()
    qs.delete()
    return deleted


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

    hands_per_wind = []
    for i, wind in enumerate(["East", "South", "West", "North"]):
        hands_per_wind.append([wind, []])
        for j in range(4):
            h = all_hands[i * 4 + j]
            if h is None:
                h = Hand(tenant=tenant, round_nb=round_nb, table_nb=table_nb, hand_nb=i * 4 + j + 1, pts=0, win_by=0, win_from=0)
                h.save()
            hands_per_wind[-1][1].append(h)

    scores = [None, None, None, None]
    for position_val in position_vals:
        scores[position_val.position - 1] = position_val

    template = loader.get_template('mahj/admin_scores_per_hand.html')
    context = {
        'hands_per_wind': hands_per_wind,
        'completed': all_hands[16],
        'scores': scores,
        'round_nb': round_nb,
        'table_nb': table_nb,
    }
    return HttpResponse(template.render(context, request))


@user_passes_test(is_scorer)
def create_hand_points(request):
    tenant = get_tenant(request)
    table_nb = request.POST.get('table_nb')
    round_nb = request.POST.get('round_nb')
    for i in range(16):
        try:
            hand = Hand.objects.get(tenant=tenant, table_nb=table_nb, round_nb=round_nb, hand_nb=i + 1)
        except Hand.DoesNotExist:
            hand = Hand(tenant=tenant, table_nb=table_nb, round_nb=round_nb, hand_nb=i + 1, pts=0, win_by=0, win_from=0)
        hand.pts = int(request.POST.get('pts_' + str(i + 1)))
        hand.win_by = int(request.POST.get('by_' + str(i + 1)))
        hand.win_from = int(request.POST.get('from_' + str(i + 1)))
        hand.save()
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
    unpublished = _unpublish_rounds_from(tenant, position.round_nb)
    broadcast_scorer_row(subdomain, _row_payload(tenant, position.round_nb, position.table_nb))
    if unpublished:
        invalidate_leaderboard(subdomain)
        broadcast_publish_state(subdomain, {'published_rounds': _published_rounds(tenant)})
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

    with transaction.atomic():
        Position.objects.bulk_update(to_update, ['minipoints', 'tablepoints'])

    round_nb = to_update[0].round_nb
    table_nb = to_update[0].table_nb
    subdomain = tenant.subdomain if tenant else ''

    # Scorer-to-scorer live sync: cheap, no cache bust, not sent to public displays.
    broadcast_scorer_row(subdomain, _row_payload(tenant, round_nb, table_nb))

    # If this edit falls inside a previously published round (or any later round),
    # those publications become invalid. Unpublish and notify.
    if _unpublish_rounds_from(tenant, round_nb):
        invalidate_leaderboard(subdomain)
        broadcast_publish_state(subdomain, {'published_rounds': _published_rounds(tenant)})

    return HttpResponse("")


@user_passes_test(is_scorer)
def set_round_published(request):
    """Publish or unpublish a round. Publishing requires all 4 positions of every
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
    return JsonResponse({'status': 'ok', 'published_rounds': _published_rounds(tenant)})
