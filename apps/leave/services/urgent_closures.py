from datetime import date, timedelta
from decimal import Decimal
from math import ceil

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts.services import can_approve_leave_for_employee, is_hr_employee
from apps.leave.models import VacationRequest, VacationSchedule, VacationScheduleItem, VacationUrgentClosureRequest

from .approval_routes import get_expected_vacation_approver
from .constants import REQUEST_STATUS_UI
from .dates import format_period_label, get_chargeable_leave_days, quantize_leave_days
from .employee_presentation import enrich_application_employee_presentation, serialize_application_employee_presentation
from .risk import build_vacation_request_risk_explanation, calculate_vacation_request_risk
from .validation import get_overlapping_requests, get_overlapping_schedule_items


URGENT_CLOSURE_OPTION_LIMIT = 5


def _format_days(value):
    value = quantize_leave_days(value or Decimal("0"))
    if value == value.to_integral_value():
        return str(int(value))
    return str(value).replace(".", ",").rstrip("0").rstrip(",")


def _days_label(value):
    return f"{_format_days(value)} д."


def _period_label(start_date, end_date):
    if not start_date or not end_date:
        return "Не указан"
    return format_period_label(start_date, end_date)


def urgent_closure_detail_url(closure_request):
    return reverse("urgent_closure_detail", args=[closure_request.id])


def _active_urgent_closure_queryset():
    return VacationUrgentClosureRequest.objects.filter(status__in=VacationUrgentClosureRequest.ACTIVE_STATUSES)


def get_urgent_closure_requests_queryset():
    return VacationUrgentClosureRequest.objects.select_related(
        "employee",
        "employee__department",
        "employee__deputy_department",
        "employee__managed_department",
        "employee__employee_position",
        "employee__employee_position__production_group",
        "created_by",
        "department_reviewer",
        "finalized_by",
        "rejected_by",
        "created_schedule_item",
        "created_schedule_item__schedule",
    )


def _get_expected_reviewer(employee):
    return get_expected_vacation_approver(employee).employee


def can_view_urgent_closure_request(actor, closure_request):
    if actor is None or closure_request is None:
        return False
    if is_hr_employee(actor):
        return True
    if actor.id == closure_request.employee_id:
        return True
    if can_approve_leave_for_employee(actor, closure_request.employee):
        return True
    return False


def can_department_review_urgent_closure(actor, closure_request):
    return (
        closure_request.status == VacationUrgentClosureRequest.STATUS_DEPARTMENT_REVIEW
        and can_approve_leave_for_employee(actor, closure_request.employee)
    )


def can_employee_review_urgent_closure(actor, closure_request):
    return (
        actor is not None
        and closure_request.status == VacationUrgentClosureRequest.STATUS_EMPLOYEE_REVIEW
        and actor.id == closure_request.employee_id
    )


def can_finalize_urgent_closure(actor, closure_request):
    return (
        is_hr_employee(actor)
        and closure_request.status == VacationUrgentClosureRequest.STATUS_HR_FINALIZATION
    )


def _whole_required_days(required_days):
    required_days = quantize_leave_days(required_days or Decimal("0.00"))
    if required_days <= 0:
        return 0
    return int(ceil(float(required_days)))


def _candidate_end_dates(latest_end, earliest_start):
    candidates = [
        latest_end,
        latest_end - timedelta(days=14),
        latest_end - timedelta(days=30),
        latest_end - timedelta(days=45),
        latest_end - timedelta(days=60),
        latest_end - timedelta(days=90),
        latest_end - timedelta(days=120),
    ]
    month_anchors = []
    for month in range(10, 13):
        for day in (15, 25):
            try:
                month_anchors.append(date(latest_end.year, month, day))
            except ValueError:
                continue
    unique = []
    seen = set()
    for candidate in [*candidates, *month_anchors]:
        if candidate < earliest_start or candidate > latest_end or candidate in seen:
            continue
        unique.append(candidate)
        seen.add(candidate)
    return sorted(unique, reverse=True)


def _start_date_for_chargeable_days(end_date, target_days, earliest_start):
    current = end_date
    while current >= earliest_start:
        chargeable_days = get_chargeable_leave_days(current, end_date, "paid")
        if chargeable_days == target_days:
            return current
        if chargeable_days > target_days:
            return None
        current -= timedelta(days=1)
    return None


def _period_has_employee_overlap(employee, start_date, end_date, *, exclude_schedule_item_id=None):
    if get_overlapping_requests(employee, start_date, end_date).exists():
        return True
    schedule_items = get_overlapping_schedule_items(employee, start_date, end_date)
    if exclude_schedule_item_id is not None:
        schedule_items = schedule_items.exclude(pk=exclude_schedule_item_id)
    return schedule_items.exists()


def _build_option_payload(employee, start_date, end_date, required_days):
    chargeable_days = get_chargeable_leave_days(start_date, end_date, "paid")
    calendar_days = (end_date - start_date).days + 1
    overlap = _period_has_employee_overlap(employee, start_date, end_date)
    risk_payload = calculate_vacation_request_risk(employee, start_date, end_date, "paid")
    risk_explanation = build_vacation_request_risk_explanation(employee, start_date, end_date, "paid")
    risk_label = dict(VacationRequest.RISK_CHOICES).get(risk_payload["risk_level"], "Низкий")
    can_submit = not overlap and chargeable_days == required_days
    message = "Можно отправить руководителю и сотруднику."
    if overlap:
        message = "У сотрудника уже есть отпуск или заявка на эти даты."
    elif risk_explanation.get("is_conflict"):
        message = "Есть конфликт состава, но вариант можно отправить на проверку руководителю."
    elif risk_payload["risk_level"] == VacationRequest.RISK_HIGH:
        message = "Риск высокий: руководителю стоит проверить состав отдела."

    return {
        "start_date": start_date,
        "end_date": end_date,
        "period_label": _period_label(start_date, end_date),
        "calendar_days": calendar_days,
        "chargeable_days": chargeable_days,
        "chargeable_days_label": _days_label(chargeable_days),
        "risk_level": risk_payload["risk_level"],
        "risk_label": risk_label,
        "risk_score": risk_payload["risk_score"],
        "risk_short_reason": risk_explanation.get("short_reason", ""),
        "risk_recommended_action": risk_explanation.get("recommended_action", ""),
        "risk_is_conflict": risk_explanation.get("is_conflict", False),
        "can_submit": can_submit,
        "message": message,
    }


def build_urgent_closure_options(employee, planning_year, required_days, deadline, *, limit=URGENT_CLOSURE_OPTION_LIMIT):
    target_days = _whole_required_days(required_days)
    if target_days <= 0:
        return []

    planning_start = date(planning_year, 1, 1)
    latest_end = min(deadline, planning_start - timedelta(days=1))
    earliest_start = date(max(1, planning_year - 1), 1, 1)
    if earliest_start > latest_end:
        return []

    options = []
    seen_periods = set()
    for end_date in _candidate_end_dates(latest_end, earliest_start):
        start_date = _start_date_for_chargeable_days(end_date, target_days, earliest_start)
        if start_date is None:
            continue
        key = (start_date, end_date)
        if key in seen_periods:
            continue
        seen_periods.add(key)
        options.append(_build_option_payload(employee, start_date, end_date, target_days))
        if len(options) >= limit:
            break

    return sorted(
        options,
        key=lambda option: (
            not option["can_submit"],
            option["risk_is_conflict"],
            option["risk_score"],
            -option["end_date"].toordinal(),
        ),
    )


def build_urgent_closure_preview(*, employee, planning_year, required_days, deadline, start_date, end_date):
    required_days = quantize_leave_days(required_days)
    _validate_urgent_closure_period(employee, planning_year, required_days, deadline, start_date, end_date)
    return _build_option_payload(employee, start_date, end_date, required_days)


def detect_previous_year_closure_need(employee, planning_year, planning_need):
    planning_start = date(planning_year, 1, 1)
    planning_end = date(planning_year, 12, 31)
    required_days = Decimal("0.00")
    deadline = None

    for row in planning_need.get("mandatory_rows") or []:
        open_days = quantize_leave_days(Decimal(row.get("open_days") or 0))
        must_use_by = row.get("must_use_by")
        if open_days <= 0 or must_use_by is None:
            continue
        in_year_end = min(must_use_by, planning_end)
        available_in_planning_year = Decimal("0.00")
        if planning_start <= in_year_end:
            available_in_planning_year = quantize_leave_days(
                get_chargeable_leave_days(planning_start, in_year_end, "paid")
            )
        shortage = quantize_leave_days(max(open_days - available_in_planning_year, Decimal("0.00")))
        if shortage <= 0:
            continue
        required_days = quantize_leave_days(required_days + shortage)
        deadline = min(deadline, must_use_by) if deadline else must_use_by

    if required_days <= 0 or deadline is None:
        return None

    active_request = (
        _active_urgent_closure_queryset()
        .filter(employee=employee, planning_year=planning_year, deadline=deadline)
        .order_by("-created_at", "-id")
        .first()
    )
    options = [] if active_request else build_urgent_closure_options(employee, planning_year, required_days, deadline)
    return {
        "required_days": required_days,
        "required_days_label": _days_label(required_days),
        "deadline": deadline,
        "deadline_label": deadline.strftime("%d.%m.%Y"),
        "closure_year": planning_year - 1,
        "active_request": active_request,
        "active_request_url": urgent_closure_detail_url(active_request) if active_request else "",
        "create_url": reverse("urgent_closure_create", args=[planning_year, employee.id]),
        "preview_url": reverse("urgent_closure_preview", args=[planning_year, employee.id]),
        "modal_id": f"urgent-closure-{planning_year}-{employee.id}",
        "options": options,
        "can_create": bool(options),
        "explanation": (
            f"В графике {planning_year} до {deadline:%d.%m.%Y} не хватает списываемых дней, "
            f"поэтому остаток нужно закрыть в {planning_year - 1} году."
        ),
    }


def _validate_urgent_closure_period(employee, planning_year, required_days, deadline, start_date, end_date):
    required_days = quantize_leave_days(required_days)
    if end_date < start_date:
        raise ValidationError("Дата окончания не может быть раньше даты начала.")
    if start_date >= date(planning_year, 1, 1):
        raise ValidationError("Срочный остаток нужно закрывать до начала года черновика.")
    if end_date > deadline:
        raise ValidationError("Период должен закончиться не позже срока использования остатка.")
    if _period_has_employee_overlap(employee, start_date, end_date):
        raise ValidationError("У сотрудника уже есть отпуск или активная заявка на эти даты.")
    chargeable_days = quantize_leave_days(get_chargeable_leave_days(start_date, end_date, "paid"))
    if chargeable_days != required_days:
        raise ValidationError(
            f"Период должен списывать ровно {_days_label(required_days)}, сейчас списывает {_days_label(chargeable_days)}."
        )
    return int(chargeable_days)


def _risk_payload_for_closure(employee, start_date, end_date):
    payload = calculate_vacation_request_risk(employee, start_date, end_date, "paid")
    return {
        "risk_score": payload["risk_score"],
        "risk_level": payload["risk_level"],
        "department_load_level": payload["department_load_level"],
        "overlapping_absences_count": payload["overlapping_absences_count"],
        "remaining_staff_count": payload["remaining_staff_count"],
        "min_staff_required": payload["min_staff_required"],
        "balance_after_closure": payload["balance_after_request"],
    }


@transaction.atomic
def create_urgent_closure_request(
    *,
    employee,
    planning_year,
    required_days,
    deadline,
    start_date,
    end_date,
    actor,
    reason="",
):
    if not is_hr_employee(actor):
        raise ValidationError("Создать закрытие срочного остатка может только HR.")
    reviewer = _get_expected_reviewer(employee)
    if reviewer is None:
        raise ValidationError("Для сотрудника не найден согласующий руководитель.")
    if _active_urgent_closure_queryset().filter(employee=employee, planning_year=planning_year, deadline=deadline).exists():
        raise ValidationError("По этому срочному остатку уже есть активное согласование.")

    _validate_urgent_closure_period(employee, planning_year, required_days, deadline, start_date, end_date)
    closure_request = VacationUrgentClosureRequest.objects.create(
        employee=employee,
        planning_year=planning_year,
        closure_year=start_date.year,
        required_days=quantize_leave_days(required_days),
        deadline=deadline,
        proposed_start_date=start_date,
        proposed_end_date=end_date,
        reason=reason,
        created_by=actor,
        **_risk_payload_for_closure(employee, start_date, end_date),
    )

    from .notifications import notify_urgent_closure_created

    notify_urgent_closure_created(closure_request)
    return closure_request


def _update_closure_period(closure_request, start_date, end_date):
    _validate_urgent_closure_period(
        closure_request.employee,
        closure_request.planning_year,
        closure_request.required_days,
        closure_request.deadline,
        start_date,
        end_date,
    )
    risk_payload = _risk_payload_for_closure(closure_request.employee, start_date, end_date)
    closure_request.proposed_start_date = start_date
    closure_request.proposed_end_date = end_date
    for field_name, value in risk_payload.items():
        setattr(closure_request, field_name, value)


@transaction.atomic
def approve_urgent_closure_by_manager(closure_request_id, *, reviewer, comment="", start_date=None, end_date=None):
    closure_request = get_urgent_closure_requests_queryset().select_for_update(of=("self",)).get(pk=closure_request_id)
    if not can_department_review_urgent_closure(reviewer, closure_request):
        raise ValidationError("Подтвердить период может только согласующий руководитель.")
    if start_date and end_date:
        _update_closure_period(closure_request, start_date, end_date)
    closure_request.status = VacationUrgentClosureRequest.STATUS_EMPLOYEE_REVIEW
    closure_request.department_reviewer = reviewer
    closure_request.department_reviewed_at = timezone.now()
    closure_request.department_comment = comment
    closure_request.save()

    from .notifications import notify_urgent_closure_employee_review

    notify_urgent_closure_employee_review(closure_request)
    return closure_request


@transaction.atomic
def reject_urgent_closure(closure_request_id, *, actor, comment=""):
    closure_request = get_urgent_closure_requests_queryset().select_for_update(of=("self",)).get(pk=closure_request_id)
    if closure_request.status not in VacationUrgentClosureRequest.ACTIVE_STATUSES:
        raise ValidationError("Отклонить можно только активное согласование.")
    if not (
        can_department_review_urgent_closure(actor, closure_request)
        or can_employee_review_urgent_closure(actor, closure_request)
        or can_finalize_urgent_closure(actor, closure_request)
    ):
        raise ValidationError("У вас нет прав для отклонения этого согласования.")
    closure_request.status = VacationUrgentClosureRequest.STATUS_REJECTED
    closure_request.rejected_by = actor
    closure_request.rejected_at = timezone.now()
    closure_request.rejection_comment = comment
    closure_request.save()

    from .notifications import notify_urgent_closure_rejected

    notify_urgent_closure_rejected(closure_request)
    return closure_request


@transaction.atomic
def accept_urgent_closure_by_employee(closure_request_id, *, employee, comment=""):
    closure_request = get_urgent_closure_requests_queryset().select_for_update(of=("self",)).get(pk=closure_request_id)
    if not can_employee_review_urgent_closure(employee, closure_request):
        raise ValidationError("Принять период может только сотрудник, которому предложен отпуск.")
    closure_request.status = VacationUrgentClosureRequest.STATUS_HR_FINALIZATION
    closure_request.employee_responded_at = timezone.now()
    closure_request.employee_comment = comment
    closure_request.save()

    from .notifications import notify_urgent_closure_hr_finalization

    notify_urgent_closure_hr_finalization(closure_request)
    return closure_request


@transaction.atomic
def propose_urgent_closure_period_by_employee(
    closure_request_id,
    *,
    employee,
    start_date,
    end_date,
    comment="",
):
    closure_request = get_urgent_closure_requests_queryset().select_for_update(of=("self",)).get(pk=closure_request_id)
    if not can_employee_review_urgent_closure(employee, closure_request):
        raise ValidationError("Предложить другой период может только сотрудник, которому предложен отпуск.")
    _update_closure_period(closure_request, start_date, end_date)
    closure_request.status = VacationUrgentClosureRequest.STATUS_DEPARTMENT_REVIEW
    closure_request.employee_responded_at = timezone.now()
    closure_request.employee_comment = comment
    closure_request.department_reviewer = None
    closure_request.department_reviewed_at = None
    closure_request.department_comment = ""
    closure_request.save()

    from .notifications import notify_urgent_closure_period_changed_by_employee

    notify_urgent_closure_period_changed_by_employee(closure_request)
    return closure_request


@transaction.atomic
def finalize_urgent_closure(closure_request_id, *, actor, comment=""):
    closure_request = get_urgent_closure_requests_queryset().select_for_update(of=("self",)).get(pk=closure_request_id)
    if not can_finalize_urgent_closure(actor, closure_request):
        raise ValidationError("Финализировать закрытие срочного остатка может только HR.")
    chargeable_days = _validate_urgent_closure_period(
        closure_request.employee,
        closure_request.planning_year,
        closure_request.required_days,
        closure_request.deadline,
        closure_request.proposed_start_date,
        closure_request.proposed_end_date,
    )
    risk_payload = _risk_payload_for_closure(
        closure_request.employee,
        closure_request.proposed_start_date,
        closure_request.proposed_end_date,
    )
    schedule, created = VacationSchedule.objects.get_or_create(
        year=closure_request.closure_year,
        defaults={
            "status": VacationSchedule.STATUS_APPROVED,
            "created_by": actor,
            "approved_by": actor,
            "approved_at": timezone.now(),
        },
    )
    if created is False and schedule.status == VacationSchedule.STATUS_DRAFT:
        schedule.status = VacationSchedule.STATUS_APPROVED
        schedule.approved_by = actor
        schedule.approved_at = timezone.now()
        schedule.save(update_fields=["status", "approved_by", "approved_at"])

    schedule_item = VacationScheduleItem.objects.create(
        schedule=schedule,
        employee=closure_request.employee,
        start_date=closure_request.proposed_start_date,
        end_date=closure_request.proposed_end_date,
        vacation_type="paid",
        chargeable_days=chargeable_days,
        status=VacationScheduleItem.STATUS_APPROVED,
        source=VacationScheduleItem.SOURCE_MANUAL,
        risk_score=risk_payload["risk_score"],
        risk_level=risk_payload["risk_level"],
        generated_by_ai=False,
        was_changed_by_manager=True,
        manager_comment="Создано для закрытия срочного остатка прошлого периода.",
    )

    closure_request.status = VacationUrgentClosureRequest.STATUS_COMPLETED
    closure_request.finalized_by = actor
    closure_request.finalized_at = timezone.now()
    closure_request.final_comment = comment
    closure_request.created_schedule_item = schedule_item
    for field_name, value in risk_payload.items():
        setattr(closure_request, field_name, value)
    closure_request.save()

    from .notifications import notify_urgent_closure_completed

    notify_urgent_closure_completed(closure_request)
    return schedule_item


def enrich_urgent_closure_request(closure_request):
    status_meta = _urgent_closure_status_meta(closure_request)
    closure_request.status_label = status_meta["label"]
    closure_request.status_icon = status_meta["icon"]
    closure_request.status_css_class = status_meta["css_class"]
    closure_request.period_label = _period_label(closure_request.proposed_start_date, closure_request.proposed_end_date)
    closure_request.required_days_label = _days_label(closure_request.required_days)
    closure_request.deadline_label = closure_request.deadline.strftime("%d.%m.%Y")
    closure_request.risk_label = closure_request.get_risk_level_display()
    closure_request.risk_explanation = build_vacation_request_risk_explanation(
        closure_request.employee,
        closure_request.proposed_start_date,
        closure_request.proposed_end_date,
        "paid",
    )
    closure_request.risk_short_reason = closure_request.risk_explanation.get("short_reason", "")
    closure_request.risk_recommended_action = closure_request.risk_explanation.get("recommended_action", "")
    closure_request.risk_is_conflict = closure_request.risk_explanation.get("is_conflict", False)
    closure_request.detail_url = urgent_closure_detail_url(closure_request)
    closure_request.profile_url = f"{reverse('employee_profile', args=[closure_request.employee_id])}?from=applications"
    closure_request.old_period_label = "Срочный остаток"
    closure_request.new_period_label = closure_request.period_label
    closure_request.origin_label = "Закрытие срочного остатка"
    closure_request.is_manager_initiated = True
    closure_request.initiator_name = (
        closure_request.created_by.full_name
        if closure_request.created_by_id and closure_request.created_by
        else "HR"
    )
    enrich_application_employee_presentation(closure_request)
    return closure_request


def _urgent_closure_status_meta(closure_request):
    if closure_request.status == VacationUrgentClosureRequest.STATUS_COMPLETED:
        return REQUEST_STATUS_UI[VacationRequest.STATUS_APPROVED]
    if closure_request.status == VacationUrgentClosureRequest.STATUS_REJECTED:
        return REQUEST_STATUS_UI[VacationRequest.STATUS_REJECTED]
    meta = REQUEST_STATUS_UI[VacationRequest.STATUS_PENDING].copy()
    if closure_request.status == VacationUrgentClosureRequest.STATUS_DEPARTMENT_REVIEW:
        meta["label"] = "У руководителя"
        meta["icon"] = "supervisor_account"
    elif closure_request.status == VacationUrgentClosureRequest.STATUS_EMPLOYEE_REVIEW:
        meta["label"] = "У сотрудника"
        meta["icon"] = "person"
    elif closure_request.status == VacationUrgentClosureRequest.STATUS_HR_FINALIZATION:
        meta["label"] = "Финализация HR"
        meta["icon"] = "verified_user"
    return meta


def urgent_closure_review_status(closure_request):
    if closure_request.status == VacationUrgentClosureRequest.STATUS_COMPLETED:
        return VacationRequest.STATUS_APPROVED
    if closure_request.status == VacationUrgentClosureRequest.STATUS_REJECTED:
        return VacationRequest.STATUS_REJECTED
    return VacationRequest.STATUS_PENDING


def serialize_urgent_closure_request_row(closure_request):
    enrich_urgent_closure_request(closure_request)
    return {
        "id": closure_request.id,
        "employee_name": closure_request.employee.full_name,
        "employee_department": closure_request.employee.department.name if closure_request.employee.department else "Не указан",
        "profile_url": closure_request.profile_url,
        "old_period_label": closure_request.old_period_label,
        "new_period_label": closure_request.period_label,
        "status": urgent_closure_review_status(closure_request),
        "status_label": closure_request.status_label,
        "status_icon": closure_request.status_icon,
        "status_css_class": closure_request.status_css_class,
        "risk_score": closure_request.risk_score,
        "risk_label": closure_request.risk_label,
        "risk_short_reason": closure_request.risk_short_reason,
        "risk_recommended_action": closure_request.risk_recommended_action,
        "risk_is_conflict": closure_request.risk_is_conflict,
        "reason_preview": closure_request.reason,
        "can_approve": getattr(closure_request, "can_approve", False),
        "decision_locked": getattr(closure_request, "decision_locked", False),
        "detail_url": closure_request.detail_url,
        "origin_label": closure_request.origin_label,
        "is_manager_initiated": True,
        "initiator_name": closure_request.initiator_name,
        "required_days_label": closure_request.required_days_label,
        "deadline_label": closure_request.deadline_label,
    } | serialize_application_employee_presentation(closure_request)
