from django.urls import path

from accessguard_web.pages import views

app_name = "pages"
urlpatterns = [
    path("", views.landing, name="landing"),
    path("pricing/", views.pricing, name="pricing"),
    path("example-report/", views.example_report, name="example_report"),
    path("help/", views.help_page, name="help"),
    path("help/<slug:slug>/", views.help_page, name="help_page"),
    path("terms/", views.terms, name="terms"),
    path("healthz", views.healthz, name="healthz"),
    path("readyz", views.readyz, name="readyz"),
]
