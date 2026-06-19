import calendar
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from math import ceil
from types import SimpleNamespace
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Avg
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from apps.accounts.services import get_managed_department_id, is_department_head_employee, is_hr_employee
from apps.leave.models import (
    DepartmentWorkload,
    VacationPreference,
    VacationPreferenceCollection,
    VacationRequest,
    VacationSchedule,
    VacationScheduleCandidate,
    VacationScheduleCandidatePackage,
    VacationScheduleCandidatePackagePeriod,
    VacationScheduleDepartmentApproval,
    VacationScheduleGenerationRun,
    VacationScheduleItem,
    VacationScheduleManualSuggestionCache,
)
from apps.leave.services.candidate_feedback import build_schedule_candidate_feedback_context
from apps.leave.services.dates import format_period_label, get_chargeable_leave_days, quantize_leave_days
from apps.leave.services.employee_presentation import get_employee_identity_presentation
from apps.leave.services.ledger import (
    get_employee_available_balance,
    get_employee_entitlement_rows,
    get_employee_entitlement_rows_bulk,
    get_employee_list_leave_summaries,
)
from apps.leave.ml.scoring import ACTIVE_CANDIDATE_SCORER_VERSION, score_candidate_features
from apps.leave.services.preferences import (
    get_eligible_preference_employees,
    get_employee_preference_pair_map,
    get_employee_preference_pair,
    get_employee_preference_state_map,
    get_employee_preference_state,
    get_paid_leave_available_from,
)
from apps.leave.services.planning_cycles import is_active_planning_year
from apps.leave.services.risk import calculate_vacation_request_risk_with_explanation
from apps.leave.services.schedule_auto_place_jobs import get_active_schedule_auto_place_job, schedule_auto_place_job_page_payload
from apps.leave.services.staffing import format_staff_count
from apps.leave.services.urgent_closures import detect_previous_year_closure_need, get_active_urgent_closure_payload_map
from apps.leave.services.validation import MIN_CONTINUOUS_PAID_LEAVE_DAYS, get_overlapping_requests, get_overlapping_schedule_items

from apps.leave.services.schedule_drafts.constants import *
from apps.leave.services.schedule_drafts.types import *


def _decimal_to_whole_days(value):
    value = quantize_leave_days(value or Decimal("0.00"))
    if value <= 0:
        return 0
    return int(ceil(float(value)))


def _end_date_for_chargeable_days(start_date, target_days, latest_end):
    if target_days <= 0 or start_date > latest_end:
        return None

    current = start_date
    while current <= latest_end:
        if get_chargeable_leave_days(start_date, current, "paid") == target_days:
            return current
        if get_chargeable_leave_days(start_date, current, "paid") > target_days:
            return None
        current += timedelta(days=1)
    return None


def _start_date_for_chargeable_days(end_date, target_days, earliest_start):
    if target_days <= 0 or end_date < earliest_start:
        return None

    current = end_date
    while current >= earliest_start:
        chargeable_days = get_chargeable_leave_days(current, end_date, "paid")
        if chargeable_days == target_days:
            return current
        if chargeable_days > target_days:
            return None
        current -= timedelta(days=1)
    return None


def _low_workload_months_for_employee(employee, year):
    department_id = getattr(employee, "department_id", None)
    if not department_id:
        return set()
    workloads = list(
        DepartmentWorkload.objects.filter(
            department_id=department_id,
            year=year,
        )
        .order_by("load_level", "month")
        .values_list("month", "load_level")
    )
    if not workloads:
        return set()
    lowest_load = workloads[0][1]
    return {month for month, load_level in workloads if load_level == lowest_load}


def _candidate_start_dates(year, employee, start_bound, latest_end, *, urgent=False, target_days=None, low_workload_months=None):
    if start_bound > latest_end:
        return []

    planning_window_days = (latest_end - start_bound).days
    if planning_window_days <= 45:
        return [start_bound + timedelta(days=offset) for offset in range((latest_end - start_bound).days + 1)]

    if low_workload_months is None:
        low_workload_months = _low_workload_months_for_employee(employee, year)
    low_workload_months = set(low_workload_months or [])
    preferred_month = ((employee.id * 5) % 12) + 1
    starts = set()
    starts.add(start_bound)

    if urgent:
        target_days = target_days or AUTO_DRAFT_FALLBACK_CHUNK_DAYS
        for offset in (
            target_days + 14,
            target_days + 7,
            target_days,
            max(1, target_days - 7),
        ):
            candidate = latest_end - timedelta(days=offset)
            if start_bound <= candidate <= latest_end:
                starts.add(candidate)

    for month in range(1, 13):
        last_day = calendar.monthrange(year, month)[1]
        for day in AUTO_DRAFT_ANCHOR_DAYS:
            candidate = date(year, month, min(day, last_day))
            if start_bound <= candidate <= latest_end:
                starts.add(candidate)

    def sort_key(value):
        if urgent:
            return (value,)
        if value.month in low_workload_months:
            return (0, value.month, value.day, value)
        forward_distance = (value.month - preferred_month) % 12
        backward_distance = (preferred_month - value.month) % 12
        return (1, min(forward_distance, backward_distance), value.day, value)

    return sorted(starts, key=sort_key)


def _auto_target_day_options(target_days):
    target_days = _decimal_to_whole_days(target_days)
    if target_days <= 0:
        return []

    options = [target_days]
    for fallback_days in AUTO_DRAFT_FALLBACK_STEPS:
        if target_days > fallback_days:
            options.append(fallback_days)
    return options


def _current_placements_from_items(items):
    return [
        DraftPlacement(item.employee_id, item.start_date, item.end_date, item.id)
        for item in items
    ]


def _draft_items_by_employee(items):
    grouped = {}
    for item in items:
        grouped.setdefault(item.employee_id, []).append(item)
    return grouped


def _build_draft_generation_context(year, schedule):
    eligible_employees = list(get_eligible_preference_employees(year))
    draft_items = _draft_items_for_schedule(schedule)
    grouped_items = _draft_items_by_employee(draft_items)
    employee_ids = [employee.id for employee in eligible_employees]
    preference_pair_by_employee = get_employee_preference_pair_map(employee_ids, year)
    preference_state_by_employee = get_employee_preference_state_map(employee_ids, year)
    planning_need_by_employee = build_employee_schedule_planning_need_map(
        eligible_employees,
        year,
        draft_items_by_employee=grouped_items,
        preference_pair_by_employee=preference_pair_by_employee,
        preference_state_by_employee=preference_state_by_employee,
    )
    planning_static_by_employee = {
        employee_id: {
            "available_days": planning_need["available_days"],
            "plan_available_days": planning_need["plan_available_days"],
            "entitlement_rows": planning_need["entitlement_rows"],
        }
        for employee_id, planning_need in planning_need_by_employee.items()
    }
    return DraftGenerationContext(
        year=year,
        schedule=schedule,
        eligible_employees=eligible_employees,
        draft_items_by_employee=grouped_items,
        preference_pair_by_employee=preference_pair_by_employee,
        preference_state_by_employee=preference_state_by_employee,
        placements=_current_placements_from_items(draft_items),
        planning_need_by_employee=planning_need_by_employee,
        planning_static_by_employee=planning_static_by_employee,
    )


def _current_employee_planning_need(context, employee):
    static = getattr(context, "planning_static_by_employee", {}).get(employee.id)
    if static:
        draft_items = context.draft_items_by_employee.get(employee.id, [])
        preference_pair = context.preference_pair_by_employee.get(employee.id)
        preference_state = context.preference_state_by_employee.get(employee.id)
        return _build_employee_schedule_planning_need_from_rows(
            employee,
            context.year,
            draft_items,
            static["available_days"],
            static["plan_available_days"],
            static["entitlement_rows"],
            requested_preference_days=_requested_preference_days_for_plan(
                preference_pair,
                preference_state,
                draft_items,
            ),
            remainder_policy=_preference_remainder_policy(preference_pair, preference_state),
            preference_state=preference_state,
        )
    return build_employee_schedule_planning_need(
        employee,
        context.year,
        context.draft_items_by_employee.get(employee.id, []),
        preference_pair=context.preference_pair_by_employee.get(employee.id),
        preference_state=context.preference_state_by_employee.get(employee.id),
    )


def _preference_candidate_kind(priority):
    if priority == VacationPreference.PRIORITY_PRIMARY:
        return DRAFT_CANDIDATE_PRIMARY_PREFERENCE
    return DRAFT_CANDIDATE_BACKUP_PREFERENCE


def _iter_preference_generation_candidates(employee, pair):
    pair = pair or {}
    for priority in (VacationPreference.PRIORITY_PRIMARY, VacationPreference.PRIORITY_BACKUP):
        preference = pair.get(priority)
        label = _selected_preference_label(preference, pair).lower()
        yield DraftGenerationCandidate(
            employee=employee,
            start_date=preference.start_date if preference else None,
            end_date=preference.end_date if preference else None,
            kind=_preference_candidate_kind(priority),
            source=VacationScheduleItem.SOURCE_GENERATED,
            comment=f"Создано из сбора пожеланий: {label}.",
            preference=preference,
            metadata={"priority": priority},
        )


def _candidate_assessment_reason(assessment):
    return assessment.get("reason") or _manual_reason("unknown", "")


def _apply_hard_rule_assessment(candidate, assessment):
    candidate.metadata = candidate.metadata or {}
    candidate.assessment = assessment
    passed = bool(assessment.get("can_place"))
    reason = _candidate_assessment_reason(assessment)
    risk_payload = assessment.get("risk_payload") or {}
    candidate.metadata.update(
        {
            "passed_hard_rules": passed,
            "block_reason_key": "" if passed else reason.get("kind", ""),
            "block_reason": "" if passed else reason.get("text", ""),
            "block_reason_detail": "" if passed else reason.get("detail", ""),
            "risk_score": risk_payload.get("risk_score", 0),
            "risk_level": risk_payload.get("risk_level", VacationRequest.RISK_LOW),
            "chargeable_days": assessment.get("chargeable_days", 0),
        }
    )
    return candidate


def _apply_planning_need_metadata(candidate, planning_need, year):
    planning_need = planning_need or {}
    candidate.metadata.update(
        {
            "planning_year": year,
            "available_days": planning_need.get("available_days", Decimal("0.00")),
            "plan_available_days": planning_need.get("plan_available_days", Decimal("0.00")),
            "target_days": planning_need.get("target_days", Decimal("0.00")),
            "placed_days": planning_need.get("placed_days", Decimal("0.00")),
            "open_required_days": planning_need.get("open_required_days", Decimal("0.00")),
            "blocking_days": planning_need.get("blocking_days", Decimal("0.00")),
            "deadline_blocking_days": planning_need.get("deadline_blocking_days", Decimal("0.00")),
            "annual_remaining_days": planning_need.get("annual_remaining_days", Decimal("0.00")),
            "mandatory_days": planning_need.get("mandatory_days", Decimal("0.00")),
            "requested_preference_days": planning_need.get("requested_preference_days", Decimal("0.00")),
            "planning_basis": planning_need.get("planning_basis", ""),
            "remainder_policy": planning_need.get("remainder_policy", VacationPreference.REMAINDER_AUTO),
            "has_blocker": bool(planning_need.get("has_blocker")),
            "needs_manual_attention": bool(planning_need.get("needs_manual_attention")),
            "nearest_deadline": planning_need.get("nearest_deadline"),
            "mandatory_rows_count": len(planning_need.get("mandatory_rows") or []),
        }
    )
    return candidate


def _candidate_passed_hard_rules(candidate):
    if "passed_hard_rules" in candidate.metadata:
        return bool(candidate.metadata["passed_hard_rules"])
    return bool(candidate.assessment and candidate.assessment.get("can_place"))


def _assess_generation_candidate_hard_rules(
    candidate,
    year,
    placements,
    *,
    max_chargeable_days=None,
    exclude_schedule_item_id=None,
    exclude_schedule_item_ids=None,
    assessment_cache=None,
    risk_context_cache=None,
    include_risk_explanation=True,
):
    assessment = assess_schedule_draft_candidate(
        candidate.employee,
        candidate.start_date,
        candidate.end_date,
        year,
        placements,
        max_chargeable_days=max_chargeable_days,
        exclude_schedule_item_id=exclude_schedule_item_id,
        exclude_schedule_item_ids=exclude_schedule_item_ids,
        assessment_cache=assessment_cache,
        risk_context_cache=risk_context_cache,
        include_risk_explanation=include_risk_explanation,
    )
    return _apply_hard_rule_assessment(candidate, assessment)


def _build_preference_generation_candidates(context, employee):
    candidates = list(
        _iter_preference_generation_candidates(
            employee,
            context.preference_pair_by_employee.get(employee.id),
        )
    )
    planning_need = context.planning_need_by_employee.get(employee.id)
    for candidate in candidates:
        _apply_planning_need_metadata(candidate, planning_need, context.year)
        _assess_generation_candidate_hard_rules(
            candidate,
            context.year,
            context.placements,
            exclude_schedule_item_ids=context.excluded_schedule_item_ids,
            assessment_cache=context.assessment_cache,
            risk_context_cache=context.risk_context_cache,
        )
    return candidates


def _select_preference_generation_candidate(context, employee):
    ranked_candidates = _rank_generation_candidates(_build_preference_generation_candidates(context, employee))
    return _select_preference_generation_candidate_from_ranked(ranked_candidates)


def _select_first_passed_generation_candidate(candidates):
    for candidate in candidates:
        if _candidate_passed_hard_rules(candidate):
            return candidate
    return None


def _apply_candidate_scoring(candidate):
    if "scoring_score" in candidate.metadata and "scoring_confidence" in candidate.metadata:
        return candidate
    features = _generation_candidate_features(candidate)
    scoring = score_candidate_features(features, passed_hard_rules=_candidate_passed_hard_rules(candidate))
    candidate.metadata.update(
        {
            "scoring_score": scoring.score,
            "scoring_confidence": scoring.confidence,
            "scoring_model_version": scoring.model_version,
            "scoring_recommendation": scoring.recommendation,
            "scoring_scorer_kind": scoring.scorer_kind,
            "scoring_explanation": scoring.explanation,
            "planning_ends_by_nearest_deadline": features.get("planning_ends_by_nearest_deadline", False),
        }
    )
    return candidate


def _candidate_scoring_decimal(candidate, key):
    return _feature_decimal(candidate.metadata.get(key))


def _candidate_coverage_balance(candidate):
    coverage_ratio = _feature_decimal(candidate.metadata.get("chargeable_days"))
    open_required_days = _feature_decimal(candidate.metadata.get("open_required_days"))
    if open_required_days <= 0:
        return Decimal("0.00")
    return Decimal("1.00") - abs((coverage_ratio / open_required_days) - Decimal("1.00"))


def _candidate_preference_rank(candidate):
    priority = candidate.metadata.get("priority")
    if priority == VacationPreference.PRIORITY_PRIMARY:
        return 2
    if priority == VacationPreference.PRIORITY_BACKUP:
        return 1
    return 0


def _candidate_is_acceptable_primary_preference(candidate):
    if candidate is None:
        return False
    if candidate.metadata.get("priority") != VacationPreference.PRIORITY_PRIMARY:
        return False
    if not _candidate_passed_hard_rules(candidate):
        return False
    risk_payload = (candidate.assessment or {}).get("risk_payload") or {}
    risk_level = risk_payload.get("risk_level") or candidate.metadata.get("risk_level")
    if risk_level == VacationRequest.RISK_HIGH:
        return False
    if _feature_decimal(risk_payload.get("department_load_level")) >= Decimal("4.00"):
        return False
    if _feature_decimal(risk_payload.get("risk_score") or candidate.metadata.get("risk_score")) >= Decimal("65.00"):
        return False
    return True


def _select_preference_generation_candidate_from_ranked(candidates):
    candidates = list(candidates or [])
    selected = _select_first_passed_generation_candidate(candidates)
    primary = next(
        (
            candidate
            for candidate in candidates
            if candidate.metadata.get("priority") == VacationPreference.PRIORITY_PRIMARY
        ),
        None,
    )
    if selected is None:
        return selected
    if selected is primary:
        if _candidate_is_acceptable_primary_preference(primary):
            return selected
        alternative = _select_first_passed_generation_candidate(
            [candidate for candidate in candidates if candidate is not primary]
        )
        return alternative or selected
    if not _candidate_is_acceptable_primary_preference(primary):
        return selected
    return primary


def _selected_candidate_first(candidates, selected_candidate):
    candidates = list(candidates or [])
    if selected_candidate is None:
        return candidates
    return [selected_candidate, *[candidate for candidate in candidates if candidate is not selected_candidate]]


def _candidate_rank_key(candidate):
    start_ordinal = candidate.start_date.toordinal() if candidate.start_date else 0
    generation_order = int(candidate.metadata.get("generation_order") or 0)
    return (
        1 if _candidate_passed_hard_rules(candidate) else 0,
        _candidate_scoring_decimal(candidate, "scoring_score"),
        _candidate_scoring_decimal(candidate, "scoring_confidence"),
        Decimal("100.00") - _feature_decimal(candidate.metadata.get("risk_score")),
        _candidate_coverage_balance(candidate),
        1 if candidate.metadata.get("scoring_recommendation") == "prefer" else 0,
        1 if candidate.metadata.get("planning_ends_by_nearest_deadline") else 0,
        _candidate_preference_rank(candidate),
        -generation_order,
        -start_ordinal,
    )


def _rank_generation_candidates(candidates):
    scored_candidates = []
    for generation_order, candidate in enumerate(candidates, start=1):
        candidate.metadata.setdefault("generation_order", generation_order)
        scored_candidates.append(_apply_candidate_scoring(candidate))
    return sorted(scored_candidates, key=_candidate_rank_key, reverse=True)


def _json_safe_generation_value(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe_generation_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_generation_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _feature_decimal(value):
    if value is None or value == "":
        return Decimal("0.00")
    if isinstance(value, bool):
        return Decimal("1.00") if value else Decimal("0.00")
    try:
        return quantize_leave_days(Decimal(str(value)))
    except Exception:
        return Decimal("0.00")


def _feature_float(value):
    return float(_feature_decimal(value))


def _feature_ratio(numerator, denominator):
    denominator = _feature_decimal(denominator)
    if denominator <= 0:
        return 0.0
    return round(float(_feature_decimal(numerator) / denominator), 4)


def _period_months(start_date, end_date):
    if not start_date or not end_date or end_date < start_date:
        return []
    months = []
    cursor = date(start_date.year, start_date.month, 1)
    end_marker = date(end_date.year, end_date.month, 1)
    while cursor <= end_marker and len(months) < 24:
        months.append(cursor.month)
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return months


def _calendar_days(start_date, end_date):
    if not start_date or not end_date or end_date < start_date:
        return 0
    return (end_date - start_date).days + 1


def _day_of_year(value):
    return value.timetuple().tm_yday if value else 0


def _employee_tenure_days_at_year_end(employee, year):
    if not year or not getattr(employee, "date_joined", None):
        return 0
    return max((date(year, 12, 31) - employee.date_joined).days + 1, 0)


def _deadline_gap_days(candidate):
    deadline = candidate.metadata.get("nearest_deadline")
    if not deadline or not candidate.end_date:
        return 0
    return (deadline - candidate.end_date).days


def _risk_feature_payload(candidate):
    risk_payload = (candidate.assessment or {}).get("risk_payload") or {}
    risk_explanation = risk_payload.get("risk_explanation") or {}
    details = list(risk_explanation.get("details") or [])
    primary_detail = details[0] if details else {}
    remaining_staff = int(risk_payload.get("remaining_staff_count") or risk_explanation.get("remaining_staff") or 0)
    min_staff_required = int(risk_payload.get("min_staff_required") or risk_explanation.get("required_staff") or 0)
    risk_level = risk_payload.get("risk_level") or candidate.metadata.get("risk_level") or VacationRequest.RISK_LOW
    return {
        "risk_score": int(risk_payload.get("risk_score") or candidate.metadata.get("risk_score") or 0),
        "risk_level": risk_level,
        "risk_level_weight": RISK_LEVEL_FEATURE_WEIGHT.get(risk_level, 0),
        "risk_is_conflict": bool(risk_explanation.get("is_conflict")),
        "risk_department_load_level": int(risk_payload.get("department_load_level") or 0),
        "risk_overlapping_absences_count": int(risk_payload.get("overlapping_absences_count") or 0),
        "risk_remaining_staff_count": remaining_staff,
        "risk_min_staff_required": min_staff_required,
        "risk_staff_margin": remaining_staff - min_staff_required,
        "risk_balance_after_request": _feature_float(risk_payload.get("balance_after_request")),
        "risk_substitution_used": bool(risk_explanation.get("substitution_used")),
        "risk_details_count": len(details),
        "risk_primary_detail_kind": primary_detail.get("kind", ""),
    }


def _generation_candidate_features(candidate):
    preference = candidate.preference
    employee = candidate.employee
    year = candidate.metadata.get("planning_year") or (candidate.start_date.year if candidate.start_date else None)
    chargeable_days = candidate.metadata.get("chargeable_days") or 0
    open_required_days = candidate.metadata.get("open_required_days") or candidate.metadata.get("target_days") or 0
    target_days = candidate.metadata.get("target_chargeable_days") or candidate.metadata.get("target_days") or 0
    months = _period_months(candidate.start_date, candidate.end_date)
    department_id = getattr(employee, "department_id", None) or 0
    position = getattr(employee, "employee_position", None)
    production_group_id = getattr(position, "production_group_id", None) or 0
    deadline = candidate.metadata.get("nearest_deadline")
    preference_calendar_days = _calendar_days(
        getattr(preference, "start_date", None),
        getattr(preference, "end_date", None),
    )
    features = {
        **candidate.metadata,
        "feature_schema_version": DRAFT_CANDIDATE_FEATURE_SCHEMA_VERSION,
        "candidate_kind": candidate.kind,
        "candidate_source": candidate.source,
        "candidate_passed_hard_rules": _candidate_passed_hard_rules(candidate),
        "candidate_block_reason_key": candidate.metadata.get("block_reason_key", ""),
        "employee_role": getattr(employee, "role", ""),
        "employee_role_weight": EMPLOYEE_ROLE_FEATURE_WEIGHT.get(getattr(employee, "role", ""), 0),
        "employee_is_management": bool(getattr(employee, "is_management", False)),
        "employee_is_enterprise_deputy": bool(getattr(employee, "is_enterprise_deputy", False)),
        "employee_department_id": department_id,
        "employee_has_department": bool(department_id),
        "employee_production_group_id": production_group_id,
        "employee_has_production_group": bool(production_group_id),
        "employee_annual_paid_leave_days": int(getattr(employee, "annual_paid_leave_days", 0) or 0),
        "employee_manual_leave_adjustment_days": int(getattr(employee, "manual_leave_adjustment_days", 0) or 0),
        "employee_tenure_days_at_year_end": _employee_tenure_days_at_year_end(employee, year),
        "period_start_month": candidate.start_date.month if candidate.start_date else 0,
        "period_end_month": candidate.end_date.month if candidate.end_date else 0,
        "period_start_day_of_year": _day_of_year(candidate.start_date),
        "period_end_day_of_year": _day_of_year(candidate.end_date),
        "period_calendar_days": _calendar_days(candidate.start_date, candidate.end_date),
        "period_chargeable_days": int(chargeable_days or 0),
        "period_month_count": len(set(months)),
        "period_crosses_month": bool(candidate.start_date and candidate.end_date and candidate.start_date.month != candidate.end_date.month),
        "period_overlaps_summer": bool(SUMMER_VACATION_MONTHS.intersection(months)),
        "planning_available_days": _feature_float(candidate.metadata.get("available_days")),
        "planning_plan_available_days": _feature_float(candidate.metadata.get("plan_available_days")),
        "planning_target_days": _feature_float(candidate.metadata.get("target_days")),
        "planning_placed_days": _feature_float(candidate.metadata.get("placed_days")),
        "planning_open_required_days": _feature_float(open_required_days),
        "planning_blocking_days": _feature_float(candidate.metadata.get("blocking_days")),
        "planning_deadline_blocking_days": _feature_float(candidate.metadata.get("deadline_blocking_days")),
        "planning_annual_remaining_days": _feature_float(candidate.metadata.get("annual_remaining_days")),
        "planning_mandatory_days": _feature_float(candidate.metadata.get("mandatory_days")),
        "planning_requested_preference_days": _feature_float(candidate.metadata.get("requested_preference_days")),
        "planning_candidate_target_days": _feature_float(target_days),
        "planning_candidate_coverage_ratio": _feature_ratio(chargeable_days, open_required_days),
        "planning_candidate_over_open_days": _feature_float(max(_feature_decimal(chargeable_days) - _feature_decimal(open_required_days), Decimal("0.00"))),
        "planning_basis": candidate.metadata.get("planning_basis", ""),
        "planning_remainder_policy": candidate.metadata.get("remainder_policy", ""),
        "planning_has_blocker": bool(candidate.metadata.get("has_blocker")),
        "planning_needs_manual_attention": bool(candidate.metadata.get("needs_manual_attention")),
        "planning_has_nearest_deadline": deadline is not None,
        "planning_nearest_deadline_gap_days": _deadline_gap_days(candidate),
        "planning_ends_by_nearest_deadline": bool(deadline and candidate.end_date and candidate.end_date <= deadline),
        "planning_mandatory_rows_count": int(candidate.metadata.get("mandatory_rows_count") or 0),
        "preference_has_preference": preference is not None,
        "preference_priority": getattr(preference, "priority", ""),
        "preference_status": getattr(preference, "status", ""),
        "preference_remainder_policy": getattr(preference, "remainder_policy", ""),
        "preference_calendar_days": preference_calendar_days,
        "preference_exact_period_match": bool(
            preference
            and candidate.start_date == preference.start_date
            and candidate.end_date == preference.end_date
        ),
        **_risk_feature_payload(candidate),
    }
    return _json_safe_generation_value(features)


def _candidate_decision(candidate, selected_candidate):
    if candidate is selected_candidate:
        return VacationScheduleCandidate.DECISION_SELECTED
    if not _candidate_passed_hard_rules(candidate):
        return VacationScheduleCandidate.DECISION_BLOCKED
    return VacationScheduleCandidate.DECISION_REJECTED


def _candidate_explanation(candidate, decision):
    if decision == VacationScheduleCandidate.DECISION_SELECTED:
        return candidate.comment
    if decision == VacationScheduleCandidate.DECISION_BLOCKED:
        return candidate.metadata.get("block_reason") or "Кандидат заблокирован жесткими правилами."
    return "Кандидат прошел жесткие правила, но выбран другой вариант."


def _start_schedule_generation_run(schedule, actor):
    return VacationScheduleGenerationRun.objects.create(
        schedule=schedule,
        year=schedule.year,
        mode=VacationScheduleGenerationRun.MODE_HYBRID,
        status=VacationScheduleGenerationRun.STATUS_RUNNING,
        actor=actor,
        model_version=DRAFT_GENERATION_HYBRID_MODEL_VERSION,
    )


def _finish_schedule_generation_run(generation_run, *, manual_count=0):
    candidates = generation_run.candidates.all()
    selected_count = candidates.filter(decision=VacationScheduleCandidate.DECISION_SELECTED).count()
    candidates_count = candidates.count()
    average_score = candidates.aggregate(value=Avg("score"))["value"]
    generation_run.status = VacationScheduleGenerationRun.STATUS_COMPLETED
    generation_run.candidates_count = candidates_count
    generation_run.selected_count = selected_count
    generation_run.rejected_count = max(candidates_count - selected_count, 0)
    generation_run.manual_count = max(int(manual_count or 0), 0)
    generation_run.average_score = average_score
    generation_run.finished_at = timezone.now()
    generation_run.error_message = ""
    generation_run.save(
        update_fields=[
            "status",
            "candidates_count",
            "selected_count",
            "rejected_count",
            "manual_count",
            "average_score",
            "finished_at",
            "error_message",
        ]
    )
    return generation_run


def _persist_generation_candidates(generation_run, schedule, candidates, *, selected_candidate=None):
    selected_candidate_record = None
    selected_at = timezone.now()
    candidate_records = []
    candidate_decisions = []
    for decision_rank, candidate in enumerate(candidates, start=1):
        if "scoring_score" not in candidate.metadata or "scoring_confidence" not in candidate.metadata:
            _apply_candidate_scoring(candidate)
        decision = _candidate_decision(candidate, selected_candidate)
        candidate_decisions.append(decision)
        candidate_records.append(
            VacationScheduleCandidate(
                generation_run=generation_run,
                schedule=schedule,
                employee=candidate.employee,
                start_date=candidate.start_date,
                end_date=candidate.end_date,
                vacation_type="paid",
                chargeable_days=int(candidate.metadata.get("chargeable_days") or 0),
                kind=candidate.kind,
                source=candidate.source,
                passed_hard_rules=_candidate_passed_hard_rules(candidate),
                block_reason_key=(candidate.metadata.get("block_reason_key") or "")[:80],
                block_reason=candidate.metadata.get("block_reason") or "",
                risk_score=int(candidate.metadata.get("risk_score") or 0),
                risk_level=candidate.metadata.get("risk_level") or VacationScheduleItem.RISK_LOW,
                features=_generation_candidate_features(candidate),
                score=_candidate_scoring_decimal(candidate, "scoring_score"),
                confidence=_candidate_scoring_decimal(candidate, "scoring_confidence"),
                model_version=candidate.metadata.get("scoring_model_version") or DRAFT_GENERATION_HYBRID_MODEL_VERSION,
                explanation=candidate.metadata.get("scoring_explanation") or _candidate_explanation(candidate, decision),
                decision=decision,
                decision_rank=decision_rank,
                selected_at=selected_at if decision == VacationScheduleCandidate.DECISION_SELECTED else None,
            )
        )

    created_records = VacationScheduleCandidate.objects.bulk_create(candidate_records)
    for candidate, stored_candidate, decision in zip(candidates, created_records, candidate_decisions):
        candidate.stored_candidate = stored_candidate
        if decision == VacationScheduleCandidate.DECISION_SELECTED:
            selected_candidate_record = stored_candidate
    return selected_candidate_record


def _dedupe_generation_candidates(candidates):
    seen = set()
    unique_candidates = []
    for candidate in candidates:
        key = (
            candidate.employee.id,
            candidate.kind,
            candidate.start_date,
            candidate.end_date,
            candidate.source,
        )
        if key in seen:
            continue
        seen.add(key)
        unique_candidates.append(candidate)
    return unique_candidates


def _auto_generation_candidate_from_payload(employee, payload, *, kind, comment, planning_need, metadata=None):
    metadata = {
        "planning_basis": planning_need.get("planning_basis", ""),
        "has_blocker": planning_need["has_blocker"],
        "target_days": planning_need["open_required_days"],
        **(metadata or {}),
    }
    candidate = DraftGenerationCandidate(
        employee=employee,
        start_date=payload["start_date"],
        end_date=payload["end_date"],
        kind=kind,
        source=VacationScheduleItem.SOURCE_GENERATED,
        comment=comment,
        metadata=metadata,
    )
    _apply_planning_need_metadata(candidate, planning_need, payload["start_date"].year)
    return _apply_hard_rule_assessment(candidate, payload["assessment"])


def _auto_generation_candidates_from_payloads(
    employee,
    payloads,
    *,
    kind,
    comment,
    planning_need,
    metadata=None,
):
    return [
        _auto_generation_candidate_from_payload(
            employee,
            payload,
            kind=kind,
            comment=comment,
            planning_need=planning_need,
            metadata={
                "target_chargeable_days": payload.get("target_days"),
                **(metadata or {}),
            },
        )
        for payload in payloads
    ]


def _build_auto_generation_candidates(
    context,
    employee,
    current_items,
    planning_need,
    *,
    candidates_per_strategy=AUTO_DRAFT_MAX_CANDIDATES_PER_STRATEGY,
    max_candidates=AUTO_DRAFT_MAX_CANDIDATES_PER_EMPLOYEE,
    include_risk_explanation=True,
):
    candidates = []
    if not planning_need["needs_manual_attention"]:
        return candidates

    _, planning_end = _planning_year_bounds(context.year)
    open_required_days = planning_need["open_required_days"]
    if open_required_days <= 0:
        return candidates

    def add_period_candidates(*, target_days, latest_end, urgent, allow_short_parts, kind, comment, max_chargeable_days=None):
        payloads = list(
            _iter_auto_candidate_payloads_for_need(
                employee,
                context.year,
                context.placements,
                target_days,
                latest_end,
                urgent=urgent,
                allow_short_parts=allow_short_parts,
                max_chargeable_days=max_chargeable_days,
                limit=candidates_per_strategy,
                exclude_schedule_item_ids=context.excluded_schedule_item_ids,
                assessment_cache=context.assessment_cache,
                risk_context_cache=context.risk_context_cache,
                include_risk_explanation=include_risk_explanation,
            )
        )
        candidates.extend(
            _auto_generation_candidates_from_payloads(
                employee,
                payloads,
                kind=kind,
                comment=comment,
                planning_need=planning_need,
            )
        )
        return bool(payloads)

    def add_topup_candidates(*, target_days, latest_end, comment, max_chargeable_days=None):
        payloads = list(
            _iter_adjacent_topup_candidate_payloads(
                employee,
                context.year,
                context.placements,
                current_items,
                target_days,
                latest_end,
                max_chargeable_days=max_chargeable_days,
                limit=candidates_per_strategy,
                exclude_schedule_item_ids=context.excluded_schedule_item_ids,
                assessment_cache=context.assessment_cache,
                risk_context_cache=context.risk_context_cache,
                include_risk_explanation=include_risk_explanation,
            )
        )
        candidates.extend(
            _auto_generation_candidates_from_payloads(
                employee,
                payloads,
                kind=DRAFT_CANDIDATE_AUTO_TOPUP,
                comment=comment,
                planning_need=planning_need,
                metadata={"extends_existing_item": True},
            )
        )
        return bool(payloads)

    if open_required_days >= MIN_CONTINUOUS_PAID_LEAVE_DAYS:
        backup_candidate = _backup_preference_candidate(
            context,
            employee,
            planning_need,
            metadata={"auto_place_preference_seed": True},
            include_risk_explanation=include_risk_explanation,
        )
        if backup_candidate is not None:
            nearest_deadline = planning_need.get("nearest_deadline")
            if not planning_need.get("has_blocker") or (nearest_deadline and backup_candidate.end_date <= nearest_deadline):
                candidates.append(backup_candidate)

    if planning_need["has_blocker"]:
        previous_year_closure = detect_previous_year_closure_need(employee, context.year, planning_need)
        previous_year_closure_days = quantize_leave_days(
            previous_year_closure["required_days"] if previous_year_closure else Decimal("0.00")
        )
        current_year_blocking_days = quantize_leave_days(
            max(planning_need["blocking_days"] - previous_year_closure_days, Decimal("0.00"))
        )
        if current_year_blocking_days > 0:
            target_before_deadline = quantize_leave_days(
                max(open_required_days - previous_year_closure_days, current_year_blocking_days)
            )
            if add_period_candidates(
                target_days=target_before_deadline,
                latest_end=planning_need["nearest_deadline"],
                urgent=True,
                allow_short_parts=False,
                kind=DRAFT_CANDIDATE_AUTO_URGENT,
                comment="Автоматически распределено: срочный остаток отпуска до срока.",
            ):
                return _dedupe_generation_candidates(candidates)[:max_candidates]

            if add_topup_candidates(
                target_days=current_year_blocking_days,
                latest_end=planning_need["nearest_deadline"],
                max_chargeable_days=open_required_days,
                comment="Автоматически продлено: срочный остаток закрыт соседней частью отпуска.",
            ):
                return _dedupe_generation_candidates(candidates)[:max_candidates]

            if add_period_candidates(
                target_days=current_year_blocking_days,
                latest_end=planning_need["nearest_deadline"],
                urgent=True,
                allow_short_parts=True,
                kind=DRAFT_CANDIDATE_AUTO_URGENT,
                comment="Автоматически распределено: короткий срочный остаток до срока.",
            ):
                return _dedupe_generation_candidates(candidates)[:max_candidates]

            if planning_need["annual_remaining_days"] > 0:
                if planning_need["annual_remaining_days"] < MIN_CONTINUOUS_PAID_LEAVE_DAYS and add_topup_candidates(
                    target_days=planning_need["annual_remaining_days"],
                    latest_end=planning_end,
                    max_chargeable_days=open_required_days,
                    comment="Автоматически продлено: автодобор выполнен, срочный остаток остался на ручную проверку.",
                ):
                    return _dedupe_generation_candidates(candidates)[:max_candidates]

                if add_period_candidates(
                    target_days=planning_need["annual_remaining_days"],
                    latest_end=planning_end,
                    urgent=False,
                    allow_short_parts=False,
                    kind=DRAFT_CANDIDATE_AUTO,
                    comment="Автоматически распределено: автодобор выполнен, срочный остаток остался на ручную проверку.",
                ):
                    return _dedupe_generation_candidates(candidates)[:max_candidates]
            return _dedupe_generation_candidates(candidates)[:max_candidates]

        target_after_previous_year_closure = quantize_leave_days(
            max(open_required_days - previous_year_closure_days, Decimal("0.00"))
        )
        if target_after_previous_year_closure <= 0:
            return candidates
        add_period_candidates(
            target_days=target_after_previous_year_closure,
            latest_end=planning_end,
            urgent=False,
            allow_short_parts=False,
            kind=DRAFT_CANDIDATE_AUTO,
            comment="Автоматически распределено: автодобор при отдельном срочном остатке.",
        )
        return _dedupe_generation_candidates(candidates)[:max_candidates]

    if open_required_days < MIN_CONTINUOUS_PAID_LEAVE_DAYS and add_topup_candidates(
        target_days=open_required_days,
        latest_end=planning_end,
        max_chargeable_days=open_required_days,
        comment="Автоматически продлено: короткий остаток объединен с соседним отпуском.",
    ):
        return _dedupe_generation_candidates(candidates)[:max_candidates]

    add_period_candidates(
        target_days=open_required_days,
        latest_end=planning_end,
        urgent=False,
        allow_short_parts=False,
        kind=DRAFT_CANDIDATE_AUTO,
        comment="Автоматически распределено: добивка по пожеланию сотрудника.",
    )
    return _dedupe_generation_candidates(candidates)[:max_candidates]


def _select_auto_generation_candidate(context, employee, current_items, planning_need):
    candidates = _rank_auto_generation_candidates(_build_auto_generation_candidates(context, employee, current_items, planning_need))
    return _select_first_passed_generation_candidate(candidates)


def _rank_auto_generation_candidates(candidates):
    ranked_candidates = _rank_generation_candidates(candidates)
    return sorted(
        ranked_candidates,
        key=lambda candidate: (
            1
            if candidate.metadata.get("is_preference_candidate")
            and _candidate_passed_hard_rules(candidate)
            and _candidate_scoring_decimal(candidate, "scoring_score") >= Decimal("55.00")
            else 0
        ),
        reverse=True,
    )
