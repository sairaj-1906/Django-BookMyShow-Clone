from django.urls import path

from . import dashboard_views as views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("export/<str:report>/", views.export_report, name="dashboard_export"),
]
