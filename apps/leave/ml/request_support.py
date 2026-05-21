from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.leave.models import VacationRequest, VacationScheduleCandidate, VacationScheduleItem
from apps.leave.ml.scoring import score_candidate_features
from apps.leave.services.dates import format_period_label, get_chargeable_leave_days
from apps.leave.services.risk import build_vacation_request_risk_explanation, calculate_vacation_request_risk
from apps.leave.services.validation import validate_schedule_change_request, validate_vacation_request_for_employee

REQUEST_AI_FEATURE_SCHEMA_VERSION = 1
REQUEST_AI_ALTERNATIVE_LIMIT = 2
REQUEST_AI_ALTERNATIVE_MAX_WEEKS = 12

RISK_LEVEL_FEATURE_WEIGHT = {
    VacationRequest.RISK_LOW: 1,
    VacationRequest.RISK_MEDIUM: 2,
    VacationRequest.RISK_HIGH: 3,
}

RECOMMENDATION_LABELS = {
    "prefer": "Хороший период",
    "normal": "Можно отправлять",
    "avoid": "Лучше проверить даты",
    "blocked": "Сначала исправьте период",
}

RECOMMENDATION_ACTIONS = {
    "prefer": "Модуль считает выбранные даты удачными для заявки.",
    "normal": "Период можно отправить, но руководитель всё равно проверит заявку.",
    "avoid": "Модуль советует посмотреть более спокойный период рядом.",
    "blocked": "Период не проходит жесткие правила, нейромодуль не может разрешить отправку.",
}

TRANSFER_RECOMMENDATION_LABELS = {
    "prefer": "удачный перенос",
    "normal": "можно отправлять",
    "avoid": "лучше проверить",
    "blocked": "есть ограничения",
}

TRANSFER_RECOMMENDATION_ACTIONS = {
    "prefer": "Модуль считает новые даты удачными для графика.",
    "normal": "Новые даты можно отправлять на согласование.",
    "avoid": "Модуль советует внимательно проверить влияние переноса на отдел.",
    "blocked": "Новые даты не проходят жесткие правила переноса.",
}

RECOMMENDATION_EXPLANATION_LABELS = {
    "prefer": "хороший период",
    "normal": "допустимый период",
    "avoid": "период, который лучше проверить",
    "blocked": "заблокированный период",
}

VACATION_TYPE_BASIS = {
    "paid": "employee_paid_request",
    "unpaid": "employee_unpaid_request",
    "study": "employee_study_request",
}


def _percent(value):
    value = max(Decimal("0.00"), min(Decimal("100.00"), Decimal(str(value or 0))))
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _percent_label(value):
    value = _percent(value)
    text = f"{value:.2f}".replace(".", ",")
    return f"{text}%"


def _feature_float(value):
    try:
        return float(Decimal(str(value or 0)))
    except Exception:
        return 0.0


def _feature_ratio(numerator, denominator):
    denominator_value = Decimal(str(denominator or 0))
    if denominator_value <= 0:
        return 0.0
    return round(float(Decimal(str(numerator or 0)) / denominator_value), 4)


def _calendar_days(start_date, end_date):
    if not start_date or not end_date or end_date < start_date:
        return 0
    return (end_date - start_date).days + 1


def _period_months(start_date, end_date):
    if not start_date or not end_date or end_date < start_date:
        return []
    months = []
    cursor = start_date.replace(day=1)
    end_marker = end_date.replace(day=1)
    while cursor <= end_marker and len(months) < 24:
        months.append(cursor.month)
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)
    return months


def _summer_overlap_days(start_date, end_date):
    if not start_date or not end_date or end_date < start_date:
        return 0
    current = start_date
    total = 0
    while current <= end_date:
        if current.month in {6, 7, 8}:
            total += 1
        current += timedelta(days=1)
    return total


def _day_of_year(value):
    return value.timetuple().tm_yday if value else 0


def _employee_tenure_days_at_year_end(employee, year):
    joined = getattr(employee, "date_joined", None)
    if not joined or not year:
        return 0
    return max((joined.replace(year=year, month=12, day=31) - joined).days, 0)


def _risk_feature_payload(risk_payload, risk_explanation):
    risk_explanation = risk_explanation or {}
    details = list(risk_explanation.get("details") or [])
    primary_detail = details[0] if details else {}
    remaining_staff = int(risk_payload.get("remaining_staff_count") or risk_explanation.get("remaining_staff") or 0)
    min_staff_required = int(risk_payload.get("min_staff_required") or risk_explanation.get("required_staff") or 0)
    risk_level = risk_payload.get("risk_level") or VacationRequest.RISK_LOW
    balance_after = risk_payload.get("balance_after_request")
    if balance_after is None:
        balance_after = risk_payload.get("balance_after_change")
    return {
        "risk_score": int(risk_payload.get("risk_score") or 0),
        "risk_level": risk_level,
        "risk_level_weight": RISK_LEVEL_FEATURE_WEIGHT.get(risk_level, 1),
        "risk_is_conflict": bool(risk_explanation.get("is_conflict")),
        "risk_department_load_level": int(risk_payload.get("department_load_level") or 0),
        "risk_overlapping_absences_count": int(risk_payload.get("overlapping_absences_count") or 0),
        "risk_remaining_staff_count": remaining_staff,
        "risk_min_staff_required": min_staff_required,
        "risk_staff_margin": remaining_staff - min_staff_required,
        "risk_balance_after_request": _feature_float(balance_after),
        "risk_substitution_used": bool(risk_explanation.get("substitution_used")),
        "risk_has_substitution_capacity": bool(risk_explanation.get("has_substitution_capacity")),
        "risk_details_count": len(details),
        "risk_primary_detail_kind": primary_detail.get("kind", ""),
    }


def _request_candidate_features(
    employee,
    start_date,
    end_date,
    vacation_type,
    *,
    can_submit,
    risk_payload,
    risk_explanation,
    block_reason_key="",
    candidate_kind=VacationScheduleCandidate.KIND_MANUAL,
    candidate_source=VacationScheduleItem.SOURCE_MANUAL,
    planning_basis=None,
    target_days=None,
):
    calendar_days = _calendar_days(start_date, end_date)
    chargeable_days = get_chargeable_leave_days(start_date, end_date, vacation_type) if calendar_days else 0
    scoring_days = chargeable_days if vacation_type == "paid" else calendar_days
    target_days = max(_feature_float(target_days if target_days is not None else scoring_days), 1.0)
    months = _period_months(start_date, end_date)
    department_id = getattr(employee, "department_id", None) or 0
    position = getattr(employee, "employee_position", None)
    production_group_id = getattr(position, "production_group_id", None) or 0
    year = start_date.year if start_date else None
    balance_after = risk_payload.get("balance_after_request")
    if balance_after is None:
        balance_after = risk_payload.get("balance_after_change")
    return {
        "feature_schema_version": REQUEST_AI_FEATURE_SCHEMA_VERSION,
        "candidate_kind": candidate_kind,
        "candidate_source": candidate_source,
        "candidate_passed_hard_rules": bool(can_submit),
        "candidate_block_reason_key": block_reason_key,
        "employee_role": getattr(employee, "role", ""),
        "employee_is_manager": bool(getattr(employee, "is_management", False)),
        "employee_is_management": bool(getattr(employee, "is_management", False)),
        "employee_is_enterprise_deputy": bool(getattr(employee, "is_enterprise_deputy", False)),
        "employee_department_id": department_id,
        "employee_has_department": bool(department_id),
        "employee_production_group_id": production_group_id,
        "employee_has_production_group": bool(production_group_id),
        "employee_annual_paid_leave_days": int(getattr(employee, "annual_paid_leave_days", 0) or 0),
        "employee_manual_leave_adjustment_days": int(getattr(employee, "manual_leave_adjustment_days", 0) or 0),
        "employee_tenure_days_at_year_end": _employee_tenure_days_at_year_end(employee, year),
        "period_start_month": start_date.month if start_date else 0,
        "period_end_month": end_date.month if end_date else 0,
        "period_start_day_of_year": _day_of_year(start_date),
        "period_end_day_of_year": _day_of_year(end_date),
        "period_calendar_days": calendar_days,
        "period_chargeable_days": _feature_float(scoring_days),
        "period_month_count": len(set(months)),
        "period_crosses_month": bool(start_date and end_date and start_date.month != end_date.month),
        "period_overlaps_summer": bool({6, 7, 8}.intersection(months)),
        "period_summer_overlap_days": _summer_overlap_days(start_date, end_date),
        "planning_available_days": _feature_float(scoring_days),
        "planning_plan_available_days": _feature_float(scoring_days),
        "planning_target_days": _feature_float(target_days),
        "planning_placed_days": 0.0,
        "planning_open_required_days": _feature_float(target_days),
        "planning_blocking_days": 0.0,
        "planning_deadline_blocking_days": 0.0,
        "planning_annual_remaining_days": _feature_float(balance_after),
        "planning_mandatory_days": 0.0,
        "planning_requested_preference_days": 0.0,
        "planning_candidate_target_days": _feature_float(target_days),
        "planning_candidate_coverage_ratio": _feature_ratio(scoring_days, target_days),
        "planning_candidate_over_open_days": max(_feature_float(scoring_days) - _feature_float(target_days), 0.0),
        "planning_basis": planning_basis or VACATION_TYPE_BASIS.get(vacation_type, "employee_request"),
        "planning_remainder_policy": "employee_selected_period",
        "planning_has_blocker": False,
        "planning_needs_manual_attention": False,
        "planning_has_nearest_deadline": False,
        "planning_nearest_deadline_gap_days": 0,
        "planning_ends_by_nearest_deadline": False,
        "planning_mandatory_rows_count": 0,
        "preference_has_preference": False,
        "preference_priority": "",
        "preference_status": "",
        "preference_remainder_policy": "",
        "preference_calendar_days": 0,
        "preference_exact_period_match": False,
        "request_vacation_type": vacation_type,
        "request_is_balance_affecting": vacation_type == "paid",
        **_risk_feature_payload(risk_payload, risk_explanation),
    }


def _request_explanation(scoring, vacation_type, risk_payload, risk_explanation):
    if scoring.recommendation == "blocked":
        text = (
            f"Период не проходит жесткие правила отправки. Оценка {_percent_label(scoring.score)}, "
            f"уверенность {_percent_label(scoring.confidence)}."
        )
        return text

    risk_explanation = risk_explanation or {}
    risk_level = risk_payload.get("risk_level") or VacationRequest.RISK_LOW
    risk_score = int(risk_payload.get("risk_score") or 0)
    factors = []
    if risk_explanation.get("is_conflict"):
        factors.append("есть конфликт по минимальному составу")
    elif risk_level == VacationRequest.RISK_HIGH:
        factors.append("расчетный риск высокий")
    elif risk_level == VacationRequest.RISK_MEDIUM:
        factors.append("есть умеренная нагрузка на отдел")
    else:
        factors.append("критичных пересечений не найдено")

    overlapping_count = int(risk_payload.get("overlapping_absences_count") or 0)
    if overlapping_count:
        factors.append(f"в этот период уже отсутствуют {_employee_count_label(overlapping_count)}")
    if int(risk_payload.get("department_load_level") or 0) >= 4:
        factors.append("нагрузка отдела повышена")

    remaining_staff = int(risk_payload.get("remaining_staff_count") or 0)
    min_staff_required = int(risk_payload.get("min_staff_required") or 0)
    if remaining_staff and min_staff_required:
        factors.append(f"останется {remaining_staff} при минимуме {min_staff_required}")

    recommendation_label = RECOMMENDATION_EXPLANATION_LABELS.get(scoring.recommendation, "допустимый период")
    text = (
        f"Нейромодуль {scoring.model_version} оценил выбранные даты как {recommendation_label}: "
        f"{', '.join(factors[:3])}. Риск {risk_score}%, оценка {_percent_label(scoring.score)}, "
        f"уверенность {_percent_label(scoring.confidence)}."
    )
    if vacation_type != "paid" and scoring.recommendation != "blocked":
        text = (
            f"{text} Баланс оплачиваемого отпуска не списывается; оценка показывает влияние отсутствия "
            "на состав и нагрузку отдела."
        )
    return text


def _risk_label(risk_payload):
    return dict(VacationRequest.RISK_CHOICES).get(risk_payload.get("risk_level"), "Низкий")


def _employee_count_label(value):
    value = int(value or 0)
    last_two = value % 100
    last = value % 10
    if 11 <= last_two <= 14:
        suffix = "сотрудников"
    elif last == 1:
        suffix = "сотрудник"
    elif 2 <= last <= 4:
        suffix = "сотрудника"
    else:
        suffix = "сотрудников"
    return f"{value} {suffix}"


def _score_request_period(
    employee,
    start_date,
    end_date,
    vacation_type,
    *,
    can_submit=None,
    risk_payload=None,
    risk_explanation=None,
    block_reason_key="",
    exclude_request_id=None,
    exclude_schedule_item_id=None,
    candidate_kind=VacationScheduleCandidate.KIND_MANUAL,
    candidate_source=VacationScheduleItem.SOURCE_MANUAL,
    planning_basis=None,
    target_days=None,
):
    if can_submit is None:
        try:
            validate_vacation_request_for_employee(
                employee,
                start_date,
                end_date,
                vacation_type,
                exclude_request_id=exclude_request_id,
                exclude_schedule_item_id=exclude_schedule_item_id,
            )
            can_submit = True
        except ValidationError:
            can_submit = False
            block_reason_key = block_reason_key or "validation_error"
    risk_payload = risk_payload or calculate_vacation_request_risk(
        employee,
        start_date,
        end_date,
        vacation_type,
        exclude_request_id=exclude_request_id,
        exclude_schedule_item_id=exclude_schedule_item_id,
    )
    risk_explanation = risk_explanation or build_vacation_request_risk_explanation(
        employee,
        start_date,
        end_date,
        vacation_type,
        exclude_request_id=exclude_request_id,
        exclude_schedule_item_id=exclude_schedule_item_id,
    )
    features = _request_candidate_features(
        employee,
        start_date,
        end_date,
        vacation_type,
        can_submit=can_submit,
        risk_payload=risk_payload,
        risk_explanation=risk_explanation,
        block_reason_key=block_reason_key,
        candidate_kind=candidate_kind,
        candidate_source=candidate_source,
        planning_basis=planning_basis,
        target_days=target_days,
    )
    scoring = score_candidate_features(features, passed_hard_rules=bool(can_submit))
    return {
        "can_submit": bool(can_submit),
        "start_date": start_date,
        "end_date": end_date,
        "period_label": format_period_label(start_date, end_date),
        "calendar_days": _calendar_days(start_date, end_date),
        "chargeable_days": get_chargeable_leave_days(start_date, end_date, vacation_type),
        "module_score": scoring.score,
        "module_score_label": _percent_label(scoring.score),
        "module_confidence": scoring.confidence,
        "module_confidence_label": _percent_label(scoring.confidence),
        "module_model_version": scoring.model_version,
        "module_recommendation": scoring.recommendation,
        "module_recommendation_label": RECOMMENDATION_LABELS.get(scoring.recommendation, "Можно отправлять"),
        "module_action": RECOMMENDATION_ACTIONS.get(scoring.recommendation, ""),
        "module_explanation": _request_explanation(scoring, vacation_type, risk_payload, risk_explanation),
        "module_scorer_kind": scoring.scorer_kind,
        "risk_label": _risk_label(risk_payload),
        "risk_score": int(risk_payload.get("risk_score") or 0),
        "risk_level": risk_payload.get("risk_level") or VacationRequest.RISK_LOW,
        "risk_is_conflict": bool(risk_explanation.get("is_conflict")),
    }


def _alternative_offsets():
    for week in range(1, REQUEST_AI_ALTERNATIVE_MAX_WEEKS + 1):
        days = week * 7
        yield days
        yield -days


def _build_request_alternatives(employee, start_date, end_date, vacation_type, selected_support):
    if not start_date or not end_date or end_date < start_date:
        return []
    duration = end_date - start_date
    alternatives = []
    seen = {(start_date, end_date)}
    today = timezone.localdate()
    for offset_days in _alternative_offsets():
        candidate_start = start_date + timedelta(days=offset_days)
        candidate_end = candidate_start + duration
        if (candidate_start, candidate_end) in seen:
            continue
        seen.add((candidate_start, candidate_end))
        if candidate_start < today:
            continue
        if candidate_start.year != start_date.year or candidate_end.year != start_date.year:
            continue
        try:
            validate_vacation_request_for_employee(employee, candidate_start, candidate_end, vacation_type)
        except ValidationError:
            continue
        candidate_support = _score_request_period(
            employee,
            candidate_start,
            candidate_end,
            vacation_type,
            can_submit=True,
        )
        alternatives.append(candidate_support)

    alternatives.sort(
        key=lambda item: (
            _percent(item.get("module_score")),
            _percent(item.get("module_confidence")),
            Decimal("100.00") - Decimal(item.get("risk_score") or 0),
            -item["start_date"].toordinal(),
        ),
        reverse=True,
    )
    return alternatives[:REQUEST_AI_ALTERNATIVE_LIMIT]


def build_vacation_request_ai_support(
    employee,
    start_date,
    end_date,
    vacation_type,
    *,
    can_submit=None,
    risk_payload=None,
    risk_explanation=None,
    block_reason_key="",
    include_alternatives=True,
    exclude_request_id=None,
    exclude_schedule_item_id=None,
):
    support = _score_request_period(
        employee,
        start_date,
        end_date,
        vacation_type,
        can_submit=can_submit,
        risk_payload=risk_payload,
        risk_explanation=risk_explanation,
        block_reason_key=block_reason_key,
        exclude_request_id=exclude_request_id,
        exclude_schedule_item_id=exclude_schedule_item_id,
    )
    support["module_alternatives"] = (
        _build_request_alternatives(employee, start_date, end_date, vacation_type, support)
        if include_alternatives
        else []
    )
    return support


def build_schedule_change_ai_support(
    schedule_item,
    new_start_date,
    new_end_date,
    *,
    can_submit=None,
    risk_payload=None,
    risk_explanation=None,
    block_reason_key="",
    exclude_change_request_id=None,
):
    if can_submit is None:
        try:
            validate_schedule_change_request(
                schedule_item,
                new_start_date,
                new_end_date,
                exclude_change_request_id=exclude_change_request_id,
            )
            can_submit = True
        except ValidationError:
            can_submit = False
            block_reason_key = block_reason_key or "validation_error"

    support = _score_request_period(
        schedule_item.employee,
        new_start_date,
        new_end_date,
        schedule_item.vacation_type,
        can_submit=can_submit,
        risk_payload=risk_payload,
        risk_explanation=risk_explanation,
        block_reason_key=block_reason_key,
        exclude_schedule_item_id=schedule_item.id,
        candidate_source=VacationScheduleItem.SOURCE_TRANSFER,
        planning_basis="schedule_change",
        target_days=schedule_item.chargeable_days,
    )
    recommendation = support.get("module_recommendation") or "normal"
    support.update(
        {
            "module_recommendation_label": TRANSFER_RECOMMENDATION_LABELS.get(
                recommendation,
                TRANSFER_RECOMMENDATION_LABELS["normal"],
            ),
            "module_action": TRANSFER_RECOMMENDATION_ACTIONS.get(
                recommendation,
                TRANSFER_RECOMMENDATION_ACTIONS["normal"],
            ),
            "module_alternatives": [],
        }
    )
    return support


def vacation_request_ai_model_fields(ai_support):
    return {
        "ai_score": ai_support.get("module_score"),
        "ai_confidence": ai_support.get("module_confidence"),
        "ai_model_version": ai_support.get("module_model_version") or "",
        "ai_recommendation": ai_support.get("module_recommendation") or "",
        "ai_explanation": ai_support.get("module_explanation") or "",
        "ai_scorer_kind": ai_support.get("module_scorer_kind") or "",
    }


def schedule_change_ai_model_fields(ai_support, *, evaluated_at=None):
    return {
        "ai_score": ai_support.get("module_score"),
        "ai_confidence": ai_support.get("module_confidence"),
        "ai_model_version": ai_support.get("module_model_version") or "",
        "ai_recommendation": ai_support.get("module_recommendation") or "",
        "ai_explanation": ai_support.get("module_explanation") or "",
        "ai_scorer_kind": ai_support.get("module_scorer_kind") or "",
        "ai_evaluated_at": evaluated_at or timezone.now(),
    }


def schedule_change_decision_ai_model_fields(ai_support, *, evaluated_at=None):
    return {
        "decision_ai_score": ai_support.get("module_score"),
        "decision_ai_confidence": ai_support.get("module_confidence"),
        "decision_ai_model_version": ai_support.get("module_model_version") or "",
        "decision_ai_recommendation": ai_support.get("module_recommendation") or "",
        "decision_ai_explanation": ai_support.get("module_explanation") or "",
        "decision_ai_scorer_kind": ai_support.get("module_scorer_kind") or "",
        "decision_ai_evaluated_at": evaluated_at or timezone.now(),
    }


def vacation_request_decision_ai_model_fields(ai_support, *, evaluated_at=None):
    return {
        "decision_ai_score": ai_support.get("module_score"),
        "decision_ai_confidence": ai_support.get("module_confidence"),
        "decision_ai_model_version": ai_support.get("module_model_version") or "",
        "decision_ai_recommendation": ai_support.get("module_recommendation") or "",
        "decision_ai_explanation": ai_support.get("module_explanation") or "",
        "decision_ai_scorer_kind": ai_support.get("module_scorer_kind") or "",
        "decision_ai_evaluated_at": evaluated_at or timezone.now(),
    }
