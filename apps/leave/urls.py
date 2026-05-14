from django.urls import path

from . import views


urlpatterns = [
    path("calendar/", views.graphics, name="calendar"),
    path("calendar/planning/", views.schedule_planning_current, name="schedule_planning_current"),
    path("calendar/planning/<int:year>/", views.schedule_planning, name="schedule_planning"),
    path("calendar/vacation-request-preview/", views.vacation_request_preview, name="vacation_request_preview"),
    path(
        "calendar/preferences/start/",
        views.start_vacation_preferences_collection,
        name="preferences_collection_start",
    ),
    path(
        "calendar/preferences/<int:year>/finish/",
        views.finish_vacation_preferences_collection,
        name="preferences_collection_finish",
    ),
    path(
        "preferences/<int:year>/readiness/",
        views.preference_collection_readiness,
        name="preference_collection_readiness",
    ),
    path(
        "calendar/drafts/<int:year>/create/",
        views.create_schedule_draft,
        name="schedule_draft_create",
    ),
    path(
        "calendar/drafts/<int:year>/auto-place/",
        views.auto_place_schedule_draft_remaining,
        name="schedule_draft_auto_place",
    ),
    path(
        "calendar/drafts/<int:year>/auto-place/jobs/<int:job_id>/status/",
        views.auto_place_schedule_draft_status,
        name="schedule_draft_auto_place_status",
    ),
    path(
        "calendar/drafts/<int:year>/auto-place/preview/",
        views.auto_place_schedule_draft_preview,
        name="schedule_draft_auto_place_preview",
    ),
    path(
        "calendar/drafts/<int:year>/employees/<int:employee_id>/day-calculation/",
        views.schedule_draft_day_calculation,
        name="schedule_draft_day_calculation",
    ),
    path(
        "calendar/drafts/<int:year>/items/<int:item_id>/review/",
        views.schedule_draft_item_review,
        name="schedule_draft_item_review",
    ),
    path(
        "calendar/drafts/<int:year>/manual-place/<int:employee_id>/preview/",
        views.manual_schedule_draft_preview,
        name="schedule_draft_manual_preview",
    ),
    path(
        "calendar/drafts/<int:year>/manual-place/<int:employee_id>/preview-package/",
        views.manual_schedule_draft_package_preview,
        name="schedule_draft_manual_package_preview",
    ),
    path(
        "calendar/drafts/<int:year>/manual-place/<int:employee_id>/suggestions/",
        views.manual_schedule_draft_suggestions,
        name="schedule_draft_manual_suggestions",
    ),
    path(
        "calendar/drafts/<int:year>/manual-place/<int:employee_id>/",
        views.manual_place_schedule_draft_item,
        name="schedule_draft_manual_place",
    ),
    path(
        "calendar/drafts/<int:year>/urgent-closures/<int:employee_id>/create/",
        views.create_urgent_closure,
        name="urgent_closure_create",
    ),
    path(
        "calendar/drafts/<int:year>/urgent-closures/<int:employee_id>/preview/",
        views.urgent_closure_preview,
        name="urgent_closure_preview",
    ),
    path(
        "calendar/drafts/<int:year>/items/<int:item_id>/feedback/",
        views.schedule_draft_candidate_feedback,
        name="schedule_draft_candidate_feedback",
    ),
    path(
        "calendar/drafts/<int:year>/",
        views.schedule_draft_detail,
        name="schedule_draft_detail",
    ),
    path("preferences/<int:year>/", views.vacation_preferences, name="vacation_preferences"),
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
        "calendar/schedule-items/<int:item_id>/transfer-preview/",
        views.schedule_change_preview,
        name="schedule_change_request_preview",
    ),
    path(
        "applications/transfers/<int:pk>/",
        views.schedule_change_detail,
        name="schedule_change_detail",
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
    path(
        "applications/urgent-closures/<int:pk>/",
        views.urgent_closure_detail,
        name="urgent_closure_detail",
    ),
    path(
        "applications/urgent-closures/<int:pk>/manager-approve/",
        views.approve_urgent_closure_manager,
        name="urgent_closure_manager_approve",
    ),
    path(
        "applications/urgent-closures/<int:pk>/employee-accept/",
        views.accept_urgent_closure_employee,
        name="urgent_closure_employee_accept",
    ),
    path(
        "applications/urgent-closures/<int:pk>/employee-propose/",
        views.propose_urgent_closure_employee,
        name="urgent_closure_employee_propose",
    ),
    path(
        "applications/urgent-closures/<int:pk>/finalize/",
        views.finalize_urgent_closure_hr,
        name="urgent_closure_finalize",
    ),
    path(
        "applications/urgent-closures/<int:pk>/reject/",
        views.reject_urgent_closure_request,
        name="urgent_closure_reject",
    ),
    path("analytics/", views.analytics, name="analytics"),
]


