from django.contrib import admin, messages
from django.contrib.auth.models import User
from django.utils.text import slugify
from django.contrib.auth.forms import PasswordResetForm
from django.urls import reverse
from django.contrib.sites.shortcuts import get_current_site

from .models import Player, Team, Round, Score, DriveUsed, CoursePar, EventSettings, SMSResponse

from .magic_utils import create_magic_link
from .sms_utils import prepare_recipients, broadcast, have_twilio_creds


@admin.action(description="Create/Link Users for selected players")
def action_provision_users(modeladmin, request, queryset):
    created = linked = skipped = 0
    for p in queryset:
        if not p.email:
            skipped += 1
            continue
        user = p.user or User.objects.filter(email__iexact=p.email).first()
        if not user:
            username = p.email.lower() if p.email else ""
            if not username or User.objects.filter(username=username).exists():
                base = slugify(f"{p.first_name}.{p.last_name}") or "player"
                candidate = base
                i = 1
                while User.objects.filter(username=candidate).exists():
                    i += 1
                    candidate = f"{base}{i}"
                username = candidate
            user = User.objects.create_user(username=username, email=p.email)
            user.set_unusable_password()
            user.first_name = p.first_name
            user.last_name = p.last_name
            user.save()
            created += 1
        else:
            linked += 1
        if p.user_id != user.id:
            p.user = user
            p.save(update_fields=["user"])
    messages.success(request, f"Users created: {created}, linked: {linked}, skipped (no email): {skipped}")

@admin.action(description="Send set-password email to selected players")
def action_send_set_password(modeladmin, request, queryset):
    sent = skipped = 0
    for p in queryset:
        if not (p.user and p.user.email):
            skipped += 1
            continue
        form = PasswordResetForm({"email": p.user.email})
        if form.is_valid():
            form.save(
                request=request,
                use_https=request.is_secure(),
                email_template_name="registration/password_reset_email.html",
                subject_template_name="registration/password_reset_subject.txt",
                from_email=None,
            )
            sent += 1
        else:
            skipped += 1
    messages.success(request, f"Password emails sent: {sent}, skipped: {skipped}")


@admin.action(description="Invite selected players (create/link user + email reset)")
def action_invite_players(modeladmin, request, queryset):
    created = linked = sent = skipped = 0
    for p in queryset:
        if not p.email:
            skipped += 1
            continue

        # create/link user
        user = p.user or User.objects.filter(email__iexact=p.email).first()
        if not user:
            username = p.email.lower()
            if not username or User.objects.filter(username=username).exists():
                base = slugify(f"{p.first_name}.{p.last_name}") or "player"
                candidate = base
                i = 1
                while User.objects.filter(username=candidate).exists():
                    i += 1
                    candidate = f"{base}{i}"
                username = candidate
            user = User.objects.create_user(username=username, email=p.email)
            user.set_unusable_password()
            user.first_name, user.last_name = p.first_name, p.last_name
            user.save()
            created += 1
        else:
            linked += 1

        if p.user_id != user.id:
            p.user = user
            p.save(update_fields=["user"])

        # send set-password email
        form = PasswordResetForm({"email": user.email})
        if form.is_valid():
            form.save(
                request=request,
                use_https=request.is_secure(),
                email_template_name="registration/password_reset_email.html",
                subject_template_name="registration/password_reset_subject.txt",
                from_email=None,
            )
            sent += 1
        else:
            skipped += 1

    messages.success(request, f"Invited: {sent}, created: {created}, linked: {linked}, skipped: {skipped}")


@admin.action(description="SMS magic sign-in link to selected players")
def action_sms_magic_link(modeladmin, request, queryset):
    if not have_twilio_creds():
        messages.error(request, "Missing TWILIO settings.")
        return
    sent = skipped = 0
    for p in queryset:
        if not (p.user and p.phone):
            skipped += 1
            continue
        nums = prepare_recipients([p.phone])
        if not nums:
            skipped += 1
            continue
        url = create_magic_link(request, p.user, ttl_seconds=15*60, sent_to=nums[0])
        body = f"Beer Open sign-in link (15 min): {url}"
        res = broadcast(nums, body, dry_run=False)
        if res["errors"]:
            skipped += 1
        else:
            sent += 1
    messages.success(request, f"Magic links sent: {sent}. Skipped: {skipped}.")


@admin.action(description="Populate holes 1–18 (par 4)")
def action_populate_par(modeladmin, request, queryset):
    created = 0
    for h in range(1, 19):
        obj, made = CoursePar.objects.get_or_create(hole=h, defaults={"par": 4})
        created += int(made)
    from django.contrib import messages
    messages.success(request, f"Ensured 1–18 exist. Created {created} row(s).")

@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display  = ("first_name", "last_name", "playing", "can_score", "email", "phone", "user")
    list_filter   = ("playing", "can_score")
    search_fields = ("first_name", "last_name", "email")
    actions = [action_provision_users, action_send_set_password, action_invite_players, action_sms_magic_link]

@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ("name", "tee_time")
    filter_horizontal = ("players",)

@admin.register(Round)
class RoundAdmin(admin.ModelAdmin):
    list_display = ("team", "event_date", "created_at")
    list_filter  = ("event_date",)

@admin.register(Score)
class ScoreAdmin(admin.ModelAdmin):
    list_display = ("round", "hole", "strokes")
    list_filter  = ("round__event_date",)
    ordering     = ("round", "hole")

@admin.register(DriveUsed)
class DriveUsedAdmin(admin.ModelAdmin):
    list_display = ("score", "player")
    list_filter  = ("score__round__event_date",)

@admin.register(CoursePar)
class CourseParAdmin(admin.ModelAdmin):
    list_display = ("hole", "par")
    list_editable = ("par",)
    actions = [action_populate_par]

@admin.register(EventSettings)
class EventSettingsAdmin(admin.ModelAdmin):
    list_display = ("event_name", "event_date", "leaderboard_public")

    def has_add_permission(self, request):
        # Only allow adding if none exists
        return not EventSettings.objects.exists()

@admin.register(SMSResponse)
class SMSResponseAdmin(admin.ModelAdmin):
    list_display = ("received_at", "from_number", "player", "campaign", "message_body")
    list_filter  = ("campaign", "received_at")
    search_fields = ("from_number", "message_body", "player__first_name", "player__last_name")
