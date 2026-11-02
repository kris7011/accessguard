from django.urls import path

from accessguard_web.projects import views

app_name = "projects"
urlpatterns = [
    path("new/", views.project_new, name="new"),
    path("<int:project_id>/", views.detail, name="detail"),
    path("<int:project_id>/setup/target/", views.setup_target, name="setup_target"),
    path("<int:project_id>/setup/users/", views.setup_users, name="setup_users"),
    path("<int:project_id>/setup/rules/", views.setup_rules, name="setup_rules"),
    path("<int:project_id>/setup/limits/", views.setup_limits, name="setup_limits"),
    path("<int:project_id>/import/", views.import_openapi, name="import_openapi"),
    path("<int:project_id>/import/apply/", views.apply_openapi, name="apply_openapi"),
    path("<int:project_id>/spec.json", views.export_spec, name="export_spec"),
    path("<int:project_id>/demo-variant/", views.demo_variant, name="demo_variant"),
    path("<int:project_id>/delete/", views.delete, name="delete"),
]
