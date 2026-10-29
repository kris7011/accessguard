from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from accessguard_web.accounts import views

app_name = "accounts"
urlpatterns = [
    path("signup/", views.signup, name="signup"),
    path("login/", views.ThrottledLoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path(
        "password-reset/",
        auth_views.PasswordResetView.as_view(
            template_name="accounts/password_reset.html",
            email_template_name="accounts/password_reset_email.txt",
            success_url=reverse_lazy("accounts:password_reset_done"),
        ),
        name="password_reset",
    ),
    path(
        "password-reset/sent/",
        auth_views.PasswordResetDoneView.as_view(template_name="accounts/password_reset_done.html"),
        name="password_reset_done",
    ),
    path(
        "reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(
            template_name="accounts/password_reset_confirm.html",
            success_url=reverse_lazy("accounts:login"),
        ),
        name="password_reset_confirm",
    ),
    path("organizations/new/", views.create_organization_view, name="create_organization"),
    path("organizations/<int:organization_id>/switch/", views.switch, name="switch"),
    path("settings/", views.settings_view, name="settings"),
    path("settings/invite/", views.invite, name="invite"),
    path("settings/members/<int:membership_id>/", views.change_role, name="change_role"),
    path("settings/delete/", views.delete_organization, name="delete_organization"),
    path("invite/<str:token>/", views.accept_invite, name="accept_invite"),
]
