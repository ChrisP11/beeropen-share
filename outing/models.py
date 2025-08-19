from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from django.conf import settings
from datetime import date


class Player(models.Model):
    first_name = models.CharField(max_length=30)
    last_name  = models.CharField(max_length=30)
    email      = models.EmailField(blank=True)
    phone      = models.CharField(max_length=20, blank=True)
    shirt_size = models.CharField(max_length=8, blank=True)
    notes      = models.TextField(blank=True)
    playing    = models.BooleanField(default=True)  # toggle per year
    can_score = models.BooleanField(
        default=True,
        help_text="Allow this player to edit their team's scorecard."
    )
    user       = models.OneToOneField(
        User, null=True, blank=True, on_delete=models.SET_NULL,
        help_text="Link if the player will log in to score."
    )

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

class Team(models.Model):
    name     = models.CharField(max_length=40)
    players  = models.ManyToManyField(Player, related_name="teams", blank=True)
    tee_time = models.TimeField(null=True, blank=True)  # admin assigns

    def __str__(self):
        return self.name

class Round(models.Model):
    """One scramble round for a team on the event date (Beer Open is one day)."""
    team       = models.ForeignKey(Team, on_delete=models.CASCADE)
    event_date = models.DateField(default=date.today)
    created_at = models.DateTimeField(auto_now_add=True)
    finalized_at = models.DateTimeField(null=True, blank=True)
    finalized_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="finalized_rounds")

    class Meta:
        unique_together = ("team", "event_date")

    def __str__(self):
        return f"{self.team} @ {self.event_date}"

class Score(models.Model):
    """One row per hole. Strokes are team strokes for the scramble."""
    round   = models.ForeignKey(Round, on_delete=models.CASCADE)
    hole    = models.PositiveSmallIntegerField()  # 1–18
    strokes = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        unique_together = ("round", "hole")
        ordering = ["hole"]

    def __str__(self):
        return f"{self.round} H{self.hole}: {self.strokes or '—'}"

class DriveUsed(models.Model):
    """Which player's drive counted on this hole (for the '1 per player per 9' rule)."""
    score  = models.OneToOneField(Score, on_delete=models.CASCADE, related_name="drive_used")
    player = models.ForeignKey(Player, on_delete=models.PROTECT)

    def __str__(self):
        return f"Drive: {self.player} on {self.score}"


class CoursePar(models.Model):
    hole = models.PositiveSmallIntegerField(
        choices=[(i, i) for i in range(1, 19)],
        unique=True
    )
    par = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ["hole"]

    def __str__(self):
        return f"H{self.hole} Par {self.par}"


class EventSettings(models.Model):
    event_name = models.CharField(max_length=80, default="Beer Open")
    event_date = models.DateField(default=date.today)
    leaderboard_public = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Event settings"
        verbose_name_plural = "Event settings"

    def __str__(self):
        return f"{self.event_name} ({self.event_date})"

    # make it a singleton (pk=1)
    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1, defaults={"event_name": "Beer Open"})
        return obj


class MagicLoginToken(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    token_hash = models.CharField(max_length=128, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    sent_to = models.CharField(max_length=32, blank=True)  # phone we texted

    class Meta:
        indexes = [models.Index(fields=["expires_at", "used_at"])]


class SMSResponse(models.Model):  # NEW
    received_at  = models.DateTimeField(auto_now_add=True)
    from_number  = models.CharField(max_length=20)
    message_body = models.TextField()
    player       = models.ForeignKey(Player, null=True, blank=True, on_delete=models.SET_NULL)
    campaign     = models.CharField(max_length=40, blank=True)  # e.g., "2025_shirts"
    def __str__(self):
        who = self.player and f"{self.player.first_name} {self.player.last_name}" or self.from_number
        return f"{self.received_at:%Y-%m-%d %H:%M} {who}: {self.message_body[:40]}"


# --- Past Events data ---
class ArchiveEvent(models.Model):
    KIND_CHOICES = [
        ("open", "Open"),
        ("ito", "Ito"),
        ("local", "Local"),
    ]
    year = models.PositiveIntegerField()
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default="open")

    # Core info
    date = models.DateField(null=True, blank=True)
    location = models.CharField(max_length=200, blank=True)

    # Assets (now: relative STATIC paths instead of uploads)
    # e.g. "outing/archive/2024/BeerOpen2024.png"
    logo   = models.CharField(max_length=255, blank=True,
                              help_text="Relative static path, e.g. outing/archive/2024/BeerOpen2024.png")
    plaque = models.CharField(max_length=255, blank=True,
                              help_text="Relative static path, e.g. outing/archive/2024/plaque.jpg")

    # Content (Markdown preferred)
    writeup_md = models.TextField(blank=True)
    odds_md = models.TextField(blank=True)

    # Publishing
    published = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("year", "kind")
        ordering = ["-year", "kind"]

    def __str__(self):
        return f"{self.year} {self.get_kind_display()}"


class ArchiveImage(models.Model):
    event = models.ForeignKey(ArchiveEvent, on_delete=models.CASCADE, related_name="gallery")

    # Keep the same field name `image`, but store a relative STATIC path
    # e.g. "outing/archive/2024/gallery/01.jpg"
    image = models.CharField(max_length=255,
                             help_text="Relative static path, e.g. outing/archive/2024/gallery/01.jpg")

    caption = models.CharField(max_length=200, blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return f"Image for {self.event} ({self.id})"
