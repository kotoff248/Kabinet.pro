from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as date_format

from apps.accounts.services import (
    can_initiate_schedule_change_for_employee,
    can_initiate_schedule_change_for_item,
    can_review_schedule_change_request,
)
from apps.leave.ml.request_support import (
    build_schedule_change_ai_support,
    schedule_change_ai_model_fields,
    schedule_change_decision_ai_model_fields,
)
from apps.leave.models import VacationScheduleChangeRequest, VacationScheduleItem

from .constants import REQUEST_STATUS_UI
from .dates import format_period_label
from .employee_presentation import (
    enrich_application_employee_presentation,
    serialize_application_employee_presentation,
)
from .notifications import notify_schedule_change_created, notify_schedule_change_reviewed
from .risk import (
    build_saved_schedule_change_risk_explanation,
    build_schedule_change_risk_explanation,
    calculate_schedule_change_request_risk,
    calculate_schedule_change_risk,
)
from .text import build_text_preview
from .validation import validate_schedule_change_request

SCHEDULE_CHANGE_MODULE_BADGE_RECOMMENDATION_LABELS = {
    "prefer": "удачный период",
    "normal": "можно одобрять",
    "avoid": "лучше проверить",
    "blocked": "есть ограничения",
}

SCHEDULE_CHANGE_MODULE_BADGE_VARIANTS = {
    "prefer": "planned",
    "normal": "info",
    "avoid": "medium",
    "blocked": "risk",
}


def is_manager_initiated_schedule_change(change_request):
    return (
        change_request.requested_by_id is not None
        and change_request.requested_by_id != change_request.employee_id
    )


def _percent_badge_label(value):
    if value is None or value == "":
        return ""
    try:
        percent = max(Decimal("0"), min(Decimal("100"), Decimal(str(value))))
    except Exception:
        return ""
    return f"{int(percent.quantize(Decimal('1'), rounding=ROUND_HALF_UP))}%"


def _schedule_change_module_badge_payload(*, score, recommendation, explanation, source_label):
    score_label = _percent_badge_label(score)
    if not score_label:
        return None

    recommendation = recommendation or "normal"
    return {
        "score_label": score_label,
        "recommendation": recommendation,
        "recommendation_label": SCHEDULE_CHANGE_MODULE_BADGE_RECOMMENDATION_LABELS.get(
            recommendation,
            SCHEDULE_CHANGE_MODULE_BADGE_RECOMMENDATION_LABELS["normal"],
        ),
        "variant": SCHEDULE_CHANGE_MODULE_BADGE_VARIANTS.get(
            recommendation,
            SCHEDULE_CHANGE_MODULE_BADGE_VARIANTS["normal"],
        ),
        "source_label": source_label,
        "tooltip_text": explanation or f"Оценка модуля: {source_label.lower()}.",
    }


def _schedule_change_created_module_badge(change_request):
    return _schedule_change_module_badge_payload(
        score=change_request.ai_score,
        recommendation=change_request.ai_recommendation,
        explanation=change_request.ai_explanation,
        source_label="При создании переноса",
    )


def _schedule_change_decision_module_badge(change_request):
    return _schedule_change_module_badge_payload(
        score=getattr(change_request, "decision_ai_score", None),
        recommendation=getattr(change_request, "decision_ai_recommendation", ""),
        explanation=getattr(change_request, "decision_ai_explanation", ""),
        source_label="На момент решения",
    )


def _schedule_change_risk_payload(change_request):
    return {
        "risk_score": change_request.risk_score,
        "risk_level": change_request.risk_level,
        "department_load_level": change_request.department_load_level,
        "overlapping_absences_count": change_request.overlapping_absences_count,
        "remaining_staff_count": change_request.remaining_staff_count,
        "min_staff_required": change_request.min_staff_required,
        "balance_after_change": change_request.balance_after_change,
    }


def _schedule_change_live_module_badge(change_request):
    if change_request.status != VacationScheduleChangeRequest.STATUS_PENDING:
        return None

    try:
        ai_support = build_schedule_change_ai_support(
            change_request.schedule_item,
            change_request.new_start_date,
            change_request.new_end_date,
            risk_payload=_schedule_change_risk_payload(change_request),
            risk_explanation=getattr(change_request, "risk_explanation", None),
            exclude_change_request_id=change_request.id,
        )
    except Exception:
        return None

    return _schedule_change_module_badge_payload(
        score=ai_support.get("module_score"),
        recommendation=ai_support.get("module_recommendation"),
        explanation=ai_support.get("module_explanation"),
        source_label="На сейчас",
    )


def _schedule_change_reconstructed_decision_module_badge(change_request):
    if change_request.status == VacationScheduleChangeRequest.STATUS_PENDING:
        return None

    try:
        ai_support = build_schedule_change_ai_support(
            change_request.schedule_item,
            change_request.new_start_date,
            change_request.new_end_date,
            can_submit=True,
            risk_payload=_schedule_change_risk_payload(change_request),
            risk_explanation=getattr(change_request, "risk_explanation", None),
            exclude_change_request_id=change_request.id,
        )
    except Exception:
        return None

    return _schedule_change_module_badge_payload(
        score=ai_support.get("module_score"),
        recommendation=ai_support.get("module_recommendation"),
        explanation=ai_support.get("module_explanation"),
        source_label="На момент решения",
    )


def _schedule_change_module_badge(change_request):
    if getattr(change_request, "_module_badge_resolved", False):
        return getattr(change_request, "module_badge", None)
    if change_request.status == VacationScheduleChangeRequest.STATUS_PENDING:
        badge = _schedule_change_created_module_badge(change_request) or _schedule_change_live_module_badge(change_request)
    else:
        badge = (
            _schedule_change_decision_module_badge(change_request)
            or _schedule_change_reconstructed_decision_module_badge(change_request)
            or _schedule_change_created_module_badge(change_request)
        )
    change_request._module_badge_resolved = True
    return badge


def _validate_reviewer_can_review_change(reviewer, change_request):
    if not can_review_schedule_change_request(reviewer, change_request):
        if is_manager_initiated_schedule_change(change_request):
            raise ValidationError("Принять или отклонить предложение переноса может только сотрудник.")
        raise ValidationError("У вас нет прав для согласования этого переноса.")


def _empty_transfer_action():
    return {
        "can_request_transfer": False,
        "transfer_url": "",
        "transfer_preview_url": "",
        "transfer_title": "",
        "transfer_employee_id": "",
        "transfer_exclude_schedule_item_id": "",
        "transfer_year": "",
        "transfer_action_label": "",
        "transfer_submit_label": "",
        "transfer_hint": "",
        "transfer_modal_title": "",
        "transfer_modal_subtitle": "",
    }


def build_schedule_change_transfer_action(
    *,
    actor,
    employee,
    schedule_item_id,
    start_date,
    end_date,
    vacation_type_label,
    schedule_status,
    today=None,
    pending_change_exists=None,
):
    today = today or timezone.localdate()
    if schedule_status not in VacationScheduleItem.ACTIVE_STATUSES or start_date <= today:
        return _empty_transfer_action()
    if pending_change_exists is None:
        pending_change_exists = VacationScheduleChangeRequest.objects.filter(
            schedule_item_id=schedule_item_id,
            status=VacationScheduleChangeRequest.STATUS_PENDING,
        ).exists()
    if pending_change_exists:
        return _empty_transfer_action()
    if not can_initiate_schedule_change_for_employee(actor, employee):
        return _empty_transfer_action()

    is_manager_action = actor is not None and actor.id != employee.id
    period_title = f"{format_period_label(start_date, end_date)} · {vacation_type_label}"
    return {
        "can_request_transfer": True,
        "transfer_url": reverse("schedule_change_request_create", args=[schedule_item_id]),
        "transfer_preview_url": reverse("schedule_change_request_preview", args=[schedule_item_id]),
        "transfer_title": period_title,
        "transfer_employee_id": employee.id,
        "transfer_exclude_schedule_item_id": schedule_item_id,
        "transfer_year": start_date.year,
        "transfer_action_label": "Предложить перенос" if is_manager_action else "Запросить перенос",
        "transfer_submit_label": "Отправить предложение" if is_manager_action else "Запросить перенос",
        "transfer_hint": (
            "Сотрудник получит уведомление и сможет принять или отклонить перенос."
            if is_manager_action
            else "Старый отпуск останется в графике, пока руководитель не согласует перенос."
        ),
        "transfer_modal_title": "Предложить перенос отпуска" if is_manager_action else "Запросить перенос отпуска",
        "transfer_modal_subtitle": (
            "Выберите новые даты и укажите причину. Предложение уйдёт сотруднику."
            if is_manager_action
            else "Выберите новые даты и укажите причину. Запрос уйдёт руководителю на согласование."
        ),
    }

def get_schedule_change_requests_queryset():
    return VacationScheduleChangeRequest.objects.select_related(
        "employee",
        "employee__department",
        "employee__deputy_department",
        "employee__managed_department",
        "employee__employee_position",
        "employee__employee_position__production_group",
        "schedule_item",
        "schedule_item__schedule",
        "requested_by",
        "reviewed_by",
    )

def _change_request_status_meta(change_request):
    return REQUEST_STATUS_UI[change_request.status]

def enrich_schedule_change_request(change_request, *, include_live_risk_explanation=False):
    status_meta = _change_request_status_meta(change_request)
    change_request.status_label = status_meta["label"]
    change_request.status_icon = status_meta["icon"]
    change_request.status_css_class = status_meta["css_class"]
    change_request.risk_label = change_request.get_risk_level_display()
    change_request.old_period_label = format_period_label(change_request.old_start_date, change_request.old_end_date)
    change_request.new_period_label = format_period_label(change_request.new_start_date, change_request.new_end_date)
    change_request.created_at_formatted = date_format(change_request.created_at, "j E Y")
    live_risk_payload = (
        calculate_schedule_change_request_risk(change_request)
        if include_live_risk_explanation
        else None
    )
    change_request.risk_explanation = (
        build_schedule_change_risk_explanation(change_request)
        if include_live_risk_explanation
        else build_saved_schedule_change_risk_explanation(change_request)
    )
    if live_risk_payload is not None:
        change_request.balance_after_change = live_risk_payload["balance_after_change"]
    change_request.risk_score = change_request.risk_explanation["score"]
    change_request.risk_label = change_request.risk_explanation["label"]
    change_request.risk_short_reason = change_request.risk_explanation["short_reason"]
    change_request.risk_recommended_action = change_request.risk_explanation["recommended_action"]
    change_request.risk_is_conflict = change_request.risk_explanation["is_conflict"]
    change_request.reason_preview = build_text_preview(change_request.reason)
    change_request.module_badge = _schedule_change_module_badge(change_request)
    change_request.detail_url = reverse("schedule_change_detail", args=[change_request.id])
    change_request.profile_url = f"{reverse('employee_profile', args=[change_request.employee_id])}?from=applications"
    change_request.is_manager_initiated = is_manager_initiated_schedule_change(change_request)
    change_request.origin_label = (
        "Предложение руководителя" if change_request.is_manager_initiated else "Запрос сотрудника"
    )
    change_request.initiator_name = (
        change_request.requested_by.full_name
        if change_request.requested_by_id and change_request.requested_by
        else "Не указан"
    )
    enrich_application_employee_presentation(change_request)
    return change_request

def serialize_schedule_change_request_row(change_request):
    enrich_schedule_change_request(
        change_request,
        include_live_risk_explanation=change_request.status == VacationScheduleChangeRequest.STATUS_PENDING,
    )
    return {
        "id": change_request.id,
        "employee_name": change_request.employee.full_name,
        "employee_department": change_request.employee.department.name if change_request.employee.department else "Не указан",
        "profile_url": f"{reverse('employee_profile', args=[change_request.employee_id])}?from=applications",
        "old_period_label": change_request.old_period_label,
        "new_period_label": change_request.new_period_label,
        "status": change_request.status,
        "status_label": change_request.status_label,
        "status_icon": change_request.status_icon,
        "status_css_class": change_request.status_css_class,
        "risk_score": change_request.risk_score,
        "risk_label": change_request.risk_label,
        "risk_short_reason": change_request.risk_short_reason,
        "risk_recommended_action": change_request.risk_recommended_action,
        "risk_is_conflict": change_request.risk_is_conflict,
        "reason_preview": change_request.reason_preview,
        "module_badge": change_request.module_badge,
        "can_approve": getattr(change_request, "can_approve", False),
        "decision_locked": getattr(change_request, "decision_locked", False),
        "detail_url": change_request.detail_url,
        "origin_label": change_request.origin_label,
        "is_manager_initiated": change_request.is_manager_initiated,
        "initiator_name": change_request.initiator_name,
        "approve_url": reverse("schedule_change_approve", args=[change_request.id]),
        "reject_url": reverse("schedule_change_reject", args=[change_request.id]),
    } | serialize_application_employee_presentation(change_request)

@transaction.atomic
def create_schedule_change_request(schedule_item_id, requested_by, new_start_date, new_end_date, reason=""):
    schedule_item = VacationScheduleItem.objects.select_related("employee", "schedule").select_for_update().get(
        pk=schedule_item_id
    )
    if not can_initiate_schedule_change_for_item(requested_by, schedule_item):
        raise ValidationError("У вас нет прав для создания переноса по этому отпуску.")

    validate_schedule_change_request(schedule_item, new_start_date, new_end_date)
    risk_payload = calculate_schedule_change_risk(schedule_item, new_start_date, new_end_date)
    ai_support = build_schedule_change_ai_support(
        schedule_item,
        new_start_date,
        new_end_date,
        can_submit=True,
        risk_payload=risk_payload,
        block_reason_key="",
    )
    ai_fields = schedule_change_ai_model_fields(ai_support)
    change_request = VacationScheduleChangeRequest.objects.create(
        schedule_item=schedule_item,
        employee=schedule_item.employee,
        old_start_date=schedule_item.start_date,
        old_end_date=schedule_item.end_date,
        new_start_date=new_start_date,
        new_end_date=new_end_date,
        reason=reason,
        requested_by=requested_by,
        **risk_payload,
        **ai_fields,
    )
    notify_schedule_change_created(change_request)
    return change_request

@transaction.atomic
def approve_schedule_change_request(change_request_id, *, reviewer, review_comment=""):
    change_request = get_schedule_change_requests_queryset().select_for_update(of=("self",)).get(pk=change_request_id)
    if change_request.status != VacationScheduleChangeRequest.STATUS_PENDING:
        raise ValidationError("Одобрить можно только запрос переноса в ожидании.")
    _validate_reviewer_can_review_change(reviewer, change_request)

    schedule_item = VacationScheduleItem.objects.select_related("employee", "schedule").select_for_update().get(
        pk=change_request.schedule_item_id
    )
    new_chargeable_days = validate_schedule_change_request(
        schedule_item,
        change_request.new_start_date,
        change_request.new_end_date,
        exclude_change_request_id=change_request.id,
    )
    risk_payload = calculate_schedule_change_risk(
        schedule_item,
        change_request.new_start_date,
        change_request.new_end_date,
    )
    reviewed_at = timezone.now()
    decision_ai_support = build_schedule_change_ai_support(
        schedule_item,
        change_request.new_start_date,
        change_request.new_end_date,
        can_submit=True,
        risk_payload=risk_payload,
        block_reason_key="",
        exclude_change_request_id=change_request.id,
    )
    decision_ai_fields = schedule_change_decision_ai_model_fields(
        decision_ai_support,
        evaluated_at=reviewed_at,
    )

    schedule_item.status = VacationScheduleItem.STATUS_TRANSFERRED
    schedule_item.was_changed_by_manager = True
    schedule_item.manager_comment = (
        "Перенесено по принятому предложению руководителя."
        if is_manager_initiated_schedule_change(change_request)
        else "Перенесено по согласованному запросу сотрудника."
    )
    schedule_item.save(update_fields=["status", "was_changed_by_manager", "manager_comment"])

    replacement_item = VacationScheduleItem.objects.create(
        schedule=schedule_item.schedule,
        employee=schedule_item.employee,
        start_date=change_request.new_start_date,
        end_date=change_request.new_end_date,
        vacation_type=schedule_item.vacation_type,
        chargeable_days=new_chargeable_days,
        status=VacationScheduleItem.STATUS_APPROVED,
        source=VacationScheduleItem.SOURCE_TRANSFER,
        risk_score=risk_payload["risk_score"],
        risk_level=risk_payload["risk_level"],
        generated_by_ai=False,
        was_changed_by_manager=True,
        manager_comment=(
            "Создано после принятия предложения переноса."
            if is_manager_initiated_schedule_change(change_request)
            else "Создано после согласования переноса."
        ),
        previous_item=schedule_item,
        created_from_change_request=change_request,
    )

    change_request.status = VacationScheduleChangeRequest.STATUS_APPROVED
    change_request.reviewed_by = reviewer
    change_request.reviewed_at = reviewed_at
    change_request.review_comment = review_comment
    for field_name, value in risk_payload.items():
        setattr(change_request, field_name, value)
    for field_name, value in decision_ai_fields.items():
        setattr(change_request, field_name, value)
    change_request.save(
        update_fields=[
            "status",
            "reviewed_by",
            "reviewed_at",
            "review_comment",
            "risk_score",
            "risk_level",
            "department_load_level",
            "overlapping_absences_count",
            "remaining_staff_count",
            "min_staff_required",
            "balance_after_change",
            *decision_ai_fields.keys(),
        ]
    )
    notify_schedule_change_reviewed(change_request)
    return replacement_item

@transaction.atomic
def reject_schedule_change_request(change_request_id, *, reviewer, review_comment=""):
    change_request = get_schedule_change_requests_queryset().select_for_update(of=("self",)).get(pk=change_request_id)
    if change_request.status != VacationScheduleChangeRequest.STATUS_PENDING:
        raise ValidationError("Отклонить можно только запрос переноса в ожидании.")
    _validate_reviewer_can_review_change(reviewer, change_request)

    risk_payload = calculate_schedule_change_risk(
        change_request.schedule_item,
        change_request.new_start_date,
        change_request.new_end_date,
    )
    reviewed_at = timezone.now()
    decision_ai_support = build_schedule_change_ai_support(
        change_request.schedule_item,
        change_request.new_start_date,
        change_request.new_end_date,
        risk_payload=risk_payload,
        exclude_change_request_id=change_request.id,
    )
    decision_ai_fields = schedule_change_decision_ai_model_fields(
        decision_ai_support,
        evaluated_at=reviewed_at,
    )
    change_request.status = VacationScheduleChangeRequest.STATUS_REJECTED
    change_request.reviewed_by = reviewer
    change_request.reviewed_at = reviewed_at
    change_request.review_comment = review_comment
    for field_name, value in risk_payload.items():
        setattr(change_request, field_name, value)
    for field_name, value in decision_ai_fields.items():
        setattr(change_request, field_name, value)
    change_request.save(
        update_fields=[
            "status",
            "reviewed_by",
            "reviewed_at",
            "review_comment",
            "risk_score",
            "risk_level",
            "department_load_level",
            "overlapping_absences_count",
            "remaining_staff_count",
            "min_staff_required",
            "balance_after_change",
            *decision_ai_fields.keys(),
        ]
    )
    notify_schedule_change_reviewed(change_request)
    return change_request
