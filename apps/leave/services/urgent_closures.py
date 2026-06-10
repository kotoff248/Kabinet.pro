from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from math import ceil

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts.services import can_approve_leave_for_employee, is_hr_employee
from apps.leave.models import (
    VacationRequest,
    VacationSchedule,
    VacationScheduleCandidate,
    VacationScheduleItem,
    VacationUrgentClosureRequest,
)

from .approval_routes import get_expected_vacation_approver
from apps.leave.ml.scoring import score_candidate_features
from .constants import REQUEST_STATUS_UI
from .dates import format_period_label, get_chargeable_leave_days, quantize_leave_days
from .employee_presentation import enrich_application_employee_presentation, serialize_application_employee_presentation
from .risk import build_vacation_request_risk_explanation, calculate_vacation_request_risk
from .validation import get_overlapping_requests, get_overlapping_schedule_items


URGENT_CLOSURE_OPTION_LIMIT = 5
URGENT_CLOSURE_OPTION_SCAN_LIMIT = 80
DEMO_EMPLOYEE_RESPONSE_ACCEPT = "accept"
DEMO_EMPLOYEE_RESPONSE_PROPOSE = "propose"
URGENT_CLOSURE_FEATURE_SCHEMA_VERSION = 1
RISK_LEVEL_FEATURE_WEIGHT = {
    VacationRequest.RISK_LOW: 1,
    VacationRequest.RISK_MEDIUM: 2,
    VacationRequest.RISK_HIGH: 3,
}
URGENT_CLOSURE_MODULE_BADGE_RECOMMENDATION_LABELS = {
    "prefer": "удачный период",
    "normal": "можно одобрять",
    "avoid": "лучше проверить",
    "blocked": "есть ограничения",
}
URGENT_CLOSURE_MODULE_BADGE_VARIANTS = {
    "prefer": "planned",
    "normal": "info",
    "avoid": "medium",
    "blocked": "risk",
}


def _format_days(value):
    value = quantize_leave_days(value or Decimal("0"))
    if value == value.to_integral_value():
        return str(int(value))
    return str(value).replace(".", ",").rstrip("0").rstrip(",")


def _days_label(value):
    return f"{_format_days(value)} д."


def _percent(value):
    value = max(Decimal("0.00"), min(Decimal("100.00"), Decimal(str(value or 0))))
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _percent_label(value):
    value = _percent(value)
    text = f"{value:.2f}".replace(".", ",")
    return f"{text}%"


def _percent_badge_label(value):
    value = _percent(value)
    return f"{int(value.quantize(Decimal('1'), rounding=ROUND_HALF_UP))}%"


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
    candidates = set()
    current = latest_end
    while current >= earliest_start and len(candidates) < URGENT_CLOSURE_OPTION_SCAN_LIMIT:
        candidates.add(current)
        current -= timedelta(days=7)

    for candidate in [
        latest_end,
        latest_end - timedelta(days=14),
        latest_end - timedelta(days=30),
        latest_end - timedelta(days=45),
        latest_end - timedelta(days=60),
        latest_end - timedelta(days=90),
        latest_end - timedelta(days=120),
    ]:
        candidates.add(candidate)

    month_anchors = []
    for month in range(1, 13):
        for day in (15, 25):
            try:
                month_anchors.append(date(latest_end.year, month, day))
            except ValueError:
                continue
    unique = []
    seen = set()
    for candidate in [*sorted(candidates, reverse=True), *month_anchors]:
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


def _feature_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _feature_ratio(numerator, denominator):
    denominator = Decimal(str(denominator or 0))
    if denominator <= 0:
        return 0.0
    return _feature_float(Decimal(str(numerator or 0)) / denominator)


def _day_of_year(value):
    return value.timetuple().tm_yday if value else 0


def _period_months(start_date, end_date):
    if not start_date or not end_date:
        return []
    months = []
    current = date(start_date.year, start_date.month, 1)
    end_month = date(end_date.year, end_date.month, 1)
    while current <= end_month:
        months.append(current.month)
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    return months


def _employee_tenure_days_at_year_end(employee, year):
    joined = getattr(employee, "date_joined", None)
    if not joined or not year:
        return 0
    return max((date(year, 12, 31) - joined).days, 0)


def _urgent_closure_candidate_features(
    employee,
    planning_year,
    required_days,
    deadline,
    option,
):
    start_date = option["start_date"]
    end_date = option["end_date"]
    chargeable_days = option["chargeable_days"]
    calendar_days = option["calendar_days"]
    months = _period_months(start_date, end_date)
    risk_score = int(option.get("risk_score") or 0)
    risk_level = option.get("risk_level") or VacationRequest.RISK_LOW
    remaining_staff = int(option.get("remaining_staff_count") or 0)
    min_staff_required = int(option.get("min_staff_required") or 0)
    department_id = getattr(employee, "department_id", None) or 0
    position = getattr(employee, "employee_position", None)
    production_group_id = getattr(position, "production_group_id", None) or 0
    features = {
        "feature_schema_version": URGENT_CLOSURE_FEATURE_SCHEMA_VERSION,
        "candidate_kind": VacationScheduleCandidate.KIND_AUTO_URGENT,
        "candidate_source": VacationScheduleItem.SOURCE_GENERATED,
        "candidate_passed_hard_rules": bool(option["can_submit"]),
        "candidate_block_reason_key": "" if option["can_submit"] else "urgent_closure_invalid_period",
        "employee_role": getattr(employee, "role", ""),
        "employee_is_management": bool(getattr(employee, "is_management", False)),
        "employee_is_enterprise_deputy": bool(getattr(employee, "is_enterprise_deputy", False)),
        "employee_department_id": department_id,
        "employee_has_department": bool(department_id),
        "employee_production_group_id": production_group_id,
        "employee_has_production_group": bool(production_group_id),
        "employee_annual_paid_leave_days": int(getattr(employee, "annual_paid_leave_days", 0) or 0),
        "employee_manual_leave_adjustment_days": int(getattr(employee, "manual_leave_adjustment_days", 0) or 0),
        "employee_tenure_days_at_year_end": _employee_tenure_days_at_year_end(employee, planning_year),
        "period_start_month": start_date.month,
        "period_end_month": end_date.month,
        "period_start_day_of_year": _day_of_year(start_date),
        "period_end_day_of_year": _day_of_year(end_date),
        "period_calendar_days": calendar_days,
        "period_chargeable_days": _feature_float(chargeable_days),
        "period_month_count": len(set(months)),
        "period_crosses_month": bool(start_date.month != end_date.month),
        "period_overlaps_summer": bool({6, 7, 8}.intersection(months)),
        "period_summer_overlap_days": 0,
        "planning_available_days": _feature_float(required_days),
        "planning_plan_available_days": _feature_float(required_days),
        "planning_target_days": _feature_float(required_days),
        "planning_placed_days": 0.0,
        "planning_open_required_days": _feature_float(required_days),
        "planning_blocking_days": _feature_float(required_days),
        "planning_deadline_blocking_days": _feature_float(required_days),
        "planning_annual_remaining_days": 0.0,
        "planning_mandatory_days": _feature_float(required_days),
        "planning_requested_preference_days": 0.0,
        "planning_candidate_target_days": _feature_float(required_days),
        "planning_candidate_coverage_ratio": _feature_ratio(chargeable_days, required_days),
        "planning_candidate_over_open_days": max(_feature_float(chargeable_days) - _feature_float(required_days), 0.0),
        "planning_basis": "urgent_closure",
        "planning_remainder_policy": "deadline_closure",
        "planning_has_blocker": True,
        "planning_needs_manual_attention": False,
        "planning_has_nearest_deadline": True,
        "planning_nearest_deadline_gap_days": (deadline - end_date).days,
        "planning_ends_by_nearest_deadline": end_date <= deadline,
        "planning_mandatory_rows_count": 1,
        "preference_has_preference": False,
        "preference_priority": "",
        "preference_status": "",
        "preference_remainder_policy": "",
        "preference_calendar_days": 0,
        "preference_exact_period_match": False,
        "risk_score": risk_score,
        "risk_level": risk_level,
        "risk_level_weight": RISK_LEVEL_FEATURE_WEIGHT.get(risk_level, 1),
        "risk_is_conflict": bool(option.get("risk_is_conflict")),
        "risk_department_load_level": int(option.get("department_load_level") or 0),
        "risk_overlapping_absences_count": int(option.get("overlapping_absences_count") or 0),
        "risk_remaining_staff_count": remaining_staff,
        "risk_min_staff_required": min_staff_required,
        "risk_staff_margin": remaining_staff - min_staff_required,
        "risk_balance_after_request": _feature_float(option.get("balance_after_closure")),
        "risk_substitution_used": bool(option.get("risk_substitution_used")),
        "risk_has_substitution_capacity": bool(option.get("risk_has_substitution_capacity")),
        "risk_details_count": int(option.get("risk_details_count") or 0),
        "risk_primary_detail_kind": option.get("risk_primary_detail_kind", ""),
    }
    return features


def _apply_urgent_closure_scoring(employee, planning_year, required_days, deadline, option):
    features = _urgent_closure_candidate_features(employee, planning_year, required_days, deadline, option)
    scoring = score_candidate_features(features, passed_hard_rules=bool(option["can_submit"]))
    option.update(
        {
            "features": features,
            "module_score": scoring.score,
            "module_score_label": _percent_label(scoring.score),
            "module_confidence": scoring.confidence,
            "module_confidence_label": _percent_label(scoring.confidence),
            "module_model_version": scoring.model_version,
            "module_recommendation": scoring.recommendation,
            "module_explanation": scoring.explanation,
            "module_scorer_kind": scoring.scorer_kind,
        }
    )
    return option


def _build_option_payload(employee, planning_year, start_date, end_date, required_days, deadline):
    chargeable_days = get_chargeable_leave_days(start_date, end_date, "paid")
    calendar_days = (end_date - start_date).days + 1
    overlap = _period_has_employee_overlap(employee, start_date, end_date)
    risk_payload = calculate_vacation_request_risk(employee, start_date, end_date, "paid")
    risk_explanation = build_vacation_request_risk_explanation(employee, start_date, end_date, "paid")
    risk_details = risk_explanation.get("details") or []
    primary_detail = risk_details[0] if risk_details else {}
    risk_label = dict(VacationRequest.RISK_CHOICES).get(risk_payload["risk_level"], "Низкий")
    can_submit = not overlap and chargeable_days == required_days
    message = "Можно отправить руководителю и сотруднику."
    if overlap:
        message = "У сотрудника уже есть отпуск или заявка на эти даты."
    elif risk_explanation.get("is_conflict"):
        message = "Есть конфликт состава, но вариант можно отправить на проверку руководителю."
    elif risk_payload["risk_level"] == VacationRequest.RISK_HIGH:
        message = "Риск высокий: руководителю стоит проверить состав отдела."

    option = {
        "start_date": start_date,
        "end_date": end_date,
        "period_label": _period_label(start_date, end_date),
        "calendar_days": calendar_days,
        "chargeable_days": chargeable_days,
        "chargeable_days_label": _days_label(chargeable_days),
        "risk_level": risk_payload["risk_level"],
        "risk_label": risk_label,
        "risk_score": risk_payload["risk_score"],
        "department_load_level": risk_payload.get("department_load_level") or 1,
        "overlapping_absences_count": risk_payload.get("overlapping_absences_count") or 0,
        "remaining_staff_count": risk_payload.get("remaining_staff_count") or 0,
        "min_staff_required": risk_payload.get("min_staff_required") or 0,
        "balance_after_closure": risk_payload.get("balance_after_request") or 0,
        "risk_short_reason": risk_explanation.get("short_reason", ""),
        "risk_recommended_action": risk_explanation.get("recommended_action", ""),
        "risk_is_conflict": risk_explanation.get("is_conflict", False),
        "risk_substitution_used": risk_explanation.get("substitution_used", False),
        "risk_has_substitution_capacity": risk_explanation.get("has_substitution_capacity", False),
        "risk_details_count": len(risk_details),
        "risk_primary_detail_kind": primary_detail.get("kind", ""),
        "can_submit": can_submit,
        "message": message,
    }
    return _apply_urgent_closure_scoring(employee, planning_year, required_days, deadline, option)


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
        options.append(_build_option_payload(employee, planning_year, start_date, end_date, target_days, deadline))

    ranked_options = sorted(
        options,
        key=lambda option: (
            not option["can_submit"],
            option["risk_is_conflict"],
            -_percent(option.get("module_score")),
            -_percent(option.get("module_confidence")),
            option["risk_score"],
            -option["end_date"].toordinal(),
        ),
    )
    return ranked_options[:limit]


def build_urgent_closure_preview(*, employee, planning_year, required_days, deadline, start_date, end_date):
    required_days = quantize_leave_days(required_days)
    _validate_urgent_closure_period(employee, planning_year, required_days, deadline, start_date, end_date)
    return _build_option_payload(employee, planning_year, start_date, end_date, required_days, deadline)


def detect_previous_year_closure_need(employee, planning_year, planning_need, *, include_options=True):
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
    if deadline < timezone.localdate():
        return None

    active_request = (
        _active_urgent_closure_queryset()
        .filter(employee=employee, planning_year=planning_year, deadline=deadline)
        .order_by("-created_at", "-id")
        .first()
    )
    options = (
        []
        if active_request or not include_options
        else build_urgent_closure_options(employee, planning_year, required_days, deadline)
    )
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
        "options_url": reverse("urgent_closure_options", args=[planning_year, employee.id]),
        "modal_id": f"urgent-closure-{planning_year}-{employee.id}",
        "options": options,
        "can_create": False if active_request else (bool(options) if include_options else True),
        "explanation": (
            f"В графике {planning_year} до {deadline:%d.%m.%Y} не хватает списываемых дней, "
            f"поэтому остаток нужно закрыть в {planning_year - 1} году."
        ),
    }


def _active_urgent_closure_payload(active_request):
    employee = active_request.employee
    planning_year = active_request.planning_year
    return {
        "required_days": active_request.required_days,
        "required_days_label": _days_label(active_request.required_days),
        "deadline": active_request.deadline,
        "deadline_label": active_request.deadline.strftime("%d.%m.%Y"),
        "closure_year": active_request.closure_year,
        "active_request": active_request,
        "active_request_url": urgent_closure_detail_url(active_request),
        "create_url": reverse("urgent_closure_create", args=[planning_year, employee.id]),
        "preview_url": reverse("urgent_closure_preview", args=[planning_year, employee.id]),
        "modal_id": f"urgent-closure-{planning_year}-{employee.id}",
        "options": [],
        "can_create": False,
        "explanation": (
            f"Срочное закрытие уже отправлено на согласование: "
            f"{_period_label(active_request.proposed_start_date, active_request.proposed_end_date)}, "
            f"нужно закрыть {_days_label(active_request.required_days)} до {active_request.deadline:%d.%m.%Y}."
        ),
    }


def get_active_urgent_closure_payload(employee, planning_year):
    active_request = (
        _active_urgent_closure_queryset()
        .select_related("employee")
        .filter(employee=employee, planning_year=planning_year)
        .order_by("deadline", "-created_at", "-id")
        .first()
    )
    if active_request is None:
        return None
    return _active_urgent_closure_payload(active_request)


def get_active_urgent_closure_payload_map(employee_ids, planning_year):
    payloads = {}
    active_requests = (
        _active_urgent_closure_queryset()
        .select_related("employee")
        .filter(employee_id__in=employee_ids, planning_year=planning_year)
        .order_by("employee_id", "deadline", "-created_at", "-id")
    )
    for active_request in active_requests:
        if active_request.employee_id in payloads:
            continue
        payloads[active_request.employee_id] = _active_urgent_closure_payload(active_request)
    return payloads


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


def _risk_payload_from_urgent_closure_option(option):
    return {
        "risk_score": option["risk_score"],
        "risk_level": option["risk_level"],
        "department_load_level": option["department_load_level"],
        "overlapping_absences_count": option["overlapping_absences_count"],
        "remaining_staff_count": option["remaining_staff_count"],
        "min_staff_required": option["min_staff_required"],
        "balance_after_closure": option["balance_after_closure"],
    }


def _urgent_closure_ai_model_fields(option, *, evaluated_at=None):
    return {
        "ai_score": option.get("module_score"),
        "ai_confidence": option.get("module_confidence"),
        "ai_model_version": option.get("module_model_version") or "",
        "ai_recommendation": option.get("module_recommendation") or "",
        "ai_explanation": option.get("module_explanation") or "",
        "ai_scorer_kind": option.get("module_scorer_kind") or "",
        "ai_evaluated_at": evaluated_at or timezone.now(),
    }


def _urgent_closure_decision_ai_model_fields(option, *, evaluated_at=None):
    return {
        "decision_ai_score": option.get("module_score"),
        "decision_ai_confidence": option.get("module_confidence"),
        "decision_ai_model_version": option.get("module_model_version") or "",
        "decision_ai_recommendation": option.get("module_recommendation") or "",
        "decision_ai_explanation": option.get("module_explanation") or "",
        "decision_ai_scorer_kind": option.get("module_scorer_kind") or "",
        "decision_ai_evaluated_at": evaluated_at or timezone.now(),
    }


def _current_urgent_closure_option(closure_request):
    return _build_option_payload(
        closure_request.employee,
        closure_request.planning_year,
        closure_request.proposed_start_date,
        closure_request.proposed_end_date,
        closure_request.required_days,
        closure_request.deadline,
    )


def _urgent_closure_module_badge_payload(*, score, recommendation, explanation, source_label):
    if score is None or score == "":
        return None
    score_label = _percent_badge_label(score)
    recommendation = recommendation or "normal"
    return {
        "score_label": score_label,
        "recommendation": recommendation,
        "recommendation_label": URGENT_CLOSURE_MODULE_BADGE_RECOMMENDATION_LABELS.get(
            recommendation,
            URGENT_CLOSURE_MODULE_BADGE_RECOMMENDATION_LABELS["normal"],
        ),
        "variant": URGENT_CLOSURE_MODULE_BADGE_VARIANTS.get(
            recommendation,
            URGENT_CLOSURE_MODULE_BADGE_VARIANTS["normal"],
        ),
        "source_label": source_label,
        "tooltip_text": explanation or f"Оценка модуля: {source_label.lower()}.",
    }


def _urgent_closure_module_badge(closure_request):
    if closure_request.status in VacationUrgentClosureRequest.TERMINAL_STATUSES:
        return _urgent_closure_module_badge_payload(
            score=getattr(closure_request, "decision_ai_score", None),
            recommendation=getattr(closure_request, "decision_ai_recommendation", ""),
            explanation=getattr(closure_request, "decision_ai_explanation", ""),
            source_label="На момент решения",
        )
    return _urgent_closure_module_badge_payload(
        score=getattr(closure_request, "ai_score", None),
        recommendation=getattr(closure_request, "ai_recommendation", ""),
        explanation=getattr(closure_request, "ai_explanation", ""),
        source_label="Текущий период",
    )


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

    option = build_urgent_closure_preview(
        employee=employee,
        planning_year=planning_year,
        required_days=required_days,
        deadline=deadline,
        start_date=start_date,
        end_date=end_date,
    )
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
        **_risk_payload_from_urgent_closure_option(option),
        **_urgent_closure_ai_model_fields(option),
    )

    from .notifications import notify_urgent_closure_created

    notify_urgent_closure_created(closure_request)
    return closure_request


def _demo_alternative_urgent_closure_option(closure_request):
    current_period = (closure_request.proposed_start_date, closure_request.proposed_end_date)
    for option in build_urgent_closure_options(
        closure_request.employee,
        closure_request.planning_year,
        closure_request.required_days,
        closure_request.deadline,
        limit=12,
    ):
        candidate_period = (option["start_date"], option["end_date"])
        if candidate_period == current_period or not option["can_submit"]:
            continue
        return option
    return None


@transaction.atomic
def apply_urgent_closure_demo_responses(
    closure_request,
    *,
    auto_manager=False,
    auto_employee=False,
    employee_response=DEMO_EMPLOYEE_RESPONSE_ACCEPT,
):
    result = {
        "manager_approved": False,
        "employee_accepted": False,
        "employee_proposed": False,
        "employee_skipped_reason": "",
    }
    if not auto_manager:
        return result

    reviewer = _get_expected_reviewer(closure_request.employee)
    closure_request = approve_urgent_closure_by_manager(
        closure_request.id,
        reviewer=reviewer,
        comment="Демо: руководитель подтвердил период.",
    )
    result["manager_approved"] = True

    if not auto_employee:
        return result

    if employee_response == DEMO_EMPLOYEE_RESPONSE_PROPOSE:
        alternative = _demo_alternative_urgent_closure_option(closure_request)
        if alternative is None:
            result["employee_skipped_reason"] = "не найден другой допустимый период для демо-ответа сотрудника"
            return result
        propose_urgent_closure_period_by_employee(
            closure_request.id,
            employee=closure_request.employee,
            start_date=alternative["start_date"],
            end_date=alternative["end_date"],
            comment="Демо: сотрудник предложил другой период.",
        )
        result["employee_proposed"] = True
        return result

    accept_urgent_closure_by_employee(
        closure_request.id,
        employee=closure_request.employee,
        comment="Демо: сотрудник принял предложенный период.",
    )
    result["employee_accepted"] = True
    return result


def _update_closure_period(closure_request, start_date, end_date):
    option = build_urgent_closure_preview(
        employee=closure_request.employee,
        planning_year=closure_request.planning_year,
        required_days=closure_request.required_days,
        deadline=closure_request.deadline,
        start_date=start_date,
        end_date=end_date,
    )
    closure_request.proposed_start_date = start_date
    closure_request.proposed_end_date = end_date
    closure_request.closure_year = start_date.year
    for field_name, value in _risk_payload_from_urgent_closure_option(option).items():
        setattr(closure_request, field_name, value)
    for field_name, value in _urgent_closure_ai_model_fields(option).items():
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
    rejected_at = timezone.now()
    decision_option = _current_urgent_closure_option(closure_request)
    decision_ai_fields = _urgent_closure_decision_ai_model_fields(decision_option, evaluated_at=rejected_at)
    closure_request.status = VacationUrgentClosureRequest.STATUS_REJECTED
    closure_request.rejected_by = actor
    closure_request.rejected_at = rejected_at
    closure_request.rejection_comment = comment
    for field_name, value in decision_ai_fields.items():
        setattr(closure_request, field_name, value)
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
    decision_option = build_urgent_closure_preview(
        employee=closure_request.employee,
        planning_year=closure_request.planning_year,
        required_days=closure_request.required_days,
        deadline=closure_request.deadline,
        start_date=closure_request.proposed_start_date,
        end_date=closure_request.proposed_end_date,
    )
    chargeable_days = int(decision_option["chargeable_days"])
    risk_payload = _risk_payload_from_urgent_closure_option(decision_option)
    finalized_at = timezone.now()
    decision_ai_fields = _urgent_closure_decision_ai_model_fields(decision_option, evaluated_at=finalized_at)
    schedule, created = VacationSchedule.objects.get_or_create(
        year=closure_request.closure_year,
        defaults={
            "status": VacationSchedule.STATUS_APPROVED,
            "created_by": actor,
            "approved_by": actor,
            "approved_at": finalized_at,
        },
    )
    if created is False and schedule.status == VacationSchedule.STATUS_DRAFT:
        schedule.status = VacationSchedule.STATUS_APPROVED
        schedule.approved_by = actor
        schedule.approved_at = finalized_at
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
    closure_request.finalized_at = finalized_at
    closure_request.final_comment = comment
    closure_request.created_schedule_item = schedule_item
    for field_name, value in risk_payload.items():
        setattr(closure_request, field_name, value)
    for field_name, value in decision_ai_fields.items():
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
    closure_request.module_badge = _urgent_closure_module_badge(closure_request)
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
        "module_badge": closure_request.module_badge,
        "can_approve": getattr(closure_request, "can_approve", False),
        "decision_locked": getattr(closure_request, "decision_locked", False),
        "detail_url": closure_request.detail_url,
        "origin_label": closure_request.origin_label,
        "is_manager_initiated": True,
        "initiator_name": closure_request.initiator_name,
        "required_days_label": closure_request.required_days_label,
        "deadline_label": closure_request.deadline_label,
    } | serialize_application_employee_presentation(closure_request)
