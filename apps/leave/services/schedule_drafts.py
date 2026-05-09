import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from math import ceil
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from apps.leave.models import (
    VacationPreference,
    VacationPreferenceCollection,
    VacationRequest,
    VacationSchedule,
    VacationScheduleItem,
)

from .dates import format_period_label, get_chargeable_leave_days, quantize_leave_days
from .employee_presentation import get_employee_identity_presentation
from .ledger import (
    get_employee_available_balance,
    get_employee_entitlement_rows,
    get_employee_entitlement_rows_bulk,
    get_employee_list_leave_summaries,
)
from .preferences import (
    get_eligible_preference_employees,
    get_employee_preference_pair_map,
    get_employee_preference_pair,
    get_employee_preference_state_map,
    get_employee_preference_state,
    get_paid_leave_available_from,
)
from .risk import calculate_vacation_request_risk_with_explanation
from .staffing import format_staff_count
from .urgent_closures import detect_previous_year_closure_need
from .validation import MIN_CONTINUOUS_PAID_LEAVE_DAYS, get_overlapping_requests, get_overlapping_schedule_items


@dataclass(frozen=True)
class DraftPlacement:
    employee_id: int
    start_date: date
    end_date: date
    item_id: int | None = None


@dataclass
class DraftItemBalance:
    item_id: int
    start_date: date
    end_date: date
    remaining_days: Decimal


AUTO_DRAFT_FALLBACK_CHUNK_DAYS = 28
AUTO_DRAFT_FALLBACK_STEPS = (28, 21, 14)
AUTO_DRAFT_ANCHOR_DAYS = (1, 8, 15, 22)
AUTO_DRAFT_MAX_CHUNKS_PER_EMPLOYEE = 6
AUTO_DRAFT_MIN_GAP_BETWEEN_ITEMS_DAYS = 14


def schedule_draft_url(year):
    return reverse("schedule_draft_detail", args=[year])


def schedule_draft_create_url(year):
    return reverse("schedule_draft_create", args=[year])


def get_schedule_draft_status(year):
    schedule = VacationSchedule.objects.filter(year=year).first()
    draft_schedule = schedule if schedule is not None and schedule.status == VacationSchedule.STATUS_DRAFT else None
    items_count = 0
    if draft_schedule is not None:
        items_count = draft_schedule.items.filter(status=VacationScheduleItem.STATUS_DRAFT).count()
    return {
        "schedule": draft_schedule,
        "exists": draft_schedule is not None,
        "blocked_by_existing_schedule": schedule is not None and draft_schedule is None,
        "items_count": items_count,
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


def _has_employee_draft_overlap(placements, employee_id, start_date, end_date, *, exclude_item_id=None):
    return any(
        placement.employee_id == employee_id
        and placement.item_id != exclude_item_id
        and _periods_overlap(placement.start_date, placement.end_date, start_date, end_date)
        for placement in placements
    )


def _manual_reason(kind, text, detail=""):
    return {
        "kind": kind,
        "text": text,
        "detail": detail,
    }


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


def assess_schedule_draft_candidate(
    employee,
    start_date,
    end_date,
    year,
    placements,
    *,
    max_chargeable_days=None,
    exclude_schedule_item_id=None,
):
    if not start_date or not end_date:
        return {
            "can_place": False,
            "has_conflict": True,
            "reason": _manual_reason("missing_period", "Период не заполнен."),
        }
    if start_date.year != year or end_date.year != year or end_date < start_date:
        return {
            "can_place": False,
            "has_conflict": True,
            "reason": _manual_reason("invalid_period", "Период вне выбранного года."),
        }

    available_from = get_paid_leave_available_from(employee)
    if start_date < available_from:
        return {
            "can_place": False,
            "has_conflict": True,
            "reason": _manual_reason("too_early", f"Оплачиваемый отпуск доступен с {available_from:%d.%m.%Y}."),
        }

    chargeable_days = get_chargeable_leave_days(start_date, end_date, "paid")
    if chargeable_days <= 0:
        return {
            "can_place": False,
            "has_conflict": True,
            "reason": _manual_reason("empty_period", "В периоде нет списываемых дней отпуска."),
        }
    if max_chargeable_days is not None and Decimal(chargeable_days) > quantize_leave_days(max_chargeable_days):
        return {
            "can_place": False,
            "has_conflict": True,
            "reason": _manual_reason("too_many_days", "Период превышает остаток, который нужно распределить."),
        }

    if get_overlapping_requests(employee, start_date, end_date).exists():
        return {
            "can_place": False,
            "has_conflict": True,
            "reason": _manual_reason("employee_overlap", "У сотрудника уже есть активная заявка на эти даты."),
        }
    schedule_overlaps = get_overlapping_schedule_items(employee, start_date, end_date)
    if exclude_schedule_item_id is not None:
        schedule_overlaps = schedule_overlaps.exclude(pk=exclude_schedule_item_id)
    if schedule_overlaps.exists() or _has_employee_draft_overlap(
        placements,
        employee.id,
        start_date,
        end_date,
        exclude_item_id=exclude_schedule_item_id,
    ):
        return {
            "can_place": False,
            "has_conflict": True,
            "reason": _manual_reason("employee_overlap", "У сотрудника уже есть отпуск на эти даты."),
        }

    extra_absent_ids = _extra_absent_ids_for_period(
        placements,
        start_date,
        end_date,
        exclude_employee_id=employee.id,
    )
    risk_payload = calculate_vacation_request_risk_with_explanation(
        employee=employee,
        start_date=start_date,
        end_date=end_date,
        vacation_type="paid",
        exclude_schedule_item_id=exclude_schedule_item_id,
        extra_absent_employee_ids=extra_absent_ids,
    )
    explanation = risk_payload.get("risk_explanation") or {}
    has_conflict = bool(explanation.get("is_conflict"))
    if risk_payload.get("balance_after_request") is not None and risk_payload["balance_after_request"] < Decimal("0"):
        return {
            "can_place": False,
            "has_conflict": True,
            "risk_payload": risk_payload,
            "reason": _manual_reason("negative_balance", "Недостаточно оплачиваемых дней."),
        }
    if has_conflict:
        return {
            "can_place": False,
            "has_conflict": True,
            "risk_payload": risk_payload,
            "reason": _manual_reason(
                "staffing_conflict",
                explanation.get("short_reason") or "Период нарушает правила состава.",
            ),
        }

    return {
        "can_place": True,
        "has_conflict": False,
        "risk_payload": risk_payload,
        "chargeable_days": chargeable_days,
        "reason": _manual_reason("ok", "Период можно поставить в черновик."),
    }


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
    item_balances = _draft_item_balances(draft_items)
    covered = Decimal("0.00")
    open_rows = []

    for row in sorted(mandatory_rows, key=lambda item: (item["must_use_by"], item["period_start"])):
        row_open = quantize_leave_days(row["remaining_days"])
        row_covered = Decimal("0.00")

        for item_balance in item_balances:
            if item_balance.remaining_days <= 0 or item_balance.end_date > row["must_use_by"]:
                continue
            used_days = min(row_open, item_balance.remaining_days)
            if used_days <= 0:
                continue
            item_balance.remaining_days = quantize_leave_days(item_balance.remaining_days - used_days)
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
    if blocking_days > 0:
        if nearest_deadline and nearest_deadline < planning_start:
            return {
                "key": "overdue",
                "label": "Срок прошел",
                "icon": "report",
                "tone": "blocker",
            }
        if nearest_deadline and nearest_deadline <= date(year, 1, 31):
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
        min(Decimal(employee.annual_paid_leave_days), max(plan_available_days - mandatory_days, Decimal("0.00")))
    )
    base_target_days = quantize_leave_days(min(available_days, mandatory_days + annual_target_days))
    preference_target_days = Decimal("0.00")
    if preference_state == VacationPreference.STATUS_FILLED:
        remainder_policy = remainder_policy or VacationPreference.REMAINDER_AUTO
        preference_target_days = quantize_leave_days(min(available_days, mandatory_days + requested_preference_days))
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
    future_available_days = quantize_leave_days(max(available_days - plan_available_days, Decimal("0.00")))
    deferred_days = quantize_leave_days(max(available_days - target_days, Decimal("0.00")))
    optional_annual_days = quantize_leave_days(max(base_target_days - preference_target_days, Decimal("0.00")))
    remainder_approval_days = Decimal("0.00")
    employee_deferred_days = Decimal("0.00")
    auto_remainder_days = Decimal("0.00")
    if preference_state == VacationPreference.STATUS_FILLED and optional_annual_days > 0:
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

    if blocking_days > 0:
        action_text = f"Блокирует согласование: {_days_label(blocking_days)} нужно закрыть до {nearest_deadline_label}."
    elif open_required_days > 0:
        action_text = f"Осталось распределить {_days_label(open_required_days)} годового плана."
    elif remainder_approval_days > 0:
        action_text = f"Пожелание закрыто. {_days_label(remainder_approval_days)} остатка ждут отдельного согласования."
    elif employee_deferred_days > 0:
        action_text = f"Пожелание закрыто. {_days_label(employee_deferred_days)} не планируются сверх указанного периода."
    elif future_available_days > 0:
        action_text = f"Годовой план закрыт. {_days_label(future_available_days)} откроется позже как резерв."
    else:
        action_text = "Годовой план закрыт."

    plan_breakdown = [
        {
            "label": "Обяз.",
            "value": _days_label(mandatory_days),
            "tone": "mandatory" if mandatory_days > 0 else "muted",
        },
        {
            "label": "Годовой план",
            "value": _days_label(annual_target_days),
            "tone": "annual" if annual_target_days > 0 else "muted",
        },
    ]
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
                "label": "Остаток авто",
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
                "label": "Отложено",
                "value": _days_label(employee_deferred_days),
                "tone": "muted",
            }
        )
    if future_available_days > 0:
        plan_breakdown.append(
            {
                "label": "Будущий резерв",
                "value": _days_label(future_available_days),
                "tone": "future",
            }
        )

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
        requested_preference_days=_requested_preference_days(preference_pair, preference_state),
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
            requested_preference_days=_requested_preference_days(
                preference_pair_by_employee.get(employee.id),
                preference_state_by_employee.get(employee.id),
            ),
            remainder_policy=_preference_remainder_policy(
                preference_pair_by_employee.get(employee.id),
                preference_state_by_employee.get(employee.id),
            ),
            preference_state=preference_state_by_employee.get(employee.id),
        )
        for employee in employees
    }


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


def _candidate_start_dates(year, employee, start_bound, latest_end, *, urgent=False, target_days=None):
    if start_bound > latest_end:
        return []

    planning_window_days = (latest_end - start_bound).days
    if planning_window_days <= 45:
        return [start_bound + timedelta(days=offset) for offset in range((latest_end - start_bound).days + 1)]

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
        forward_distance = (value.month - preferred_month) % 12
        backward_distance = (preferred_month - value.month) % 12
        return min(forward_distance, backward_distance), value.day, value

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


def _replace_employee_placements(placements, employee_id, items):
    placements[:] = [
        placement
        for placement in placements
        if placement.employee_id != employee_id
    ]
    placements.extend(
        DraftPlacement(item.employee_id, item.start_date, item.end_date, item.id)
        for item in items
    )


def _create_draft_item_from_assessment(schedule, employee, start_date, end_date, assessment, *, source, comment):
    risk_payload = assessment["risk_payload"]
    return VacationScheduleItem.objects.create(
        schedule=schedule,
        employee=employee,
        start_date=start_date,
        end_date=end_date,
        vacation_type="paid",
        chargeable_days=assessment["chargeable_days"],
        status=VacationScheduleItem.STATUS_DRAFT,
        source=source,
        risk_score=risk_payload["risk_score"],
        risk_level=risk_payload["risk_level"],
        generated_by_ai=False,
        was_changed_by_manager=source == VacationScheduleItem.SOURCE_MANUAL,
        manager_comment=comment,
    )


def _merge_comment_for_items(items):
    has_manual = any(item.was_changed_by_manager or item.source == VacationScheduleItem.SOURCE_MANUAL for item in items)
    if has_manual:
        return "Соседние части объединены HR в один непрерывный отпуск."
    return "Соседние части объединены системой в один непрерывный отпуск."


def _merge_adjacent_employee_draft_items(schedule, employee, draft_items_by_employee, placements):
    items = sorted(
        [
            item
            for item in draft_items_by_employee.get(employee.id, [])
            if item.status == VacationScheduleItem.STATUS_DRAFT
        ],
        key=lambda item: (item.start_date, item.end_date, item.id or 0),
    )
    if len(items) < 2:
        return 0

    merged_items = []
    deleted_ids = []
    index = 0

    while index < len(items):
        group = [items[index]]
        group_start = items[index].start_date
        group_end = items[index].end_date
        cursor = index + 1

        while cursor < len(items) and items[cursor].start_date <= group_end + timedelta(days=1):
            group.append(items[cursor])
            group_start = min(group_start, items[cursor].start_date)
            group_end = max(group_end, items[cursor].end_date)
            cursor += 1

        if len(group) == 1:
            merged_items.append(group[0])
            index = cursor
            continue

        keep_item = group[0]
        group_ids = {item.id for item in group if item.id is not None}
        other_placements = [
            placement
            for placement in placements
            if placement.employee_id != employee.id or placement.item_id not in group_ids
        ]
        chargeable_days = get_chargeable_leave_days(group_start, group_end, "paid")
        risk_payload = calculate_vacation_request_risk_with_explanation(
            employee=employee,
            start_date=group_start,
            end_date=group_end,
            vacation_type="paid",
            exclude_schedule_item_id=keep_item.id,
            extra_absent_employee_ids=_extra_absent_ids_for_period(
                other_placements,
                group_start,
                group_end,
                exclude_employee_id=employee.id,
            ),
        )
        has_manual = any(item.was_changed_by_manager or item.source == VacationScheduleItem.SOURCE_MANUAL for item in group)

        keep_item.start_date = group_start
        keep_item.end_date = group_end
        keep_item.chargeable_days = chargeable_days
        keep_item.risk_score = risk_payload["risk_score"]
        keep_item.risk_level = risk_payload["risk_level"]
        keep_item.source = VacationScheduleItem.SOURCE_MANUAL if has_manual else VacationScheduleItem.SOURCE_GENERATED
        keep_item.was_changed_by_manager = has_manual
        keep_item.manager_comment = _merge_comment_for_items(group)
        keep_item.save(
            update_fields=[
                "start_date",
                "end_date",
                "chargeable_days",
                "risk_score",
                "risk_level",
                "source",
                "was_changed_by_manager",
                "manager_comment",
            ]
        )

        remove_ids = [item.id for item in group[1:] if item.id is not None]
        if remove_ids:
            VacationScheduleItem.objects.filter(pk__in=remove_ids).delete()
            deleted_ids.extend(remove_ids)
        merged_items.append(keep_item)
        index = cursor

    if deleted_ids:
        draft_items_by_employee[employee.id] = sorted(
            merged_items,
            key=lambda item: (item.start_date, item.end_date, item.id or 0),
        )
        _replace_employee_placements(placements, employee.id, draft_items_by_employee[employee.id])

    return len(deleted_ids)


def _find_auto_candidate(employee, year, placements, target_days, latest_end, *, urgent=False, allow_short_parts=False):
    target_days = _decimal_to_whole_days(target_days)
    if target_days <= 0:
        return None

    planning_start, planning_end = _planning_year_bounds(year)
    start_bound = max(planning_start, get_paid_leave_available_from(employee))
    latest_end = min(latest_end or planning_end, planning_end)

    for start_date in _candidate_start_dates(
        year,
        employee,
        start_bound,
        latest_end,
        urgent=urgent,
        target_days=target_days,
    ):
        end_date = _end_date_for_chargeable_days(start_date, target_days, latest_end)
        if end_date is None:
            continue
        calendar_days = (end_date - start_date).days + 1
        if not allow_short_parts and calendar_days < MIN_CONTINUOUS_PAID_LEAVE_DAYS:
            continue
        if _has_short_gap_to_employee_placement(placements, employee.id, start_date, end_date):
            continue

        assessment = assess_schedule_draft_candidate(
            employee,
            start_date,
            end_date,
            year,
            placements,
            max_chargeable_days=Decimal(target_days),
        )
        if not assessment["can_place"]:
            continue

        return {
            "start_date": start_date,
            "end_date": end_date,
            "assessment": assessment,
        }

    return None


def _find_auto_candidate_for_need(
    employee,
    year,
    placements,
    target_days,
    latest_end,
    *,
    urgent=False,
    allow_short_parts=False,
):
    for option_days in _auto_target_day_options(target_days):
        candidate = _find_auto_candidate(
            employee,
            year,
            placements,
            option_days,
            latest_end,
            urgent=urgent,
            allow_short_parts=allow_short_parts,
        )
        if candidate is not None:
            return candidate
    return None


def _sort_auto_place_employees(employees, planning_need_by_employee):
    def sort_key(employee):
        planning_need = planning_need_by_employee[employee.id]
        nearest_deadline = planning_need["nearest_deadline"] or date.max
        return (
            0 if planning_need["has_blocker"] else 1,
            nearest_deadline,
            -float(planning_need["blocking_days"]),
            -float(planning_need["open_required_days"]),
            employee.last_name,
            employee.first_name,
            employee.id,
        )

    return sorted(
        [
            employee
            for employee in employees
            if planning_need_by_employee[employee.id]["needs_manual_attention"]
        ],
        key=sort_key,
    )


def _auto_place_target_for_planning_need(employee, year, planning_need):
    if not planning_need["needs_manual_attention"]:
        return None

    _, planning_end = _planning_year_bounds(year)
    if planning_need["has_blocker"]:
        previous_year_closure = detect_previous_year_closure_need(employee, year, planning_need)
        previous_year_closure_days = quantize_leave_days(
            previous_year_closure["required_days"] if previous_year_closure else Decimal("0.00")
        )
        current_year_blocking_days = quantize_leave_days(
            max(planning_need["blocking_days"] - previous_year_closure_days, Decimal("0.00"))
        )
        if current_year_blocking_days > 0:
            return {
                "target_days": current_year_blocking_days,
                "latest_end": planning_need["nearest_deadline"],
                "urgent": True,
                "allow_short_parts": False,
                "comment": "Автоматически распределено: срочный остаток отпуска.",
            }

        current_year_target_days = quantize_leave_days(
            max(planning_need["open_required_days"] - previous_year_closure_days, Decimal("0.00"))
        )
        if current_year_target_days <= 0:
            return None

        return {
            "target_days": current_year_target_days,
            "latest_end": planning_end,
            "urgent": False,
            "allow_short_parts": False,
            "comment": "Автоматически распределено: годовой план при отдельном срочном остатке.",
        }

    return {
        "target_days": planning_need["open_required_days"],
        "latest_end": planning_end,
        "urgent": False,
        "allow_short_parts": False,
        "comment": "Автоматически распределено: добивка по пожеланию сотрудника.",
    }


@transaction.atomic
def create_schedule_draft_from_preferences(*, year, actor):
    collection = VacationPreferenceCollection.objects.select_for_update().filter(year=year).first()
    if collection is None:
        raise ValidationError("Сбор пожеланий за этот год не найден.")
    if collection.status != VacationPreferenceCollection.STATUS_FINISHED:
        raise ValidationError("Черновик можно создать только после завершения сбора пожеланий.")

    existing_schedule = VacationSchedule.objects.select_for_update().filter(year=year).first()
    if existing_schedule is not None:
        if existing_schedule.status == VacationSchedule.STATUS_DRAFT:
            return {
                "schedule": existing_schedule,
                "created": False,
                "placed_count": existing_schedule.items.filter(status=VacationScheduleItem.STATUS_DRAFT).count(),
            }
        raise ValidationError("Для этого года уже есть утвержденный или согласуемый график.")

    schedule = VacationSchedule.objects.create(
        year=year,
        status=VacationSchedule.STATUS_DRAFT,
        created_by=actor,
        generated_at=timezone.now(),
    )
    placements = []
    placed_count = 0

    for employee in get_eligible_preference_employees(year):
        if get_employee_preference_state(employee, year) != VacationPreference.STATUS_FILLED:
            continue

        pair = get_employee_preference_pair(employee, year)
        for priority in (VacationPreference.PRIORITY_PRIMARY, VacationPreference.PRIORITY_BACKUP):
            preference = pair.get(priority)
            assessment = assess_preference_candidate(employee, preference, year, placements)
            if not assessment["can_place"]:
                continue

            risk_payload = assessment["risk_payload"]
            item = VacationScheduleItem.objects.create(
                schedule=schedule,
                employee=employee,
                start_date=preference.start_date,
                end_date=preference.end_date,
                vacation_type="paid",
                chargeable_days=assessment["chargeable_days"],
                status=VacationScheduleItem.STATUS_DRAFT,
                source=VacationScheduleItem.SOURCE_GENERATED,
                risk_score=risk_payload["risk_score"],
                risk_level=risk_payload["risk_level"],
                generated_by_ai=False,
                was_changed_by_manager=False,
                manager_comment=f"Создано из сбора пожеланий: {_selected_preference_label(preference, pair).lower()}.",
            )
            placements.append(DraftPlacement(item.employee_id, item.start_date, item.end_date, item.id))
            placed_count += 1
            break

    return {
        "schedule": schedule,
        "created": True,
        "placed_count": placed_count,
    }


@transaction.atomic
def auto_place_remaining_schedule_draft(*, year, actor):
    schedule = VacationSchedule.objects.select_for_update().filter(
        year=year,
        status=VacationSchedule.STATUS_DRAFT,
    ).first()
    if schedule is None:
        raise ValidationError("Черновик графика за этот год не найден.")

    eligible_employees = get_eligible_preference_employees(year)
    draft_items = _draft_items_for_schedule(schedule)
    draft_items_by_employee = {}
    for item in draft_items:
        draft_items_by_employee.setdefault(item.employee_id, []).append(item)

    employee_ids = [employee.id for employee in eligible_employees]
    preference_pair_by_employee = get_employee_preference_pair_map(employee_ids, year)
    preference_state_by_employee = get_employee_preference_state_map(employee_ids, year)
    placements = _current_placements_from_items(draft_items)
    planning_need_by_employee = build_employee_schedule_planning_need_map(
        eligible_employees,
        year,
        draft_items_by_employee=draft_items_by_employee,
        preference_pair_by_employee=preference_pair_by_employee,
        preference_state_by_employee=preference_state_by_employee,
    )
    placed_count = 0
    unresolved_count = 0

    for employee in _sort_auto_place_employees(eligible_employees, planning_need_by_employee):
        chunks_count = 0
        while chunks_count < AUTO_DRAFT_MAX_CHUNKS_PER_EMPLOYEE:
            current_items = draft_items_by_employee.get(employee.id, [])
            planning_need = build_employee_schedule_planning_need(
                employee,
                year,
                current_items,
                preference_pair=preference_pair_by_employee.get(employee.id),
                preference_state=preference_state_by_employee.get(employee.id),
            )
            if not planning_need["needs_manual_attention"]:
                planning_need_by_employee[employee.id] = planning_need
                break

            auto_target = _auto_place_target_for_planning_need(employee, year, planning_need)
            if auto_target is None:
                unresolved_count += 1
                planning_need_by_employee[employee.id] = planning_need
                break

            candidate = _find_auto_candidate_for_need(
                employee,
                year,
                placements,
                auto_target["target_days"],
                auto_target["latest_end"],
                urgent=auto_target["urgent"],
                allow_short_parts=auto_target["allow_short_parts"],
            )
            if candidate is None:
                unresolved_count += 1
                planning_need_by_employee[employee.id] = planning_need
                break

            item = _create_draft_item_from_assessment(
                schedule,
                employee,
                candidate["start_date"],
                candidate["end_date"],
                candidate["assessment"],
                source=VacationScheduleItem.SOURCE_GENERATED,
                comment=auto_target["comment"],
            )
            draft_items_by_employee.setdefault(employee.id, []).append(item)
            placements.append(DraftPlacement(item.employee_id, item.start_date, item.end_date, item.id))
            _merge_adjacent_employee_draft_items(schedule, employee, draft_items_by_employee, placements)
            placed_count += 1
            chunks_count += 1

        if chunks_count >= AUTO_DRAFT_MAX_CHUNKS_PER_EMPLOYEE:
            planning_need = build_employee_schedule_planning_need(
                employee,
                year,
                draft_items_by_employee.get(employee.id, []),
                preference_pair=preference_pair_by_employee.get(employee.id),
                preference_state=preference_state_by_employee.get(employee.id),
            )
            if planning_need["needs_manual_attention"]:
                unresolved_count += 1

    unresolved_count = build_schedule_draft_page_context(year)["draft_summary"]["manual"]

    return {
        "schedule": schedule,
        "placed_count": placed_count,
        "unresolved_count": unresolved_count,
    }


@transaction.atomic
def place_manual_schedule_draft_item(*, year, employee_id, start_date, end_date, actor):
    schedule = VacationSchedule.objects.select_for_update().filter(
        year=year,
        status=VacationSchedule.STATUS_DRAFT,
    ).first()
    if schedule is None:
        raise ValidationError("Черновик графика за этот год не найден.")

    employee = next(
        (candidate for candidate in get_eligible_preference_employees(year) if candidate.id == employee_id),
        None,
    )
    if employee is None:
        raise ValidationError("Сотрудник не участвует в планировании графика за этот год.")

    draft_items = _draft_items_for_schedule(schedule)
    draft_items_by_employee = {}
    for item in draft_items:
        draft_items_by_employee.setdefault(item.employee_id, []).append(item)
    planning_need = build_employee_schedule_planning_need(
        employee,
        year,
        draft_items_by_employee.get(employee.id, []),
        preference_pair=get_employee_preference_pair(employee, year),
        preference_state=get_employee_preference_state(employee, year),
    )
    if not planning_need["needs_manual_attention"]:
        raise ValidationError("По сотруднику уже закрыта плановая потребность.")

    assessment = assess_schedule_draft_candidate(
        employee,
        start_date,
        end_date,
        year,
        _current_placements_from_items(draft_items),
        max_chargeable_days=planning_need["open_required_days"],
    )
    if not assessment["can_place"]:
        raise ValidationError(assessment["reason"]["text"])

    item = _create_draft_item_from_assessment(
        schedule,
        employee,
        start_date,
        end_date,
        assessment,
        source=VacationScheduleItem.SOURCE_MANUAL,
        comment=f"Вручную размещено HR: {actor.full_name if actor else 'HR'}.",
    )
    draft_items_by_employee.setdefault(employee.id, []).append(item)
    placements = _current_placements_from_items(draft_items)
    placements.append(DraftPlacement(item.employee_id, item.start_date, item.end_date, item.id))
    _merge_adjacent_employee_draft_items(schedule, employee, draft_items_by_employee, placements)
    item = next(
        (
            candidate
            for candidate in draft_items_by_employee.get(employee.id, [])
            if candidate.start_date <= start_date and candidate.end_date >= end_date
        ),
        item,
    )
    return {
        "schedule": schedule,
        "item": item,
    }


def build_manual_schedule_draft_preview(*, year, employee_id, start_date, end_date):
    schedule = VacationSchedule.objects.filter(
        year=year,
        status=VacationSchedule.STATUS_DRAFT,
    ).first()
    if schedule is None:
        raise ValidationError("Черновик графика за этот год не найден.")

    employee = next(
        (candidate for candidate in get_eligible_preference_employees(year) if candidate.id == employee_id),
        None,
    )
    if employee is None:
        raise ValidationError("Сотрудник не участвует в планировании графика за этот год.")

    draft_items = _draft_items_for_schedule(schedule)
    draft_items_by_employee = {}
    for item in draft_items:
        draft_items_by_employee.setdefault(item.employee_id, []).append(item)

    planning_need = build_employee_schedule_planning_need(
        employee,
        year,
        draft_items_by_employee.get(employee.id, []),
        preference_pair=get_employee_preference_pair(employee, year),
        preference_state=get_employee_preference_state(employee, year),
    )
    calendar_days = (end_date - start_date).days + 1 if end_date >= start_date else 0
    chargeable_days = get_chargeable_leave_days(start_date, end_date, "paid") if calendar_days else 0
    placements = _current_placements_from_items(draft_items)
    current_items = draft_items_by_employee.get(employee.id, [])
    adjacent_items = _adjacent_employee_items(current_items, start_date, end_date)
    adjacent_ids = {item.id for item in adjacent_items if item.id is not None}
    merged_start = min([start_date, *(item.start_date for item in adjacent_items)])
    merged_end = max([end_date, *(item.end_date for item in adjacent_items)])
    merged_chargeable_days = get_chargeable_leave_days(merged_start, merged_end, "paid") if calendar_days else 0
    has_short_gap = _has_short_gap_to_employee_placement(
        placements,
        employee.id,
        start_date,
        end_date,
        exclude_item_ids=adjacent_ids,
    )

    if not planning_need["needs_manual_attention"]:
        return {
            "can_submit": False,
            "message": "По сотруднику уже закрыта плановая потребность.",
            "calendar_days": calendar_days,
            "chargeable_days": chargeable_days,
            "merged_calendar_days": (merged_end - merged_start).days + 1 if calendar_days else 0,
            "merged_chargeable_days": merged_chargeable_days,
            "remaining_after_placement": planning_need["open_required_days"],
            "risk_label": "Низкий",
            "risk_score": 0,
            "risk_short_reason": "",
            "risk_recommended_action": "",
            "risk_is_conflict": False,
            "will_merge": bool(adjacent_items),
            "merged_period_label": _period_label(merged_start, merged_end) if calendar_days else "",
            "short_gap_warning": has_short_gap,
            "planning_need": planning_need,
        }

    assessment = assess_schedule_draft_candidate(
        employee,
        start_date,
        end_date,
        year,
        placements,
        max_chargeable_days=planning_need["open_required_days"],
    )
    can_submit = bool(assessment["can_place"])
    message = "Период можно поставить в черновик."
    risk_payload = assessment.get("risk_payload")
    risk_explanation = (risk_payload or {}).get("risk_explanation") or {}

    if can_submit:
        other_placements = [
            placement
            for placement in placements
            if placement.employee_id != employee.id or placement.item_id not in adjacent_ids
        ]
        risk_payload = calculate_vacation_request_risk_with_explanation(
            employee=employee,
            start_date=merged_start,
            end_date=merged_end,
            vacation_type="paid",
            extra_absent_employee_ids=_extra_absent_ids_for_period(
                other_placements,
                merged_start,
                merged_end,
                exclude_employee_id=employee.id,
            ),
        )
        risk_explanation = risk_payload.get("risk_explanation") or {}
        if adjacent_items:
            message = "Период будет объединён с соседней частью в один непрерывный отпуск."
        if has_short_gap:
            message = (
                "Поставить можно, но рядом уже есть другой отпуск с коротким разрывом. "
                "Проверьте, что такое разделение согласовано с сотрудником."
            )
        if risk_explanation.get("is_conflict"):
            message = "Поставить можно, но после размещения будет конфликт состава."
        elif risk_payload.get("risk_level") == VacationRequest.RISK_HIGH:
            message = "Поставить можно, но риск состава высокий."
    else:
        message = assessment["reason"]["text"]
        if risk_payload is None:
            risk_payload = {
                "risk_score": 0,
                "risk_level": VacationRequest.RISK_LOW,
                "balance_after_request": get_employee_available_balance(employee),
            }

    risk_label = dict(VacationRequest.RISK_CHOICES).get(risk_payload["risk_level"], "Низкий")
    remaining_after_placement = quantize_leave_days(
        max(planning_need["open_required_days"] - Decimal(chargeable_days), Decimal("0.00"))
    )
    return {
        "can_submit": can_submit,
        "message": message,
        "calendar_days": calendar_days,
        "chargeable_days": chargeable_days,
        "merged_calendar_days": (merged_end - merged_start).days + 1 if calendar_days else 0,
        "merged_chargeable_days": merged_chargeable_days,
        "remaining_after_placement": remaining_after_placement,
        "risk_label": risk_label,
        "risk_score": risk_payload["risk_score"],
        "risk_short_reason": risk_explanation.get("short_reason", ""),
        "risk_recommended_action": risk_explanation.get("recommended_action", ""),
        "risk_is_conflict": risk_explanation.get("is_conflict", False),
        "will_merge": bool(adjacent_items),
        "merged_period_label": _period_label(merged_start, merged_end) if calendar_days else "",
        "short_gap_warning": has_short_gap,
        "planning_need": planning_need,
    }


def _source_label_for_item(item, pair):
    primary = pair.get(VacationPreference.PRIORITY_PRIMARY)
    backup = pair.get(VacationPreference.PRIORITY_BACKUP)
    if primary and item.start_date == primary.start_date and item.end_date == primary.end_date:
        return "Основное пожелание"
    if backup and item.start_date == backup.start_date and item.end_date == backup.end_date:
        return "Запасной период"
    if item.source == VacationScheduleItem.SOURCE_MANUAL:
        return "Вручную"
    return "Сформировано системой"


def _employee_org_payload(employee):
    position = employee.employee_position
    group = position.production_group if position and position.production_group_id else None
    return {
        "department_name": employee.department.name if employee.department_id else "Без отдела",
        "group_name": group.name if group else "Без группы",
        "position": employee.position,
    }


def _employee_identity_payload(employee):
    identity = get_employee_identity_presentation(employee)
    return {
        "role_icon": identity["employee_role_icon"],
        "role_icon_type": identity["employee_role_icon_type"],
        "role_variant": identity["employee_role_variant"],
        "role_label": identity["employee_role_label"],
        "management_badges": identity["employee_management_badges"],
    }


def _profile_url(employee, year):
    params = urlencode(
        {
            "from": "preferences",
            "back_url": schedule_draft_url(year),
            "back_label": "К черновику",
        }
    )
    return f"{reverse('employee_profile', args=[employee.id])}?{params}"


def _draft_items_for_schedule(schedule):
    if schedule is None:
        return []

    return list(
        schedule.items.select_related(
            "employee",
            "employee__department",
            "employee__employee_position",
            "employee__employee_position__production_group",
        )
        .filter(status=VacationScheduleItem.STATUS_DRAFT)
        .order_by("start_date", "employee__last_name", "employee__first_name", "employee__middle_name")
    )


@transaction.atomic
def normalize_schedule_draft_adjacent_items(year):
    schedule = VacationSchedule.objects.select_for_update().filter(
        year=year,
        status=VacationSchedule.STATUS_DRAFT,
    ).first()
    if schedule is None:
        return 0

    draft_items = _draft_items_for_schedule(schedule)
    draft_items_by_employee = {}
    for item in draft_items:
        draft_items_by_employee.setdefault(item.employee_id, []).append(item)

    placements = _current_placements_from_items(draft_items)
    merged_count = 0
    for items in list(draft_items_by_employee.values()):
        if len(items) < 2:
            continue
        employee = items[0].employee
        merged_count += _merge_adjacent_employee_draft_items(
            schedule,
            employee,
            draft_items_by_employee,
            placements,
        )
    return merged_count


def _generic_risk_summary(risk_level):
    if risk_level == VacationRequest.RISK_HIGH:
        return "Высокий риск сохранен при создании черновика."
    if risk_level == VacationRequest.RISK_MEDIUM:
        return "Средний риск сохранен при создании черновика."
    return "Критичных пересечений не найдено."


def _draft_item_rows(schedule, year, items, planning_need_by_employee, preference_pair_by_employee=None):
    if schedule is None:
        return []

    placements = [DraftPlacement(item.employee_id, item.start_date, item.end_date, item.id) for item in items]
    preference_pair_by_employee = preference_pair_by_employee or {}
    rows = []
    for item in items:
        employee = item.employee
        pair = preference_pair_by_employee.get(employee.id) or get_employee_preference_pair(employee, year)
        risk_score = int(item.risk_score or 0)
        risk_level = item.risk_level or VacationRequest.RISK_LOW
        explanation = {}
        risk_summary = _generic_risk_summary(risk_level)

        if risk_level == VacationRequest.RISK_HIGH:
            extra_absent_ids = _extra_absent_ids_for_period(
                placements,
                item.start_date,
                item.end_date,
                exclude_employee_id=employee.id,
            )
            risk_payload = calculate_vacation_request_risk_with_explanation(
                employee=employee,
                start_date=item.start_date,
                end_date=item.end_date,
                vacation_type=item.vacation_type,
                exclude_schedule_item_id=item.id,
                extra_absent_employee_ids=extra_absent_ids,
            )
            risk_score = risk_payload["risk_score"]
            risk_level = risk_payload["risk_level"]
            explanation = risk_payload.get("risk_explanation") or {}
            risk_summary = explanation.get("short_reason") or risk_summary

        has_conflict = bool(explanation.get("is_conflict"))
        has_high_risk = risk_level == VacationRequest.RISK_HIGH
        org = _employee_org_payload(employee)
        identity = _employee_identity_payload(employee)
        rows.append(
            {
                "item": item,
                "employee": employee,
                "employee_name": employee.full_name,
                "department_name": org["department_name"],
                "group_name": org["group_name"],
                "position": org["position"],
                "period_label": _short_period_label(item.start_date, item.end_date),
                "full_period_label": _period_label(item.start_date, item.end_date),
                "source_label": _source_label_for_item(item, pair),
                "chargeable_days": item.chargeable_days,
                "risk_score": risk_score,
                "risk_level": risk_level,
                "risk_label": dict(VacationRequest.RISK_CHOICES).get(risk_level, "Низкий"),
                "has_conflict": has_conflict,
                "has_high_risk": has_high_risk,
                "issue_label": "Конфликт" if has_conflict else ("Высокий риск" if has_high_risk else "Без проблем"),
                "issue_icon": "warning" if has_conflict else ("bolt" if has_high_risk else "verified"),
                "risk_summary": risk_summary,
                "risk_details": list(explanation.get("details") or [])[:3],
                "profile_url": _profile_url(employee, year),
                "planning_need": planning_need_by_employee.get(employee.id),
                **identity,
            }
        )
    return rows


def _manual_row_for_employee(
    employee,
    year,
    placed_employee_ids,
    placed_rows,
    planning_need,
    preference_state_by_employee=None,
    preference_pair_by_employee=None,
):
    preference_state_by_employee = preference_state_by_employee or {}
    preference_pair_by_employee = preference_pair_by_employee or {}
    state = preference_state_by_employee.get(employee.id)
    if state is None:
        state = get_employee_preference_state(employee, year)
    pair = preference_pair_by_employee.get(employee.id)
    if pair is None:
        pair = get_employee_preference_pair(employee, year)
    primary = pair.get(VacationPreference.PRIORITY_PRIMARY)
    backup = pair.get(VacationPreference.PRIORITY_BACKUP)
    has_draft_item = employee.id in placed_employee_ids
    reason = None

    if not planning_need["needs_manual_attention"]:
        return None

    if planning_need["has_blocker"]:
        reason = _manual_reason(
            "deadline_blocker",
            f"Срочно закрыть {_days_label(planning_need['blocking_days'])}",
            (
                f"Блокирует согласование: остаток нужно использовать до "
                f"{planning_need['nearest_deadline_label']}."
            ),
        )
    elif state in {VacationPreference.STATUS_PENDING, "missing"}:
        reason = _manual_reason("pending", "Не ответил на сбор.", "HR сможет выбрать даты вручную на следующем этапе.")
    elif state == VacationPreference.STATUS_SKIPPED:
        reason = _manual_reason("skipped", "Без пожеланий.", "Сотрудник разрешил поставить отпуск по необходимости.")
    elif not has_draft_item:
        placements = [
            DraftPlacement(row["employee"].id, row["item"].start_date, row["item"].end_date, row["item"].id)
            for row in placed_rows
        ]
        primary_assessment = assess_preference_candidate(employee, primary, year, placements)
        backup_assessment = assess_preference_candidate(employee, backup, year, placements)
        if primary_assessment["has_conflict"] and backup_assessment["has_conflict"]:
            reason = _manual_reason(
                "staffing_conflict",
                "Основной и запасной периоды не прошли проверку.",
                "Нужно подобрать другой период с учетом правил состава.",
            )
        else:
            reason = _manual_reason(
                "not_placed",
                "Нужно проверить вручную.",
                "Пожелания есть, но черновой пункт не найден.",
            )
    elif planning_need["needs_manual_attention"]:
        reason = _manual_reason(
            "remaining_plan",
            f"Осталось распределить {_days_label(planning_need['open_required_days'])}",
            "Пожелание или обязательный остаток еще не закрыты полностью.",
        )

    if reason is None:
        return None

    org = _employee_org_payload(employee)
    identity = _employee_identity_payload(employee)
    urgent_closure = detect_previous_year_closure_need(employee, year, planning_need)
    return {
        "employee": employee,
        "employee_name": employee.full_name,
        "department_name": org["department_name"],
        "group_name": org["group_name"],
        "position": org["position"],
        "status": state,
        "reason": reason,
        "primary_label": _period_label(primary.start_date if primary else None, primary.end_date if primary else None),
        "backup_label": _period_label(backup.start_date if backup else None, backup.end_date if backup else None),
        "profile_url": _profile_url(employee, year),
        "manual_place_url": reverse("schedule_draft_manual_place", args=[year, employee.id]),
        "manual_preview_url": reverse("schedule_draft_manual_preview", args=[year, employee.id]),
        "planning_need": planning_need,
        "urgent_closure": urgent_closure,
        **identity,
    }


def build_schedule_draft_page_context(year):
    collection = VacationPreferenceCollection.objects.filter(year=year).first()
    normalize_schedule_draft_adjacent_items(year)
    schedule = VacationSchedule.objects.filter(year=year, status=VacationSchedule.STATUS_DRAFT).first()
    eligible_employees = get_eligible_preference_employees(year)
    employee_ids = [employee.id for employee in eligible_employees]
    preference_pair_by_employee = get_employee_preference_pair_map(employee_ids, year)
    preference_state_by_employee = get_employee_preference_state_map(employee_ids, year)
    draft_items = _draft_items_for_schedule(schedule)
    draft_items_by_employee = {}
    for item in draft_items:
        draft_items_by_employee.setdefault(item.employee_id, []).append(item)
    planning_need_by_employee = build_employee_schedule_planning_need_map(
        eligible_employees,
        year,
        draft_items_by_employee=draft_items_by_employee,
        preference_pair_by_employee=preference_pair_by_employee,
        preference_state_by_employee=preference_state_by_employee,
    )
    placed_rows = _draft_item_rows(
        schedule,
        year,
        draft_items,
        planning_need_by_employee,
        preference_pair_by_employee=preference_pair_by_employee,
    )
    placed_employee_ids = {row["employee"].id for row in placed_rows}
    manual_rows = [
        row
        for employee in eligible_employees
        for row in [
            _manual_row_for_employee(
                employee,
                year,
                placed_employee_ids,
                placed_rows,
                planning_need_by_employee[employee.id],
                preference_state_by_employee=preference_state_by_employee,
                preference_pair_by_employee=preference_pair_by_employee,
            )
        ]
        if row is not None
    ]
    conflict_count = sum(1 for row in placed_rows if row["has_conflict"])
    high_risk_count = sum(1 for row in placed_rows if row["has_high_risk"] and not row["has_conflict"])
    departments = sorted({row["department_name"] for row in placed_rows + manual_rows})
    blocking_rows = [row for row in manual_rows if row["planning_need"]["has_blocker"]]
    total_open_required_days = quantize_leave_days(
        sum((row["planning_need"]["open_required_days"] for row in manual_rows), Decimal("0.00"))
    )
    total_blocking_days = quantize_leave_days(
        sum((row["planning_need"]["blocking_days"] for row in blocking_rows), Decimal("0.00"))
    )
    total_remaining_plan_days = quantize_leave_days(
        sum(
            (
                max(
                    row["planning_need"]["open_required_days"] - row["planning_need"]["blocking_days"],
                    Decimal("0.00"),
                )
                for row in manual_rows
            ),
            Decimal("0.00"),
        )
    )
    return {
        "year": year,
        "collection": collection,
        "schedule": schedule,
        "draft_exists": schedule is not None,
        "draft_url": schedule_draft_url(year),
        "draft_create_url": schedule_draft_create_url(year),
        "draft_auto_place_url": reverse("schedule_draft_auto_place", args=[year]),
        "draft_auto_place_next_url": schedule_draft_url(year),
        "readiness_url": reverse("preference_collection_readiness", args=[year]),
        "placed_rows": placed_rows,
        "manual_rows": manual_rows,
        "planning_need_by_employee": planning_need_by_employee,
        "draft_summary": {
            "placed": len(placed_rows),
            "manual": len(manual_rows),
            "blocking": len(blocking_rows),
            "open_required_days": total_open_required_days,
            "open_required_days_label": _days_label(total_open_required_days),
            "remaining_plan_days": total_remaining_plan_days,
            "remaining_plan_days_label": _days_label(total_remaining_plan_days),
            "blocking_days": total_blocking_days,
            "blocking_days_label": _days_label(total_blocking_days),
            "high_risk": high_risk_count,
            "conflicts": conflict_count,
            "departments": len(departments),
            "total": len(placed_rows) + len(manual_rows),
        },
        "draft_status": {
            "label": "Черновик создан" if schedule else "Черновик не создан",
            "icon": "edit_calendar" if schedule else "pending_actions",
        },
        "manual_count_label": format_staff_count(len(manual_rows)),
        "approval_blocked": bool(blocking_rows),
    }
