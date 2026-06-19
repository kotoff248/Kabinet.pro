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
from apps.leave.services.risk import calculate_vacation_request_risk, calculate_vacation_request_risk_with_explanation
from apps.leave.services.schedule_auto_place_jobs import get_active_schedule_auto_place_job, schedule_auto_place_job_page_payload
from apps.leave.services.staffing import (
    build_department_staffing_context,
    format_staff_count,
    get_department_staffing_rule,
    get_weighted_department_workload,
)
from apps.leave.services.urgent_closures import detect_previous_year_closure_need, get_active_urgent_closure_payload_map
from apps.leave.services.validation import MIN_CONTINUOUS_PAID_LEAVE_DAYS, get_overlapping_requests, get_overlapping_schedule_items

from apps.leave.services.schedule_drafts.constants import *
from apps.leave.services.schedule_drafts.types import *


def _assessment_placements_cache_key(placements):
    return tuple(
        sorted(
            (
                placement.employee_id,
                placement.start_date,
                placement.end_date,
                placement.item_id or 0,
            )
            for placement in placements or []
        )
    )


def _assessment_cache_key(
    employee,
    start_date,
    end_date,
    year,
    placements,
    *,
    max_chargeable_days=None,
    exclude_schedule_item_id=None,
    exclude_schedule_item_ids=None,
    include_risk_explanation=True,
):
    excluded_ids = set(exclude_schedule_item_ids or [])
    if exclude_schedule_item_id is not None:
        excluded_ids.add(exclude_schedule_item_id)
    return (
        employee.id,
        start_date,
        end_date,
        year,
        str(quantize_leave_days(max_chargeable_days)) if max_chargeable_days is not None else "",
        tuple(sorted(excluded_ids)),
        bool(include_risk_explanation),
        _assessment_placements_cache_key(placements),
    )


def _cached_risk_context(employee, start_date, end_date, cache):
    department = getattr(employee, "department", None)
    department_id = getattr(employee, "department_id", None)
    if not department_id:
        return {}

    cache = cache if cache is not None else {}
    staffing_rules = cache.setdefault("staffing_rules", {})
    staffing_contexts = cache.setdefault("staffing_contexts", {})
    weighted_workloads = cache.setdefault("weighted_workloads", {})

    if department_id not in staffing_rules:
        staffing_rules[department_id] = get_department_staffing_rule(department)
    staffing_rule = staffing_rules[department_id]

    staffing_key = (department_id, end_date)
    if staffing_key not in staffing_contexts:
        staffing_contexts[staffing_key] = build_department_staffing_context(department, end_date)

    workload_key = (department_id, start_date, end_date)
    if workload_key not in weighted_workloads:
        weighted_workloads[workload_key] = get_weighted_department_workload(
            department,
            start_date,
            end_date,
            staffing_rule,
        )

    return {
        "staffing_context": staffing_contexts[staffing_key],
        "staffing_rule": staffing_rule,
        "weighted_workload": weighted_workloads[workload_key],
    }


def assess_schedule_draft_candidate(
    employee,
    start_date,
    end_date,
    year,
    placements,
    *,
    max_chargeable_days=None,
    exclude_schedule_item_id=None,
    exclude_schedule_item_ids=None,
    risk_context=None,
    assessment_cache=None,
    risk_context_cache=None,
    include_risk_explanation=True,
):
    cache_key = None
    if assessment_cache is not None:
        cache_key = _assessment_cache_key(
            employee,
            start_date,
            end_date,
            year,
            placements,
            max_chargeable_days=max_chargeable_days,
            exclude_schedule_item_id=exclude_schedule_item_id,
            exclude_schedule_item_ids=exclude_schedule_item_ids,
            include_risk_explanation=include_risk_explanation,
        )
        cached_assessment = assessment_cache.get(cache_key)
        if cached_assessment is not None:
            return cached_assessment

    def finish(assessment):
        if assessment_cache is not None and cache_key is not None:
            assessment_cache[cache_key] = assessment
        return assessment

    if not start_date or not end_date:
        return finish({
            "can_place": False,
            "has_conflict": True,
            "reason": _manual_reason("missing_period", "Период не заполнен."),
        })
    if start_date.year != year or end_date.year != year or end_date < start_date:
        return finish({
            "can_place": False,
            "has_conflict": True,
            "reason": _manual_reason("invalid_period", "Период вне выбранного года."),
        })

    available_from = get_paid_leave_available_from(employee)
    if start_date < available_from:
        return finish({
            "can_place": False,
            "has_conflict": True,
            "reason": _manual_reason("too_early", f"Оплачиваемый отпуск доступен с {available_from:%d.%m.%Y}."),
        })

    chargeable_days = get_chargeable_leave_days(start_date, end_date, "paid")
    if chargeable_days <= 0:
        return finish({
            "can_place": False,
            "has_conflict": True,
            "reason": _manual_reason("empty_period", "В периоде нет списываемых дней отпуска."),
        })
    if max_chargeable_days is not None and Decimal(chargeable_days) > quantize_leave_days(max_chargeable_days):
        return finish({
            "can_place": False,
            "has_conflict": True,
            "reason": _manual_reason("too_many_days", "Период превышает остаток, который нужно распределить."),
        })

    if get_overlapping_requests(employee, start_date, end_date).exists():
        return finish({
            "can_place": False,
            "has_conflict": True,
            "reason": _manual_reason("employee_overlap", "У сотрудника уже есть активная заявка на эти даты."),
        })
    schedule_overlaps = get_overlapping_schedule_items(employee, start_date, end_date)
    if exclude_schedule_item_id is not None:
        schedule_overlaps = schedule_overlaps.exclude(pk=exclude_schedule_item_id)
    if exclude_schedule_item_ids:
        schedule_overlaps = schedule_overlaps.exclude(pk__in=list(exclude_schedule_item_ids))
    if schedule_overlaps.exists() or _has_employee_draft_overlap(
        placements,
        employee.id,
        start_date,
        end_date,
        exclude_item_id=exclude_schedule_item_id,
        exclude_item_ids=exclude_schedule_item_ids,
    ):
        return finish({
            "can_place": False,
            "has_conflict": True,
            "reason": _manual_reason("employee_overlap", "У сотрудника уже есть отпуск на эти даты."),
        })

    extra_absent_ids = _extra_absent_ids_for_period(
        placements,
        start_date,
        end_date,
        exclude_employee_id=employee.id,
    )
    if risk_context is None and risk_context_cache is not None:
        risk_context = _cached_risk_context(employee, start_date, end_date, risk_context_cache)
    risk_calculator = (
        calculate_vacation_request_risk_with_explanation
        if include_risk_explanation
        else calculate_vacation_request_risk
    )
    risk_payload = risk_calculator(
        employee=employee,
        start_date=start_date,
        end_date=end_date,
        vacation_type="paid",
        exclude_schedule_item_id=exclude_schedule_item_id,
        exclude_schedule_item_ids=exclude_schedule_item_ids,
        extra_absent_employee_ids=extra_absent_ids,
        **(risk_context or {}),
    )
    explanation = risk_payload.get("risk_explanation") or {}
    has_conflict = bool(risk_payload.get("is_conflict") or explanation.get("is_conflict"))
    if risk_payload.get("balance_after_request") is not None and risk_payload["balance_after_request"] < Decimal("0"):
        return finish({
            "can_place": False,
            "has_conflict": True,
            "risk_payload": risk_payload,
            "reason": _manual_reason("negative_balance", "Недостаточно оплачиваемых дней."),
        })
    if has_conflict:
        return finish({
            "can_place": False,
            "has_conflict": True,
            "risk_payload": risk_payload,
            "reason": _manual_reason(
                "staffing_conflict",
                explanation.get("short_reason") or "Период нарушает правила состава.",
            ),
        })

    return finish({
        "can_place": True,
        "has_conflict": False,
        "risk_payload": risk_payload,
        "chargeable_days": chargeable_days,
        "reason": _manual_reason("ok", "Период можно поставить в черновик."),
    })
