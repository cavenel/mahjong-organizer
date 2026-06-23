from django.db import models

class Tenant(models.Model):
    name = models.CharField(max_length=255)
    subdomain = models.CharField(max_length=255)

    @classmethod
    def get_default_pk(cls):
        tenant, created = cls.objects.get_or_create(
            subdomain='default',
            defaults=dict(name='Empty subdomain'),
        )
        return tenant.pk
    
    def __str__(self):
        return self.subdomain

class TenantAwareModel(models.Model):
    id = models.AutoField(primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, default=Tenant.get_default_pk)
    class Meta:
        abstract = True
      

class Player_data(TenantAwareModel):
    full_name = models.CharField(max_length=70)
    first_name = models.CharField(max_length=70,default="")
    EMA_ID    = models.CharField(max_length=70)
    country   = models.CharField(max_length=70)
    email     = models.CharField(max_length=70)
    team      = models.CharField(max_length=70, default="", blank=True)
    
    def last_name (self):
        return " ".join(self.full_name.split(" ")[1:]).upper()
        
    def __str__(self):
        return self.full_name
    
class Player(TenantAwareModel):
    full_name = models.CharField(max_length=70,default="")
    first_name = models.CharField(max_length=70,default="")
    EMA_ID    = models.CharField(max_length=70,default="")
    country   = models.CharField(max_length=70,default="Sweden")
    email     = models.CharField(max_length=70,default="")
    rand_id   = models.IntegerField(default=0)
    team      = models.CharField(max_length=70, default="", blank=True)

    def save(self, *args, **kwargs):
        if self.first_name == "":
            if "Player" in self.full_name:
                self.first_name = self.full_name.replace("Player ","")
            else:
                self.first_name = self.full_name.split(" ")[0]
        return super().save(*args, **kwargs)
            
    def last_name (self):
        return " ".join(self.full_name.split(" ")[1:]).upper()
        
    def __str__(self):
        return self.full_name
    
class Position(TenantAwareModel):
    round_nb    = models.IntegerField()
    table_nb    = models.IntegerField()
    player      = models.ForeignKey(Player, on_delete=models.CASCADE)
    position    = models.IntegerField()

    minipoints  = models.IntegerField(default=None, null=True)
    tablepoints = models.FloatField(default=None, null=True)
    # Per-player penalty (minipoints), entered on the score sheet. Integer, may be
    # negative. The table total after penalties = sum of the four hand totals +
    # this; table points are ranked on that after-penalty total. A non-zero sum of
    # the four players' minipoints is expected when penalties are applied.
    penalty     = models.IntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=['tenant', 'round_nb', 'table_nb']),
            models.Index(fields=['tenant', 'player']),
        ]

    def __str__(self):
        return "R{0}, T{1}, {2}: {3} [{4}MP / {5}TP]({6})".format(self.round_nb, self.table_nb, ["","E","S","W","N"][self.position], str(self.player), self.minipoints, self.tablepoints, str(self.id))

class Hand(TenantAwareModel):
    round_nb    = models.IntegerField()
    table_nb    = models.IntegerField()
    hand_nb     = models.IntegerField()

    pts         = models.IntegerField(default=0)
    win_by      = models.IntegerField(blank=True, default=None)
    win_from    = models.IntegerField(blank=True, default=None)
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

    def win_by_player (self):
        try:
            position_vals = Position.objects.using(self._state.db).filter(tenant=self.tenant, round_nb=self.round_nb, table_nb=self.table_nb, position=self.win_by)
            pos = position_vals[0]
            return pos.player
        except Exception:
            return None
        

    def win_from_player (self):
        try:
            position_vals = Position.objects.using(self._state.db).filter(tenant=self.tenant, round_nb=self.round_nb, table_nb=self.table_nb, position=self.win_from)
            pos = position_vals[0]
            return pos.player
        except Exception:
            return None

    def __str__(self):
        return "R{0}, T{1}, {2} pts by {3} in pos {4} from {5} in pos {6} ({7})".format(self.round_nb, self.table_nb, self.pts, self.win_by_player(), self.win_by, self.win_from_player(), self.win_from, str(self.id))

class Screen(TenantAwareModel):
    name         = models.CharField(default="Unknown",max_length=70)
    view         = models.CharField(default="",null=True,max_length=70)
    time         = models.DateTimeField(auto_now_add=True, blank=False)
    last_refresh = models.DateTimeField(auto_now_add=True, blank=False)
    
    def __str__(self):
        return str(self.time) + " / " + self.view

class ScreenMode(TenantAwareModel):
    name         = models.CharField(default="Unknown",max_length=70)
    views        = models.CharField(default="Unknown",max_length=100)
    
    def __str__(self):
        return str(self.name)

class Variable(TenantAwareModel):
    welcome      = models.CharField(default="",max_length=70)
    title        = models.CharField(default="",max_length=70)
    fullname     = models.CharField(default="",max_length=70)
    city         = models.CharField(default="",max_length=70)
    period       = models.CharField(default="",max_length=70)
    rules        = models.CharField(default="",max_length=70)
    total_time   = models.IntegerField(default=1*60*60 + 55 * 60,null=False)
    nb_rounds    = models.IntegerField(default=7,null=False)
    zoom         = models.FloatField(default=1.0,null=False)
    score_lines  = models.IntegerField(default=20,null=False)
    counter      = models.BigIntegerField(default=-1,null=False)  # -1 = never started; survives restarts
    #sw_class_1   = models.ForeignKey(Player, null=True, blank=True, default=None, related_name='+',on_delete=models.CASCADE)
    #sw_class_2   = models.ForeignKey(Player, null=True, blank=True, default=None, related_name='+',on_delete=models.CASCADE)
    #sw_class_3   = models.ForeignKey(Player, null=True, blank=True, default=None, related_name='+',on_delete=models.CASCADE)
    #sw_class_4   = models.ForeignKey(Player, null=True, blank=True, default=None, related_name='+',on_delete=models.CASCADE)
    
    def __str__(self):
        return self.welcome + " ; " + str(self.nb_rounds) + " ; " + str(self.title) + \
               self.fullname + " ; " + str(self.city) + " ; " + str(self.period) + " ; " + str(self.zoom) + " ; " + str(self.score_lines) + " ; " + str(self.total_time)


class PublishedRound(TenantAwareModel):
    round_nb     = models.IntegerField()
    # 100 = fully visible (non-last rounds or fully revealed last round).
    # For the last round, publishing starts at 0 (hidden — podium reveal suspense),
    # then admin_display +/- bumps it: 1..11 progressively reveal positions 10..1, >11 = fully visible.
    reveal_level = models.IntegerField(default=100)
    published_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('tenant', 'round_nb')]

    def __str__(self):
        return f"R{self.round_nb} published (reveal={self.reveal_level})"


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
    phase      = models.CharField(max_length=20, default='idle')
    step       = models.IntegerField(default=0)
    stat_key   = models.CharField(max_length=40, default='', blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['tenant'], name='unique_ceremony_per_tenant'),
        ]

    def __str__(self):
        return f"Ceremony {self.phase} step={self.step} stat={self.stat_key}"


class Schedule(TenantAwareModel):
    day          = models.CharField(default="",max_length=70)
    time         = models.CharField(default="",null=True,max_length=70)
    name         = models.CharField(default="",null=True,max_length=70)
    
    def __str__(self):
        return str(self.day) + " - " + self.time + " : " + self.time
