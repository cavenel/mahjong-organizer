import pathlib

from django.core.cache import cache
from django.forms import ModelForm

from ..models import Position, Tenant, Variable, Hand


BASE_DIR = pathlib.Path(__file__).resolve().parent.parent

VARIABLES_TTL = 300  # 5 minutes; invalidated on Variable writes via signals.
TENANT_TTL = 600     # 10 minutes; invalidated on Tenant writes via signals.


def get_counter(tenant):
    v = Variable.objects.filter(tenant=tenant).first()
    return v.counter if v else -1


def set_counter(tenant, value):
    # .update() skips signals: counter writes don't need to invalidate leaderboard cache.
    Variable.objects.filter(tenant=tenant).update(counter=value)
    if tenant is not None:
        cache.delete(f'variables:{tenant.subdomain}')


def is_scorer(user):
    return user.is_authenticated and (user.is_staff or user.groups.filter(name='Scorer').exists())

def is_display_op(user):
    return user.is_authenticated and (user.is_staff or user.groups.filter(name='Display_op').exists())

def is_scorer_or_display_op(user):
    return is_scorer(user) or is_display_op(user)


class PositionForm(ModelForm):
    class Meta:
        model = Position
        fields = ['id', 'minipoints', 'tablepoints']


def get_domain(request):
    host = request.get_host()
    parts = host.split('.')
    if len(parts) >= 3:
        subdomain = parts[0]
    else:
        subdomain = ""
    return subdomain


def get_tenant(request):
    if hasattr(request, '_tenant'):
        return request._tenant
    subdomain = get_domain(request)
    if subdomain == "192":
        subdomain = "devvarberg"
    cache_key = f'tenant:{subdomain}'
    tenant = cache.get(cache_key)
    if tenant is None:
        tenant = Tenant.objects.filter(subdomain=subdomain).first()
        if tenant is None and request.user.is_authenticated and request.user.is_staff:
            tenant = Tenant(subdomain=subdomain)
            tenant.save()
        if tenant is not None:
            cache.set(cache_key, tenant, TENANT_TTL)
    request._tenant = tenant
    return tenant


def get_variables(request):
    tenant = get_tenant(request)
    subdomain = tenant.subdomain if tenant else ''
    cache_key = f'variables:{subdomain}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    variables = Variable.objects.filter(tenant=tenant).first()
    if variables is None:
        variables = Variable(tenant=tenant, welcome="Welcome")
        variables.save()
    cache.set(cache_key, variables, VARIABLES_TTL)
    return variables


def player_statistics(request, player, variables):
    tenant = get_tenant(request)
    position_vals = Position.objects.filter(tenant=tenant, player=player).order_by('round_nb')
    position_vals = [p for p in position_vals if p.minipoints is not None]
    hand_vals = []
    num_wins = {"num": 0, "round": ""}
    for position_val in position_vals:
        win_hands = Hand.objects.filter(tenant=tenant).order_by('id').filter(
            round_nb=position_val.round_nb,
            table_nb=position_val.table_nb,
            win_by=position_val.position,
        )
        hand_vals += win_hands
        if len(win_hands) > num_wins["num"]:
            num_wins = {"num": len(win_hands), "round": position_val.round_nb}
    biggest_hands = sorted(
        [{"pts": h.pts, "round": h.round_nb} for h in hand_vals],
        reverse=True, key=lambda x: x["pts"],
    )
    biggest_total = sorted(
        [{"pts": p.minipoints, "round": p.round_nb} for p in position_vals],
        reverse=True, key=lambda x: x["pts"],
    )
    return {
        "biggest_hands": biggest_hands,
        "biggest_total": biggest_total,
        "num_wins": num_wins,
    }


def get_podium(scores_json):
    podium = [[], [], []]
    for score in scores_json:
        if score["pos"] < 4:
            podium[score["pos"] - 1].append(score)
    return podium
