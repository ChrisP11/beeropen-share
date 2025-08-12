from django.urls import path
from django.shortcuts import redirect
from . import views

def root_redirect(request):
    return redirect("dashboard") if request.user.is_authenticated else redirect("login")

urlpatterns = [

    # Public landing page
    path("", views.home_view, name="home"),

    # App pages
    path("app/", views.dashboard_view, name="dashboard"),
    path("team/<int:team_id>/scorecard/", views.team_scorecard_view, name="team_scorecard"),
    path("leaderboard/", views.leaderboard_page, name="leaderboard"),
    path("leaderboard/partial/", views.leaderboard_partial, name="leaderboard_partial"),

    # staff-only team management
    path("admin/teams/manage/", views.team_manage_view, name="team_manage"),    
    path("admin/players/bulk-import/", views.player_bulk_import_view, name="player_bulk_import"),
    path("admin/sms/replies/",   views.sms_replies_view,   name="sms_replies"),  
    path("admin/players/sizes/", views.player_sizes_view, name="player_sizes"),

    # SMS magic-link auth
    path("accounts/magic/", views.magic_request_view, name="magic_request"),
    path("accounts/magic/<int:token_id>/<str:raw>/", views.magic_login_view, name="magic_login"),

    # SMS broadcast MVP
    path("admin/sms/broadcast/", views.sms_broadcast_view, name="sms_broadcast"),
    path("twilio/sms/inbound/", views.twilio_inbound_view, name="twilio_inbound"), 
    


]
