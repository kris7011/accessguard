"""URL routes. Product pages live under /app/, the runner API under /api/runner/v1/."""

from django.contrib import admin
from django.urls import include, path

from accessguard_web import conf
from accessguard_web.billing import views as billing_views
from accessguard_web.projects import views as project_views
from accessguard_web.scans import runner_api

runner_patterns = [
    path("claim", runner_api.claim, name="claim"),
    path("jobs/<int:scan_id>/heartbeat", runner_api.heartbeat, name="heartbeat"),
    path("jobs/<int:scan_id>/report", runner_api.report, name="report"),
    path("jobs/<int:scan_id>/fail", runner_api.fail, name="fail"),
]

urlpatterns = [
    path("", include("accessguard_web.pages.urls")),
    path("accounts/", include("accessguard_web.accounts.urls")),
    path("app/", project_views.dashboard, name="dashboard"),
    path("app/projects/", include("accessguard_web.projects.urls")),
    path("app/", include("accessguard_web.scans.urls")),
    path("app/billing/", include("accessguard_web.billing.urls")),
    path("api/runner/v1/", include((runner_patterns, "runner_api"))),
    path("webhooks/stripe/", billing_views.webhook, name="stripe_webhook"),
    path(conf.product()["ADMIN_PATH"], admin.site.urls),
]
