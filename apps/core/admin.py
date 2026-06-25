from django.contrib import admin

from apps.core.models import DemoBaselineSnapshot, DemoDataResetJob, Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("title", "recipient", "event_type", "status", "requires_action", "created_at")
    list_filter = ("status", "event_type", "requires_action", "priority")
    search_fields = ("title", "message", "recipient__last_name", "recipient__login", "dedupe_key")
    readonly_fields = ("created_at", "updated_at", "read_at", "done_at")


@admin.register(DemoBaselineSnapshot)
class DemoBaselineSnapshotAdmin(admin.ModelAdmin):
    list_display = ("key", "planning_year", "seed_value", "updated_at")
    search_fields = ("key",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(DemoDataResetJob)
class DemoDataResetJobAdmin(admin.ModelAdmin):
    list_display = ("id", "status", "preset", "employee_count", "history_years", "seed_value", "progress_percent", "process_id", "created_at", "updated_at")
    list_filter = ("status", "preset")
    search_fields = ("id", "token", "seed_value", "stage_label", "message", "error_message")
    readonly_fields = ("created_at", "updated_at", "started_at", "finished_at")
