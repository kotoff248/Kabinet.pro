from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as date_format

from apps.accounts.services import can_approve_leave_for_employee
from apps.employees.models import Employees
from apps.leave.models import VacationRequest

from .constants import REQUEST_STATUS_UI
from .dates import format_period_label
from .employee_presentation import (
    enrich_application_employee_presentation,
    serialize_application_employee_presentation,
)
from .notifications import (
    delete_vacation_request_notifications,
    notify_vacation_request_created,
    notify_vacation_request_reviewed,
)
from .querysets import get_vacation_requests_queryset
from .request_history import (
    record_vacation_request_created,
    record_vacation_request_deleted,
    record_vacation_request_reviewed,
)
from .risk import (
    build_saved_vacation_risk_explanation,
    build_vacation_object_risk_explanation,
    calculate_vacation_request_risk,
)
from apps.leave.ml.request_support import (
    build_vacation_request_ai_support,
    vacation_request_ai_model_fields,
    vacation_request_decision_ai_model_fields,
)
from .schedule_items import create_schedule_item_from_paid_vacation_request
from .text import build_text_preview
from .validation import validate_vacation_request_for_employee

APPLICATION_MODULE_BADGE_RECOMMENDATION_LABELS = {
    "prefer": "удачный период",
    "normal": "можно одобрять",
    "avoid": "лучше проверить",
    "blocked": "есть ограничения",
}

APPLICATION_MODULE_BADGE_VARIANTS = {
    "prefer": "planned",
    "normal": "info",
    "avoid": "medium",
    "blocked": "risk",
}

VACATION_REQUEST_RISK_MODEL_FIELDS = {
    "risk_score",
    "risk_level",
    "department_load_level",
    "overlapping_absences_count",
    "remaining_staff_count",
    "min_staff_required",
    "balance_after_request",
}


def _percent_badge_label(value):
    if value is None or value == "":
        return ""
    try:
        percent = max(Decimal("0"), min(Decimal("100"), Decimal(str(value))))
    except Exception:
        return ""
    return f"{int(percent.quantize(Decimal('1'), rounding=ROUND_HALF_UP))}%"


def _application_module_badge(request_obj):
    if request_obj.status == VacationRequest.STATUS_PENDING:
        score = request_obj.ai_score
        recommendation = request_obj.ai_recommendation
        explanation = request_obj.ai_explanation
        source_label = "На момент подачи"
    elif request_obj.decision_ai_score is not None:
        score = request_obj.decision_ai_score
        recommendation = request_obj.decision_ai_recommendation
        explanation = request_obj.decision_ai_explanation
        source_label = "На момент решения"
    else:
        return None

    score_label = _percent_badge_label(score)
    if not score_label:
        return None

    recommendation = recommendation or "normal"
    return {
        "score_label": score_label,
        "recommendation": recommendation,
        "recommendation_label": APPLICATION_MODULE_BADGE_RECOMMENDATION_LABELS.get(
            recommendation,
            APPLICATION_MODULE_BADGE_RECOMMENDATION_LABELS["normal"],
        ),
        "variant": APPLICATION_MODULE_BADGE_VARIANTS.get(
            recommendation,
            APPLICATION_MODULE_BADGE_VARIANTS["normal"],
        ),
        "source_label": source_label,
        "tooltip_text": explanation or f"Оценка модуля сохранена: {source_label.lower()}.",
    }


def _validate_reviewer_can_approve(reviewer, employee):
    if not can_approve_leave_for_employee(reviewer, employee):
        raise ValidationError("У вас нет прав для согласования этой заявки.")


def _vacation_request_risk_model_fields(risk_payload):
    return {
        key: risk_payload[key]
        for key in VACATION_REQUEST_RISK_MODEL_FIELDS
        if key in risk_payload
    }


@transaction.atomic
def create_vacation_request(employee, start_date, end_date, vacation_type, reason=""):
    employee = Employees.objects.select_for_update().get(pk=employee.pk)
    validate_vacation_request_for_employee(employee, start_date, end_date, vacation_type)
    risk_payload = calculate_vacation_request_risk(employee, start_date, end_date, vacation_type)
    ai_support = build_vacation_request_ai_support(
        employee,
        start_date,
        end_date,
        vacation_type,
        can_submit=True,
        risk_payload=risk_payload,
        include_alternatives=False,
    )
    vacation = VacationRequest.objects.create(
        employee=employee,
        start_date=start_date,
        end_date=end_date,
        vacation_type=vacation_type,
        status=VacationRequest.STATUS_PENDING,
        reason=reason,
        **_vacation_request_risk_model_fields(risk_payload),
        **vacation_request_ai_model_fields(ai_support),
    )
    record_vacation_request_created(vacation)
    notify_vacation_request_created(vacation)
    return vacation

def enrich_vacation_request(request_obj, *, include_live_risk_explanation=False):
    status_meta = REQUEST_STATUS_UI[request_obj.status]
    request_obj.status_label = status_meta["label"]
    request_obj.status_icon = status_meta["icon"]
    request_obj.status_css_class = status_meta["css_class"]
    request_obj.vacation_type_display_label = (
        "Оплачиваемый вне графика"
        if request_obj.vacation_type == "paid"
        else request_obj.get_vacation_type_display()
    )
    request_obj.risk_label = request_obj.get_risk_level_display()
    request_obj.period_label = format_period_label(request_obj.start_date, request_obj.end_date)
    request_obj.request_type = {
        VacationRequest.STATUS_APPROVED: "vacation",
        VacationRequest.STATUS_PENDING: "pre_holiday",
        VacationRequest.STATUS_REJECTED: "canceled_holiday",
    }[request_obj.status]
    request_obj.start_date_formatted = date_format(request_obj.start_date, "j E Y")
    request_obj.end_date_formatted = date_format(request_obj.end_date, "j E Y")
    request_obj.risk_explanation = (
        build_vacation_object_risk_explanation(request_obj)
        if include_live_risk_explanation
        else build_saved_vacation_risk_explanation(request_obj)
    )
    request_obj.risk_score = request_obj.risk_explanation["score"]
    request_obj.risk_label = request_obj.risk_explanation["label"]
    request_obj.risk_short_reason = request_obj.risk_explanation["short_reason"]
    request_obj.risk_recommended_action = request_obj.risk_explanation["recommended_action"]
    request_obj.risk_is_conflict = request_obj.risk_explanation["is_conflict"]
    request_obj.reason_preview = build_text_preview(request_obj.reason)
    request_obj.module_badge = _application_module_badge(request_obj)
    enrich_application_employee_presentation(request_obj)
    return request_obj

def serialize_vacation_request_row(request_obj):
    enrich_vacation_request(request_obj)
    return {
        "id": request_obj.id,
        "employee_name": request_obj.employee.full_name,
        "employee_department": request_obj.employee.department.name if request_obj.employee.department else "Не указан",
        "profile_url": f"{reverse('employee_profile', args=[request_obj.employee_id])}?from=applications",
        "detail_url": reverse("vacation_detail", args=[request_obj.id]),
        "period_label": request_obj.period_label,
        "start_date_formatted": request_obj.start_date_formatted,
        "end_date_formatted": request_obj.end_date_formatted,
        "vacation_type_label": request_obj.vacation_type_display_label,
        "status": request_obj.status,
        "status_label": request_obj.status_label,
        "status_icon": request_obj.status_icon,
        "status_css_class": request_obj.status_css_class,
        "risk_score": request_obj.risk_score,
        "risk_label": request_obj.risk_label,
        "risk_short_reason": request_obj.risk_short_reason,
        "risk_recommended_action": request_obj.risk_recommended_action,
        "risk_is_conflict": request_obj.risk_is_conflict,
        "reason_preview": request_obj.reason_preview,
        "module_badge": request_obj.module_badge,
        "can_approve": getattr(request_obj, "can_approve", False),
        "decision_locked": getattr(request_obj, "decision_locked", False),
    } | serialize_application_employee_presentation(request_obj)

def get_employee_vacation_requests(employee):
    requests = list(get_vacation_requests_queryset().filter(employee=employee).order_by("-created_at"))
    return [enrich_vacation_request(request_obj) for request_obj in requests]

@transaction.atomic
def approve_vacation_request(vacation_id, *, reviewer, review_comment=""):
    vacation = VacationRequest.objects.select_related("employee").select_for_update().get(pk=vacation_id)
    if vacation.status != VacationRequest.STATUS_PENDING:
        raise ValidationError("Одобрить можно только заявку со статусом 'В ожидании'.")

    employee = Employees.objects.select_for_update().get(pk=vacation.employee_id)
    _validate_reviewer_can_approve(reviewer, employee)
    validate_vacation_request_for_employee(
        employee=employee,
        start_date=vacation.start_date,
        end_date=vacation.end_date,
        vacation_type=vacation.vacation_type,
        exclude_request_id=vacation.id,
    )
    risk_payload = calculate_vacation_request_risk(
        employee=employee,
        start_date=vacation.start_date,
        end_date=vacation.end_date,
        vacation_type=vacation.vacation_type,
        exclude_request_id=vacation.id,
    )
    decision_ai_support = build_vacation_request_ai_support(
        employee,
        vacation.start_date,
        vacation.end_date,
        vacation.vacation_type,
        risk_payload=risk_payload,
        include_alternatives=False,
        exclude_request_id=vacation.id,
    )
    reviewed_at = timezone.now()
    decision_ai_fields = vacation_request_decision_ai_model_fields(
        decision_ai_support,
        evaluated_at=reviewed_at,
    )
    vacation.status = VacationRequest.STATUS_APPROVED
    vacation.reviewed_by = reviewer
    vacation.reviewed_at = reviewed_at
    vacation.review_comment = review_comment
    for field_name, value in risk_payload.items():
        setattr(vacation, field_name, value)
    for field_name, value in decision_ai_fields.items():
        setattr(vacation, field_name, value)
    vacation.save(
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
            "balance_after_request",
            *decision_ai_fields.keys(),
        ]
    )
    if vacation.vacation_type == "paid":
        create_schedule_item_from_paid_vacation_request(vacation, risk_payload=risk_payload)
    record_vacation_request_reviewed(vacation)
    notify_vacation_request_reviewed(vacation)
    return vacation

@transaction.atomic
def reject_vacation_request(vacation_id, *, reviewer, review_comment=""):
    vacation = VacationRequest.objects.select_related("employee").select_for_update().get(pk=vacation_id)
    if vacation.status != VacationRequest.STATUS_PENDING:
        raise ValidationError("Отклонить можно только заявку со статусом 'В ожидании'.")

    _validate_reviewer_can_approve(reviewer, vacation.employee)
    risk_payload = calculate_vacation_request_risk(
        employee=vacation.employee,
        start_date=vacation.start_date,
        end_date=vacation.end_date,
        vacation_type=vacation.vacation_type,
        exclude_request_id=vacation.id,
    )
    decision_ai_support = build_vacation_request_ai_support(
        vacation.employee,
        vacation.start_date,
        vacation.end_date,
        vacation.vacation_type,
        risk_payload=risk_payload,
        include_alternatives=False,
        exclude_request_id=vacation.id,
    )
    reviewed_at = timezone.now()
    decision_ai_fields = vacation_request_decision_ai_model_fields(
        decision_ai_support,
        evaluated_at=reviewed_at,
    )
    vacation.status = VacationRequest.STATUS_REJECTED
    vacation.reviewed_by = reviewer
    vacation.reviewed_at = reviewed_at
    vacation.review_comment = review_comment
    for field_name, value in risk_payload.items():
        setattr(vacation, field_name, value)
    for field_name, value in decision_ai_fields.items():
        setattr(vacation, field_name, value)
    vacation.save(
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
            "balance_after_request",
            *decision_ai_fields.keys(),
        ]
    )
    record_vacation_request_reviewed(vacation)
    notify_vacation_request_reviewed(vacation)
    return vacation

@transaction.atomic
def delete_pending_vacation_request(vacation_id, *, actor):
    vacation = VacationRequest.objects.select_related("employee").select_for_update().get(pk=vacation_id)
    if vacation.status != VacationRequest.STATUS_PENDING:
        raise ValidationError("Удалить можно только заявку со статусом 'В ожидании'.")
    if actor is None or (
        actor.id != vacation.employee_id and not can_approve_leave_for_employee(actor, vacation.employee)
    ):
        raise ValidationError("У вас нет прав для удаления этой заявки.")

    employee = vacation.employee
    record_vacation_request_deleted(vacation, actor=actor)
    delete_vacation_request_notifications(vacation)
    vacation.delete()
    return employee
