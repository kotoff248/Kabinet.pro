from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.services import (
    get_managed_department_id,
    is_department_head_employee,
    is_enterprise_head_employee,
    is_hr_employee,
)
from apps.leave.models import (
    VacationSchedule,
    VacationScheduleCandidateFeedback,
    VacationScheduleItem,
    VacationScheduleManualSuggestionCache,
)
from apps.leave.services.schedule_auto_place_jobs import get_active_schedule_auto_place_job


FEEDBACK_COMMENT_MAX_LENGTH = 800

FEEDBACK_DECISION_META = {
    VacationScheduleCandidateFeedback.DECISION_AGREE: {
        "label": "Согласен",
        "summary_label": "Согласовано",
        "icon": "thumb_up",
        "tone": "positive",
    },
    VacationScheduleCandidateFeedback.DECISION_NEEDS_CHANGE: {
        "label": "Нужна правка",
        "summary_label": "Нужна правка",
        "icon": "edit_note",
        "tone": "warning",
    },
    VacationScheduleCandidateFeedback.DECISION_REJECT: {
        "label": "Отклонить",
        "summary_label": "Отклонено",
        "icon": "block",
        "tone": "danger",
    },
}


@dataclass(frozen=True)
class ScheduleCandidateFeedbackResult:
    feedback: VacationScheduleCandidateFeedback
    item_returned_to_manual: bool = False
    employee_id: int | None = None


def _feedback_role_for_actor(actor):
    if is_hr_employee(actor):
        return VacationScheduleCandidateFeedback.ROLE_HR
    if is_department_head_employee(actor):
        return VacationScheduleCandidateFeedback.ROLE_DEPARTMENT_HEAD
    if is_enterprise_head_employee(actor):
        return VacationScheduleCandidateFeedback.ROLE_ENTERPRISE_HEAD
    return ""


def can_leave_schedule_candidate_feedback(actor, schedule_item):
    if actor is None or schedule_item is None:
        return False
    if not getattr(actor, "is_active_employee", True):
        return False
    if is_hr_employee(actor) or is_enterprise_head_employee(actor):
        return True
    if is_department_head_employee(actor):
        managed_department_id = get_managed_department_id(actor)
        employee = getattr(schedule_item, "employee", None)
        return managed_department_id is not None and getattr(employee, "department_id", None) == managed_department_id
    return False


def can_return_rejected_schedule_item_to_manual(actor, schedule_item):
    if schedule_item is None or actor is None:
        return False
    return (
        is_hr_employee(actor)
        and schedule_item.status == VacationScheduleItem.STATUS_DRAFT
        and schedule_item.schedule.status == VacationSchedule.STATUS_DRAFT
    )


def _feedback_summary(feedback_entries):
    counts = {decision: 0 for decision in FEEDBACK_DECISION_META}
    for feedback in feedback_entries:
        if feedback.decision in counts:
            counts[feedback.decision] += 1

    items = [
        {
            "decision": decision,
            "count": count,
            **FEEDBACK_DECISION_META[decision],
        }
        for decision, count in counts.items()
        if count
    ]
    return {
        "total": sum(counts.values()),
        "items": items,
        "counts": counts,
    }


def _feedback_payload(feedback):
    if feedback is None:
        return None

    meta = FEEDBACK_DECISION_META.get(feedback.decision, {})
    return {
        "decision": feedback.decision,
        "comment": feedback.comment,
        "role": feedback.reviewer_role,
        "reviewer_name": feedback.reviewer.full_name if feedback.reviewer_id else "",
        "updated_at": feedback.updated_at,
        **meta,
    }


def build_schedule_candidate_feedback_context(items, actor=None):
    item_ids = [item.id for item in items if item.id]
    context_by_item = {
        item.id: {
            "summary": _feedback_summary([]),
            "current": None,
            "can_submit": can_leave_schedule_candidate_feedback(actor, item),
            "reject_returns_to_manual": can_return_rejected_schedule_item_to_manual(actor, item),
        }
        for item in items
        if item.id
    }
    if not item_ids:
        return context_by_item

    feedback_entries = list(
        VacationScheduleCandidateFeedback.objects.select_related("reviewer")
        .filter(schedule_item_id__in=item_ids)
        .order_by("schedule_item_id", "decision", "-updated_at")
    )
    grouped_entries = {}
    for feedback in feedback_entries:
        grouped_entries.setdefault(feedback.schedule_item_id, []).append(feedback)

    actor_id = getattr(actor, "id", None)
    for item_id, entries in grouped_entries.items():
        current = None
        if actor_id is not None:
            current = next((feedback for feedback in entries if feedback.reviewer_id == actor_id), None)
        context_by_item.setdefault(
            item_id,
            {
                "summary": _feedback_summary([]),
                "current": None,
                "can_submit": False,
                "reject_returns_to_manual": False,
            },
        )
        context_by_item[item_id]["summary"] = _feedback_summary(entries)
        context_by_item[item_id]["current"] = _feedback_payload(current)

    return context_by_item


def _invalidate_schedule_draft_manual_suggestion_cache(schedule):
    if schedule is None:
        return 0

    schedule.manual_suggestion_cache_version = int(schedule.manual_suggestion_cache_version or 0) + 1
    schedule.manual_suggestion_cache_rebuilt_at = timezone.now()
    schedule.save(update_fields=["manual_suggestion_cache_version", "manual_suggestion_cache_rebuilt_at"])
    deleted_count, _ = VacationScheduleManualSuggestionCache.objects.filter(schedule=schedule).delete()
    return deleted_count


def _return_schedule_item_to_manual(schedule_item, actor):
    active_job = get_active_schedule_auto_place_job(
        year=schedule_item.schedule.year,
        schedule=schedule_item.schedule,
    )
    if active_job is not None:
        raise ValidationError("Дождитесь завершения действия «Добрать незакрытые дни», затем повторите отклонение.")

    actor_name = actor.full_name if actor else "HR"
    schedule_item.status = VacationScheduleItem.STATUS_CANCELLED
    schedule_item.was_changed_by_manager = True
    schedule_item.manager_comment = f"Отклонено при проверке рекомендации: {actor_name}."
    schedule_item.save(update_fields=["status", "was_changed_by_manager", "manager_comment"])
    _invalidate_schedule_draft_manual_suggestion_cache(schedule_item.schedule)


@transaction.atomic
def submit_schedule_candidate_feedback(*, schedule_item, actor, decision, comment="", return_rejected_item=False):
    if schedule_item is None:
        raise ValidationError("Пункт черновика не найден.")
    try:
        schedule_item = (
            VacationScheduleItem.objects.select_for_update()
            .select_related("schedule", "employee")
            .get(pk=schedule_item.pk)
        )
    except VacationScheduleItem.DoesNotExist:
        raise ValidationError("Пункт черновика не найден.")
    if schedule_item.status != VacationScheduleItem.STATUS_DRAFT:
        raise ValidationError("Отзыв можно оставить только по пункту черновика.")
    if schedule_item.schedule.status != VacationSchedule.STATUS_DRAFT:
        raise ValidationError("Отзыв можно оставить только по черновику графика.")
    if not can_leave_schedule_candidate_feedback(actor, schedule_item):
        raise ValidationError("У вас нет прав оставить отзыв по этому пункту черновика.")
    if decision not in FEEDBACK_DECISION_META:
        raise ValidationError("Выберите корректный вариант отзыва.")

    role = _feedback_role_for_actor(actor)
    if not role:
        raise ValidationError("Эта роль не может оставлять отзыв по кандидату.")

    normalized_comment = (comment or "").strip()
    if len(normalized_comment) > FEEDBACK_COMMENT_MAX_LENGTH:
        normalized_comment = normalized_comment[:FEEDBACK_COMMENT_MAX_LENGTH].rstrip()

    feedback, _ = VacationScheduleCandidateFeedback.objects.update_or_create(
        schedule_item=schedule_item,
        reviewer=actor,
        defaults={
            "candidate_id": schedule_item.selected_candidate_id,
            "generation_run_id": schedule_item.generation_run_id,
            "reviewer_role": role,
            "decision": decision,
            "comment": normalized_comment,
            "score_snapshot": schedule_item.ai_score,
            "confidence_snapshot": schedule_item.ai_confidence,
            "model_version_snapshot": schedule_item.ai_model_version,
            "explanation_snapshot": schedule_item.ai_explanation,
        },
    )

    item_returned_to_manual = False
    if (
        return_rejected_item
        and decision == VacationScheduleCandidateFeedback.DECISION_REJECT
        and can_return_rejected_schedule_item_to_manual(actor, schedule_item)
    ):
        _return_schedule_item_to_manual(schedule_item, actor)
        item_returned_to_manual = True

    return ScheduleCandidateFeedbackResult(
        feedback=feedback,
        item_returned_to_manual=item_returned_to_manual,
        employee_id=schedule_item.employee_id,
    )
