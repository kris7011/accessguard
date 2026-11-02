from django.urls import path

from accessguard_web.billing import views

app_name = "billing"
urlpatterns = [
    path("", views.overview, name="overview"),
    path("checkout/<str:plan_id>/", views.checkout, name="checkout"),
    path("portal/", views.portal, name="portal"),
]
