import re

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models

# ── Printed player cards ──────────────────────────────────────────────────────
# The card *geometry* is a fixed set of formats (each drives a sheet grid, the
# duplex mirroring and its own header partial — see views/print_views.py
# CARD_LAYOUTS), while everything *visual* is the organiser's CSS. Adding a
# format means a new entry here, in CARD_LAYOUTS and a header partial.
CARD_FORMATS = ("a6_portrait", "a7_landscape")
# Themes are bundles of CSS rules on the card page (body.theme-<name>), each
# shipping a default variable block in card_themes.py. No template branches.
CARD_THEMES = ("classic", "minimal", "bold")
CARD_CSS_MAX = 20_000

# What custom CSS may not contain. The card page renders card_css with |safe
# inside a <style> (escaping would break `>` selectors), so this validator is
# what keeps that safe: closing the style element would open the door to markup
# and script, and the remote-fetch forms would turn the public card page into a
# beacon or let it pull in unreviewed styling. Anything else is fair game — it's
# a tenant admin styling their own tournament's cards.
_CARD_CSS_FORBIDDEN = (
    ("</style", "may not close the <style> element"),
    ("@import", "may not use @import"),
    ("expression(", "may not use expression()"),
    ("javascript:", "may not use javascript: URLs"),
    ("behavior:", "may not use behavior:"),
    ("-moz-binding", "may not use -moz-binding"),
)
# url(...) is allowed only for embedded data: URIs, so a card never fetches from
# another host. Matches the opening quote (if any) then requires "data:".
_CARD_CSS_URL = re.compile(r"""url\(\s*['"]?\s*(?!data:)""", re.IGNORECASE)


def validate_card_css(value):
    """Reject custom card CSS that could escape the <style> element or phone home.

    Used both as a model field validator (so the admin autosave rejects a bad
    value with a readable message) and as the guarantee behind rendering the
    stored value with |safe.
    """
    if not value:
        return
    if len(value) > CARD_CSS_MAX:
        raise ValidationError(
            f"Custom CSS is too long: {len(value)} characters "
            f"(maximum {CARD_CSS_MAX}).")
    lowered = value.lower()
    for needle, reason in _CARD_CSS_FORBIDDEN:
        if needle in lowered:
            raise ValidationError(f"Custom CSS {reason}.")
    if _CARD_CSS_URL.search(value):
        raise ValidationError(
            "Custom CSS may only use url() with an embedded data: URI, "
            "so cards never load from another site.")

class Tenant(models.Model):
    # The fallback every tenant FK defaults to. Referenced by get_default_pk below
    # and by the delete guard in views/user_admin — deleting this row would strand
    # every record that points at it, so it is named once.
    DEFAULT_SUBDOMAIN = 'default'

    name = models.CharField(max_length=255)
    subdomain = models.CharField(max_length=255)

    class Meta:
        constraints = [
            # The subdomain is the tenant key: every request resolves its tenant
            # from the host, and get_or_create(subdomain=...) assumes one row. Two
            # rows sharing one would make which tenant a request lands on depend on
            # row order.
            models.UniqueConstraint(fields=['subdomain'], name='unique_tenant_subdomain'),
        ]

    @classmethod
    def get_default_pk(cls):
        tenant, created = cls.objects.get_or_create(
            subdomain=cls.DEFAULT_SUBDOMAIN,
            defaults=dict(name='Empty subdomain'),
        )
        return tenant.pk

    def __str__(self):
        return self.subdomain


class Membership(models.Model):
    """One user's access to one tenant — the whole of per-tenant authorization.

    Deliberately NOT a ``TenantAwareModel``: that base scopes rows to a tenant via
    a default FK, whereas Membership *is* the scope definition and joins
    ``auth.User`` to a ``Tenant`` explicitly. A user with no Membership for the
    request's tenant has no access there — cross-tenant isolation falls straight
    out of the row's absence.

    Tiers (see docs/dev/access-control.md):
      - platform superuser — Django ``is_superuser``; cross-tenant, needs no row.
      - tenant admin — ``is_tenant_admin`` for a tenant; implies every app role
        there.
      - tenant role — ``is_scorer`` / ``is_display_op`` / ``is_publisher``, scoped
        to this one tenant. Plain booleans on the membership row, so a role check
        is one indexed row read, no extra join table. ``is_publisher`` implies the
        scorer role at check time (views/helpers.has_role); the row records only
        what was granted.
    """
    user   = models.ForeignKey(User, on_delete=models.CASCADE, related_name='memberships')
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='memberships')
    is_tenant_admin = models.BooleanField(default=False)
    is_scorer       = models.BooleanField(default=False)
    is_display_op   = models.BooleanField(default=False)
    is_publisher    = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'tenant'], name='unique_membership_per_tenant'),
        ]

    def __str__(self):
        roles = [n for n in ('tenant_admin', 'scorer', 'display_op', 'publisher')
                 if getattr(self, f'is_{n}')]
        return f"{self.user} @ {self.tenant}: {', '.join(roles) or 'no roles'}"

class TenantAwareModel(models.Model):
    id = models.AutoField(primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, default=Tenant.get_default_pk)
    class Meta:
        abstract = True


class Player(TenantAwareModel):
    """A human competitor — the player list, one row per real person.

    Holds the person's own data (name, federation id, country, team) and their
    ``draw_number``: the single place the draw is recorded (unique per tenant,
    null until the person is drawn in). The seating chart (Seat) is keyed by draw
    number, so the competitor at a seat is the Player holding that number — a
    name/country/team correction here shows everywhere at once with nothing
    duplicated, and re-drawing is just re-assigning draw numbers here. A Player
    with ``draw_number`` null is on the player list but not yet in the draw.
    """
    full_name  = models.CharField(max_length=70, default="")
    # The person's real first and last name, stored raw from the import's two
    # columns (mixed case preserved). full_name is the canonical "First Last"
    # display; last_name is blank for a mononym.
    first_name = models.CharField(max_length=70, default="")
    last_name  = models.CharField(max_length=70, default="", blank=True)
    # A short display token disambiguated across the field: the first name, plus
    # just enough of the surname ("Chris D.") when two competitors share a first
    # name. Built in bulk at import; the save() fallback below covers single edits.
    short_name = models.CharField(max_length=70, default="")
    EMA_ID     = models.CharField(max_length=70, default="")
    country    = models.CharField(max_length=70, default="")
    team       = models.CharField(max_length=70, default="", blank=True)
    draw_number = models.IntegerField(null=True, blank=True, default=None)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'draw_number'],
                name='unique_draw_number_per_tenant'),
        ]

    def save(self, *args, **kwargs):
        # Best-effort fallback for single saves (the import/editor bulk paths set
        # these explicitly). Only fill blanks so a real value is never clobbered.
        if not self.first_name:
            self.first_name = self.full_name.split(" ")[0]
        if not self.last_name:
            self.last_name = " ".join(self.full_name.split(" ")[1:])
        if not self.short_name:
            self.short_name = self.first_name
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.full_name


class Seat(TenantAwareModel):
    """One place at a table in one round: its wind, the draw slot it belongs to,
    and the score recorded there for the round.

    The seating chart is fixed by the draw (which *draw_number* sits at which
    table/wind each round) and comes straight from the imported schedule, so it
    exists before the draw is made and never changes when the draw does. The
    competitor sitting here is the Player whose ``draw_number`` matches this
    seat's; an unclaimed draw number is shown as "Player <n>".
    """
    class Wind(models.IntegerChoices):
        EAST  = 1, 'East'
        SOUTH = 2, 'South'
        WEST  = 3, 'West'
        NORTH = 4, 'North'

    round_nb    = models.IntegerField()
    table_nb    = models.IntegerField()
    wind        = models.IntegerField(choices=Wind.choices)
    # Which draw slot occupies this seat (the structural key of the seating
    # chart). The competitor is the Player with this draw_number — see Player.
    draw_number = models.IntegerField()

    minipoints  = models.IntegerField(default=None, null=True)
    tablepoints = models.FloatField(default=None, null=True)
    # Optimistic lock for the score grid, same convention as Hand.version: every
    # saved edit bumps it, and a save carrying an older version is answered with
    # a 409 + the current row instead of overwriting another scorer's numbers.
    version     = models.IntegerField(default=0)
    # Per-player penalty (minipoints), entered on the score sheet. Integer, may be
    # negative. The table total after penalties = sum of the four hand totals +
    # this; table points are ranked on that after-penalty total. A non-zero sum of
    # the four players' minipoints is expected when penalties are applied.
    penalty     = models.IntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=['tenant', 'round_nb', 'table_nb']),
            models.Index(fields=['tenant', 'draw_number']),
        ]

    def player_name(self):
        """The competitor's full name, or the "Player <n>" placeholder for a draw
        number no player claims yet. ``player`` is the transient attribute
        _attach_players sets from the draw; absent means it was never resolved."""
        player = getattr(self, 'player', None)
        return player.full_name if player else f"Player {self.draw_number}"

    def player_short_name(self):
        """Short form of ``player_name`` (disambiguated ``short_name``) for the
        compact seat views."""
        player = getattr(self, 'player', None)
        return player.short_name if player else f"Player {self.draw_number}"

    def __str__(self):
        return "R{0}, T{1}, {2}: #{3} [{4}MP / {5}TP]({6})".format(
            self.round_nb, self.table_nb, self.get_wind_display(),
            self.draw_number, self.minipoints, self.tablepoints, str(self.id))


class Hand(TenantAwareModel):
    """One hand played at a table in a round.

    Winner/discarder are seat winds (1=East .. 4=North). ``win_by`` encodes the
    three outcomes a played hand can have:
      - win        -> ``win_by`` is a wind (1..4).
          - self-draw  -> ``win_from`` is NULL (winner drew their own tile).
          - discard win -> ``win_from`` is the seat that dealt in.
      - draw       -> ``win_by`` is 0 (played, nobody won).
    ``win_by`` NULL is an unplayed placeholder row: it only exists on a sheet
    that has not been validated yet. Validation prunes trailing unplayed rows,
    so on a validated sheet the Hand row count is exactly the hands played.
    """
    round_nb    = models.IntegerField()
    table_nb    = models.IntegerField()
    hand_nb     = models.IntegerField()

    points      = models.IntegerField(default=0)
    win_by      = models.IntegerField(blank=True, null=True, default=None)
    win_from    = models.IntegerField(blank=True, null=True, default=None)
    version     = models.IntegerField(default=0)
    confidence  = models.FloatField(default=1.0)

    class Meta:
        indexes = [
            models.Index(fields=['tenant', 'round_nb', 'table_nb']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'round_nb', 'table_nb', 'hand_nb'],
                name='unique_hand_per_cell'),
        ]

    @property
    def is_draw(self):
        """Played hand that nobody won (``win_by`` 0). NULL is unplayed, not a draw."""
        return self.win_by == 0

    @property
    def is_self_draw(self):
        """A win with no discarder — the winner drew their own tile."""
        return self.win_by is not None and self.win_by != 0 and self.win_from is None

    def _seat_player(self, wind):
        if wind is None:
            return None
        try:
            seat = Seat.objects.using(self._state.db).filter(
                tenant=self.tenant, round_nb=self.round_nb,
                table_nb=self.table_nb, wind=wind).first()
            if seat is None:
                return None
            return Player.objects.using(self._state.db).filter(
                tenant=self.tenant, draw_number=seat.draw_number).first()
        except Exception:
            return None

    def win_by_player(self):
        return self._seat_player(self.win_by)

    # Only __str__ reads this one; it stays as the counterpart of win_by_player()
    # so the winner/discarder pair is symmetric.
    def win_from_player(self):
        return self._seat_player(self.win_from)

    def __str__(self):
        return "R{0}, T{1}, {2} pts by {3} in seat {4} from {5} in seat {6} ({7})".format(
            self.round_nb, self.table_nb, self.points, self.win_by_player(),
            self.win_by, self.win_from_player(), self.win_from, str(self.id))


class ScoreSheet(TenantAwareModel):
    """Score-entry state for one (round, table).

    A row exists once a sheet has been opened for the table; ``validated`` marks
    it human-checked. "Sheet started" is the row's existence; "sheet validated"
    is the flag.
    """
    round_nb  = models.IntegerField()
    table_nb  = models.IntegerField()
    validated = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'round_nb', 'table_nb'],
                name='unique_scoresheet_per_cell'),
        ]

    def __str__(self):
        return f"R{self.round_nb} T{self.table_nb} sheet ({'valid' if self.validated else 'open'})"


class Screen(TenantAwareModel):
    name = models.CharField(default="Unknown",max_length=70)
    view = models.CharField(default="",null=True,max_length=70)
    # Creation timestamp. Vestigial: nothing reads it but __str__; kept because
    # dropping a column is not worth a migration on every install.
    time = models.DateTimeField(auto_now_add=True, blank=False)

    # Legacy auto-assigned placeholders that should read as "no custom name" in the
    # UI, so an un-renamed screen falls back to its bare positional label (/1, /2…).
    _PLACEHOLDER_NAMES = ("", "Unknown", "Screen_X")

    @property
    def friendly_name(self):
        """The operator-given label, or "" if the screen was never renamed."""
        name = (self.name or "").strip()
        return "" if name in self._PLACEHOLDER_NAMES else name

    def __str__(self):
        return f"{self.time} / {self.view or ''}"

class ScreenMode(TenantAwareModel):
    name         = models.CharField(default="Unknown",max_length=70)
    # One view string per screen; the list grows with the screen count, so it is
    # stored as JSON (see add_mode in admin_views).
    views        = models.JSONField(default=list)

    def __str__(self):
        return str(self.name)

class TournamentSettings(TenantAwareModel):
    """Per-tournament configuration (one row per tenant). Exposed to templates as
    ``tournament``."""
    welcome      = models.CharField(default="",max_length=255)
    title        = models.CharField(default="",max_length=70)
    fullname     = models.CharField(default="",max_length=70)
    city         = models.CharField(default="",max_length=70)
    period       = models.CharField(default="",max_length=70)
    # Optional home nation. When set, standings compute a national sub-ranking
    # (``pos_se``) over the players whose ``country`` matches this, and the EMA
    # report stamps ``countrycourt`` as the organising federation. Both empty by
    # default so a generic install bakes in no nationality.
    home_country = models.CharField(default="",max_length=70,blank=True)
    countrycourt = models.CharField(default="",max_length=8,blank=True)
    # Public spectator-site URL advertised on projector screens (QR + caption)
    # and printed player cards. Blank → the tenant's <subdomain>.<BASE_DOMAIN>.
    # Set it to where the static site is published (see PublishTarget) so the QR
    # points spectators at the published site rather than the live app.
    public_url   = models.CharField(default="",max_length=255,blank=True)
    total_time   = models.IntegerField(default=1*60*60 + 55 * 60,null=False)
    nb_rounds    = models.IntegerField(default=7,null=False)
    # Discipline: only "MCR" vs "Riichi" are meaningful — MCR ranks on table
    # points then minipoints, everything else on minipoints alone.
    rules        = models.CharField(default="MCR",max_length=70)
    # Team tournament? The single source of truth for whether team standings,
    # columns and printouts appear (was inferred from "any player has a team").
    has_teams    = models.BooleanField(default=False)
    # Test tournament: unlocks the destructive rehearsal tools (random-fill /
    # clear-all on Scoring, random players in the editor). Off for real events.
    is_test      = models.BooleanField(default=False)
    zoom       = models.FloatField(default=1.0,null=False)
    score_lines  = models.IntegerField(default=20,null=False)
    total_columns = models.IntegerField(default=3,null=False)  # columns in the "totals" standings layout
    rotation_time = models.IntegerField(default=10,null=False)  # seconds each page shows before the standings screen rotates
    counter      = models.BigIntegerField(default=-1,null=False)  # -1 = never started; survives restarts
    # Optional staff-uploaded PNG logo (KBs), shown in place of the static mcr_logo
    # on on-screen surfaces. logo_etag is the md5 of the bytes, used to cache-bust
    # the served URL so projector screens refresh when the logo changes.
    logo         = models.BinaryField(null=True, blank=True, default=None, editable=True)
    logo_etag    = models.CharField(default="", max_length=32, blank=True)
    # Printed player cards. The format picks the sheet layout and header partial;
    # the theme and card_css decide how the card looks. card_css blank means "use
    # the theme's default variable block" (card_themes.effective_card_css).
    # Validators rather than choices=: the settings autosave runs run_validators()
    # only — Field.validate() would also enforce blank=False and start rejecting
    # the cleared text fields that work today.
    card_format  = models.CharField(
        default="a6_portrait", max_length=16,
        validators=[RegexValidator(r'^(%s)$' % '|'.join(CARD_FORMATS),
                                   "Unknown card format.")])
    card_theme   = models.CharField(
        default="classic", max_length=16,
        validators=[RegexValidator(r'^(%s)$' % '|'.join(CARD_THEMES),
                                   "Unknown card theme.")])
    card_css     = models.TextField(default="", blank=True,
                                    validators=[validate_card_css])

    class Meta:
        # One row per tenant — the whole module treats it that way (get_tournament
        # fetches "the" settings), and CeremonyState makes the same claim with the
        # same constraint. Without it two workers hitting a fresh tenant at once
        # could each provision a row, after which which one is read is arbitrary.
        constraints = [
            models.UniqueConstraint(fields=['tenant'], name='unique_settings_per_tenant'),
        ]

    def __str__(self):
        return self.welcome + " ; " + str(self.nb_rounds) + " ; " + str(self.title) + \
               self.fullname + " ; " + str(self.city) + " ; " + str(self.period) + " ; " + str(self.zoom) + " ; " + str(self.score_lines) + " ; " + str(self.total_time)


class PublishTarget(TenantAwareModel):
    """Per-tenant static-publish SFTP target, configured by staff in the admin.

    When a row exists and is enabled, this tenant's spectator site is published
    to its own web host; a multi-tenant instance thus publishes each tenant
    independently, and a tenant with no enabled target simply doesn't publish.
    Resolution lives in ``publish.sftp_upload``.

    ``password_enc`` / ``private_key_enc`` hold Fernet ciphertext (see
    ``publish.secrets``), never plaintext, and are never rendered back to the
    client — the editor is write-only. ``host_key`` is a known_hosts line pinning
    the target's host key (public data, not a secret). The operator can paste one;
    otherwise the first successful connect records what it saw and every connect
    after that is verified against it, so a changed key is refused.
    """
    enabled         = models.BooleanField(default=False)
    host            = models.CharField(default="", max_length=255, blank=True)
    port            = models.IntegerField(default=22)
    username        = models.CharField(default="", max_length=255, blank=True)
    path            = models.CharField(default="", max_length=1024, blank=True)
    # Where the per-publish tournament dump is uploaded (see tenant_dump). Blank
    # → a "mahj-backups" directory in the SFTP login directory, never derived from
    # `path`: a dump holds every score, including a withheld final round the public
    # site is still hiding, so it must not be web-fetchable. See dump_dir() for why
    # nothing under `path` is safe to assume.
    backup_path     = models.CharField(default="", max_length=1024, blank=True)
    host_key        = models.TextField(default="", blank=True)
    password_enc    = models.BinaryField(null=True, blank=True, default=None, editable=False)
    private_key_enc = models.BinaryField(null=True, blank=True, default=None, editable=False)

    class Meta:
        # One target per tenant, like TournamentSettings and CeremonyState. The editor
        # reads this row with get_or_create and the three resolution sites read it with
        # .order_by('id').first() — which concedes duplicates are possible. Two
        # concurrent saves (a double-clicked Save button) each inserted a row, after
        # which every later save raised MultipleObjectsReturned and the publisher
        # settings page 500'd permanently, with the tenant unable to publish.
        constraints = [
            models.UniqueConstraint(fields=['tenant'],
                                    name='unique_publish_target_per_tenant'),
        ]

    def __str__(self):
        return f'{self.username}@{self.host}:{self.path}'


class ScanConfig(TenantAwareModel):
    """Per-tenant score-sheet scanning setup: the API key, and the paper sheet.

    Both halves are per tenant because both used to be per *install*: the OCR key
    was one env var (so every tenant's scans billed to the operator) and the
    alignment template was one committed image (so only the operator's own sheet
    could ever be read). Neither falls back — a tournament with no key, or no
    sheet, does not scan. A guessed sheet would fail every photo silently and
    permanently, with an error that reads as bad photography.

    ``api_key_enc`` holds Fernet ciphertext (see ``mahj.secrets``), never
    plaintext, and is never rendered back to the client — the editor is
    write-only, and ``key_tail`` exists so support can identify a key without it.
    ``last_error`` is stamped by the worker and shown on the admin page: it is the
    only way an organiser learns that a key was revoked, or that photos are
    failing to align, without standing next to the scanner.

    ``template_img`` is a flat image of the tenant's blank score sheet and
    ``bbox_*`` the score-column crop within it, in that image's pixel
    coordinates. ``template_etag`` is the md5 of the bytes and doubles as the
    cache key for the ORB descriptors built from it (see ``views.scan``).
    """
    api_key_enc   = models.BinaryField(null=True, blank=True, default=None, editable=False)
    key_tail      = models.CharField(default="", max_length=8, blank=True)
    last_error    = models.CharField(default="", max_length=200, blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True, default=None)

    template_img   = models.BinaryField(null=True, blank=True, default=None)
    template_etag  = models.CharField(default="", max_length=32, blank=True)
    bbox_x1        = models.PositiveIntegerField(default=0)
    bbox_y1        = models.PositiveIntegerField(default=0)
    bbox_x2        = models.PositiveIntegerField(default=0)
    bbox_y2        = models.PositiveIntegerField(default=0)

    class Meta:
        # One row per tenant, like TournamentSettings and PublishTarget — the editor
        # reads it with get_or_create and resolution reads it with .first().
        constraints = [
            models.UniqueConstraint(fields=['tenant'],
                                    name='unique_scan_config_per_tenant'),
        ]

    def __str__(self):
        return f'scan config for {self.tenant.subdomain}'

    @property
    def bbox(self):
        """The crop box as _warp_crop wants it, or None if no usable one is set."""
        if self.bbox_x2 <= self.bbox_x1 or self.bbox_y2 <= self.bbox_y1:
            return None
        return (self.bbox_x1, self.bbox_y1, self.bbox_x2, self.bbox_y2)

    @property
    def has_template(self):
        """A template is only usable with a crop box: half a setup reads nothing."""
        return bool(self.template_img) and self.bbox is not None


class PublishedRound(TenantAwareModel):
    round_nb = models.IntegerField()
    # A published round is normally visible to everyone. ``withheld`` marks the
    # final round as published-but-held-back during the podium-reveal suspense:
    # the results are prepared for the ceremony but hidden from the public until
    # the reveal. The reveal animation itself is driven client-side by the
    # ceremony page, not by mutating this field.
    withheld = models.BooleanField(default=False)

    class Meta:
        unique_together = [('tenant', 'round_nb')]

    def __str__(self):
        return f"R{self.round_nb} published (withheld={self.withheld})"


class CeremonyState(TenantAwareModel):
    """Drives the prize-giving ceremony takeover of all display screens.

    Persistent (not cache) so a live ceremony survives a cache eviction or
    process restart. One row per tenant.

    phase:
      'idle'    — ceremony off; screens show their configured view.
      'teams'   — revealing top teams 10->1 (step = how many revealed).
      'players' — revealing top players 10->1 (step = how many revealed).
      'stat'    — showing one overall_winners category (stat_key).
      'blank'   — holding/title slide on all screens.
    """
    phase    = models.CharField(max_length=20, default='idle')
    step     = models.IntegerField(default=0)
    stat_key = models.CharField(max_length=40, default='', blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['tenant'], name='unique_ceremony_per_tenant'),
        ]

    def __str__(self):
        return f"Ceremony {self.phase} step={self.step} stat={self.stat_key}"


class Schedule(TenantAwareModel):
    """One line of the tournament agenda (registration, a playing round, lunch…).

    ``day``/``time`` are free-text strings shown verbatim; the list is ordered by
    ``id`` (import / creation order) everywhere it is read. ``is_round`` marks the
    rows that are actual playing rounds: those rows are mapped positionally to
    round numbers (the Nth ``is_round`` row is round N — see
    ``scoring.player_rounds``), so their count must match ``nb_rounds``.
    """
    day          = models.CharField(default="",max_length=70)
    time         = models.CharField(default="",null=True,max_length=70)
    name         = models.CharField(default="",null=True,max_length=70)
    # Explicit "this row is a playing round" flag, and the single authority for it:
    # the display, print and scoring code all read this, never ``name``. Import
    # seeds it from the name only when the template omits the "Is round" column
    # (see _name_is_round), and staff can correct it in Tournament settings.
    is_round     = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.day} - {self.time or ''} : {self.name or ''}"
