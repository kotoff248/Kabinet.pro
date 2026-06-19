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


def assess_preference_candidate(employee, preference, year, placements):
    if preference is None or preference.status != VacationPreference.STATUS_FILLED:
        return {
            "can_place": False,
            "has_conflict": True,
            "reason": _manual_reason("missing_period", "Период не заполнен."),
        }

    return assess_schedule_draft_candidate(employee, preference.start_date, preference.end_date, year, placements)


def _selected_preference_label(preference, pair):
    if preference is None:
        return "Пожелание"
    primary = pair.get(VacationPreference.PRIORITY_PRIMARY)
    backup = pair.get(VacationPreference.PRIORITY_BACKUP)
    if primary and preference.id == primary.id:
        return "Основное пожелание"
    if backup and preference.id == backup.id:
        return "Запасной период"
    return preference.get_priority_display()


def _draft_item_days(item):
    if item.chargeable_days is not None:
        return quantize_leave_days(item.chargeable_days)
    return quantize_leave_days(get_chargeable_leave_days(item.start_date, item.end_date, item.vacation_type))


def _draft_item_balances(draft_items):
    return [
        DraftItemBalance(
            item_id=item.id,
            start_date=item.start_date,
            end_date=item.end_date,
            remaining_days=_draft_item_days(item),
        )
        for item in sorted(draft_items, key=lambda item: (item.end_date, item.start_date, item.id or 0))
    ]


def _draft_item_key(item, fallback_index):
    return item.id if item.id is not None else f"virtual-{fallback_index}"


def _draft_item_chargeable_until(item, deadline):
    if item.start_date > deadline:
        return Decimal("0.00")
    clipped_end = min(item.end_date, deadline)
    if clipped_end < item.start_date:
        return Decimal("0.00")
    return quantize_leave_days(get_chargeable_leave_days(item.start_date, clipped_end, item.vacation_type))


def _mandatory_rows_for_year(employee, year):
    _, planning_end = _planning_year_bounds(year)
    rows = get_employee_entitlement_rows(employee, as_of_date=planning_end, limit=100)
    return [
        row
        for row in rows
        if row["remaining_days"] > 0 and row["must_use_by"] <= planning_end
    ], rows


def _mandatory_rows_from_entitlement_rows(rows, year):
    _, planning_end = _planning_year_bounds(year)
    return [
        row
        for row in rows
        if row["remaining_days"] > 0 and row["must_use_by"] <= planning_end
    ]


def _covered_mandatory_days_by_deadline(mandatory_rows, draft_items):
    draft_items = sorted(
        list(draft_items or []),
        key=lambda item: (item.end_date, item.start_date, item.id or 0),
    )
    allocated_by_item = {
        _draft_item_key(item, index): Decimal("0.00")
        for index, item in enumerate(draft_items)
    }
    covered = Decimal("0.00")
    open_rows = []

    for row in sorted(mandatory_rows, key=lambda item: (item["must_use_by"], item["period_start"])):
        row_open = quantize_leave_days(row["remaining_days"])
        row_covered = Decimal("0.00")

        for index, item in enumerate(draft_items):
            item_key = _draft_item_key(item, index)
            eligible_days = _draft_item_chargeable_until(item, row["must_use_by"])
            available_days = quantize_leave_days(eligible_days - allocated_by_item[item_key])
            if available_days <= 0:
                continue
            used_days = min(row_open, available_days)
            if used_days <= 0:
                continue
            allocated_by_item[item_key] = quantize_leave_days(allocated_by_item[item_key] + used_days)
            row_open = quantize_leave_days(row_open - used_days)
            row_covered = quantize_leave_days(row_covered + used_days)
            if row_open <= 0:
                break

        covered = quantize_leave_days(covered + row_covered)
        if row_open > 0:
            open_row = dict(row)
            open_row["open_days"] = row_open
            open_rows.append(open_row)

    return covered, open_rows


def _planning_status(*, blocking_days, open_required_days, nearest_deadline, year):
    planning_start, _ = _planning_year_bounds(year)
    today = timezone.localdate()
    if blocking_days > 0:
        if nearest_deadline and nearest_deadline < today:
            return {
                "key": "overdue",
                "label": "Срок прошел",
                "icon": "report",
                "tone": "blocker",
            }
        if nearest_deadline and (nearest_deadline < planning_start or nearest_deadline <= date(year, 1, 31)):
            return {
                "key": "critical",
                "label": "Критичный срок",
                "icon": "priority_high",
                "tone": "blocker",
            }
        return {
            "key": "mandatory",
            "label": "Срочный остаток",
            "icon": "event_busy",
            "tone": "blocker",
        }
    if open_required_days > 0:
        return {
            "key": "needs_planning",
            "label": "Нужно добить",
            "icon": "add_task",
            "tone": "warning",
        }
    return {
        "key": "covered",
        "label": "План закрыт",
        "icon": "verified",
        "tone": "ok",
    }


def _build_employee_schedule_planning_need_from_rows(
    employee,
    year,
    draft_items,
    available_days,
    plan_available_days,
    entitlement_rows,
    *,
    requested_preference_days=Decimal("0.00"),
    remainder_policy=VacationPreference.REMAINDER_AUTO,
    preference_state=None,
):
    draft_items = list(draft_items or [])
    requested_preference_days = quantize_leave_days(requested_preference_days or Decimal("0.00"))
    mandatory_rows = _mandatory_rows_from_entitlement_rows(entitlement_rows, year)
    mandatory_days = quantize_leave_days(
        sum((Decimal(row["remaining_days"]) for row in mandatory_rows), Decimal("0.00"))
    )
    placed_days = quantize_leave_days(
        sum((_draft_item_days(item) for item in draft_items), Decimal("0.00"))
    )
    _, open_mandatory_rows = _covered_mandatory_days_by_deadline(mandatory_rows, draft_items)
    blocking_days = quantize_leave_days(
        sum((Decimal(row["open_days"]) for row in open_mandatory_rows), Decimal("0.00"))
    )
    plan_available_days = quantize_leave_days(plan_available_days or Decimal("0.00"))
    annual_target_days = quantize_leave_days(
        min(Decimal(employee.annual_paid_leave_days), max(available_days - mandatory_days, Decimal("0.00")))
    )
    base_target_days = quantize_leave_days(min(available_days, mandatory_days + annual_target_days))
    preference_target_days = Decimal("0.00")
    if preference_state == VacationPreference.STATUS_FILLED:
        remainder_policy = remainder_policy or VacationPreference.REMAINDER_AUTO
        preference_target_days = quantize_leave_days(min(available_days, max(mandatory_days, requested_preference_days)))
        if remainder_policy == VacationPreference.REMAINDER_AUTO:
            target_days = quantize_leave_days(max(base_target_days, preference_target_days))
            planning_basis = "preference" if preference_target_days >= base_target_days else "annual_plan"
        else:
            target_days = quantize_leave_days(max(mandatory_days, preference_target_days))
            planning_basis = remainder_policy
    else:
        remainder_policy = VacationPreference.REMAINDER_AUTO
        target_days = base_target_days
        planning_basis = "annual_plan" if annual_target_days > 0 else "mandatory"
    open_target_days = quantize_leave_days(max(target_days - placed_days, Decimal("0.00")))
    open_required_days = quantize_leave_days(max(open_target_days, blocking_days))
    deadline_blocking_days = blocking_days
    annual_remaining_days = quantize_leave_days(max(open_required_days - deadline_blocking_days, Decimal("0.00")))
    future_available_days = quantize_leave_days(max(available_days - plan_available_days, Decimal("0.00")))
    deferred_days = quantize_leave_days(max(available_days - target_days, Decimal("0.00")))
    planned_or_requested_days = quantize_leave_days(max(preference_target_days, placed_days))
    optional_annual_days = quantize_leave_days(max(base_target_days - planned_or_requested_days, Decimal("0.00")))
    remainder_approval_days = Decimal("0.00")
    employee_deferred_days = Decimal("0.00")
    auto_remainder_days = Decimal("0.00")
    if optional_annual_days > 0:
        if remainder_policy == VacationPreference.REMAINDER_APPROVAL:
            remainder_approval_days = optional_annual_days
        elif remainder_policy == VacationPreference.REMAINDER_DEFER:
            employee_deferred_days = optional_annual_days
        else:
            auto_remainder_days = optional_annual_days
    nearest_deadline = min((row["must_use_by"] for row in open_mandatory_rows), default=None)
    nearest_deadline_label = nearest_deadline.strftime("%d.%m.%Y") if nearest_deadline else ""
    status = _planning_status(
        blocking_days=blocking_days,
        open_required_days=open_required_days,
        nearest_deadline=nearest_deadline,
        year=year,
    )

    if deadline_blocking_days > 0 and annual_remaining_days > 0:
        remaining_label = "добора незакрытых дней" if remainder_policy == VacationPreference.REMAINDER_AUTO else "по пожеланию"
        action_text = (
            f"Сначала {_days_label(deadline_blocking_days)} до {nearest_deadline_label}. "
            f"Затем {_days_label(annual_remaining_days)} {remaining_label}."
        )
    elif deadline_blocking_days > 0:
        action_text = (
            f"Блокирует согласование: {_days_label(deadline_blocking_days)} "
            f"нужно закрыть до {nearest_deadline_label}."
        )
    elif open_required_days > 0:
        if remainder_policy == VacationPreference.REMAINDER_AUTO:
            action_text = f"Осталось добрать {_days_label(open_required_days)} незакрытых дней."
        elif preference_state == VacationPreference.STATUS_FILLED:
            action_text = f"Осталось поставить {_days_label(open_required_days)} по пожеланию."
        else:
            action_text = f"Осталось закрыть {_days_label(open_required_days)} обязательного остатка."
    elif remainder_approval_days > 0:
        action_text = f"Пожелание закрыто. {_days_label(remainder_approval_days)} остатка ждут отдельного согласования."
    elif employee_deferred_days > 0:
        action_text = f"Пожелание закрыто. {_days_label(employee_deferred_days)} не планируются сверх указанного периода."
    elif future_available_days > 0:
        action_text = f"План на {year} год закрыт."
    else:
        action_text = f"План на {year} год закрыт."

    plan_breakdown = []
    if requested_preference_days > 0:
        plan_breakdown.append(
            {
                "label": "Пожелание",
                "value": _days_label(requested_preference_days),
                "tone": "preference",
            }
        )
    if auto_remainder_days > 0:
        plan_breakdown.append(
            {
                "label": "Добор незакрытых дней",
                "value": _days_label(auto_remainder_days),
                "tone": "annual",
            }
        )
    if remainder_approval_days > 0:
        plan_breakdown.append(
            {
                "label": "На согласование",
                "value": _days_label(remainder_approval_days),
                "tone": "future",
            }
        )
    if employee_deferred_days > 0:
        plan_breakdown.append(
            {
                "label": "Не планируется",
                "value": _days_label(employee_deferred_days),
                "tone": "muted",
            }
        )
    if deadline_blocking_days > 0:
        plan_breakdown.append(
            {
                "label": "К сроку",
                "value": _days_label(deadline_blocking_days),
                "tone": "mandatory",
            }
        )

    if deadline_blocking_days > 0 and annual_remaining_days > 0:
        manual_task_label = (
            f"{_days_label(deadline_blocking_days)} до {nearest_deadline_label}; "
            f"план {_days_label(annual_remaining_days)}"
        )
    elif deadline_blocking_days > 0:
        manual_task_label = f"{_days_label(deadline_blocking_days)} до {nearest_deadline_label}"
    else:
        manual_task_label = _days_label(open_required_days)

    return {
        "available_days": available_days,
        "available_days_label": _days_label(available_days),
        "plan_available_days": plan_available_days,
        "plan_available_days_label": _days_label(plan_available_days),
        "future_available_days": future_available_days,
        "future_available_days_label": _days_label(future_available_days),
        "mandatory_days": mandatory_days,
        "mandatory_days_label": _days_label(mandatory_days),
        "base_target_days": base_target_days,
        "base_target_days_label": _days_label(base_target_days),
        "annual_target_days": annual_target_days,
        "annual_target_days_label": _days_label(annual_target_days),
        "optional_annual_days": optional_annual_days,
        "optional_annual_days_label": _days_label(optional_annual_days),
        "requested_preference_days": requested_preference_days,
        "requested_preference_days_label": _days_label(requested_preference_days),
        "remainder_policy": remainder_policy,
        "remainder_policy_label": _remainder_policy_label(remainder_policy),
        "auto_remainder_days": auto_remainder_days,
        "auto_remainder_days_label": _days_label(auto_remainder_days),
        "remainder_approval_days": remainder_approval_days,
        "remainder_approval_days_label": _days_label(remainder_approval_days),
        "employee_deferred_days": employee_deferred_days,
        "employee_deferred_days_label": _days_label(employee_deferred_days),
        "planning_basis": planning_basis,
        "target_days": target_days,
        "target_days_label": _days_label(target_days),
        "placed_days": placed_days,
        "placed_days_label": _days_label(placed_days),
        "open_required_days": open_required_days,
        "open_required_days_label": _days_label(open_required_days),
        "deadline_blocking_days": deadline_blocking_days,
        "deadline_blocking_days_label": _days_label(deadline_blocking_days),
        "annual_remaining_days": annual_remaining_days,
        "annual_remaining_days_label": _days_label(annual_remaining_days),
        "manual_task_label": manual_task_label,
        "blocking_days": blocking_days,
        "blocking_days_label": _days_label(blocking_days),
        "deferred_days": deferred_days,
        "deferred_days_label": _days_label(deferred_days),
        "nearest_deadline": nearest_deadline,
        "nearest_deadline_label": nearest_deadline_label,
        "status": status,
        "action_text": action_text,
        "has_blocker": blocking_days > 0,
        "needs_manual_attention": open_required_days > 0,
        "plan_breakdown": plan_breakdown,
        "mandatory_rows": open_mandatory_rows,
        "entitlement_rows": entitlement_rows,
    }


def build_employee_schedule_planning_need(employee, year, draft_items=None, preference_pair=None, preference_state=None):
    planning_start, planning_end = _planning_year_bounds(year)
    available_days = quantize_leave_days(get_employee_available_balance(employee, planning_end))
    plan_available_days = quantize_leave_days(get_employee_available_balance(employee, planning_start))
    _, entitlement_rows = _mandatory_rows_for_year(employee, year)
    if preference_pair is None:
        preference_pair = get_employee_preference_pair(employee, year)
    if preference_state is None:
        preference_state = get_employee_preference_state(employee, year)
    return _build_employee_schedule_planning_need_from_rows(
        employee,
        year,
        draft_items,
        available_days,
        plan_available_days,
        entitlement_rows,
        requested_preference_days=_requested_preference_days_for_plan(preference_pair, preference_state, draft_items),
        remainder_policy=_preference_remainder_policy(preference_pair, preference_state),
        preference_state=preference_state,
    )


def build_employee_schedule_planning_need_map(
    employees,
    year,
    draft_items_by_employee=None,
    preference_pair_by_employee=None,
    preference_state_by_employee=None,
):
    employees = list(employees)
    if not employees:
        return {}

    draft_items_by_employee = draft_items_by_employee or {}
    planning_start, planning_end = _planning_year_bounds(year)
    leave_summaries = get_employee_list_leave_summaries(employees, planning_end)
    plan_leave_summaries = get_employee_list_leave_summaries(employees, planning_start)
    entitlement_rows_by_employee = get_employee_entitlement_rows_bulk(employees, planning_end, limit=100)
    employee_ids = [employee.id for employee in employees]
    if preference_pair_by_employee is None:
        preference_pair_by_employee = get_employee_preference_pair_map(employee_ids, year)
    if preference_state_by_employee is None:
        preference_state_by_employee = get_employee_preference_state_map(employee_ids, year)
    return {
        employee.id: _build_employee_schedule_planning_need_from_rows(
            employee,
            year,
            draft_items_by_employee.get(employee.id, []),
            quantize_leave_days(leave_summaries[employee.id]["available"]),
            quantize_leave_days(plan_leave_summaries[employee.id]["available"]),
            entitlement_rows_by_employee.get(employee.id, []),
            requested_preference_days=_requested_preference_days_for_plan(
                preference_pair_by_employee.get(employee.id),
                preference_state_by_employee.get(employee.id),
                draft_items_by_employee.get(employee.id, []),
            ),
            remainder_policy=_preference_remainder_policy(
                preference_pair_by_employee.get(employee.id),
                preference_state_by_employee.get(employee.id),
            ),
            preference_state=preference_state_by_employee.get(employee.id),
        )
        for employee in employees
    }


def _day_calculation_number(value):
    value = quantize_leave_days(value or Decimal("0.00"))
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _day_calculation_reason(planning_need, year):
    if planning_need.get("has_blocker"):
        if planning_need.get("annual_remaining_days", Decimal("0.00")) > 0:
            return (
                f"Нужно закрыть обязательный остаток до {planning_need.get('nearest_deadline_label') or 'срока'} "
                f"и добрать план на {year} год."
            )
        return f"Нужно закрыть обязательный остаток до {planning_need.get('nearest_deadline_label') or 'срока'}."

    if planning_need.get("open_required_days", Decimal("0.00")) > 0:
        if planning_need.get("remainder_policy") == VacationPreference.REMAINDER_AUTO:
            return "Сотрудник разрешил автоматическое распределение, поэтому система добирает плановую цель."
        if planning_need.get("remainder_policy") == VacationPreference.REMAINDER_APPROVAL:
            return "Пожелание учтено, остаток требует отдельного согласования."
        if planning_need.get("remainder_policy") == VacationPreference.REMAINDER_DEFER:
            return "Пожелание учтено, лишний остаток не планируется автоматически."
        return "Осталось закрыть плановую потребность сотрудника."

    if planning_need.get("remainder_approval_days", Decimal("0.00")) > 0:
        return "Пожелание закрыто, оставшиеся дни ждут отдельного согласования."
    if planning_need.get("employee_deferred_days", Decimal("0.00")) > 0:
        return "Пожелание закрыто, сотрудник не просил планировать остаток автоматически."
    return f"План на {year} год закрыт."


def _day_calculation_breakdown(planning_need, year):
    nearest_deadline_label = planning_need.get("nearest_deadline_label") or ""
    mandatory_detail = f"ближайший срок {nearest_deadline_label}" if nearest_deadline_label else "срочного срока нет"
    return [
        {
            "key": "available",
            "label": f"Доступно к концу {year} года",
            "value": planning_need.get("available_days_label", _days_label(planning_need.get("available_days"))),
            "detail": "общий оплачиваемый остаток на конец года",
            "tone": "total",
        },
        {
            "key": "mandatory",
            "label": "Остаток прошлых лет",
            "value": planning_need.get("mandatory_days_label", _days_label(planning_need.get("mandatory_days"))),
            "detail": mandatory_detail,
            "tone": "mandatory" if planning_need.get("mandatory_days", Decimal("0.00")) > 0 else "muted",
        },
        {
            "key": "annual",
            "label": "Годовой план",
            "value": planning_need.get("annual_target_days_label", _days_label(planning_need.get("annual_target_days"))),
            "detail": "часть текущего планового года",
            "tone": "annual",
        },
        {
            "key": "placed",
            "label": "Уже назначено",
            "value": planning_need.get("placed_days_label", _days_label(planning_need.get("placed_days"))),
            "detail": "периоды, которые уже есть в черновике",
            "tone": "placed",
        },
        {
            "key": "open",
            "label": "Осталось добрать",
            "value": planning_need.get("open_required_days_label", _days_label(planning_need.get("open_required_days"))),
            "detail": "сколько нужно распределить сейчас",
            "tone": "open" if planning_need.get("open_required_days", Decimal("0.00")) > 0 else "ok",
        },
    ]


def build_schedule_day_calculation_payload(employee, year, planning_need):
    reason_text = _day_calculation_reason(planning_need, year)
    deadline = planning_need.get("nearest_deadline")
    org = _employee_org_payload(employee)
    return {
        "employee_id": employee.id,
        "employee_name": employee.full_name,
        "department_name": org["department_name"],
        "group_name": org["group_name"],
        "available_days": _day_calculation_number(planning_need.get("available_days")),
        "available_days_label": planning_need.get("available_days_label"),
        "mandatory_days": _day_calculation_number(planning_need.get("mandatory_days")),
        "mandatory_days_label": planning_need.get("mandatory_days_label"),
        "annual_target_days": _day_calculation_number(planning_need.get("annual_target_days")),
        "annual_target_days_label": planning_need.get("annual_target_days_label"),
        "placed_days": _day_calculation_number(planning_need.get("placed_days")),
        "placed_days_label": planning_need.get("placed_days_label"),
        "target_days": _day_calculation_number(planning_need.get("target_days")),
        "target_days_label": planning_need.get("target_days_label"),
        "open_required_days": _day_calculation_number(planning_need.get("open_required_days")),
        "open_required_days_label": planning_need.get("open_required_days_label"),
        "deadline_blocking_days": _day_calculation_number(planning_need.get("deadline_blocking_days")),
        "deadline_blocking_days_label": planning_need.get("deadline_blocking_days_label"),
        "annual_remaining_days": _day_calculation_number(planning_need.get("annual_remaining_days")),
        "annual_remaining_days_label": planning_need.get("annual_remaining_days_label"),
        "nearest_deadline": deadline.isoformat() if deadline else "",
        "nearest_deadline_label": planning_need.get("nearest_deadline_label", ""),
        "remainder_policy": planning_need.get("remainder_policy"),
        "remainder_policy_label": planning_need.get("remainder_policy_label"),
        "status_label": (planning_need.get("status") or {}).get("label", ""),
        "reason_text": reason_text,
        "action_text": planning_need.get("action_text", ""),
        "manual_task_label": planning_need.get("manual_task_label", ""),
        "max_periods_label": f"до {MANUAL_DRAFT_MAX_PACKAGE_PERIODS} периодов",
        "summary_text": f"{planning_need.get('placed_days_label')} / {planning_need.get('target_days_label')}",
        "short_reason": reason_text,
        "breakdown": _day_calculation_breakdown(planning_need, year),
    }


def build_schedule_draft_day_calculation(*, year, employee_id, employee=None):
    schedule = VacationSchedule.objects.filter(year=year, status__in=DRAFT_VIEW_SCHEDULE_STATUSES).first()
    if schedule is None:
        raise ValidationError("Черновик графика за этот год не найден.")
    if employee is None:
        employee = next(
            (candidate for candidate in get_eligible_preference_employees(year) if candidate.id == employee_id),
            None,
        )
    if employee is None:
        raise ValidationError("Сотрудник не найден в черновике графика за этот год.")

    draft_items = list(
        schedule.items.select_related("selected_candidate")
        .filter(employee_id=employee.id, status__in=_draft_view_item_statuses(schedule))
        .order_by("start_date", "end_date", "id")
    )
    planning_need = build_employee_schedule_planning_need(employee, year, draft_items=draft_items)
    return build_schedule_day_calculation_payload(employee, year, planning_need)
