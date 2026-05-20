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


def schedule_draft_url(year):
    return reverse("schedule_draft_detail", args=[year])


def schedule_draft_create_url(year):
    return reverse("schedule_draft_create", args=[year])


def get_schedule_draft_status(year):
    schedule = VacationSchedule.objects.filter(year=year).first()
    draft_schedule = (
        schedule
        if schedule is not None and schedule.status in DRAFT_VIEW_SCHEDULE_STATUSES
        else None
    )
    items_count = 0
    if draft_schedule is not None:
        items_count = draft_schedule.items.filter(status__in=_draft_view_item_statuses(draft_schedule)).count()
    sent_to_review = draft_schedule is not None and draft_schedule.status == VacationSchedule.STATUS_DEPARTMENT_REVIEW
    approved = draft_schedule is not None and draft_schedule.status == VacationSchedule.STATUS_APPROVED
    return {
        "schedule": draft_schedule,
        "exists": draft_schedule is not None,
        "is_editable": draft_schedule is not None and draft_schedule.status == VacationSchedule.STATUS_DRAFT,
        "sent_to_review": sent_to_review,
        "approved": approved,
        "blocked_by_existing_schedule": schedule is not None and draft_schedule is None,
        "items_count": items_count,
        "label": (
            "График утверждён"
            if approved
            else ("Черновик отправлен" if sent_to_review else ("Черновик создан" if draft_schedule is not None else "Черновик не создан"))
        ),
        "icon": (
            "verified"
            if approved
            else ("fact_check" if sent_to_review else ("edit_calendar" if draft_schedule is not None else "pending_actions"))
        ),
        "url": schedule_draft_url(year),
        "create_url": schedule_draft_create_url(year),
    }


def _period_label(start_date, end_date):
    if not start_date or not end_date:
        return "Не указан"
    return format_period_label(start_date, end_date)


def _short_date(value):
    return date_format(value, "j E", use_l10n=True)


def _short_period_label(start_date, end_date):
    if not start_date or not end_date:
        return "Не указан"
    return f"{_short_date(start_date)} - {_short_date(end_date)}"


def _format_days(value):
    value = quantize_leave_days(value or Decimal("0"))
    if value == value.to_integral_value():
        return str(int(value))
    return str(value).replace(".", ",").rstrip("0").rstrip(",")


def _days_label(value):
    return f"{_format_days(value)} д."


def _planning_year_bounds(year):
    return date(year, 1, 1), date(year, 12, 31)


def _periods_overlap(left_start, left_end, right_start, right_end):
    return left_start <= right_end and right_start <= left_end


def _days_between_periods(left_start, left_end, right_start, right_end):
    if left_end < right_start:
        return (right_start - left_end).days - 1
    if right_end < left_start:
        return (left_start - right_end).days - 1
    return 0


def _has_short_gap_to_employee_placement(
    placements,
    employee_id,
    start_date,
    end_date,
    *,
    exclude_item_ids=None,
):
    exclude_item_ids = set(exclude_item_ids or [])
    for placement in placements:
        if placement.employee_id != employee_id or placement.item_id in exclude_item_ids:
            continue
        gap_days = _days_between_periods(placement.start_date, placement.end_date, start_date, end_date)
        if 0 < gap_days < AUTO_DRAFT_MIN_GAP_BETWEEN_ITEMS_DAYS:
            return True
    return False


def _adjacent_employee_items(items, start_date, end_date):
    adjacent = []
    for item in items or []:
        if item.end_date + timedelta(days=1) == start_date or end_date + timedelta(days=1) == item.start_date:
            adjacent.append(item)
    return adjacent


def _extra_absent_ids_for_period(placements, start_date, end_date, *, exclude_employee_id=None):
    return {
        placement.employee_id
        for placement in placements
        if placement.employee_id != exclude_employee_id
        and _periods_overlap(placement.start_date, placement.end_date, start_date, end_date)
    }


def _has_employee_draft_overlap(
    placements,
    employee_id,
    start_date,
    end_date,
    *,
    exclude_item_id=None,
    exclude_item_ids=None,
):
    exclude_item_ids = set(exclude_item_ids or [])
    if exclude_item_id is not None:
        exclude_item_ids.add(exclude_item_id)
    return any(
        placement.employee_id == employee_id
        and placement.item_id not in exclude_item_ids
        and _periods_overlap(placement.start_date, placement.end_date, start_date, end_date)
        for placement in placements
    )


def _manual_reason(kind, text, detail=""):
    return {
        "kind": kind,
        "text": text,
        "detail": detail,
    }


def _int_or_none(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _staffing_chip(label, tone="neutral"):
    return {
        "label": label,
        "tone": tone,
    }


def _staffing_summary_from_risk_payload(risk_payload):
    risk_payload = risk_payload or {}
    if not risk_payload:
        return {}

    risk_explanation = risk_payload.get("risk_explanation") or {}
    if any(detail.get("kind") == "missing_department" for detail in (risk_explanation.get("details") or [])):
        return {}

    remaining_staff = _int_or_none(risk_payload.get("remaining_staff_count"))
    if remaining_staff is None:
        remaining_staff = _int_or_none(risk_explanation.get("remaining_staff"))
    required_staff = _int_or_none(risk_payload.get("min_staff_required"))
    if required_staff is None:
        required_staff = _int_or_none(risk_explanation.get("required_staff"))
    overlapping_absences = _int_or_none(risk_payload.get("overlapping_absences_count"))
    if overlapping_absences is None:
        overlapping_absences = _int_or_none(risk_explanation.get("overlapping_absences_count"))
    department_load_level = _int_or_none(risk_payload.get("department_load_level"))
    risk_score = _int_or_none(risk_payload.get("risk_score")) or 0
    risk_level = risk_payload.get("risk_level") or VacationRequest.RISK_LOW
    is_conflict = bool(risk_explanation.get("is_conflict"))

    if (
        remaining_staff is None
        and required_staff is None
        and overlapping_absences is None
        and department_load_level is None
    ):
        return {}

    absent_total = overlapping_absences + 1 if overlapping_absences is not None else None
    staff_margin = (
        remaining_staff - required_staff
        if remaining_staff is not None and required_staff is not None and required_staff > 0
        else None
    )
    if is_conflict or (staff_margin is not None and staff_margin < 0):
        tone = "conflict"
    elif (
        staff_margin == 0
        or risk_level == VacationRequest.RISK_HIGH
        or (department_load_level is not None and department_load_level >= 4)
    ):
        tone = "warning"
    else:
        tone = "ok"

    chips = []
    if absent_total is not None:
        chips.append(_staffing_chip(f"Отсутствуют {absent_total}"))
    if remaining_staff is not None:
        if required_staff is not None and required_staff > 0:
            chips.append(_staffing_chip(f"Останется {remaining_staff} / мин. {required_staff}", tone if tone == "conflict" else "neutral"))
        else:
            chips.append(_staffing_chip(f"Останется {remaining_staff}"))
    if staff_margin is not None:
        if staff_margin < 0:
            chips.append(_staffing_chip(f"Нехватка {abs(staff_margin)}", "conflict"))
        elif staff_margin == 0:
            chips.append(_staffing_chip("На минимуме", "warning"))
        else:
            chips.append(_staffing_chip(f"Запас +{staff_margin}", "ok"))
    elif department_load_level is not None and department_load_level >= 4:
        chips.append(_staffing_chip(f"Нагрузка {department_load_level}/5", "warning"))

    return {
        "absent_total": absent_total,
        "overlapping_absences_count": overlapping_absences,
        "remaining_staff": remaining_staff,
        "required_staff": required_staff,
        "staff_margin": staff_margin,
        "department_load_level": department_load_level,
        "risk_score": risk_score,
        "risk_level": risk_level,
        "is_conflict": is_conflict,
        "tone": tone,
        "chips": chips[:3],
    }


def _staffing_summary_from_candidate(candidate):
    risk_payload = (candidate.assessment or {}).get("risk_payload") or {}
    return _staffing_summary_from_risk_payload(risk_payload)


def _staffing_summary_sort_key(payload):
    payload = payload or {}
    summary = payload.get("staffing_summary") or {}
    tone_rank = {
        "ok": 0,
        "warning": 1,
        "conflict": 2,
    }.get(summary.get("tone"), 0)
    staff_margin = summary.get("staff_margin")
    if staff_margin is None:
        staff_margin = 999
    return (
        tone_rank,
        int(payload.get("risk_score") or summary.get("risk_score") or 0),
        -int(staff_margin),
    )


def _worst_staffing_summary_from_payloads(payloads):
    payloads = [payload for payload in (payloads or []) if (payload.get("staffing_summary") or {})]
    if not payloads:
        return {}, []
    summary = dict(max(payloads, key=_staffing_summary_sort_key).get("staffing_summary") or {})
    chips = list(summary.get("chips") or [])
    return summary, chips


def _preference_chargeable_days(preference):
    if (
        preference is None
        or preference.status != VacationPreference.STATUS_FILLED
        or not preference.start_date
        or not preference.end_date
    ):
        return Decimal("0.00")
    return quantize_leave_days(get_chargeable_leave_days(preference.start_date, preference.end_date, "paid"))


def _requested_preference_days(pair, state):
    if state != VacationPreference.STATUS_FILLED:
        return Decimal("0.00")
    pair = pair or {}
    primary = pair.get(VacationPreference.PRIORITY_PRIMARY)
    return _preference_chargeable_days(primary)


def _selected_preference_item_days(draft_items):
    total_days = Decimal("0.00")
    has_selected_preference = False
    for item in sorted(
        list(draft_items or []),
        key=lambda value: (value.start_date, value.end_date, value.id or 0),
    ):
        selected_candidate = getattr(item, "selected_candidate", None)
        if selected_candidate is None or selected_candidate.kind not in {
            VacationScheduleCandidate.KIND_PRIMARY_PREFERENCE,
            VacationScheduleCandidate.KIND_BACKUP_PREFERENCE,
        }:
            continue
        has_selected_preference = True
        total_days += quantize_leave_days(
            getattr(item, "chargeable_days", None) or selected_candidate.chargeable_days or 0
        )
    if has_selected_preference:
        return quantize_leave_days(total_days)
    return None


def _requested_preference_days_for_plan(pair, state, draft_items):
    selected_days = _selected_preference_item_days(draft_items)
    if selected_days is not None:
        return selected_days
    return _requested_preference_days(pair, state)


def _preference_remainder_policy(pair, state):
    if state != VacationPreference.STATUS_FILLED:
        return VacationPreference.REMAINDER_AUTO
    pair = pair or {}
    primary = pair.get(VacationPreference.PRIORITY_PRIMARY)
    return getattr(primary, "remainder_policy", VacationPreference.REMAINDER_AUTO) or VacationPreference.REMAINDER_AUTO


def _remainder_policy_label(policy):
    return dict(VacationPreference.REMAINDER_POLICY_CHOICES).get(
        policy or VacationPreference.REMAINDER_AUTO,
        "Можно распределить автоматически",
    )
