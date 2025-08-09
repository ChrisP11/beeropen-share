from django.urls import path
from django.shortcuts import redirect
from . import views

def root_redirect(request):
    return redirect("dashboard") if request.user.is_authenticated else redirect("login")

urlpatterns = [
    path("", root_redirect, name="home"),
    path("", views.dashboard_view, name="dashboard"),
    path("team/<int:team_id>/scorecard/", views.team_scorecard_view, name="team_scorecard"),
    path("leaderboard/", views.leaderboard_page, name="leaderboard"),
    path("leaderboard/partial/", views.leaderboard_partial, name="leaderboard_partial"),

    # staff-only team management
    path("admin/teams/manage/", views.team_manage_view, name="team_manage"),    

    # SMS magic-link auth
    path("accounts/magic/", views.magic_request_view, name="magic_request"),
    path("accounts/magic/<int:token_id>/<str:raw>/", views.magic_login_view, name="magic_login"),

    # SMS broadcast MVP
    path("admin/sms/broadcast/", views.sms_broadcast_view, name="sms_broadcast"),

]
