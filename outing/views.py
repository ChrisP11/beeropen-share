from datetime import date, time
from django.utils.timezone import now
from django.utils import timezone

from typing import Dict, List, Optional
from collections import Counter

from django.contrib.auth import login
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib import messages
from django.http import HttpResponseForbidden, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.db.models import Sum, Case, When, IntegerField, Count, Q
from django.views.decorators.http import require_POST, require_http_methods

from .models import Team, Round, Score, DriveUsed, Player, CoursePar, EventSettings, MagicLoginToken
from .sms_utils import prepare_recipients, broadcast, have_twilio_creds
from .magic_utils import create_magic_link, validate_token

def current_event_date():
    return EventSettings.load().event_date


# REPLACE your team_scorecard_view with this version (same body + finalize logic)
@login_required
def team_scorecard_view(request: HttpRequest, team_id: int) -> HttpResponse:
    team = get_object_or_404(Team, pk=team_id)

    # Who can view / edit?
    is_member = team.players.filter(user=request.user).exists()
    if not (request.user.is_staff or is_member):
        return HttpResponseForbidden("Not your team")

    # Base edit right (before lock)
    base_can_edit = request.user.is_staff or team.players.filter(user=request.user, can_score=True).exists()

    # One round per team per event date
    round_obj, _ = Round.objects.get_or_create(team=team, event_date=current_event_date())

    # Lock state + effective edit right
    is_final = bool(round_obj.finalized_at)
    can_edit = base_can_edit and not is_final

    # ---------- POST ----------
    if request.method == "POST":
        action = request.POST.get("action", "save")

        # Admin-only unlock
        if action == "unlock":
            if request.user.is_staff and is_final:
                round_obj.finalized_at = None
                round_obj.finalized_by = None
                round_obj.save(update_fields=["finalized_at", "finalized_by"])
                messages.info(request, "Round unlocked.")
            else:
                messages.error(request, "You can’t unlock this round.")
            return redirect("team_scorecard", team_id=team.id)

        # If locked, block edits
        if not can_edit:
            messages.error(request, "This scorecard is locked.")
            return redirect("team_scorecard", team_id=team.id)

        # ----- Save strokes + drives (your existing logic) -----
        for h in range(1, 19):
            strokes_key = f"h{h}"
            drive_key   = f"d{h}"

            # Save strokes
            raw = (request.POST.get(strokes_key) or "").strip()
            strokes: Optional[int] = int(raw) if raw.isdigit() else None

            sc, _ = Score.objects.get_or_create(round=round_obj, hole=h)
            sc.strokes = strokes
            sc.save()

            # Save drive used (player id or blank)
            drive_pid = (request.POST.get(drive_key) or "").strip()
            if drive_pid:
                p = Player.objects.filter(pk=drive_pid, teams=team).first()
                if p:
                    DriveUsed.objects.update_or_create(score=sc, defaults={"player": p})
            else:
                DriveUsed.objects.filter(score=sc).delete()

        # --- Quota check (warn on save, as you had) ---
        entered_front = Score.objects.filter(
            round=round_obj, hole__lte=9, strokes__isnull=False
        ).count()
        entered_back = Score.objects.filter(
            round=round_obj, hole__gte=10, strokes__isnull=False
        ).count()

        drive_qs = DriveUsed.objects.filter(score__round=round_obj).select_related("score")
        player_ids = list(team.players.values_list("id", flat=True))
        front_counts = {pid: 0 for pid in player_ids}
        back_counts  = {pid: 0 for pid in player_ids}
        for du in drive_qs:
            if du.score.hole <= 9:
                front_counts[du.player_id] = front_counts.get(du.player_id, 0) + 1
            else:
                back_counts[du.player_id] = back_counts.get(du.player_id, 0) + 1

        if entered_front == 9:
            missing = [p for p in team.players.all() if front_counts.get(p.id, 0) == 0]
            if missing:
                names = ", ".join(f"{p.first_name} {p.last_name}" for p in missing)
                messages.warning(request, f"Front nine quota: no drive used yet for {names}.")

        if entered_back == 9:
            missing = [p for p in team.players.all() if back_counts.get(p.id, 0) == 0]
            if missing:
                names = ", ".join(f"{p.first_name} {p.last_name}" for p in missing)
                messages.warning(request, f"Back nine quota: no drive used yet for {names}.")
        # --- end quota check ---

        # If finalize, enforce and lock
        if action == "finalize":
            holes_entered = Score.objects.filter(round=round_obj, strokes__isnull=False).count()
            if holes_entered < 18:
                messages.error(request, f"Cannot finalize: only {holes_entered}/18 holes have scores.")
                return redirect("team_scorecard", team_id=team.id)

            # Recompute drive counts to enforce ≥1 on each nine
            drive_qs = DriveUsed.objects.filter(score__round=round_obj).select_related("score")
            players_all = list(team.players.all())
            front_counts = {p.id: 0 for p in players_all}
            back_counts  = {p.id: 0 for p in players_all}
            for du in drive_qs:
                if du.score.hole <= 9:
                    front_counts[du.player_id] += 1
                else:
                    back_counts[du.player_id] += 1

            missing_front = [p for p in players_all if front_counts.get(p.id, 0) == 0]
            missing_back  = [p for p in players_all if back_counts.get(p.id, 0) == 0]
            if missing_front or missing_back:
                if missing_front:
                    names = ", ".join(f"{p.first_name} {p.last_name}" for p in missing_front)
                    messages.error(request, f"Front nine missing drive from: {names}")
                if missing_back:
                    names = ", ".join(f"{p.first_name} {p.last_name}" for p in missing_back)
                    messages.error(request, f"Back nine missing drive from: {names}")
                return redirect("team_scorecard", team_id=team.id)

            round_obj.finalized_at = now()
            round_obj.finalized_by = request.user
            round_obj.save(update_fields=["finalized_at", "finalized_by"])
            messages.success(request, "Scorecard finalized. Congrats!")
            return redirect("team_scorecard", team_id=team.id)

        messages.success(request, "Saved.")
        return redirect("team_scorecard", team_id=team.id)

    # ---------- GET: build simple structures for the template ----------
    scores = {s.hole: s for s in Score.objects.filter(round=round_obj).select_related("drive_used")}
    players = list(team.players.order_by("last_name", "first_name"))

    holes = []
    out_total = 0
    in_total = 0
    for h in range(1, 19):
        s = scores.get(h)
        strokes = s.strokes if (s and isinstance(s.strokes, int)) else None
        drive_pid = getattr(getattr(s, "drive_used", None), "player_id", None)
        holes.append({"n": h, "strokes": strokes, "drive_pid": drive_pid})
        if strokes is not None:
            if h <= 9:
                out_total += strokes
            else:
                in_total += strokes

    total_val = (out_total or 0) + (in_total or 0)
    total = total_val or None
    if out_total == 0: out_total = None
    if in_total  == 0: in_total  = None

    # Drive counts per player per nine (for UI badges)
    drive_qs = DriveUsed.objects.filter(score__round=round_obj).select_related("score")
    front_counts = {p.id: 0 for p in players}
    back_counts  = {p.id: 0 for p in players}
    for du in drive_qs:
        if du.score.hole <= 9:
            front_counts[du.player_id] += 1
        else:
            back_counts[du.player_id] += 1

    # Build players_info with counts for easy templating
    players_info = []
    for p in players:
        players_info.append({
            "id": p.id,
            "first": p.first_name,
            "last": p.last_name,
            "initials": f"{p.first_name[:1]}{p.last_name[:1]}",
            "front": front_counts.get(p.id, 0),
            "back":  back_counts.get(p.id, 0),
        })

    return render(request, "outing/scorecard.html", {
        "team": team,
        "round": round_obj,
        "players": players,
        "players_info": players_info,
        "holes": holes,
        "out_total": out_total,
        "in_total": in_total,
        "total": total,
        "can_edit": can_edit,     # now respects lock
        "is_final": is_final,     # NEW
        "front_counts": front_counts,
        "back_counts": back_counts,
    })



def _leaderboard_rows(event_date: date):
    # Par per hole
    par_by_hole = {p.hole: p.par for p in CoursePar.objects.all()}

    rounds = (
        Round.objects
        .filter(event_date=event_date)
        .select_related("team")
        .annotate(
            out_total=Sum(Case(When(score__hole__lte=9,  then="score__strokes"), output_field=IntegerField())),
            in_total =Sum(Case(When(score__hole__gte=10, then="score__strokes"), output_field=IntegerField())),
            holes_entered=Count("score__id", filter=Q(score__strokes__isnull=False)),
        )
    )

    def fmt_to_par(n: Optional[int]) -> Optional[str]:
        if n is None: return None
        if n == 0:    return "E"
        return f"+{n}" if n > 0 else str(n)

    rows = []
    for r in rounds:
        scored = list(
            Score.objects
            .filter(round=r, strokes__isnull=False)
            .values_list("hole", "strokes")
        )
        if scored:
            strokes_sum = sum(s for _, s in scored)
            par_sum     = sum(par_by_hole.get(h, 0) for h, _ in scored)
            to_par_val  = strokes_sum - par_sum
            to_par_str  = fmt_to_par(to_par_val)
            total_disp  = (r.out_total or 0) + (r.in_total or 0)
        else:
            to_par_val  = None
            to_par_str  = None
            total_disp  = None

        rows.append({
            "team_id": r.team_id,
            "team_name": r.team.name,
            "out": r.out_total,
            "in":  r.in_total,
            "total": total_disp,
            "to_par": to_par_val,
            "to_par_str": to_par_str,
            "holes_entered": r.holes_entered or 0,
            # rank will be filled below
        })

    # Sort: to-par first, then total, then name; None goes last
    rows.sort(key=lambda x: (
        x["to_par"] is None, x["to_par"] if x["to_par"] is not None else 10**9,
        x["total"]  is None, x["total"]  if x["total"]  is not None else 10**9,
        x["team_name"]
    ))

    # Assign ranks without touching row["rank"] during counting
    def rank_value(row):
        return row["to_par"] if row["to_par"] is not None else (
               row["total"]  if row["total"]  is not None else None)

    vals = [rank_value(r) for r in rows if rank_value(r) is not None]
    freq = Counter(vals)

    place = 0
    last_val = object()
    for idx, row in enumerate(rows, start=1):
        val = rank_value(row)
        if val is None:
            row["rank"] = None
            continue
        if val != last_val:
            place = idx
            last_val = val
        row["rank"] = f"T-{place}" if freq[val] > 1 else str(place)

    return rows



@login_required
def leaderboard_page(request):
    evt = current_event_date()
    rows = _leaderboard_rows(evt)
    # (optional) gate visibility
    settings = EventSettings.load()
    if not settings.leaderboard_public and not request.user.is_staff:
        return HttpResponseForbidden("Leaderboard is not public.")
    return render(request, "outing/leaderboard.html", {"rows": rows, "event_date": evt})

@login_required
def leaderboard_partial(request):
    evt = current_event_date()
    rows = _leaderboard_rows(evt)
    return render(request, "outing/_leaderboard_table.html", {"rows": rows})


@login_required
def dashboard_view(request):
    """
    Players: show link to *my team* scorecard (if on a team), plus Leaderboard.
    Staff:   show all teams with 'Open' links, plus link to Team Manager.
    """
    my_team = Team.objects.filter(players__user=request.user).first()
    teams = Team.objects.all().order_by("tee_time", "name") if request.user.is_staff else None
    return render(request, "outing/dashboard.html", {
        "my_team": my_team,
        "teams": teams,
        "event_date": current_event_date(),
    })


def _is_staff(u): return u.is_staff

@user_passes_test(_is_staff)
def team_manage_view(request):
    """
    Single page to:
      - set team tee time
      - add available players to a team
      - remove a player from a team
    """
    # available = players who are marked 'playing' and not currently on any team
    assigned_ids = list(
        Team.objects.values_list("players__id", flat=True)
    )
    assigned_ids = [pid for pid in assigned_ids if pid]  # drop Nones
    available_players = Player.objects.filter(playing=True).exclude(id__in=assigned_ids).order_by("last_name", "first_name")
    teams = Team.objects.prefetch_related("players").order_by("tee_time", "name")

    if request.method == "POST":
        action = request.POST.get("action")
        team_id = request.POST.get("team_id")
        team = get_object_or_404(Team, pk=team_id) if team_id else None

        if action == "set_tee":
            ts = (request.POST.get("tee_time") or "").strip()
            if ts:
                try:
                    # Expecting "HH:MM" 24h
                    hh, mm = [int(x) for x in ts.split(":")]
                    team.tee_time = time(hh, mm)
                except Exception:
                    messages.error(request, f"Invalid time: {ts}. Use HH:MM.")
                else:
                    team.save(update_fields=["tee_time"])
                    messages.success(request, f"Tee time set for {team.name}.")
            return redirect("team_manage")

        if action == "add_player":
            pid = request.POST.get("player_id")
            p = get_object_or_404(Player, pk=pid)
            team.players.add(p)
            messages.success(request, f"Added {p.first_name} {p.last_name} to {team.name}.")
            return redirect("team_manage")

        if action == "remove_player":
            pid = request.POST.get("player_id")
            p = get_object_or_404(Player, pk=pid)
            team.players.remove(p)
            messages.success(request, f"Removed {p.first_name} {p.last_name} from {team.name}.")
            return redirect("team_manage")

    # GET: render
    return render(request, "outing/team_manage.html", {
        "teams": teams,
        "available_players": available_players,
    })


def _is_staff(u): return u.is_staff

@user_passes_test(_is_staff)
def sms_broadcast_view(request):
    """Staff page to send an SMS to all 'playing' players or a single team."""
    teams = Team.objects.order_by("name")

    if request.method == "POST":
        audience    = request.POST.get("audience", "all")
        team_id     = request.POST.get("team_id")
        message     = (request.POST.get("message") or "").strip()
        test_number = (request.POST.get("test_number") or "").strip()
        dry_run     = bool(request.POST.get("dry_run"))
        add_stop    = bool(request.POST.get("add_stop"))

        if not message:
            messages.error(request, "Message is required.")
            return redirect("sms_broadcast")

        if add_stop:
            message = f"{message}\n\nReply STOP to opt out."

        # Build recipients
        phones_qs = []
        if audience == "all":
            phones_qs = Player.objects.filter(playing=True).values_list("phone", flat=True)
        elif audience == "team":
            team = get_object_or_404(Team, pk=team_id)
            phones_qs = team.players.filter(playing=True).values_list("phone", flat=True)
        elif audience == "test" and test_number:
            phones_qs = [test_number]
        else:
            messages.error(request, "Select an audience or provide a test number.")
            return redirect("sms_broadcast")

        recipients = prepare_recipients(phones_qs)
        if not recipients:
            messages.error(request, "No valid phone numbers found.")
            return redirect("sms_broadcast")

        if not have_twilio_creds():
            messages.error(request, "Missing TWILIO settings in environment.")
            return redirect("sms_broadcast")

        result = broadcast(recipients, message, dry_run=dry_run)
        if result["errors"]:
            messages.warning(request, f"Sent {result['sent']} message(s); {len(result['errors'])} error(s).")
        else:
            messages.success(request, f"{'Would send' if dry_run else 'Sent'} {result['sent']} message(s).")
        return redirect("sms_broadcast")

    # GET
    return render(request, "outing/sms_broadcast.html", {"teams": teams})


@require_http_methods(["GET","POST"])
def magic_request_view(request):
    """
    Page where a user enters their phone; we text them a magic sign-in link.
    We map phone -> Player -> User.
    """
    if request.method == "POST":
        raw_phone = (request.POST.get("phone") or "").strip()
        nums = prepare_recipients([raw_phone])
        if not nums:
            messages.error(request, "Please enter a valid US phone number.")
            return redirect("magic_request")

        phone = nums[0]
        # Find the player by phone (exact match after normalization)
        player = Player.objects.filter(phone__iexact=raw_phone).first() or \
                 Player.objects.filter(phone__iexact=phone.replace("+1","")).first() or \
                 Player.objects.filter(phone__icontains=raw_phone[-10:]).first()

        if not player or not player.user:
            messages.error(request, "We couldn’t find an account for that phone.")
            return redirect("magic_request")

        if not have_twilio_creds():
            messages.error(request, "SMS is not configured.")
            return redirect("magic_request")

        url = create_magic_link(request, player.user, ttl_seconds=15*60, sent_to=phone)
        body = f"Beer Open sign-in link (15 min): {url}"
        res = broadcast([phone], body, dry_run=False)

        if res["errors"]:
            messages.error(request, f"Send failed to {phone}.")
        else:
            messages.success(request, f"Text sent to {phone}.")
        return redirect("magic_request")

    return render(request, "outing/magic_request.html")

def magic_login_view(request, token_id: int, raw: str):
    tok = validate_token(token_id, raw)
    if not tok:
        messages.error(request, "This sign-in link is invalid or expired.")
        return redirect("magic_request")

    # Mark used, log in
    tok.used_at = now()
    tok.save(update_fields=["used_at"])
    login(request, tok.user)
    messages.success(request, "You’re signed in.")
    return redirect("dashboard")
