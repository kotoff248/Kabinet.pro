import hashlib
import json

from django.db.models import Count, Max
from django.utils import timezone

from apps.employees.models import Employees
from apps.leave.models import (
    VacationSchedule,
    VacationScheduleCandidate,
    VacationScheduleCandidateFeedback,
    VacationScheduleCandidatePackage,
    VacationScheduleItem,
)


TRAINING_SCHEDULE_STATUSES = (
    VacationSchedule.STATUS_ARCHIVED,
    VacationSchedule.STATUS_APPROVED,
)


def _iso(value):
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _feedback_role_for(employee):
    if employee is not None and employee.role == Employees.ROLE_HR:
        return VacationScheduleCandidateFeedback.ROLE_HR
    if employee is not None and employee.role == Employees.ROLE_DEPARTMENT_HEAD:
        return VacationScheduleCandidateFeedback.ROLE_DEPARTMENT_HEAD
    return VacationScheduleCandidateFeedback.ROLE_ENTERPRISE_HEAD


def training_schedule_queryset(*, max_schedule_year=None):
    max_schedule_year = int(max_schedule_year or timezone.localdate().year)
    return VacationSchedule.objects.filter(
        year__lte=max_schedule_year,
        status__in=TRAINING_SCHEDULE_STATUSES,
    ).order_by("year", "id")


def build_training_source_summary(*, max_schedule_year=None):
    max_schedule_year = int(max_schedule_year or timezone.localdate().year)
    schedules = list(training_schedule_queryset(max_schedule_year=max_schedule_year))
    schedule_ids = [schedule.id for schedule in schedules]

    candidate_stats = {
        row["schedule_id"]: row
        for row in VacationScheduleCandidate.objects.filter(schedule_id__in=schedule_ids)
        .values("schedule_id")
        .annotate(count=Count("id"), max_id=Max("id"), max_created_at=Max("created_at"))
    }
    package_stats = {
        row["schedule_id"]: row
        for row in VacationScheduleCandidatePackage.objects.filter(schedule_id__in=schedule_ids)
        .values("schedule_id")
        .annotate(count=Count("id"), max_id=Max("id"), max_created_at=Max("created_at"))
    }
    item_stats = {
        row["schedule_id"]: row
        for row in VacationScheduleItem.objects.filter(schedule_id__in=schedule_ids)
        .values("schedule_id")
        .annotate(count=Count("id"), max_id=Max("id"))
    }
    feedback_stats = {
        row["schedule_item__schedule_id"]: row
        for row in VacationScheduleCandidateFeedback.objects.filter(schedule_item__schedule_id__in=schedule_ids)
        .values("schedule_item__schedule_id")
        .annotate(count=Count("id"), max_id=Max("id"), max_updated_at=Max("updated_at"))
    }

    schedule_payload = []
    for schedule in schedules:
        candidate_row = candidate_stats.get(schedule.id, {})
        package_row = package_stats.get(schedule.id, {})
        item_row = item_stats.get(schedule.id, {})
        feedback_row = feedback_stats.get(schedule.id, {})
        schedule_payload.append(
            {
                "id": schedule.id,
                "year": schedule.year,
                "status": schedule.status,
                "approved_at": _iso(schedule.approved_at),
                "generated_at": _iso(schedule.generated_at),
                "items_count": int(item_row.get("count") or 0),
                "items_max_id": item_row.get("max_id") or 0,
                "candidates_count": int(candidate_row.get("count") or 0),
                "candidates_max_id": candidate_row.get("max_id") or 0,
                "candidates_max_created_at": _iso(candidate_row.get("max_created_at")),
                "packages_count": int(package_row.get("count") or 0),
                "packages_max_id": package_row.get("max_id") or 0,
                "packages_max_created_at": _iso(package_row.get("max_created_at")),
                "feedback_count": int(feedback_row.get("count") or 0),
                "feedback_max_id": feedback_row.get("max_id") or 0,
                "feedback_max_updated_at": _iso(feedback_row.get("max_updated_at")),
            }
        )

    fingerprint_payload = {
        "max_schedule_year": max_schedule_year,
        "schedules": schedule_payload,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "max_schedule_year": max_schedule_year,
        "schedule_ids": schedule_ids,
        "years": sorted({schedule.year for schedule in schedules}),
        "schedule_count": len(schedules),
        "candidates_count": sum(row["candidates_count"] for row in schedule_payload),
        "packages_count": sum(row["packages_count"] for row in schedule_payload),
        "feedback_count": sum(row["feedback_count"] for row in schedule_payload),
        "source_fingerprint": fingerprint,
    }


def ensure_approved_schedule_training_feedback(schedule, *, actor=None):
    reviewer = schedule.approved_by or actor
    if reviewer is None:
        return 0
    if schedule.status != VacationSchedule.STATUS_APPROVED:
        return 0

    reviewer_role = _feedback_role_for(reviewer)
    created_or_updated = 0
    items = (
        VacationScheduleItem.objects.select_related("selected_candidate", "generation_run")
        .filter(
            schedule=schedule,
            status=VacationScheduleItem.STATUS_APPROVED,
            vacation_type="paid",
            selected_candidate__isnull=False,
        )
        .order_by("employee_id", "start_date", "id")
    )
    for item in items:
        _, created = VacationScheduleCandidateFeedback.objects.update_or_create(
            schedule_item=item,
            reviewer=reviewer,
            defaults={
                "candidate": item.selected_candidate,
                "generation_run": item.generation_run,
                "reviewer_role": reviewer_role,
                "decision": VacationScheduleCandidateFeedback.DECISION_AGREE,
                "comment": "График утверждён и добавлен как обучающий пример.",
                "score_snapshot": item.ai_score,
                "confidence_snapshot": item.ai_confidence,
                "model_version_snapshot": item.ai_model_version,
                "explanation_snapshot": item.ai_explanation,
            },
        )
        created_or_updated += 1 if created else 0
    return created_or_updated
