from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from apps.leave.models import VacationPreference, VacationRequest, VacationScheduleCandidate, VacationScheduleItem
from apps.leave.ml.scoring import score_candidate_features
from apps.leave.services.constants import LEAVE_ADVANCE_MONTHS
from apps.leave.services.dates import add_months_safe, format_period_label, get_chargeable_leave_days, quantize_leave_days
from apps.leave.services.ledger import get_employee_available_balance
from apps.leave.services.risk import calculate_vacation_request_risk_with_explanation
from apps.leave.services.validation import MIN_CONTINUOUS_PAID_LEAVE_DAYS, get_overlapping_requests, get_overlapping_schedule_items

PREFERENCE_AI_FEATURE_SCHEMA_VERSION = 1
PREFERENCE_SCORE_TIE_THRESHOLD = Decimal("5.00")

RISK_LEVEL_FEATURE_WEIGHT = {
    VacationRequest.RISK_LOW: 1,
    VacationRequest.RISK_MEDIUM: 2,
    VacationRequest.RISK_HIGH: 3,
}

EMPLOYEE_ROLE_FEATURE_WEIGHT = {
    "employee": 1,
    "hr": 2,
    "department_head": 3,
    "enterprise_head": 4,
    "authorized_person": 0,
}

PREFERENCE_RECOMMENDATION_LABELS = {
    "prefer": "сильный вариант",
    "normal": "можно выбрать",
    "avoid": "лучше проверить",
    "blocked": "есть ограничения",
}

PREFERENCE_RECOMMENDATION_ACTIONS = {
    "prefer": "Модуль считает этот период удачным для будущего графика.",
    "normal": "Период выглядит допустимым для будущего графика.",
    "avoid": "Модуль советует проверить нагрузку отдела и пересечения.",
    "blocked": "Период не проходит жесткие правила планирования.",
}


def _percent(value):
    value = max(Decimal("0.00"), min(Decimal("100.00"), Decimal(str(value or 0))))
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _percent_label(value):
    value = _percent(value)
    return f"{value:.2f}".replace(".", ",") + "%"


def _feature_float(value):
    try:
        return float(Decimal(str(value or 0)))
    except Exception:
        return 0.0


def _feature_ratio(numerator, denominator):
    denominator = Decimal(str(denominator or 0))
    if denominator <= 0:
        return 0.0
    return round(float(Decimal(str(numerator or 0)) / denominator), 4)


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
    return max((date(year, 12, 31) - joined).days + 1, 0)


def _risk_feature_payload(risk_payload, risk_explanation):
    risk_payload = risk_payload or {}
    risk_explanation = risk_explanation or {}
    details = list(risk_explanation.get("details") or [])
    primary_detail = details[0] if details else {}
    remaining_staff = int(risk_payload.get("remaining_staff_count") or risk_explanation.get("remaining_staff") or 0)
    min_staff_required = int(risk_payload.get("min_staff_required") or risk_explanation.get("required_staff") or 0)
    risk_level = risk_payload.get("risk_level") or VacationRequest.RISK_LOW
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
        "risk_balance_after_request": _feature_float(risk_payload.get("balance_after_request")),
        "risk_substitution_used": bool(risk_explanation.get("substitution_used")),
        "risk_has_substitution_capacity": bool(risk_explanation.get("has_substitution_capacity")),
        "risk_details_count": len(details),
        "risk_primary_detail_kind": primary_detail.get("kind", ""),
    }


def _empty_risk_payload(available_balance):
    return {
        "risk_score": 0,
        "risk_level": VacationRequest.RISK_LOW,
        "department_load_level": 0,
        "overlapping_absences_count": 0,
        "remaining_staff_count": 0,
        "min_staff_required": 0,
        "balance_after_request": available_balance,
        "risk_explanation": {
            "is_conflict": False,
            "details": [],
            "short_reason": "",
            "recommended_action": "",
        },
    }


def _preference_kind(priority):
    if priority == VacationPreference.PRIORITY_PRIMARY:
        return VacationScheduleCandidate.KIND_PRIMARY_PREFERENCE
    return VacationScheduleCandidate.KIND_BACKUP_PREFERENCE


def _priority_label(priority):
    if priority == VacationPreference.PRIORITY_PRIMARY:
        return "Основной вариант"
    return "Запасной вариант"


def _paid_leave_available_from(employee):
    return add_months_safe(employee.date_joined, LEAVE_ADVANCE_MONTHS)


def _validate_preference_period(employee, year, start_date, end_date, available_balance):
    if not start_date or not end_date:
        return False, "missing_period", "Выберите дату начала и окончания."
    if start_date.year != year or end_date.year != year:
        return False, "invalid_year", f"Даты должны быть в пределах {year} года."
    if end_date < start_date:
        return False, "invalid_period", "Дата окончания не может быть раньше даты начала."

    available_from = _paid_leave_available_from(employee)
    if start_date < available_from:
        return False, "too_early", f"Оплачиваемый отпуск доступен с {available_from:%d.%m.%Y}."

    chargeable_days = quantize_leave_days(get_chargeable_leave_days(start_date, end_date, "paid"))
    calendar_days = _calendar_days(start_date, end_date)
    if chargeable_days <= 0:
        return False, "empty_period", "В периоде нет списываемых дней отпуска."
    if available_balance >= MIN_CONTINUOUS_PAID_LEAVE_DAYS and calendar_days < MIN_CONTINUOUS_PAID_LEAVE_DAYS:
        return False, "too_short", f"Выберите не меньше {MIN_CONTINUOUS_PAID_LEAVE_DAYS} дн. подряд."
    if chargeable_days > available_balance:
        return False, "too_many_days", "Период превышает доступный баланс дней."
    if get_overlapping_requests(employee, start_date, end_date).exists():
        return False, "employee_overlap", "У сотрудника уже есть активная заявка на эти даты."
    if get_overlapping_schedule_items(employee, start_date, end_date).exists():
        return False, "schedule_overlap", "На выбранные даты уже есть отпуск в графике."
    return True, "", ""


def _preference_candidate_features(
    *,
    employee,
    year,
    start_date,
    end_date,
    priority,
    remainder_policy,
    can_place,
    block_reason_key,
    risk_payload,
    risk_explanation,
    available_balance,
    target_days,
    requested_preference_days,
):
    calendar_days = _calendar_days(start_date, end_date)
    chargeable_days = get_chargeable_leave_days(start_date, end_date, "paid") if calendar_days else Decimal("0.00")
    months = _period_months(start_date, end_date)
    department_id = getattr(employee, "department_id", None) or 0
    position = getattr(employee, "employee_position", None)
    production_group_id = getattr(position, "production_group_id", None) or 0
    target_days = max(Decimal(str(target_days or chargeable_days or 0)), Decimal("1.00"))
    chargeable_days = Decimal(str(chargeable_days or 0))
    return {
        "feature_schema_version": PREFERENCE_AI_FEATURE_SCHEMA_VERSION,
        "candidate_kind": _preference_kind(priority),
        "candidate_source": VacationScheduleItem.SOURCE_GENERATED,
        "candidate_passed_hard_rules": bool(can_place),
        "candidate_block_reason_key": block_reason_key or "",
        "employee_role": getattr(employee, "role", ""),
        "employee_role_weight": EMPLOYEE_ROLE_FEATURE_WEIGHT.get(getattr(employee, "role", ""), 0),
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
        "period_chargeable_days": _feature_float(chargeable_days),
        "period_month_count": len(set(months)),
        "period_crosses_month": bool(start_date and end_date and start_date.month != end_date.month),
        "period_overlaps_summer": bool({6, 7, 8}.intersection(months)),
        "period_summer_overlap_days": _summer_overlap_days(start_date, end_date),
        "planning_available_days": _feature_float(available_balance),
        "planning_plan_available_days": _feature_float(available_balance),
        "planning_target_days": _feature_float(target_days),
        "planning_placed_days": 0.0,
        "planning_open_required_days": _feature_float(target_days),
        "planning_blocking_days": 0.0,
        "planning_deadline_blocking_days": 0.0,
        "planning_annual_remaining_days": _feature_float(available_balance),
        "planning_mandatory_days": 0.0,
        "planning_requested_preference_days": _feature_float(requested_preference_days),
        "planning_candidate_target_days": _feature_float(target_days),
        "planning_candidate_coverage_ratio": _feature_ratio(chargeable_days, target_days),
        "planning_candidate_over_open_days": max(_feature_float(chargeable_days - target_days), 0.0),
        "planning_basis": "employee_preference",
        "planning_remainder_policy": remainder_policy or VacationPreference.REMAINDER_AUTO,
        "planning_has_blocker": False,
        "planning_needs_manual_attention": False,
        "planning_has_nearest_deadline": False,
        "planning_nearest_deadline_gap_days": 0,
        "planning_ends_by_nearest_deadline": False,
        "planning_mandatory_rows_count": 0,
        "preference_has_preference": True,
        "preference_priority": priority,
        "preference_status": VacationPreference.STATUS_FILLED,
        "preference_remainder_policy": remainder_policy or VacationPreference.REMAINDER_AUTO,
        "preference_calendar_days": calendar_days,
        "preference_exact_period_match": True,
        **_risk_feature_payload(risk_payload, risk_explanation),
    }


def _preference_explanation(scoring, priority, risk_payload, risk_explanation, block_reason):
    if scoring.recommendation == "blocked":
        return block_reason or "Период не проходит жесткие правила планирования."

    factors = []
    risk_explanation = risk_explanation or {}
    risk_level = (risk_payload or {}).get("risk_level") or VacationRequest.RISK_LOW
    if risk_explanation.get("is_conflict"):
        factors.append("есть конфликт по минимальному составу")
    elif risk_level == VacationRequest.RISK_HIGH:
        factors.append("нагрузка отдела высокая")
    elif risk_level == VacationRequest.RISK_MEDIUM:
        factors.append("есть умеренная нагрузка на отдел")
    else:
        factors.append("критичных пересечений не найдено")
    if priority == VacationPreference.PRIORITY_PRIMARY:
        factors.append("это основной выбранный период")
    else:
        factors.append("это запасной выбранный период")
    risk_score = int((risk_payload or {}).get("risk_score") or 0)
    return (
        f"{_priority_label(priority)}: {', '.join(factors[:3])}. "
        f"Риск {risk_score}%, оценка {_percent_label(scoring.score)}, "
        f"уверенность {_percent_label(scoring.confidence)}."
    )


def _score_preference_period(
    *,
    employee,
    year,
    start_date,
    end_date,
    priority,
    remainder_policy,
    available_balance,
    target_days,
    requested_preference_days,
):
    can_place, block_reason_key, block_reason = _validate_preference_period(
        employee,
        year,
        start_date,
        end_date,
        available_balance,
    )
    if start_date and end_date and end_date >= start_date:
        risk_payload = calculate_vacation_request_risk_with_explanation(
            employee,
            start_date,
            end_date,
            "paid",
        )
    else:
        risk_payload = _empty_risk_payload(available_balance)
    risk_explanation = risk_payload.get("risk_explanation") or {}
    features = _preference_candidate_features(
        employee=employee,
        year=year,
        start_date=start_date,
        end_date=end_date,
        priority=priority,
        remainder_policy=remainder_policy,
        can_place=can_place,
        block_reason_key=block_reason_key,
        risk_payload=risk_payload,
        risk_explanation=risk_explanation,
        available_balance=available_balance,
        target_days=target_days,
        requested_preference_days=requested_preference_days,
    )
    scoring = score_candidate_features(features, passed_hard_rules=bool(can_place))
    recommendation = "blocked" if not can_place else scoring.recommendation
    return {
        "key": priority,
        "label": _priority_label(priority),
        "can_place": bool(can_place),
        "start_date": start_date,
        "end_date": end_date,
        "period_label": format_period_label(start_date, end_date) if start_date and end_date and end_date >= start_date else "Не указан",
        "calendar_days": _calendar_days(start_date, end_date),
        "chargeable_days": quantize_leave_days(get_chargeable_leave_days(start_date, end_date, "paid")) if start_date and end_date and end_date >= start_date else Decimal("0.00"),
        "module_score": scoring.score,
        "module_score_label": _percent_label(scoring.score),
        "module_confidence": scoring.confidence,
        "module_confidence_label": _percent_label(scoring.confidence),
        "module_model_version": scoring.model_version,
        "module_recommendation": recommendation,
        "module_recommendation_label": PREFERENCE_RECOMMENDATION_LABELS.get(recommendation, "можно выбрать"),
        "module_action": PREFERENCE_RECOMMENDATION_ACTIONS.get(recommendation, ""),
        "module_explanation": _preference_explanation(scoring, priority, risk_payload, risk_explanation, block_reason),
        "module_scorer_kind": scoring.scorer_kind,
        "risk_score": int(risk_payload.get("risk_score") or 0),
        "risk_level": risk_payload.get("risk_level") or VacationRequest.RISK_LOW,
        "risk_is_conflict": bool(risk_explanation.get("is_conflict")),
        "block_reason_key": block_reason_key,
        "block_reason": block_reason,
    }


def _score_for_winner(option):
    return _percent(option.get("module_score"))


def _comparison_winner(primary, backup):
    primary_can_place = bool(primary and primary.get("can_place"))
    backup_can_place = bool(backup and backup.get("can_place"))
    if not primary_can_place or not backup_can_place:
        return "unavailable"

    primary_score = _score_for_winner(primary)
    backup_score = _score_for_winner(backup)
    if backup_score - primary_score >= PREFERENCE_SCORE_TIE_THRESHOLD:
        return "backup"
    if primary_score - backup_score >= PREFERENCE_SCORE_TIE_THRESHOLD:
        return "primary"
    return "tie"


def _comparison_summary(winner, primary, backup):
    if winner == "primary":
        return f"Основной вариант сильнее · {primary['module_score_label']}"
    if winner == "backup":
        return f"Запасной вариант сильнее · {backup['module_score_label']}"
    if winner == "tie":
        score_label = primary["module_score_label"] if _score_for_winner(primary) >= _score_for_winner(backup) else backup["module_score_label"]
        return f"Варианты близкие · {score_label}"
    return "Сначала исправьте даты"


def _comparison_detail(winner, primary, backup):
    if winner == "primary":
        return primary.get("module_explanation") or "Основной период выглядит сильнее для графика."
    if winner == "backup":
        return backup.get("module_explanation") or "Запасной период выглядит сильнее для графика."
    if winner == "tie":
        return "Оба периода близки по оценке модуля. Можно выбирать по личному удобству."
    return (primary or {}).get("block_reason") or (backup or {}).get("block_reason") or "Проверьте основной и запасной период."


def _comparison_tone(winner, primary, backup):
    if winner == "unavailable":
        return "blocked"
    if winner == "tie":
        return "normal"
    selected = primary if winner == "primary" else backup
    recommendation = selected.get("module_recommendation") or "normal"
    if recommendation == "prefer":
        return "prefer"
    if recommendation == "avoid":
        return "avoid"
    return "normal"


def _build_comparison_payload(primary, backup):
    winner = _comparison_winner(primary, backup)
    return {
        "ok": True,
        "winner": winner,
        "winner_label": {
            "primary": "Лучше основной",
            "backup": "Лучше запасной",
            "tie": "Варианты примерно равны",
            "unavailable": "Нужно исправить даты",
        }.get(winner, "Нужно исправить даты"),
        "summary": _comparison_summary(winner, primary, backup),
        "detail": _comparison_detail(winner, primary, backup),
        "tone": _comparison_tone(winner, primary, backup),
        "primary": primary,
        "backup": backup,
    }


def build_vacation_preference_ai_comparison(
    employee,
    year,
    *,
    primary_start,
    primary_end,
    backup_start,
    backup_end,
    remainder_policy=VacationPreference.REMAINDER_AUTO,
):
    available_balance = quantize_leave_days(get_employee_available_balance(employee, as_of_date=date(year, 12, 31)))
    primary_chargeable = (
        quantize_leave_days(get_chargeable_leave_days(primary_start, primary_end, "paid"))
        if primary_start and primary_end and primary_end >= primary_start
        else Decimal("0.00")
    )
    backup_chargeable = (
        quantize_leave_days(get_chargeable_leave_days(backup_start, backup_end, "paid"))
        if backup_start and backup_end and backup_end >= backup_start
        else Decimal("0.00")
    )
    target_days = primary_chargeable or backup_chargeable or available_balance or Decimal("1.00")
    primary = _score_preference_period(
        employee=employee,
        year=year,
        start_date=primary_start,
        end_date=primary_end,
        priority=VacationPreference.PRIORITY_PRIMARY,
        remainder_policy=remainder_policy,
        available_balance=available_balance,
        target_days=target_days,
        requested_preference_days=primary_chargeable,
    )
    backup = _score_preference_period(
        employee=employee,
        year=year,
        start_date=backup_start,
        end_date=backup_end,
        priority=VacationPreference.PRIORITY_BACKUP,
        remainder_policy=remainder_policy,
        available_balance=available_balance,
        target_days=target_days,
        requested_preference_days=primary_chargeable,
    )
    return _build_comparison_payload(primary, backup)


def vacation_preference_ai_model_fields(ai_support, *, evaluated_at=None):
    return {
        "ai_score": ai_support.get("module_score"),
        "ai_confidence": ai_support.get("module_confidence"),
        "ai_model_version": ai_support.get("module_model_version") or "",
        "ai_recommendation": ai_support.get("module_recommendation") or "",
        "ai_explanation": ai_support.get("module_explanation") or "",
        "ai_scorer_kind": ai_support.get("module_scorer_kind") or "",
        "ai_evaluated_at": evaluated_at or timezone.now(),
    }


def _saved_preference_option(preference):
    return {
        "key": preference.priority,
        "label": _priority_label(preference.priority),
        "can_place": preference.ai_recommendation != "blocked",
        "start_date": preference.start_date,
        "end_date": preference.end_date,
        "period_label": format_period_label(preference.start_date, preference.end_date),
        "module_score": preference.ai_score,
        "module_score_label": _percent_label(preference.ai_score),
        "module_confidence": preference.ai_confidence,
        "module_confidence_label": _percent_label(preference.ai_confidence),
        "module_model_version": preference.ai_model_version,
        "module_recommendation": preference.ai_recommendation or "normal",
        "module_recommendation_label": PREFERENCE_RECOMMENDATION_LABELS.get(preference.ai_recommendation or "normal", "можно выбрать"),
        "module_action": PREFERENCE_RECOMMENDATION_ACTIONS.get(preference.ai_recommendation or "normal", ""),
        "module_explanation": preference.ai_explanation,
        "module_scorer_kind": preference.ai_scorer_kind,
    }


def build_saved_vacation_preference_ai_comparison(primary, backup):
    if (
        primary is None
        or backup is None
        or primary.status != VacationPreference.STATUS_FILLED
        or backup.status != VacationPreference.STATUS_FILLED
        or primary.ai_score is None
        or backup.ai_score is None
    ):
        return None
    payload = _build_comparison_payload(_saved_preference_option(primary), _saved_preference_option(backup))
    payload["source_label"] = "Сохранено при отправке пожеланий"
    return payload
