from django.urls import path

from . import views


urlpatterns = [
    path("calendar/", views.graphics, name="calendar"),
    path("calendar/vacation-request-preview/", views.vacation_request_preview, name="vacation_request_preview"),
    path("applications/", views.applications, name="applications"),
    path("applications/<int:pk>/", views.vacation_detail, name="vacation_detail"),
    path("applications/<int:pk>/approve/", views.approve_vacation, name="approve_vacation"),
    path("applications/<int:pk>/reject/", views.reject_vacation, name="reject_vacation"),
    path("applications/<int:pk>/delete/", views.delete_vacation, name="delete_vacation"),
    path(
        "calendar/schedule-items/<int:item_id>/transfer/",
        views.create_schedule_change,
        name="schedule_change_request_create",
    ),
    path(
        "applications/transfers/<int:pk>/approve/",
        views.approve_schedule_change,
        name="schedule_change_approve",
    ),
    path(
        "applications/transfers/<int:pk>/reject/",
        views.reject_schedule_change,
        name="schedule_change_reject",
    ),
    path("analytics/", views.analytics, name="analytics"),
]


