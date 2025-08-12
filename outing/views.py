import csv, io
from datetime import date, time
from django.utils.timezone import now
from django.utils import timezone

from typing import Dict, List, Optional
from collections import Counter, defaultdict

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponseForbidden, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render, redirect
from django.db.models import Sum, Case, When, IntegerField, Count, Q
from django.views.decorators.http import require_POST, require_http_methods
from django.views.decorators.csrf import csrf_exempt

from .models import Team, Round, Score, DriveUsed, Player, CoursePar, EventSettings, MagicLoginToken, SMSResponse
from .sms_utils import prepare_recipients, broadcast, have_twilio_creds
from .magic_utils import create_magic_link, validate_token


def current_event_date():
    return EventSettings.load().event_date


# public landing page
def home_view(request):
    evt_date = EventSettings.load().event_date  # dynamic date
    return render(request, "outing/home.html", {"event_date": evt_date})


@staff_member_required
def admin_hub_view(request):
    return render(request, "outing/admin_hub.html")


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


def _collect_recipients_from_players(qs):
    """
    Per-player normalization so we can report who’s missing/invalid.
    Returns (send_to_numbers, missing_names, invalid_names).
    """
    send_to = []
    missing = []
    invalid = []
    seen = set()

    def label(p):
        full = f"{(p.first_name or '').strip()} {(p.last_name or '').strip()}".strip()
        return full or (p.email or f"Player#{p.id}")

    for p in qs:
        raw = (p.phone or "").strip()
        if not raw:
            missing.append(label(p))
            continue
        norm = prepare_recipients([raw])  # → [] if invalid, [E164] if valid
        if not norm:
            invalid.append(f"{label(p)} ({raw})")
            continue
        n = norm[0]
        if n not in seen:
            seen.add(n)
            send_to.append(n)

    return send_to, missing, invalid

@staff_member_required
def sms_broadcast_view(request):
    teams = Team.objects.order_by("name")

    if request.method == "POST":
        audience    = request.POST.get("audience", "all")   # "all" | "team" | "test"
        team_id     = request.POST.get("team_id")
        test_number = (request.POST.get("test_number") or "").strip()
        body        = (request.POST.get("message") or "").strip()
        add_stop    = bool(request.POST.get("add_stop"))
        dry_run     = bool(request.POST.get("dry_run"))

        if not body:
            messages.error(request, "Message is required.")
            return redirect("sms_broadcast")

        if add_stop:
            body = body.rstrip() + " Reply STOP to opt out."

        if audience == "test":
            recipients = prepare_recipients([test_number])
            if not recipients:
                messages.error(request, "Enter a valid test number (e.g. +13125551212).")
                return redirect("sms_broadcast")
            body = "[TEST] " + body
            missing = []
            invalid = []

        elif audience == "team":
            if not team_id:
                messages.error(request, "Choose a team.")
                return redirect("sms_broadcast")
            team = get_object_or_404(Team, pk=team_id)
            qs = (
                Player.objects
                .filter(teams=team, playing=True)
                .only("id", "first_name", "last_name", "email", "phone")
            )
            recipients, missing, invalid = _collect_recipients_from_players(qs)
            if not recipients:
                messages.error(request, f"No valid mobile numbers for team {team.name}.")
                return redirect("sms_broadcast")

        else:
            # audience == "all" → ONLY players marked as playing
            qs = (
                Player.objects
                .filter(playing=True)
                .only("id", "first_name", "last_name", "email", "phone")
            )
            recipients, missing, invalid = _collect_recipients_from_players(qs)
            if not recipients:
                messages.error(request, "No valid mobile numbers for playing participants.")
                return redirect("sms_broadcast")

        if not have_twilio_creds():
            messages.error(request, "Twilio is not configured on this environment.")
            return redirect("sms_broadcast")

        res = broadcast(recipients, body, dry_run=dry_run)
        verb = "Would send to" if dry_run else "Sent to"
        messages.success(request, f"{verb} {len(res['sent'])} number(s).")

        # Summaries for skipped folks (always show)
        if missing:
            preview = ", ".join(missing[:10]) + (f", +{len(missing)-10} more" if len(missing) > 10 else "")
            messages.info(request, f"Skipped (no phone): {len(missing)} — {preview}")
        else:
            messages.info(request, "Skipped (no phone): 0")

        if invalid:
            preview = ", ".join(invalid[:10]) + (f", +{len(invalid)-10} more" if len(invalid) > 10 else "")
            messages.warning(request, f"Skipped (invalid phone): {len(invalid)} — {preview}")
        else:
            messages.info(request, "Skipped (invalid phone): 0")

        # Preserve carrier/API error reporting
        if res["errors"]:
            sample = ", ".join(e[0] for e in res["errors"][:5])
            messages.error(request, f"Carrier/API errors for {len(res['errors'])} recipient(s). Example(s): {sample}")

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


SIZE_MAP = {
    "XS": {"XS", "XSMALL", "X-SMALL"},
    "S":  {"S", "SM", "SMALL"},
    "M":  {"M", "MED", "MEDIUM"},
    "L":  {"L", "LG", "LARGE"},
    "XL": {"XL", "X-LARGE"},
    "2XL": {"2X", "XXL", "2XL", "XX-LARGE"},
    "3XL": {"3X", "XXXL", "3XL", "XXX-LARGE"},
}
def _normalize_size(text: str) -> str | None:
    t = "".join(ch for ch in text.upper() if ch.isalnum() or ch in {" ", "-"})
    tokens = [tok for tok in t.replace("-", " ").split() if tok]
    # allow bare "L", "XL", etc. or phrases like "size xl", "shirt 2xl"
    for tok in tokens[::-1]:  # prefer last token
        for canon, synonyms in SIZE_MAP.items():
            if tok in synonyms:
                return canon
    return None

@csrf_exempt
def twilio_inbound_view(request):
    if request.method != "POST":
        return HttpResponse(status=405)

    body = (request.POST.get("Body") or "").strip()
    body_lc = body.lower()
    from_raw = request.POST.get("From") or ""
    nums = prepare_recipients([from_raw])
    from_norm = nums[0] if nums else None

    # Resolve player by last 10 digits
    player = None
    if from_norm:
        ten = from_norm[-10:]
        player = Player.objects.filter(phone__icontains=ten).first()

    # Log every inbound
    SMSResponse.objects.create(
        from_number=from_norm or (from_raw or ""),
        message_body=body,
        player=player,
        campaign="2025_shirts"
    )

    if "help" in body_lc:
        return _twiml("Reply 'link' for a sign-in link. Reply S/M/L/XL/2XL/3XL to set your shirt size.")

    if "link" in body_lc and from_norm and player and player.user:
        if not have_twilio_creds():
            return _twiml("SMS sending not configured.")
        url = create_magic_link(request, player.user, ttl_seconds=15*60, sent_to=from_norm)
        return _twiml(f"Beer Open sign-in link (15 min): {url}")

    # Size capture (works even without keyword)
    size = _normalize_size(body)
    if size and player:
        player.shirt_size = size
        player.save(update_fields=["shirt_size"])
        return _twiml(f"Got it — your shirt size is set to {size}. Thanks!")

    return _twiml("Thanks! Reply S/M/L/XL/2XL/3XL to set your shirt size, or 'link' for a sign-in link.")

def _twiml(message: str) -> HttpResponse:
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{message}</Message></Response>'
    return HttpResponse(xml, content_type="application/xml")


@staff_member_required
def sms_replies_view(request):
    rows = (SMSResponse.objects
            .select_related("player")
            .order_by("-received_at")[:200])
    return render(request, "outing/sms_replies.html", {"rows": rows})


@staff_member_required
def player_sizes_view(request):
    players = Player.objects.prefetch_related("teams").order_by("last_name", "first_name")
    return render(request, "outing/player_sizes.html", {"players": players})


@staff_member_required
def player_bulk_import_view(request):
    """
    Upload or paste CSV with columns like:
    First Name,Last Name,Email,Phone,Status
    (extra columns like 'Name' are ignored)

    Rules:
      - playing = True only if Status == "Yes" (case-insensitive exact match)
      - phone normalized to +1XXXXXXXXXX when possible
      - match existing players by Email (preferred), else by Phone (last 10), else by First+Last
    """
    results = []
    created = updated = skipped = 0

    if request.method == "POST":
        dry_run = bool(request.POST.get("dry_run"))
        content = ""

        f = request.FILES.get("csv_file")
        if f:
            content = f.read().decode("utf-8", errors="ignore")
        else:
            content = (request.POST.get("csv_text") or "").strip()

        if not content:
            messages.error(request, "Provide a CSV file or paste CSV text.")
            return redirect("player_bulk_import")

        reader = csv.DictReader(io.StringIO(content))
        # normalize headers a bit
        field_map = {k.lower().strip(): k for k in reader.fieldnames or []}

        def get(row, *keys):
            for k in keys:
                src = field_map.get(k.lower())
                if src and row.get(src) is not None:
                    return (row.get(src) or "").strip()
            return ""

        rownum = 0
        for row in reader:
            rownum += 1
            first = get(row, "First Name", "First")
            last  = get(row, "Last Name", "Last")
            email = get(row, "Email")
            phone_raw = get(row, "Phone", "Cell", "Mobile")
            status = get(row, "Status")

            if not any([first, last, email, phone_raw]):
                skipped += 1
                results.append((rownum, "skip(blank)", ""))
                continue

            # normalize phone
            phone = ""
            if phone_raw:
                nums = prepare_recipients([phone_raw])
                phone = nums[0] if nums else ""

            # ONLY 'Yes' means playing
            playing = (status.lower() == "yes")

            # find existing: email > phone > name
            q = Player.objects.all()
            candidate = None
            if email:
                candidate = q.filter(email__iexact=email).first()
            if not candidate and phone:
                ten = phone[-10:]
                candidate = q.filter(phone__icontains=ten).first()
            if not candidate and first and last:
                candidate = q.filter(first_name__iexact=first, last_name__iexact=last).first()

            if dry_run:
                action = "would_create" if not candidate else "would_update"
                results.append((rownum, action, f"{first} {last} | {email or '—'} | {phone or '—'} | playing={playing}"))
                continue

            if not candidate:
                p = Player(first_name=first or "", last_name=last or "", email=email or "", phone=phone or "", playing=playing)
                p.save()
                created += 1
                results.append((rownum, "created", f"id={p.id} {p.first_name} {p.last_name}"))
            else:
                changed = []
                if first and candidate.first_name != first:
                    candidate.first_name = first; changed.append("first")
                if last and candidate.last_name != last:
                    candidate.last_name = last; changed.append("last")
                if email and candidate.email != email:
                    candidate.email = email; changed.append("email")
                if phone and candidate.phone != phone:
                    candidate.phone = phone; changed.append("phone")
                if candidate.playing != playing:
                    candidate.playing = playing; changed.append("playing")

                if changed:
                    candidate.save()
                    updated += 1
                    results.append((rownum, "updated", ",".join(changed)))
                else:
                    skipped += 1
                    results.append((rownum, "noop", ""))

        if dry_run:
            messages.info(request, f"Dry run: would create {sum(1 for r in results if r[1]=='would_create')} and update {sum(1 for r in results if r[1]=='would_update')}.")
        else:
            messages.success(request, f"Import done: created {created}, updated {updated}, skipped {skipped}.")

    return render(request, "outing/player_bulk_import.html", {
        "results": results,
    })