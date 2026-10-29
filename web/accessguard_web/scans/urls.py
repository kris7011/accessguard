from django.urls import path

from accessguard_web.scans import views

app_name = "scans"
urlpatterns = [
    path("projects/<int:project_id>/scan/", views.review, name="review"),
    path("scans/<int:scan_id>/", views.detail, name="detail"),
    path("scans/<int:scan_id>/status.json", views.status_json, name="status"),
    path("scans/<int:scan_id>/cancel/", views.cancel, name="cancel"),
    path("scans/<int:scan_id>/report.json", views.report_json, name="report_json"),
    path("findings/", views.findings, name="findings"),
    path("findings/<int:finding_id>/", views.finding_detail, name="finding"),
    path("findings/<int:finding_id>/triage/", views.triage, name="triage"),
    path("findings/<int:finding_id>/explain/", views.explain, name="explain"),
    path("runners/", views.runners, name="runners"),
    path("runners/new/", views.runner_create, name="runner_create"),
    path("runners/<int:runner_id>/revoke/", views.runner_revoke, name="runner_revoke"),
]
