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

from apps.accounts.services import get_managed_department_id, is_department_head_employee
from apps.leave.models import (
    VacationPreference,
    VacationPreferenceCollection,
    VacationRequest,
    VacationSchedule,
    VacationScheduleCandidate,
    VacationScheduleCandidatePackage,
    VacationScheduleCandidatePackagePeriod,
    VacationScheduleGenerationRun,
    VacationScheduleItem,
    VacationScheduleManualSuggestionCache,
)

from .dates import format_period_label, get_chargeable_leave_days, quantize_leave_days
from .candidate_scoring import ACTIVE_CANDIDATE_SCORER_VERSION, score_candidate_features
from .candidate_feedback import build_schedule_candidate_feedback_context
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
from .urgent_closures import detect_previous_year_closure_need, get_active_urgent_closure_payload_map
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


@dataclass
class DraftGenerationCandidate:
    employee: object
    start_date: date | None
    end_date: date | None
    kind: str
    source: str
    comment: str
    preference: VacationPreference | None = None
    assessment: dict | None = None
    metadata: dict = field(default_factory=dict)
    stored_candidate: VacationScheduleCandidate | None = None


@dataclass
class DraftGenerationCandidatePackage:
    employee: object
    candidates: list[DraftGenerationCandidate]
    source: str
    explanation: str
    metadata: dict = field(default_factory=dict)
    stored_package: VacationScheduleCandidatePackage | None = None


@dataclass
class DraftGenerationContext:
    year: int
    schedule: VacationSchedule
    eligible_employees: list
    draft_items_by_employee: dict
    preference_pair_by_employee: dict
    preference_state_by_employee: dict
    placements: list
    planning_need_by_employee: dict


AUTO_DRAFT_FALLBACK_CHUNK_DAYS = 28
AUTO_DRAFT_FALLBACK_STEPS = (28, 21, 14)
AUTO_DRAFT_ANCHOR_DAYS = (1, 8, 15, 22)
AUTO_DRAFT_MAX_CHUNKS_PER_EMPLOYEE = 6
AUTO_DRAFT_MAX_AUTO_PLACE_PASSES = 3
AUTO_DRAFT_MIN_GAP_BETWEEN_ITEMS_DAYS = 14
AUTO_DRAFT_MAX_CANDIDATES_PER_STRATEGY = 6
AUTO_DRAFT_MAX_CANDIDATES_PER_EMPLOYEE = 12
AUTO_DRAFT_MAX_PACKAGE_SUGGESTIONS = 10
AUTO_DRAFT_PREVIEW_PACKAGE_SUGGESTIONS = 1
AUTO_DRAFT_PERSISTED_PACKAGE_ALTERNATIVES = 6
MANUAL_DRAFT_MAX_PACKAGE_PERIODS = 3
MANUAL_DRAFT_MAX_PACKAGE_SUGGESTIONS = 18
MANUAL_DRAFT_VISIBLE_PACKAGE_SUGGESTIONS = 18
MANUAL_DRAFT_SUGGESTION_CACHE_SCHEMA_VERSION = 2
DRAFT_GENERATION_RULES_MODEL_VERSION = "rules-v1"
DRAFT_GENERATION_HYBRID_MODEL_VERSION = ACTIVE_CANDIDATE_SCORER_VERSION
DRAFT_CANDIDATE_FEATURE_SCHEMA_VERSION = 1
PRIMARY_PREFERENCE_SCORE_TOLERANCE = Decimal("5.00")
PRIMARY_PREFERENCE_MIN_SCORE = Decimal("70.00")

DRAFT_CANDIDATE_PRIMARY_PREFERENCE = "primary_preference"
DRAFT_CANDIDATE_BACKUP_PREFERENCE = "backup_preference"
DRAFT_CANDIDATE_AUTO = "auto"
DRAFT_CANDIDATE_AUTO_URGENT = "auto_urgent"
DRAFT_CANDIDATE_AUTO_TOPUP = "auto_topup"

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
SUMMER_VACATION_MONTHS = {6, 7, 8}


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
        and (exclude_item_id is None or placement.item_id != exclude_item_id)
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


def assess_schedule_draft_candidate(
    employee,
    start_date,
    end_date,
    year,
    placements,
    *,
    max_chargeable_days=None,
    exclude_schedule_item_id=None,
    risk_context=None,
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
        **(risk_context or {}),
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


def build_schedule_draft_day_calculation(*, year, employee_id):
    schedule = _get_draft_schedule_for_preview(year)
    employee = next(
        (candidate for candidate in get_eligible_preference_employees(year) if candidate.id == employee_id),
        None,
    )
    if employee is None:
        raise ValidationError("Сотрудник не найден в черновике графика за этот год.")

    draft_items = list(
        schedule.items.filter(
            employee=employee,
            status=VacationScheduleItem.STATUS_DRAFT,
        ).order_by("start_date", "end_date", "id")
    )
    planning_need = build_employee_schedule_planning_need(employee, year, draft_items=draft_items)
    return build_schedule_day_calculation_payload(employee, year, planning_need)


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
    return DraftGenerationContext(
        year=year,
        schedule=schedule,
        eligible_employees=eligible_employees,
        draft_items_by_employee=grouped_items,
        preference_pair_by_employee=preference_pair_by_employee,
        preference_state_by_employee=preference_state_by_employee,
        placements=_current_placements_from_items(draft_items),
        planning_need_by_employee=planning_need_by_employee,
    )


def _current_employee_planning_need(context, employee):
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
):
    assessment = assess_schedule_draft_candidate(
        candidate.employee,
        candidate.start_date,
        candidate.end_date,
        year,
        placements,
        max_chargeable_days=max_chargeable_days,
        exclude_schedule_item_id=exclude_schedule_item_id,
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
    for decision_rank, candidate in enumerate(candidates, start=1):
        if "scoring_score" not in candidate.metadata or "scoring_confidence" not in candidate.metadata:
            _apply_candidate_scoring(candidate)
        decision = _candidate_decision(candidate, selected_candidate)
        stored_candidate = VacationScheduleCandidate.objects.create(
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


def _build_auto_generation_candidates(context, employee, current_items, planning_need):
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
                limit=AUTO_DRAFT_MAX_CANDIDATES_PER_STRATEGY,
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
                limit=AUTO_DRAFT_MAX_CANDIDATES_PER_STRATEGY,
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
                return _dedupe_generation_candidates(candidates)[:AUTO_DRAFT_MAX_CANDIDATES_PER_EMPLOYEE]

            if add_topup_candidates(
                target_days=current_year_blocking_days,
                latest_end=planning_need["nearest_deadline"],
                max_chargeable_days=open_required_days,
                comment="Автоматически продлено: срочный остаток закрыт соседней частью отпуска.",
            ):
                return _dedupe_generation_candidates(candidates)[:AUTO_DRAFT_MAX_CANDIDATES_PER_EMPLOYEE]

            if add_period_candidates(
                target_days=current_year_blocking_days,
                latest_end=planning_need["nearest_deadline"],
                urgent=True,
                allow_short_parts=True,
                kind=DRAFT_CANDIDATE_AUTO_URGENT,
                comment="Автоматически распределено: короткий срочный остаток до срока.",
            ):
                return _dedupe_generation_candidates(candidates)[:AUTO_DRAFT_MAX_CANDIDATES_PER_EMPLOYEE]

            if planning_need["annual_remaining_days"] > 0:
                if planning_need["annual_remaining_days"] < MIN_CONTINUOUS_PAID_LEAVE_DAYS and add_topup_candidates(
                    target_days=planning_need["annual_remaining_days"],
                    latest_end=planning_end,
                    max_chargeable_days=open_required_days,
                    comment="Автоматически продлено: автодобор выполнен, срочный остаток остался на ручную проверку.",
                ):
                    return _dedupe_generation_candidates(candidates)[:AUTO_DRAFT_MAX_CANDIDATES_PER_EMPLOYEE]

                if add_period_candidates(
                    target_days=planning_need["annual_remaining_days"],
                    latest_end=planning_end,
                    urgent=False,
                    allow_short_parts=False,
                    kind=DRAFT_CANDIDATE_AUTO,
                    comment="Автоматически распределено: автодобор выполнен, срочный остаток остался на ручную проверку.",
                ):
                    return _dedupe_generation_candidates(candidates)[:AUTO_DRAFT_MAX_CANDIDATES_PER_EMPLOYEE]
            return _dedupe_generation_candidates(candidates)[:AUTO_DRAFT_MAX_CANDIDATES_PER_EMPLOYEE]

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
        return _dedupe_generation_candidates(candidates)[:AUTO_DRAFT_MAX_CANDIDATES_PER_EMPLOYEE]

    if open_required_days < MIN_CONTINUOUS_PAID_LEAVE_DAYS and add_topup_candidates(
        target_days=open_required_days,
        latest_end=planning_end,
        max_chargeable_days=open_required_days,
        comment="Автоматически продлено: короткий остаток объединен с соседним отпуском.",
    ):
        return _dedupe_generation_candidates(candidates)[:AUTO_DRAFT_MAX_CANDIDATES_PER_EMPLOYEE]

    add_period_candidates(
        target_days=open_required_days,
        latest_end=planning_end,
        urgent=False,
        allow_short_parts=False,
        kind=DRAFT_CANDIDATE_AUTO,
        comment="Автоматически распределено: добивка по пожеланию сотрудника.",
    )
    return _dedupe_generation_candidates(candidates)[:AUTO_DRAFT_MAX_CANDIDATES_PER_EMPLOYEE]


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


def _create_draft_item_from_assessment(
    schedule,
    employee,
    start_date,
    end_date,
    assessment,
    *,
    source,
    comment,
    generation_run=None,
    selected_candidate_record=None,
):
    risk_payload = assessment["risk_payload"]
    generated_by_ai = bool(
        generation_run
        and generation_run.mode
        in (VacationScheduleGenerationRun.MODE_NEURAL, VacationScheduleGenerationRun.MODE_HYBRID)
    )
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
        generated_by_ai=generated_by_ai,
        generation_run=generation_run,
        selected_candidate=selected_candidate_record,
        ai_score=selected_candidate_record.score if generated_by_ai and selected_candidate_record else None,
        ai_confidence=selected_candidate_record.confidence if generated_by_ai and selected_candidate_record else None,
        ai_model_version=selected_candidate_record.model_version if generated_by_ai and selected_candidate_record else "",
        ai_explanation=selected_candidate_record.explanation if generated_by_ai and selected_candidate_record else "",
        was_changed_by_manager=source == VacationScheduleItem.SOURCE_MANUAL,
        manager_comment=comment,
    )


def _create_draft_item_from_generation_candidate(schedule, candidate, *, generation_run=None, selected_candidate_record=None):
    if candidate.assessment is None or not _candidate_passed_hard_rules(candidate):
        raise ValidationError("Нельзя создать пункт черновика из неподходящего кандидата.")
    return _create_draft_item_from_assessment(
        schedule,
        candidate.employee,
        candidate.start_date,
        candidate.end_date,
        candidate.assessment,
        source=candidate.source,
        comment=candidate.comment,
        generation_run=generation_run,
        selected_candidate_record=selected_candidate_record or candidate.stored_candidate,
    )


def _register_draft_item_in_generation_context(context, item):
    context.draft_items_by_employee.setdefault(item.employee_id, []).append(item)
    context.placements.append(DraftPlacement(item.employee_id, item.start_date, item.end_date, item.id))


def _virtual_draft_item(
    employee,
    start_date,
    end_date,
    chargeable_days,
    *,
    source=VacationScheduleItem.SOURCE_MANUAL,
    selected_candidate=None,
):
    return SimpleNamespace(
        id=None,
        employee=employee,
        employee_id=employee.id,
        start_date=start_date,
        end_date=end_date,
        vacation_type="paid",
        chargeable_days=quantize_leave_days(chargeable_days),
        status=VacationScheduleItem.STATUS_DRAFT,
        source=source,
        selected_candidate=selected_candidate,
    )


def _planning_need_after_candidate(employee, year, current_items, start_date, end_date, chargeable_days):
    return build_employee_schedule_planning_need(
        employee,
        year,
        [*list(current_items or []), _virtual_draft_item(employee, start_date, end_date, chargeable_days)],
        preference_pair=get_employee_preference_pair(employee, year),
        preference_state=get_employee_preference_state(employee, year),
    )


def _deadline_not_closed_message(planning_need):
    return (
        "Период не закрывает срочный остаток до "
        f"{planning_need['nearest_deadline_label']}. "
        "Выберите даты, которые заканчиваются до срока использования."
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


def _remove_conflicting_generated_draft_items(schedule):
    removed_count = 0
    while True:
        draft_items = _draft_items_for_schedule(schedule)
        placements = _current_placements_from_items(draft_items)
        conflict_ids = [
            item.id
            for item in draft_items
            if item.id is not None
            and item.source == VacationScheduleItem.SOURCE_GENERATED
            and not item.was_changed_by_manager
            and _candidate_assessment_reason(
                assess_schedule_draft_candidate(
                    item.employee,
                    item.start_date,
                    item.end_date,
                    schedule.year,
                    placements,
                    exclude_schedule_item_id=item.id,
                )
            ).get("kind") == "staffing_conflict"
        ]
        if not conflict_ids:
            return removed_count

        deleted_count, _ = VacationScheduleItem.objects.filter(
            schedule=schedule,
            id__in=conflict_ids,
            source=VacationScheduleItem.SOURCE_GENERATED,
            was_changed_by_manager=False,
        ).delete()
        if deleted_count <= 0:
            return removed_count
        removed_count += deleted_count


def _iter_auto_candidate_payloads_for_target(
    employee,
    year,
    placements,
    target_days,
    latest_end,
    *,
    urgent=False,
    allow_short_parts=False,
    max_chargeable_days=None,
    limit=None,
):
    target_days = _decimal_to_whole_days(target_days)
    if target_days <= 0:
        return

    planning_start, planning_end = _planning_year_bounds(year)
    start_bound = max(planning_start, get_paid_leave_available_from(employee))
    latest_end = min(latest_end or planning_end, planning_end)
    yielded_count = 0

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
            max_chargeable_days=max_chargeable_days or Decimal(target_days),
        )
        if not assessment["can_place"]:
            continue

        yield {
            "start_date": start_date,
            "end_date": end_date,
            "assessment": assessment,
            "target_days": Decimal(target_days),
        }
        yielded_count += 1
        if limit is not None and yielded_count >= limit:
            return


def _iter_auto_candidate_payloads_for_need(
    employee,
    year,
    placements,
    target_days,
    latest_end,
    *,
    urgent=False,
    allow_short_parts=False,
    max_chargeable_days=None,
    limit=None,
):
    yielded_count = 0
    for option_days in _auto_target_day_options(target_days):
        for candidate in _iter_auto_candidate_payloads_for_target(
            employee,
            year,
            placements,
            option_days,
            latest_end,
            urgent=urgent,
            allow_short_parts=allow_short_parts,
            max_chargeable_days=max_chargeable_days or Decimal(option_days),
            limit=None if limit is None else limit - yielded_count,
        ):
            yield candidate
            yielded_count += 1
            if limit is not None and yielded_count >= limit:
                return


def _find_auto_candidate(employee, year, placements, target_days, latest_end, *, urgent=False, allow_short_parts=False):
    for candidate in _iter_auto_candidate_payloads_for_target(
        employee,
        year,
        placements,
        target_days,
        latest_end,
        urgent=urgent,
        allow_short_parts=allow_short_parts,
        limit=1,
    ):
        return candidate

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
    for candidate in _iter_auto_candidate_payloads_for_need(
        employee,
        year,
        placements,
        target_days,
        latest_end,
        urgent=urgent,
        allow_short_parts=allow_short_parts,
        limit=1,
    ):
        return candidate
    return None


def _iter_adjacent_topup_candidate_payloads(
    employee,
    year,
    placements,
    current_items,
    target_days,
    latest_end,
    *,
    max_chargeable_days=None,
    limit=None,
):
    target_days = _decimal_to_whole_days(target_days)
    if target_days <= 0:
        return

    planning_start, planning_end = _planning_year_bounds(year)
    latest_end = min(latest_end or planning_end, planning_end)
    earliest_start = max(planning_start, get_paid_leave_available_from(employee))
    max_chargeable_days = max_chargeable_days or Decimal(target_days)
    yielded_count = 0

    for item in sorted(current_items or [], key=lambda candidate: (candidate.end_date, candidate.start_date, candidate.id or 0)):
        adjacent_periods = []

        after_start = item.end_date + timedelta(days=1)
        after_end = _end_date_for_chargeable_days(after_start, target_days, latest_end)
        if after_end is not None:
            adjacent_periods.append((after_start, after_end, {item.id}))

        before_end = min(item.start_date - timedelta(days=1), latest_end)
        before_start = _start_date_for_chargeable_days(before_end, target_days, earliest_start)
        if before_start is not None:
            adjacent_periods.append((before_start, before_end, {item.id}))

        for start_date, end_date, adjacent_ids in adjacent_periods:
            merged_start = min(start_date, item.start_date)
            merged_end = max(end_date, item.end_date)
            if (merged_end - merged_start).days + 1 < MIN_CONTINUOUS_PAID_LEAVE_DAYS:
                continue
            if _has_short_gap_to_employee_placement(
                placements,
                employee.id,
                start_date,
                end_date,
                exclude_item_ids=adjacent_ids,
            ):
                continue
            assessment = assess_schedule_draft_candidate(
                employee,
                start_date,
                end_date,
                year,
                placements,
                max_chargeable_days=max_chargeable_days,
            )
            if assessment["can_place"]:
                yield {
                    "start_date": start_date,
                    "end_date": end_date,
                    "assessment": assessment,
                    "target_days": Decimal(target_days),
                }
                yielded_count += 1
                if limit is not None and yielded_count >= limit:
                    return


def _find_adjacent_topup_candidate(employee, year, placements, current_items, target_days, latest_end, *, max_chargeable_days=None):
    for candidate in _iter_adjacent_topup_candidate_payloads(
        employee,
        year,
        placements,
        current_items,
        target_days,
        latest_end,
        max_chargeable_days=max_chargeable_days,
        limit=1,
    ):
        return candidate
    return None


def _auto_candidate_payload(candidate, comment):
    if candidate is None:
        return None
    return {
        "candidate": candidate,
        "comment": comment,
    }


def _find_auto_candidate_for_planning_need(employee, year, placements, current_items, planning_need):
    if not planning_need["needs_manual_attention"]:
        return None

    _, planning_end = _planning_year_bounds(year)
    open_required_days = planning_need["open_required_days"]
    if open_required_days <= 0:
        return None

    if planning_need["has_blocker"]:
        previous_year_closure = detect_previous_year_closure_need(employee, year, planning_need)
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
            candidate = _find_auto_candidate_for_need(
                employee,
                year,
                placements,
                target_before_deadline,
                planning_need["nearest_deadline"],
                urgent=True,
                allow_short_parts=False,
            )
            if candidate is not None:
                return _auto_candidate_payload(
                    candidate,
                    "Автоматически распределено: срочный остаток отпуска до срока.",
                )

            candidate = _find_adjacent_topup_candidate(
                employee,
                year,
                placements,
                current_items,
                current_year_blocking_days,
                planning_need["nearest_deadline"],
                max_chargeable_days=open_required_days,
            )
            if candidate is not None:
                return _auto_candidate_payload(
                    candidate,
                    "Автоматически продлено: срочный остаток закрыт соседней частью отпуска.",
                )

            candidate = _find_auto_candidate_for_need(
                employee,
                year,
                placements,
                current_year_blocking_days,
                planning_need["nearest_deadline"],
                urgent=True,
                allow_short_parts=True,
            )
            if candidate is not None:
                return _auto_candidate_payload(
                    candidate,
                    "Автоматически распределено: короткий срочный остаток до срока.",
                )
            if planning_need["annual_remaining_days"] > 0:
                if planning_need["annual_remaining_days"] < MIN_CONTINUOUS_PAID_LEAVE_DAYS:
                    candidate = _find_adjacent_topup_candidate(
                        employee,
                        year,
                        placements,
                        current_items,
                        planning_need["annual_remaining_days"],
                        planning_end,
                        max_chargeable_days=open_required_days,
                    )
                    if candidate is not None:
                        return _auto_candidate_payload(
                        candidate,
                        "Автоматически продлено: автодобор выполнен, срочный остаток остался на ручную проверку.",
                    )
                candidate = _find_auto_candidate_for_need(
                    employee,
                    year,
                    placements,
                    planning_need["annual_remaining_days"],
                    planning_end,
                    urgent=False,
                    allow_short_parts=False,
                )
                if candidate is not None:
                    return _auto_candidate_payload(
                    candidate,
                    "Автоматически распределено: автодобор выполнен, срочный остаток остался на ручную проверку.",
                )
            return None

        target_after_previous_year_closure = quantize_leave_days(
            max(open_required_days - previous_year_closure_days, Decimal("0.00"))
        )
        if target_after_previous_year_closure <= 0:
            return None
        candidate = _find_auto_candidate_for_need(
            employee,
            year,
            placements,
            target_after_previous_year_closure,
            planning_end,
            urgent=False,
            allow_short_parts=False,
        )
        return _auto_candidate_payload(
            candidate,
            "Автоматически распределено: автодобор при отдельном срочном остатке.",
        )

    if open_required_days < MIN_CONTINUOUS_PAID_LEAVE_DAYS:
        candidate = _find_adjacent_topup_candidate(
            employee,
            year,
            placements,
            current_items,
            open_required_days,
            planning_end,
            max_chargeable_days=open_required_days,
        )
        if candidate is not None:
            return _auto_candidate_payload(
                candidate,
                "Автоматически продлено: короткий остаток объединен с соседним отпуском.",
            )

    candidate = _find_auto_candidate_for_need(
        employee,
        year,
        placements,
        open_required_days,
        planning_end,
        urgent=False,
        allow_short_parts=False,
    )
    return _auto_candidate_payload(
        candidate,
        "Автоматически распределено: добивка по пожеланию сотрудника.",
    )


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


def _auto_candidate_packages_for_employee(context, employee, *, package_limit=AUTO_DRAFT_MAX_PACKAGE_SUGGESTIONS):
    planning_need = _current_employee_planning_need(context, employee)
    target = _auto_place_target_for_planning_need(employee, context.year, planning_need)
    if target is None:
        return [], None

    packages = []
    seen = set()
    for package in _manual_candidate_packages(
        context,
        employee,
        limit=package_limit,
    ):
        package = _trim_auto_candidate_package_to_target(package, target["target_days"])
        key = _manual_package_key(package)
        if not key or key in seen:
            continue
        seen.add(key)
        packages.append(package)

    packages = _rank_auto_candidate_packages(packages)
    for package in packages:
        package.metadata.update(
            {
                "package_kind": "auto_place_package",
                "auto_place_package": True,
                "auto_place_target_days": target["target_days"],
            }
        )
        for candidate in package.candidates:
            if candidate.metadata.get("is_preference_candidate"):
                candidate.metadata["auto_place_preference_seed"] = True
            if candidate.comment.startswith("Предложение модуля"):
                candidate.comment = "Автоматически распределено: выбран лучший пакет периодов."
    selected_package = next(
        (
            package
            for package in packages
            if package.candidates and all(_candidate_passed_hard_rules(candidate) for candidate in package.candidates)
        ),
        None,
    )
    return packages, selected_package


def _trim_auto_candidate_package_to_target(package, target_days):
    target_days = _feature_decimal(target_days)
    if target_days <= 0:
        return package

    selected_candidates = []
    total_days = Decimal("0.00")
    for candidate in package.candidates:
        selected_candidates.append(candidate)
        total_days += _feature_decimal(
            candidate.metadata.get("chargeable_days")
            or (candidate.assessment or {}).get("chargeable_days")
        )
        if total_days >= target_days:
            break

    remaining_days = quantize_leave_days(max(target_days - total_days, Decimal("0.00")))
    if len(selected_candidates) == len(package.candidates):
        package.metadata.update(
            {
                "auto_place_target_days": target_days,
                "remaining_after_package": remaining_days,
                "package_closes_need": remaining_days <= 0,
            }
        )
        return package

    return DraftGenerationCandidatePackage(
        employee=package.employee,
        candidates=selected_candidates,
        source=package.source,
        explanation=(
            "Пакет полностью закрывает автоматическую цель."
            if remaining_days <= 0
            else "Пакет закрывает часть автоматической цели."
        ),
        metadata={
            **(package.metadata or {}),
            "total_chargeable_days": total_days,
            "periods_count": len(selected_candidates),
            "remaining_after_package": remaining_days,
            "package_closes_need": remaining_days <= 0,
            "auto_place_target_days": target_days,
        },
    )


def _rank_auto_candidate_packages(packages):
    def package_rank(package):
        candidates = package.candidates
        total_days = sum((_feature_decimal(candidate.metadata.get("chargeable_days")) for candidate in candidates), Decimal("0.00"))
        max_risk = max((int(candidate.metadata.get("risk_score") or 0) for candidate in candidates), default=0)
        closes_need = bool(package.metadata.get("package_closes_need"))
        extends_existing = any(candidate.metadata.get("extends_existing_item") for candidate in candidates)
        target_days = _feature_decimal(package.metadata.get("auto_place_target_days"))
        target_is_short = Decimal("0.00") < target_days < Decimal(MIN_CONTINUOUS_PAID_LEAVE_DAYS)
        has_partial_preference = any(str(candidate.metadata.get("preference_match") or "").endswith("_partial") for candidate in candidates)
        has_exact_preference = any(
            candidate.metadata.get("preference_match") in {"primary", "backup"}
            for candidate in candidates
        )
        single_period = len(candidates) == 1
        return (
            1 if closes_need else 0,
            1 if extends_existing else 0,
            1 if has_exact_preference else 0,
            1 if single_period and not (target_is_short and has_partial_preference) else 0,
            _manual_package_quality_score(package),
            total_days,
            Decimal("100.00") - Decimal(max_risk),
            -len(candidates),
        )

    return sorted(packages, key=package_rank, reverse=True)


def _register_virtual_candidate_package(context, package):
    for candidate in package.candidates:
        chargeable_days = (
            candidate.metadata.get("chargeable_days")
            or (candidate.assessment or {}).get("chargeable_days")
            or 0
        )
        virtual_item = _virtual_draft_item(
            candidate.employee,
            candidate.start_date,
            candidate.end_date,
            chargeable_days,
            source=candidate.source,
            selected_candidate=candidate,
        )
        _register_draft_item_in_generation_context(context, virtual_item)
    context.planning_need_by_employee[package.employee.id] = _current_employee_planning_need(context, package.employee)


def _persist_auto_candidate_packages(generation_run, schedule, packages, selected_package):
    selected_key = _manual_package_key(selected_package)
    selected_record, selected_candidate_records, selected_period_records = _persist_manual_candidate_package(
        generation_run,
        schedule,
        selected_package,
        decision=VacationScheduleCandidatePackage.DECISION_SELECTED,
        decision_rank=1,
    )

    rejected_rank = 2
    for package in packages[:AUTO_DRAFT_PERSISTED_PACKAGE_ALTERNATIVES]:
        if _manual_package_key(package) == selected_key:
            continue
        _persist_manual_candidate_package(
            generation_run,
            schedule,
            package,
            decision=VacationScheduleCandidatePackage.DECISION_REJECTED,
            decision_rank=rejected_rank,
        )
        rejected_rank += 1

    return selected_record, selected_candidate_records, selected_period_records


def _link_package_periods_to_draft_items(period_records, items):
    for period_record in period_records:
        schedule_item = _find_covering_draft_item(items, period_record.start_date, period_record.end_date)
        if schedule_item is None:
            continue
        period_record.schedule_item = schedule_item
        period_record.save(update_fields=["schedule_item"])


def _create_draft_items_from_candidate_package(
    schedule,
    context,
    employee,
    package,
    *,
    generation_run,
    selected_candidate_records,
    selected_period_records,
):
    created_items = []
    for candidate, selected_candidate_record in zip(package.candidates, selected_candidate_records):
        if not _candidate_passed_hard_rules(candidate):
            continue
        item = _create_draft_item_from_generation_candidate(
            schedule,
            candidate,
            generation_run=generation_run,
            selected_candidate_record=selected_candidate_record,
        )
        _register_draft_item_in_generation_context(context, item)
        created_items.append(item)

    _merge_adjacent_employee_draft_items(
        schedule,
        employee,
        context.draft_items_by_employee,
        context.placements,
    )
    refreshed_items = list(
        VacationScheduleItem.objects.filter(
            schedule=schedule,
            employee=employee,
            status=VacationScheduleItem.STATUS_DRAFT,
        ).order_by("start_date", "end_date", "id")
    )
    _link_package_periods_to_draft_items(selected_period_records, refreshed_items)
    context.planning_need_by_employee[employee.id] = _current_employee_planning_need(context, employee)
    return created_items


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
            "comment": "Автоматически распределено: автодобор при отдельном срочном остатке.",
        }

    return {
        "target_days": planning_need["open_required_days"],
        "latest_end": planning_end,
        "urgent": False,
        "allow_short_parts": False,
        "comment": "Автоматически распределено: добивка по пожеланию сотрудника.",
    }


def _count_manual_draft_tasks_from_context(context):
    employee_ids = [employee.id for employee in context.eligible_employees]
    active_urgent_closure_by_employee = get_active_urgent_closure_payload_map(employee_ids, context.year)
    manual_count = 0
    for employee in context.eligible_employees:
        planning_need = _current_employee_planning_need(context, employee)
        context.planning_need_by_employee[employee.id] = planning_need
        if planning_need["needs_manual_attention"] or active_urgent_closure_by_employee.get(employee.id) is not None:
            manual_count += 1
    return manual_count


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
    context = _build_draft_generation_context(year, schedule)
    generation_run = _start_schedule_generation_run(schedule, actor)
    placed_count = 0

    for employee in context.eligible_employees:
        if context.preference_state_by_employee.get(employee.id) != VacationPreference.STATUS_FILLED:
            continue

        candidates = _rank_generation_candidates(_build_preference_generation_candidates(context, employee))
        selected_candidate = _select_preference_generation_candidate_from_ranked(candidates)
        candidates = _selected_candidate_first(candidates, selected_candidate)
        selected_candidate_record = _persist_generation_candidates(
            generation_run,
            schedule,
            candidates,
            selected_candidate=selected_candidate,
        )
        if selected_candidate is None:
            continue

        item = _create_draft_item_from_generation_candidate(
            schedule,
            selected_candidate,
            generation_run=generation_run,
            selected_candidate_record=selected_candidate_record,
        )
        _register_draft_item_in_generation_context(context, item)
        placed_count += 1

    removed_conflicts = _remove_conflicting_generated_draft_items(schedule)
    if removed_conflicts:
        context = _build_draft_generation_context(year, schedule)
    manual_count = _count_manual_draft_tasks_from_context(context)
    _finish_schedule_generation_run(generation_run, manual_count=manual_count)

    return {
        "schedule": schedule,
        "created": True,
        "placed_count": max(placed_count - removed_conflicts, 0),
    }


@transaction.atomic
def auto_place_remaining_schedule_draft(
    *,
    year,
    actor,
    _pass_index=1,
    _generation_run=None,
    progress_callback=None,
    use_package_selection=False,
):
    schedule = VacationSchedule.objects.select_for_update().filter(
        year=year,
        status=VacationSchedule.STATUS_DRAFT,
    ).first()
    if schedule is None:
        raise ValidationError("Черновик графика за этот год не найден.")

    context = _build_draft_generation_context(year, schedule)
    owns_generation_run = _generation_run is None
    generation_run = _generation_run or _start_schedule_generation_run(schedule, actor)
    placed_count = 0
    unresolved_count = 0
    auto_employees = _sort_auto_place_employees(context.eligible_employees, context.planning_need_by_employee)
    total_employees = len(auto_employees)

    for processed_index, employee in enumerate(auto_employees, start=1):
        chunks_count = 0
        while chunks_count < AUTO_DRAFT_MAX_CHUNKS_PER_EMPLOYEE:
            current_items = context.draft_items_by_employee.get(employee.id, [])
            planning_need = _current_employee_planning_need(context, employee)
            if not planning_need["needs_manual_attention"]:
                context.planning_need_by_employee[employee.id] = planning_need
                break

            if use_package_selection:
                packages, selected_package = _auto_candidate_packages_for_employee(context, employee)
                if selected_package is None:
                    unresolved_count += 1
                    context.planning_need_by_employee[employee.id] = planning_need
                    break

                _, selected_candidate_records, selected_period_records = _persist_auto_candidate_packages(
                    generation_run,
                    schedule,
                    packages,
                    selected_package,
                )
                created_items = _create_draft_items_from_candidate_package(
                    schedule,
                    context,
                    employee,
                    selected_package,
                    generation_run=generation_run,
                    selected_candidate_records=selected_candidate_records,
                    selected_period_records=selected_period_records,
                )
                placed_periods = len(created_items)
                if placed_periods <= 0:
                    unresolved_count += 1
                    break
                placed_count += placed_periods
                chunks_count += placed_periods
            else:
                candidates = _rank_auto_generation_candidates(
                    _build_auto_generation_candidates(
                        context,
                        employee,
                        current_items,
                        planning_need,
                    )
                )
                selected_candidate = _select_first_passed_generation_candidate(candidates)
                selected_candidate_record = _persist_generation_candidates(
                    generation_run,
                    schedule,
                    candidates,
                    selected_candidate=selected_candidate,
                )
                if selected_candidate is None:
                    unresolved_count += 1
                    context.planning_need_by_employee[employee.id] = planning_need
                    break

                item = _create_draft_item_from_generation_candidate(
                    schedule,
                    selected_candidate,
                    generation_run=generation_run,
                    selected_candidate_record=selected_candidate_record,
                )
                _register_draft_item_in_generation_context(context, item)
                _merge_adjacent_employee_draft_items(
                    schedule,
                    employee,
                    context.draft_items_by_employee,
                    context.placements,
                )
                placed_count += 1
                chunks_count += 1

        if chunks_count >= AUTO_DRAFT_MAX_CHUNKS_PER_EMPLOYEE:
            planning_need = _current_employee_planning_need(context, employee)
            if planning_need["needs_manual_attention"]:
                unresolved_count += 1

        if progress_callback is not None:
            progress_callback(
                {
                    "processed": processed_index,
                    "total": total_employees,
                    "employee_id": employee.id,
                    "employee_name": employee.full_name,
                    "placed_count": placed_count,
                    "unresolved_count": unresolved_count,
                }
            )

    removed_conflicts = _remove_conflicting_generated_draft_items(schedule)
    placed_count = max(placed_count - removed_conflicts, 0)
    unresolved_count = build_schedule_draft_page_context(year)["draft_summary"]["manual"]
    if placed_count > 0 and unresolved_count > 0 and _pass_index < AUTO_DRAFT_MAX_AUTO_PLACE_PASSES:
        follow_up_result = auto_place_remaining_schedule_draft(
            year=year,
            actor=actor,
            _pass_index=_pass_index + 1,
            _generation_run=generation_run,
            progress_callback=progress_callback,
            use_package_selection=use_package_selection,
        )
        placed_count += follow_up_result["placed_count"]
        unresolved_count = follow_up_result["unresolved_count"]

    if owns_generation_run:
        _finish_schedule_generation_run(generation_run, manual_count=unresolved_count)
        _invalidate_schedule_draft_manual_suggestion_cache(schedule)

    return {
        "schedule": schedule,
        "placed_count": placed_count,
        "unresolved_count": unresolved_count,
    }


def _manual_package_periods_from_input(periods, year):
    normalized = []
    if not isinstance(periods, (list, tuple)) or not periods:
        raise ValidationError("Добавьте хотя бы один период отпуска.")
    if len(periods) > MANUAL_DRAFT_MAX_PACKAGE_PERIODS:
        raise ValidationError(f"За один раз можно поставить не больше {MANUAL_DRAFT_MAX_PACKAGE_PERIODS} периодов.")

    planning_start, planning_end = _planning_year_bounds(year)
    for index, period in enumerate(periods, start=1):
        start_date = period.get("start_date") if isinstance(period, dict) else None
        end_date = period.get("end_date") if isinstance(period, dict) else None
        if not start_date or not end_date:
            raise ValidationError(f"Заполните дату начала и окончания в периоде {index}.")
        if end_date < start_date:
            raise ValidationError(f"В периоде {index} дата окончания не может быть раньше даты начала.")
        if start_date < planning_start or end_date > planning_end:
            raise ValidationError(f"Период {index} должен быть внутри {year} года.")
        normalized.append(
            {
                "order": index,
                "start_date": start_date,
                "end_date": end_date,
            }
        )

    sorted_periods = sorted(normalized, key=lambda item: (item["start_date"], item["end_date"]))
    for previous, current in zip(sorted_periods, sorted_periods[1:]):
        if _periods_overlap(previous["start_date"], previous["end_date"], current["start_date"], current["end_date"]):
            raise ValidationError("Периоды внутри одного размещения не должны пересекаться.")
    return normalized


def _manual_draft_employee_context(year, employee_id, *, for_update=False):
    schedule_queryset = VacationSchedule.objects
    if for_update:
        schedule_queryset = schedule_queryset.select_for_update()
    schedule = schedule_queryset.filter(year=year, status=VacationSchedule.STATUS_DRAFT).first()
    if schedule is None:
        raise ValidationError("Черновик графика за этот год не найден.")

    employee = next(
        (candidate for candidate in get_eligible_preference_employees(year) if candidate.id == employee_id),
        None,
    )
    if employee is None:
        raise ValidationError("Сотрудник не участвует в планировании графика за этот год.")

    draft_items = _draft_items_for_schedule(schedule)
    draft_items_by_employee = _draft_items_by_employee(draft_items)
    planning_need = build_employee_schedule_planning_need(
        employee,
        year,
        draft_items_by_employee.get(employee.id, []),
        preference_pair=get_employee_preference_pair(employee, year),
        preference_state=get_employee_preference_state(employee, year),
    )
    return schedule, employee, draft_items, draft_items_by_employee, planning_need


def _risk_level_rank(risk_level):
    return RISK_LEVEL_FEATURE_WEIGHT.get(risk_level or VacationRequest.RISK_LOW, 1)


def _manual_period_preview_payload(period, assessment, remaining_after):
    risk_payload = assessment.get("risk_payload") or {
        "risk_score": 0,
        "risk_level": VacationRequest.RISK_LOW,
        "risk_explanation": {},
    }
    risk_explanation = risk_payload.get("risk_explanation") or {}
    start_date = period["start_date"]
    end_date = period["end_date"]
    chargeable_days = assessment.get("chargeable_days")
    if chargeable_days is None and end_date >= start_date:
        chargeable_days = get_chargeable_leave_days(start_date, end_date, "paid")
    chargeable_days = quantize_leave_days(chargeable_days or Decimal("0.00"))
    return {
        "order": period["order"],
        "start_date": start_date,
        "end_date": end_date,
        "start_date_iso": start_date.isoformat(),
        "end_date_iso": end_date.isoformat(),
        "period_label": _short_period_label(start_date, end_date),
        "full_period_label": _period_label(start_date, end_date),
        "calendar_days": _calendar_days(start_date, end_date),
        "chargeable_days": chargeable_days,
        "chargeable_days_label": _days_label(chargeable_days),
        "can_place": bool(assessment.get("can_place")),
        "message": _candidate_assessment_reason(assessment).get("text", ""),
        "risk_label": dict(VacationScheduleItem.RISK_CHOICES).get(risk_payload.get("risk_level"), "Низкий"),
        "risk_score": int(risk_payload.get("risk_score") or 0),
        "risk_level": risk_payload.get("risk_level") or VacationRequest.RISK_LOW,
        "risk_tone": _risk_tone(risk_payload.get("risk_level"), bool(risk_explanation.get("is_conflict"))),
        "risk_short_reason": risk_explanation.get("short_reason", ""),
        "risk_recommended_action": risk_explanation.get("recommended_action", ""),
        "risk_is_conflict": bool(risk_explanation.get("is_conflict")),
        "remaining_after_period": remaining_after,
        "assessment": assessment,
    }


def _manual_package_preview_message(can_submit, period_payloads, planning_need, post_planning_need):
    if not period_payloads:
        return "Добавьте хотя бы один период отпуска."
    first_failed = next((period for period in period_payloads if not period["can_place"]), None)
    if first_failed:
        return first_failed["message"] or "Один из периодов нельзя поставить в черновик."
    if planning_need["has_blocker"] and post_planning_need["blocking_days"] > 0:
        return _deadline_not_closed_message(planning_need)
    if post_planning_need["open_required_days"] <= 0:
        return "Периоды полностью закрывают плановую потребность."
    if can_submit:
        return "Периоды можно поставить в черновик. Часть дней останется на ручное распределение."
    return "Проверьте выбранные периоды."


def _merged_paid_leave_segments(items):
    periods = sorted(
        [
            (item.start_date, item.end_date)
            for item in items
            if getattr(item, "vacation_type", "paid") == "paid"
            and getattr(item, "status", VacationScheduleItem.STATUS_DRAFT) != VacationScheduleItem.STATUS_CANCELLED
            and item.start_date
            and item.end_date
        ],
        key=lambda period: (period[0], period[1]),
    )
    if not periods:
        return []

    segments = []
    current_start, current_end = periods[0]
    for start_date, end_date in periods[1:]:
        if start_date <= current_end + timedelta(days=1):
            current_end = max(current_end, end_date)
            continue
        segments.append((current_start, current_end))
        current_start, current_end = start_date, end_date
    segments.append((current_start, current_end))
    return segments


def _has_required_continuous_paid_leave_part(items):
    return any(
        get_chargeable_leave_days(start_date, end_date, "paid") >= MIN_CONTINUOUS_PAID_LEAVE_DAYS
        for start_date, end_date in _merged_paid_leave_segments(items)
    )


def _requires_required_continuous_paid_leave_part(planning_need):
    return _feature_decimal(planning_need.get("target_days")) >= Decimal(MIN_CONTINUOUS_PAID_LEAVE_DAYS)


def _required_continuous_paid_leave_message():
    return (
        "После размещения в графике должна быть хотя бы одна часть оплачиваемого отпуска "
        f"не меньше {MIN_CONTINUOUS_PAID_LEAVE_DAYS} дней."
    )


def build_manual_schedule_draft_package_preview(*, year, employee_id, periods):
    normalized_periods = _manual_package_periods_from_input(periods, year)
    schedule, employee, draft_items, draft_items_by_employee, planning_need = _manual_draft_employee_context(year, employee_id)
    if not planning_need["needs_manual_attention"]:
        return {
            "can_submit": False,
            "message": "По сотруднику уже закрыта плановая потребность.",
            "periods": [],
            "calendar_days": 0,
            "chargeable_days": Decimal("0.00"),
            "remaining_after_placement": planning_need["open_required_days"],
            "risk_label": "Низкий",
            "risk_score": 0,
            "risk_level": VacationRequest.RISK_LOW,
            "risk_tone": "low",
            "risk_short_reason": "",
            "risk_recommended_action": "",
            "risk_is_conflict": False,
            "planning_need": planning_need,
            "post_planning_need": planning_need,
        }

    placements = _current_placements_from_items(draft_items)
    simulated_items = list(draft_items_by_employee.get(employee.id, []))
    remaining_days = planning_need["open_required_days"]
    period_payloads = []
    total_chargeable_days = Decimal("0.00")
    total_calendar_days = 0
    can_submit = True

    for period in normalized_periods:
        assessment = assess_schedule_draft_candidate(
            employee,
            period["start_date"],
            period["end_date"],
            year,
            placements,
            max_chargeable_days=remaining_days,
        )
        chargeable_days = quantize_leave_days(assessment.get("chargeable_days") or Decimal("0.00"))
        if assessment.get("can_place"):
            total_chargeable_days += chargeable_days
            total_calendar_days += _calendar_days(period["start_date"], period["end_date"])
            simulated_items.append(_virtual_draft_item(employee, period["start_date"], period["end_date"], chargeable_days))
            placements.append(DraftPlacement(employee.id, period["start_date"], period["end_date"], None))
            post_planning_need = build_employee_schedule_planning_need(
                employee,
                year,
                simulated_items,
                preference_pair=get_employee_preference_pair(employee, year),
                preference_state=get_employee_preference_state(employee, year),
            )
            remaining_days = post_planning_need["open_required_days"]
        else:
            can_submit = False
            post_planning_need = build_employee_schedule_planning_need(
                employee,
                year,
                simulated_items,
                preference_pair=get_employee_preference_pair(employee, year),
                preference_state=get_employee_preference_state(employee, year),
            )
        period_payloads.append(_manual_period_preview_payload(period, assessment, remaining_days))
        if not assessment.get("can_place"):
            break

    post_planning_need = build_employee_schedule_planning_need(
        employee,
        year,
        simulated_items,
        preference_pair=get_employee_preference_pair(employee, year),
        preference_state=get_employee_preference_state(employee, year),
    )
    if planning_need["has_blocker"] and post_planning_need["blocking_days"] > 0:
        can_submit = False
    continuous_part_missing = (
        _requires_required_continuous_paid_leave_part(planning_need)
        and not _has_required_continuous_paid_leave_part(simulated_items)
    )
    if can_submit and continuous_part_missing:
        can_submit = False
    show_continuous_part_message = (
        continuous_part_missing
        and all(period["can_place"] for period in period_payloads)
        and not (planning_need["has_blocker"] and post_planning_need["blocking_days"] > 0)
    )

    highest_risk = max(
        period_payloads,
        key=lambda period: (_risk_level_rank(period["risk_level"]), period["risk_score"]),
        default=None,
    )
    risk_level = highest_risk["risk_level"] if highest_risk else VacationRequest.RISK_LOW
    risk_score = highest_risk["risk_score"] if highest_risk else 0
    risk_is_conflict = any(period["risk_is_conflict"] for period in period_payloads)
    risk_short_reason = next((period["risk_short_reason"] for period in period_payloads if period["risk_short_reason"]), "")
    risk_recommended_action = next(
        (period["risk_recommended_action"] for period in period_payloads if period["risk_recommended_action"]),
        "",
    )

    return {
        "can_submit": can_submit,
        "message": (
            _required_continuous_paid_leave_message()
            if show_continuous_part_message
            else _manual_package_preview_message(can_submit, period_payloads, planning_need, post_planning_need)
        ),
        "periods": period_payloads,
        "calendar_days": total_calendar_days,
        "chargeable_days": total_chargeable_days,
        "remaining_after_placement": post_planning_need["open_required_days"],
        "target_days": planning_need["target_days"],
        "placed_days": planning_need["placed_days"],
        "open_required_days": planning_need["open_required_days"],
        "blocking_after_placement": post_planning_need["blocking_days"],
        "annual_remaining_after_placement": post_planning_need["annual_remaining_days"],
        "risk_label": dict(VacationScheduleItem.RISK_CHOICES).get(risk_level, "Низкий"),
        "risk_score": risk_score,
        "risk_level": risk_level,
        "risk_tone": _risk_tone(risk_level, risk_is_conflict),
        "risk_short_reason": risk_short_reason,
        "risk_recommended_action": risk_recommended_action,
        "risk_is_conflict": risk_is_conflict,
        "planning_need": planning_need,
        "post_planning_need": post_planning_need,
    }


def _manual_candidate_from_period_preview(employee, period_payload, planning_need, year, *, actor=None, order=1):
    comment = f"Вручную размещено HR: {actor.full_name if actor else 'HR'}."
    candidate = DraftGenerationCandidate(
        employee=employee,
        start_date=period_payload["start_date"],
        end_date=period_payload["end_date"],
        kind=VacationScheduleCandidate.KIND_MANUAL,
        source=VacationScheduleItem.SOURCE_MANUAL,
        comment=comment,
        metadata={
            "manual_package_selected": True,
            "manual_package_period_order": order,
            "target_chargeable_days": period_payload["chargeable_days"],
        },
    )
    _apply_planning_need_metadata(candidate, planning_need, year)
    _apply_hard_rule_assessment(candidate, period_payload["assessment"])
    return _apply_candidate_scoring(candidate)


def _package_risk_payload(candidates):
    highest = max(
        candidates,
        key=lambda candidate: (_risk_level_rank(candidate.metadata.get("risk_level")), int(candidate.metadata.get("risk_score") or 0)),
        default=None,
    )
    if highest is None:
        return VacationScheduleItem.RISK_LOW, 0
    return highest.metadata.get("risk_level") or VacationScheduleItem.RISK_LOW, int(highest.metadata.get("risk_score") or 0)


def _package_total_days(candidates):
    return sum((_feature_decimal(candidate.metadata.get("chargeable_days")) for candidate in candidates), Decimal("0.00"))


def _package_average(candidates, key):
    if not candidates:
        return None
    return sum((_candidate_scoring_decimal(candidate, key) for candidate in candidates), Decimal("0.00")) / Decimal(len(candidates))


def _percent_decimal(value):
    return max(Decimal("0.00"), min(Decimal("100.00"), Decimal(str(value)))).quantize(Decimal("0.01"))


def _manual_package_quality_score(package):
    candidates = [_apply_candidate_scoring(candidate) for candidate in package.candidates]
    if not candidates:
        return Decimal("0.00")

    scores = [_candidate_scoring_decimal(candidate, "scoring_score") for candidate in candidates]
    average_score = sum(scores, Decimal("0.00")) / Decimal(len(scores))
    weakest_score = min(scores)
    total_days = _package_total_days(candidates)
    open_required_days = _feature_decimal(
        package.metadata.get("auto_place_target_days") or candidates[0].metadata.get("open_required_days")
    )
    remaining_days = _feature_decimal(package.metadata.get("remaining_after_package"))
    period_count = len(candidates)
    short_periods = sum(
        1
        for candidate in candidates
        if Decimal("0.00") < _feature_decimal(candidate.metadata.get("chargeable_days")) < Decimal(MIN_CONTINUOUS_PAID_LEAVE_DAYS)
    )
    max_risk = max((int(candidate.metadata.get("risk_score") or 0) for candidate in candidates), default=0)

    score = (average_score * Decimal("0.72")) + (weakest_score * Decimal("0.28"))
    if open_required_days > 0:
        coverage_gap = abs(total_days - open_required_days) / open_required_days
        score += max(Decimal("0.00"), Decimal("1.00") - coverage_gap) * Decimal("2.50")
        if remaining_days > 0:
            score -= min(Decimal("18.00"), (remaining_days / open_required_days) * Decimal("18.00"))

    score -= Decimal(max(period_count - 1, 0)) * Decimal("1.35")
    score -= Decimal(short_periods) * Decimal("0.65")
    score -= Decimal(max_risk) * Decimal("0.015")
    if any(candidate.metadata.get("extends_existing_item") for candidate in candidates):
        score += Decimal("2.50")
    if any(candidate.metadata.get("is_preference_candidate") for candidate in candidates):
        score += Decimal("2.00")

    package.metadata["package_quality_score"] = _percent_decimal(score)
    return package.metadata["package_quality_score"]


def _package_features(package):
    candidates = package.candidates
    return _json_safe_generation_value(
        {
            **(package.metadata or {}),
            "periods_count": len(candidates),
            "periods": [
                {
                    "start_date": candidate.start_date,
                    "end_date": candidate.end_date,
                    "chargeable_days": candidate.metadata.get("chargeable_days"),
                    "score": candidate.metadata.get("scoring_score"),
                    "confidence": candidate.metadata.get("scoring_confidence"),
                    "risk_score": candidate.metadata.get("risk_score"),
                    "risk_level": candidate.metadata.get("risk_level"),
                    "passed_hard_rules": _candidate_passed_hard_rules(candidate),
                }
                for candidate in candidates
            ],
        }
    )


def _persist_manual_candidate_package(generation_run, schedule, package, *, decision, decision_rank):
    candidates = [_apply_candidate_scoring(candidate) for candidate in package.candidates]
    package_score = _manual_package_quality_score(package)
    selected_at = timezone.now() if decision == VacationScheduleCandidatePackage.DECISION_SELECTED else None
    candidate_records = []
    blocked_reason = ""
    blocked_key = ""

    for order, candidate in enumerate(candidates, start=1):
        passed = _candidate_passed_hard_rules(candidate)
        if not passed:
            candidate_decision = VacationScheduleCandidate.DECISION_BLOCKED
            reason = _candidate_assessment_reason(candidate.assessment or {})
            blocked_key = blocked_key or reason.get("kind", "")
            blocked_reason = blocked_reason or reason.get("text", "")
        elif decision == VacationScheduleCandidatePackage.DECISION_SELECTED:
            candidate_decision = VacationScheduleCandidate.DECISION_SELECTED
        else:
            candidate_decision = VacationScheduleCandidate.DECISION_REJECTED

        stored_candidate = VacationScheduleCandidate.objects.create(
            generation_run=generation_run,
            schedule=schedule,
            employee=candidate.employee,
            start_date=candidate.start_date,
            end_date=candidate.end_date,
            vacation_type="paid",
            chargeable_days=int(candidate.metadata.get("chargeable_days") or 0),
            kind=candidate.kind,
            source=candidate.source,
            passed_hard_rules=passed,
            block_reason_key=(candidate.metadata.get("block_reason_key") or blocked_key or "")[:80],
            block_reason=candidate.metadata.get("block_reason") or blocked_reason,
            risk_score=int(candidate.metadata.get("risk_score") or 0),
            risk_level=candidate.metadata.get("risk_level") or VacationScheduleItem.RISK_LOW,
            features=_generation_candidate_features(candidate),
            score=_candidate_scoring_decimal(candidate, "scoring_score"),
            confidence=_candidate_scoring_decimal(candidate, "scoring_confidence"),
            model_version=candidate.metadata.get("scoring_model_version") or DRAFT_GENERATION_HYBRID_MODEL_VERSION,
            explanation=candidate.metadata.get("scoring_explanation") or _candidate_explanation(candidate, candidate_decision),
            decision=candidate_decision,
            decision_rank=(decision_rank * 10) + order,
            selected_at=selected_at if candidate_decision == VacationScheduleCandidate.DECISION_SELECTED else None,
        )
        candidate.stored_candidate = stored_candidate
        candidate_records.append(stored_candidate)

    passed_package = bool(candidates) and all(_candidate_passed_hard_rules(candidate) for candidate in candidates)
    if not passed_package:
        decision = VacationScheduleCandidatePackage.DECISION_BLOCKED
    risk_level, risk_score = _package_risk_payload(candidates)
    package_record = VacationScheduleCandidatePackage.objects.create(
        generation_run=generation_run,
        schedule=schedule,
        employee=package.employee,
        periods_count=len(candidates),
        total_chargeable_days=int(_package_total_days(candidates)),
        source=package.source,
        passed_hard_rules=passed_package,
        block_reason_key=(blocked_key or "")[:80],
        block_reason=blocked_reason,
        risk_score=risk_score,
        risk_level=risk_level,
        features=_package_features(package),
        score=package_score,
        confidence=_package_average(candidates, "scoring_confidence"),
        model_version=DRAFT_GENERATION_HYBRID_MODEL_VERSION,
        explanation=package.explanation,
        decision=decision,
        decision_rank=decision_rank,
        selected_at=selected_at if decision == VacationScheduleCandidatePackage.DECISION_SELECTED else None,
    )
    period_records = []
    for order, (candidate, candidate_record) in enumerate(zip(candidates, candidate_records), start=1):
        period_records.append(
            VacationScheduleCandidatePackagePeriod.objects.create(
                candidate_package=package_record,
                candidate=candidate_record,
                start_date=candidate.start_date,
                end_date=candidate.end_date,
                chargeable_days=int(candidate.metadata.get("chargeable_days") or 0),
                passed_hard_rules=_candidate_passed_hard_rules(candidate),
                block_reason_key=(candidate.metadata.get("block_reason_key") or "")[:80],
                block_reason=candidate.metadata.get("block_reason") or "",
                risk_score=int(candidate.metadata.get("risk_score") or 0),
                risk_level=candidate.metadata.get("risk_level") or VacationScheduleItem.RISK_LOW,
                features=_generation_candidate_features(candidate),
                order=order,
            )
        )
    package.stored_package = package_record
    return package_record, candidate_records, period_records


def _find_covering_draft_item(items, start_date, end_date):
    return next(
        (
            item
            for item in items
            if item.start_date <= start_date and item.end_date >= end_date
        ),
        None,
    )


@transaction.atomic
def place_manual_schedule_draft_items(*, year, employee_id, periods, actor):
    normalized_periods = _manual_package_periods_from_input(periods, year)
    schedule, employee, draft_items, draft_items_by_employee, planning_need = _manual_draft_employee_context(
        year,
        employee_id,
        for_update=True,
    )
    if not planning_need["needs_manual_attention"]:
        raise ValidationError("По сотруднику уже закрыта плановая потребность.")

    preview = build_manual_schedule_draft_package_preview(
        year=year,
        employee_id=employee_id,
        periods=normalized_periods,
    )
    if not preview["can_submit"]:
        raise ValidationError(preview["message"])

    context = _build_draft_generation_context(year, schedule)
    context_employee = next((candidate for candidate in context.eligible_employees if candidate.id == employee.id), employee)
    suggestion_packages = _manual_candidate_packages(
        context,
        context_employee,
        limit=MANUAL_DRAFT_MAX_PACKAGE_SUGGESTIONS,
    )
    selected_candidates = [
        _manual_candidate_from_period_preview(
            employee,
            period_payload,
            planning_need,
            year,
            actor=actor,
            order=index,
        )
        for index, period_payload in enumerate(preview["periods"], start=1)
    ]
    selected_package = DraftGenerationCandidatePackage(
        employee=employee,
        candidates=selected_candidates,
        source=VacationScheduleItem.SOURCE_MANUAL,
        explanation=preview["message"],
        metadata={
            "package_kind": "manual_selected",
            "total_chargeable_days": preview["chargeable_days"],
            "remaining_after_placement": preview["remaining_after_placement"],
        },
    )

    generation_run = _start_schedule_generation_run(schedule, actor)
    _, selected_candidate_records, selected_period_records = _persist_manual_candidate_package(
        generation_run,
        schedule,
        selected_package,
        decision=VacationScheduleCandidatePackage.DECISION_SELECTED,
        decision_rank=1,
    )
    selected_key = _manual_package_key(selected_package)
    rejected_rank = 2
    for package in suggestion_packages:
        if _manual_package_key(package) == selected_key:
            continue
        _persist_manual_candidate_package(
            generation_run,
            schedule,
            package,
            decision=VacationScheduleCandidatePackage.DECISION_REJECTED,
            decision_rank=rejected_rank,
        )
        rejected_rank += 1

    created_items = []
    placements = _current_placements_from_items(draft_items)
    current_items = draft_items_by_employee.setdefault(employee.id, [])
    for period_payload, candidate_record in zip(preview["periods"], selected_candidate_records):
        item = _create_draft_item_from_assessment(
            schedule,
            employee,
            period_payload["start_date"],
            period_payload["end_date"],
            period_payload["assessment"],
            source=VacationScheduleItem.SOURCE_MANUAL,
            comment=f"Вручную размещено HR: {actor.full_name if actor else 'HR'}.",
            generation_run=generation_run,
            selected_candidate_record=candidate_record,
        )
        current_items.append(item)
        placements.append(DraftPlacement(item.employee_id, item.start_date, item.end_date, item.id))
        created_items.append(item)

    _merge_adjacent_employee_draft_items(schedule, employee, draft_items_by_employee, placements)
    refreshed_items = list(
        VacationScheduleItem.objects.filter(
            schedule=schedule,
            employee=employee,
            status=VacationScheduleItem.STATUS_DRAFT,
        ).order_by("start_date", "end_date", "id")
    )
    for period_record in selected_period_records:
        covering_item = _find_covering_draft_item(refreshed_items, period_record.start_date, period_record.end_date)
        if covering_item is not None:
            period_record.schedule_item = covering_item
            period_record.save(update_fields=["schedule_item"])

    unresolved_count = build_schedule_draft_page_context(year)["draft_summary"]["manual"]
    _finish_schedule_generation_run(generation_run, manual_count=unresolved_count)
    _invalidate_schedule_draft_manual_suggestion_cache(schedule)
    return {
        "schedule": schedule,
        "items": created_items,
        "generation_run": generation_run,
    }


@transaction.atomic
def place_manual_schedule_draft_item(*, year, employee_id, start_date, end_date, actor):
    result = place_manual_schedule_draft_items(
        year=year,
        employee_id=employee_id,
        periods=[
            {
                "start_date": start_date,
                "end_date": end_date,
            }
        ],
        actor=actor,
    )
    return {
        "schedule": result["schedule"],
        "item": result["items"][0] if result["items"] else None,
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
    post_planning_need = planning_need

    if can_submit:
        post_planning_need = _planning_need_after_candidate(
            employee,
            year,
            current_items,
            start_date,
            end_date,
            assessment["chargeable_days"],
        )
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
        if planning_need["has_blocker"] and post_planning_need["blocking_days"] > 0:
            can_submit = False
            message = _deadline_not_closed_message(planning_need)
        if can_submit and _requires_required_continuous_paid_leave_part(planning_need):
            simulated_items = [
                *list(current_items or []),
                _virtual_draft_item(employee, start_date, end_date, assessment["chargeable_days"]),
            ]
            if not _has_required_continuous_paid_leave_part(simulated_items):
                can_submit = False
                message = _required_continuous_paid_leave_message()
    else:
        message = assessment["reason"]["text"]
        if risk_payload is None:
            risk_payload = {
                "risk_score": 0,
                "risk_level": VacationRequest.RISK_LOW,
                "balance_after_request": get_employee_available_balance(employee),
            }

    risk_label = dict(VacationRequest.RISK_CHOICES).get(risk_payload["risk_level"], "Низкий")
    remaining_after_placement = post_planning_need["open_required_days"]
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
        "blocking_after_placement": post_planning_need["blocking_days"],
        "annual_remaining_after_placement": post_planning_need["annual_remaining_days"],
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
        "new_hire_badge": identity["employee_new_hire_badge"],
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


def _calendar_employee_url(employee, year, *, focus_start=None, focus_end=None):
    params = urlencode(
        {
            "view": "year",
            "year": year,
            "employee": employee.id,
            "calendar_modal": "employee_detail",
            "calendar_employee": employee.id,
        }
    )
    if focus_start and focus_end:
        params = urlencode(
            {
                "view": "year",
                "year": year,
                "employee": employee.id,
                "calendar_modal": "employee_detail",
                "calendar_employee": employee.id,
                "calendar_focus_employee": employee.id,
                "calendar_focus_start": focus_start.isoformat(),
                "calendar_focus_end": focus_end.isoformat(),
            }
        )
    return f"{reverse('calendar')}?{params}"


def _placement_source_hint(item, pair):
    source_label = _source_label_for_item(item, pair)
    if source_label == "Основное пожелание":
        return "Совпало с основным пожеланием"
    if source_label == "Запасной период":
        return "Совпало с запасным периодом"
    if item.source == VacationScheduleItem.SOURCE_MANUAL:
        return "Поставлено вручную HR"
    if item.selected_candidate and item.selected_candidate.kind in {
        VacationScheduleCandidate.KIND_AUTO,
        VacationScheduleCandidate.KIND_AUTO_URGENT,
        VacationScheduleCandidate.KIND_AUTO_TOPUP,
    }:
        return "Подобрано модулем"
    return "Сформировано системой"


def _draft_items_for_schedule(schedule):
    if schedule is None:
        return []

    return list(
        schedule.items.select_related(
            "employee",
            "employee__department",
            "employee__employee_position",
            "employee__employee_position__production_group",
            "generation_run",
            "selected_candidate",
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


def _percent_label(value):
    if value is None:
        return ""
    value = Decimal(value).quantize(Decimal("0.01"))
    if value == value.to_integral_value():
        return f"{int(value)}%"
    return f"{str(value).replace('.', ',')}%"


def _candidate_recommendation_label(recommendation):
    return {
        "prefer": "Предпочтительный",
        "normal": "Допустимый",
        "avoid": "С осторожностью",
        "blocked": "Заблокирован",
    }.get(recommendation or "", "Оценен")


def _draft_item_ai_context(item):
    if not item.generated_by_ai or item.ai_score is None:
        return None

    selected_candidate = item.selected_candidate
    recommendation = ""
    decision_rank = None
    if selected_candidate is not None:
        recommendation = (selected_candidate.features or {}).get("scoring_recommendation", "")
        decision_rank = selected_candidate.decision_rank

    return {
        "score": item.ai_score,
        "score_label": _percent_label(item.ai_score),
        "confidence": item.ai_confidence,
        "confidence_label": _percent_label(item.ai_confidence),
        "model_version": item.ai_model_version,
        "explanation": item.ai_explanation,
        "recommendation": recommendation,
        "recommendation_label": _candidate_recommendation_label(recommendation),
        "decision_rank": decision_rank,
    }


def _candidate_kind_label(kind):
    return dict(VacationScheduleCandidate.KIND_CHOICES).get(kind, "Кандидат")


def _candidate_decision_label(decision):
    return dict(VacationScheduleCandidate.DECISION_CHOICES).get(decision, "Не выбран")


def _candidate_decision_tone(decision):
    return {
        VacationScheduleCandidate.DECISION_SELECTED: "selected",
        VacationScheduleCandidate.DECISION_REJECTED: "rejected",
        VacationScheduleCandidate.DECISION_BLOCKED: "blocked",
    }.get(decision, "pending")


def _risk_tone(risk_level, is_conflict=False):
    if is_conflict:
        return "conflict"
    if risk_level == VacationRequest.RISK_HIGH:
        return "high"
    if risk_level == VacationRequest.RISK_MEDIUM:
        return "medium"
    return "low"


def _stored_candidate_payload(candidate):
    features = candidate.features or {}
    return {
        "id": candidate.id,
        "period_label": _short_period_label(candidate.start_date, candidate.end_date),
        "full_period_label": _period_label(candidate.start_date, candidate.end_date),
        "start_date": candidate.start_date.isoformat() if candidate.start_date else "",
        "end_date": candidate.end_date.isoformat() if candidate.end_date else "",
        "chargeable_days": candidate.chargeable_days,
        "chargeable_days_label": _days_label(candidate.chargeable_days),
        "kind": candidate.kind,
        "kind_label": _candidate_kind_label(candidate.kind),
        "decision": candidate.decision,
        "decision_label": _candidate_decision_label(candidate.decision),
        "decision_tone": _candidate_decision_tone(candidate.decision),
        "decision_rank": candidate.decision_rank,
        "passed_hard_rules": candidate.passed_hard_rules,
        "hard_rules_label": "Прошел жесткие правила" if candidate.passed_hard_rules else "Заблокирован жесткими правилами",
        "block_reason": candidate.block_reason,
        "risk_score": candidate.risk_score,
        "risk_level": candidate.risk_level,
        "risk_label": dict(VacationScheduleItem.RISK_CHOICES).get(candidate.risk_level, "Низкий"),
        "risk_tone": _risk_tone(candidate.risk_level, bool(features.get("risk_is_conflict"))),
        "score": candidate.score,
        "score_label": _percent_label(candidate.score),
        "confidence": candidate.confidence,
        "confidence_label": _percent_label(candidate.confidence),
        "model_version": candidate.model_version,
        "recommendation_label": _candidate_recommendation_label(features.get("scoring_recommendation")),
        "explanation": candidate.explanation,
        "is_selected": candidate.decision == VacationScheduleCandidate.DECISION_SELECTED,
    }


def build_schedule_draft_item_review_context(item, *, actor=None):
    selected_candidate = item.selected_candidate
    candidate_filters = {
        "schedule": item.schedule,
        "employee": item.employee,
    }
    if selected_candidate and selected_candidate.generation_run_id:
        candidate_filters["generation_run_id"] = selected_candidate.generation_run_id
    elif item.generation_run_id:
        candidate_filters["generation_run_id"] = item.generation_run_id

    candidates = list(
        VacationScheduleCandidate.objects.filter(**candidate_filters)
        .select_related("generation_run")
        .order_by("decision_rank", "-score", "-confidence", "start_date", "id")
    )
    feedback_context = build_schedule_candidate_feedback_context([item], actor=actor).get(
        item.id,
        {
            "summary": {"total": 0, "items": [], "counts": {}},
            "current": None,
            "can_submit": False,
        },
    )
    feedback_context = {
        **feedback_context,
        "action_url": reverse("schedule_draft_candidate_feedback", args=[item.schedule.year, item.id]),
    }
    pair = get_employee_preference_pair(item.employee, item.schedule.year)
    return {
        "item": item,
        "employee": item.employee,
        "employee_name": item.employee.full_name,
        "period_label": _period_label(item.start_date, item.end_date),
        "short_period_label": _short_period_label(item.start_date, item.end_date),
        "chargeable_days_label": _days_label(item.chargeable_days),
        "source_hint": _placement_source_hint(item, pair),
        "risk_label": dict(VacationScheduleItem.RISK_CHOICES).get(item.risk_level, "Низкий"),
        "risk_score": item.risk_score,
        "risk_tone": _risk_tone(item.risk_level),
        "ai_decision": _draft_item_ai_context(item),
        "feedback": feedback_context,
        "candidates": [_stored_candidate_payload(candidate) for candidate in candidates],
        "calendar_url": _calendar_employee_url(
            item.employee,
            item.schedule.year,
            focus_start=item.start_date,
            focus_end=item.end_date,
        ),
    }


def _generation_candidate_payload(candidate, *, rank=None):
    risk_level = candidate.metadata.get("risk_level") or VacationScheduleItem.RISK_LOW
    risk_score = int(candidate.metadata.get("risk_score") or 0)
    is_conflict = bool((candidate.assessment or {}).get("risk_payload", {}).get("risk_explanation", {}).get("is_conflict"))
    preference_match = candidate.metadata.get("preference_match") or ""
    preference_match_label = candidate.metadata.get("preference_match_label") or ""
    passed_hard_rules = _candidate_passed_hard_rules(candidate)
    return {
        "rank": rank,
        "period_label": _short_period_label(candidate.start_date, candidate.end_date),
        "full_period_label": _period_label(candidate.start_date, candidate.end_date),
        "start_date": candidate.start_date.isoformat() if candidate.start_date else "",
        "end_date": candidate.end_date.isoformat() if candidate.end_date else "",
        "chargeable_days": int(candidate.metadata.get("chargeable_days") or 0),
        "chargeable_days_label": _days_label(candidate.metadata.get("chargeable_days") or 0),
        "kind": candidate.kind,
        "kind_label": _candidate_kind_label(candidate.kind),
        "passed_hard_rules": passed_hard_rules,
        "can_apply": passed_hard_rules,
        "preference_match": preference_match,
        "preference_match_label": preference_match_label,
        "is_preference_candidate": bool(candidate.metadata.get("is_preference_candidate") or preference_match),
        "risk_score": risk_score,
        "risk_level": risk_level,
        "risk_label": dict(VacationScheduleItem.RISK_CHOICES).get(risk_level, "Низкий"),
        "risk_tone": _risk_tone(risk_level, is_conflict),
        "score": _candidate_scoring_decimal(candidate, "scoring_score"),
        "score_label": _percent_label(_candidate_scoring_decimal(candidate, "scoring_score")),
        "confidence": _candidate_scoring_decimal(candidate, "scoring_confidence"),
        "confidence_label": _percent_label(_candidate_scoring_decimal(candidate, "scoring_confidence")),
        "recommendation_label": _candidate_recommendation_label(candidate.metadata.get("scoring_recommendation")),
        "explanation": candidate.metadata.get("scoring_explanation") or candidate.comment,
        "message": candidate.comment,
    }


def _draft_item_rows(schedule, year, items, planning_need_by_employee, preference_pair_by_employee=None, actor=None):
    if schedule is None:
        return []

    placements = [DraftPlacement(item.employee_id, item.start_date, item.end_date, item.id) for item in items]
    preference_pair_by_employee = preference_pair_by_employee or {}
    feedback_context_by_item = build_schedule_candidate_feedback_context(items, actor=actor)
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
        source_label = _source_label_for_item(item, pair)
        period_label = _short_period_label(item.start_date, item.end_date)
        chargeable_days_label = _days_label(item.chargeable_days)
        feedback_context = feedback_context_by_item.get(
            item.id,
            {
                "summary": {"total": 0, "items": [], "counts": {}},
                "current": None,
                "can_submit": False,
            },
        )
        feedback_context = {
            **feedback_context,
            "action_url": reverse("schedule_draft_candidate_feedback", args=[year, item.id]),
        }
        rows.append(
            {
                "item": item,
                "employee": employee,
                "employee_name": employee.full_name,
                "department_name": org["department_name"],
                "group_name": org["group_name"],
                "position": org["position"],
                "period_label": period_label,
                "full_period_label": _period_label(item.start_date, item.end_date),
                "assigned_label": f"Назначено: {period_label}",
                "source_label": source_label,
                "source_hint": _placement_source_hint(item, pair),
                "chargeable_days": item.chargeable_days,
                "chargeable_days_label": chargeable_days_label,
                "risk_score": risk_score,
                "risk_level": risk_level,
                "risk_label": dict(VacationRequest.RISK_CHOICES).get(risk_level, "Низкий"),
                "has_conflict": has_conflict,
                "has_high_risk": has_high_risk,
                "issue_label": "Конфликт" if has_conflict else ("Высокий риск" if has_high_risk else "Без проблем"),
                "issue_icon": "warning" if has_conflict else ("bolt" if has_high_risk else "verified"),
                "risk_summary": risk_summary,
                "risk_details": list(explanation.get("details") or [])[:3],
                "ai_decision": _draft_item_ai_context(item),
                "feedback": feedback_context,
                "profile_url": _profile_url(employee, year),
                "calendar_url": _calendar_employee_url(
                    employee,
                    year,
                    focus_start=item.start_date,
                    focus_end=item.end_date,
                ),
                "review_url": reverse("schedule_draft_item_review", args=[year, item.id]),
                "day_calculation_url": reverse("schedule_draft_day_calculation", args=[year, employee.id]),
                "manual_anchor": f"draft-manual-{employee.id}",
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
    active_urgent_closure_by_employee=None,
):
    preference_state_by_employee = preference_state_by_employee or {}
    preference_pair_by_employee = preference_pair_by_employee or {}
    active_urgent_closure_by_employee = active_urgent_closure_by_employee or {}
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
    active_urgent_closure = active_urgent_closure_by_employee.get(employee.id)

    if not planning_need["needs_manual_attention"] and active_urgent_closure is None:
        return None

    if active_urgent_closure is not None and not planning_need["needs_manual_attention"]:
        reason = _manual_reason(
            "deadline_blocker",
            f"Открыто срочное закрытие {active_urgent_closure['required_days_label']}",
            (
                f"Период уже отправлен на согласование до "
                f"{active_urgent_closure['deadline_label']}; HR нужно проконтролировать решение."
            ),
        )
    elif planning_need["has_blocker"]:
        detail = (
            f"Сначала нужно закрыть {_days_label(planning_need['blocking_days'])} "
            f"до {planning_need['nearest_deadline_label']}."
        )
        if planning_need["annual_remaining_days"] > 0:
            remaining_label = (
                "добора незакрытых дней"
                if planning_need["remainder_policy"] == VacationPreference.REMAINDER_AUTO
                else "по пожеланию"
            )
            detail = (
                f"{detail} После срочного остатка останется "
                f"{_days_label(planning_need['annual_remaining_days'])} {remaining_label}."
            )
        reason = _manual_reason(
            "deadline_blocker",
            f"Срочно закрыть {_days_label(planning_need['blocking_days'])}",
            detail,
        )
    elif state in {VacationPreference.STATUS_PENDING, "missing"}:
        reason = _manual_reason("pending", "Не ответил на сбор.", "HR сможет выбрать даты вручную на следующем этапе.")
    elif state == VacationPreference.STATUS_SKIPPED:
        reason = _manual_reason("skipped", "Без пожеланий.", "HR закрывает только обязательный остаток.")
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
            f"Осталось добрать {_days_label(planning_need['open_required_days'])}",
            "Пожелание или обязательный остаток еще не закрыты полностью.",
        )

    if reason is None:
        return None

    org = _employee_org_payload(employee)
    identity = _employee_identity_payload(employee)
    urgent_closure = detect_previous_year_closure_need(employee, year, planning_need) or active_urgent_closure
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
        "calendar_url": _calendar_employee_url(employee, year),
        "manual_place_url": reverse("schedule_draft_manual_place", args=[year, employee.id]),
        "manual_preview_url": reverse("schedule_draft_manual_preview", args=[year, employee.id]),
        "manual_package_preview_url": reverse("schedule_draft_manual_package_preview", args=[year, employee.id]),
        "manual_suggestions_url": reverse("schedule_draft_manual_suggestions", args=[year, employee.id]),
        "day_calculation_url": reverse("schedule_draft_day_calculation", args=[year, employee.id]),
        "manual_anchor": f"draft-manual-{employee.id}",
        "planning_need": planning_need,
        "urgent_closure": urgent_closure,
        **identity,
    }


def _filter_draft_scope_employees(eligible_employees, actor=None):
    if actor is None or not is_department_head_employee(actor):
        return eligible_employees

    managed_department_id = get_managed_department_id(actor)
    if managed_department_id is None:
        return []
    return [employee for employee in eligible_employees if employee.department_id == managed_department_id]


def _normalize_schedule_draft_search_query(query_params):
    return ((query_params or {}).get("q") or "").strip()


def _draft_row_matches_search(row, normalized_query):
    if not normalized_query:
        return True
    employee = row.get("employee")
    search_text = " ".join(
        str(value)
        for value in [
            row.get("employee_name"),
            getattr(employee, "login", ""),
            row.get("position"),
            row.get("department_name"),
            row.get("group_name"),
        ]
        if value
    ).casefold()
    return normalized_query.casefold() in search_text


def _filter_schedule_draft_rows(rows, query):
    normalized_query = query.casefold()
    if not normalized_query:
        return rows
    return [row for row in rows if _draft_row_matches_search(row, normalized_query)]


def _schedule_draft_visible_count_label(visible_count, total_count, unit_label):
    if visible_count == total_count:
        return f"{visible_count} {unit_label}"
    return f"{visible_count} из {total_count} {unit_label}"


def _empty_draft_summary():
    return {
        "placed": 0,
        "manual": 0,
        "blocking": 0,
        "open_required_days": Decimal("0.00"),
        "open_required_days_label": _days_label(Decimal("0.00")),
        "remaining_plan_days": Decimal("0.00"),
        "remaining_plan_days_label": _days_label(Decimal("0.00")),
        "blocking_days": Decimal("0.00"),
        "blocking_days_label": _days_label(Decimal("0.00")),
        "high_risk": 0,
        "conflicts": 0,
        "departments": 0,
        "total": 0,
    }


def _draft_item_has_stored_conflict(item):
    selected_candidate = getattr(item, "selected_candidate", None)
    if selected_candidate is None:
        return False
    return bool((selected_candidate.features or {}).get("risk_is_conflict"))


def _build_draft_summary_from_parts(draft_items, manual_planning_needs, department_names):
    manual_planning_needs = list(manual_planning_needs or [])
    blocking_needs = [need for need in manual_planning_needs if need["has_blocker"]]
    total_open_required_days = quantize_leave_days(
        sum((need["open_required_days"] for need in manual_planning_needs), Decimal("0.00"))
    )
    total_blocking_days = quantize_leave_days(
        sum((need["blocking_days"] for need in blocking_needs), Decimal("0.00"))
    )
    total_remaining_plan_days = quantize_leave_days(
        sum(
            (
                max(
                    need["open_required_days"] - need["blocking_days"],
                    Decimal("0.00"),
                )
                for need in manual_planning_needs
            ),
            Decimal("0.00"),
        )
    )
    conflict_count = sum(1 for item in draft_items if _draft_item_has_stored_conflict(item))
    high_risk_count = sum(
        1
        for item in draft_items
        if item.risk_level == VacationScheduleItem.RISK_HIGH and not _draft_item_has_stored_conflict(item)
    )
    return {
        "placed": len(draft_items),
        "manual": len(manual_planning_needs),
        "blocking": len(blocking_needs),
        "open_required_days": total_open_required_days,
        "open_required_days_label": _days_label(total_open_required_days),
        "remaining_plan_days": total_remaining_plan_days,
        "remaining_plan_days_label": _days_label(total_remaining_plan_days),
        "blocking_days": total_blocking_days,
        "blocking_days_label": _days_label(total_blocking_days),
        "high_risk": high_risk_count,
        "conflicts": conflict_count,
        "departments": len(department_names),
        "total": len(draft_items) + len(manual_planning_needs),
    }


def build_schedule_draft_summary_context(year, actor=None):
    schedule = VacationSchedule.objects.filter(year=year, status=VacationSchedule.STATUS_DRAFT).first()
    if schedule is None:
        return {
            "schedule": None,
            "draft_summary": _empty_draft_summary(),
            "approval_blocked": False,
        }

    eligible_employees = _filter_draft_scope_employees(get_eligible_preference_employees(year), actor=actor)
    employee_ids = [employee.id for employee in eligible_employees]
    employee_id_set = set(employee_ids)
    preference_pair_by_employee = get_employee_preference_pair_map(employee_ids, year)
    preference_state_by_employee = get_employee_preference_state_map(employee_ids, year)
    active_urgent_closure_by_employee = get_active_urgent_closure_payload_map(employee_ids, year)
    draft_items = [item for item in _draft_items_for_schedule(schedule) if item.employee_id in employee_id_set]
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

    manual_employee_ids = {
        employee.id
        for employee in eligible_employees
        if planning_need_by_employee[employee.id]["needs_manual_attention"]
        or active_urgent_closure_by_employee.get(employee.id) is not None
    }
    manual_planning_needs = [
        planning_need_by_employee[employee_id]
        for employee_id in manual_employee_ids
    ]
    department_names = {
        item.employee.department.name if item.employee.department_id else "Без отдела"
        for item in draft_items
    }
    department_names.update(
        employee.department.name if employee.department_id else "Без отдела"
        for employee in eligible_employees
        if employee.id in manual_employee_ids
    )
    draft_summary = _build_draft_summary_from_parts(draft_items, manual_planning_needs, department_names)
    return {
        "schedule": schedule,
        "draft_summary": draft_summary,
        "approval_blocked": draft_summary["blocking"] > 0,
    }


def build_schedule_draft_page_context(year, actor=None, query_params=None):
    query = _normalize_schedule_draft_search_query(query_params)
    collection = VacationPreferenceCollection.objects.filter(year=year).first()
    normalize_schedule_draft_adjacent_items(year)
    schedule = VacationSchedule.objects.filter(year=year, status=VacationSchedule.STATUS_DRAFT).first()
    if schedule is None:
        return {
            "year": year,
            "collection": collection,
            "schedule": None,
            "draft_exists": False,
            "draft_url": schedule_draft_url(year),
            "draft_create_url": schedule_draft_create_url(year),
            "draft_auto_place_url": reverse("schedule_draft_auto_place", args=[year]),
            "draft_auto_place_preview_url": reverse("schedule_draft_auto_place_preview", args=[year]),
            "draft_auto_place_next_url": schedule_draft_url(year),
            "readiness_url": reverse("preference_collection_readiness", args=[year]),
            "placed_rows": [],
            "manual_rows": [],
            "query": query,
            "result_count": 0,
            "visible_placed_count": 0,
            "visible_manual_count": 0,
            "placed_count_label": _schedule_draft_visible_count_label(0, 0, "записей"),
            "manual_count_label": format_staff_count(0),
            "planning_need_by_employee": {},
            "draft_summary": _empty_draft_summary(),
            "draft_status": {
                "label": "Черновик не создан",
                "icon": "pending_actions",
            },
            "approval_blocked": False,
        }
    eligible_employees = _filter_draft_scope_employees(get_eligible_preference_employees(year), actor=actor)
    employee_ids = [employee.id for employee in eligible_employees]
    preference_pair_by_employee = get_employee_preference_pair_map(employee_ids, year)
    preference_state_by_employee = get_employee_preference_state_map(employee_ids, year)
    active_urgent_closure_by_employee = get_active_urgent_closure_payload_map(employee_ids, year)
    draft_items = [item for item in _draft_items_for_schedule(schedule) if item.employee_id in set(employee_ids)]
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
    all_placed_rows = _draft_item_rows(
        schedule,
        year,
        draft_items,
        planning_need_by_employee,
        preference_pair_by_employee=preference_pair_by_employee,
        actor=actor,
    )
    placed_employee_ids = {row["employee"].id for row in all_placed_rows}
    all_manual_rows = [
        row
        for employee in eligible_employees
        for row in [
            _manual_row_for_employee(
                employee,
                year,
                placed_employee_ids,
                all_placed_rows,
                planning_need_by_employee[employee.id],
                preference_state_by_employee=preference_state_by_employee,
                preference_pair_by_employee=preference_pair_by_employee,
                active_urgent_closure_by_employee=active_urgent_closure_by_employee,
            )
        ]
        if row is not None
    ]
    placed_rows = _filter_schedule_draft_rows(all_placed_rows, query)
    manual_rows = _filter_schedule_draft_rows(all_manual_rows, query)
    conflict_count = sum(1 for row in all_placed_rows if row["has_conflict"])
    high_risk_count = sum(1 for row in all_placed_rows if row["has_high_risk"] and not row["has_conflict"])
    departments = sorted({row["department_name"] for row in all_placed_rows + all_manual_rows})
    blocking_rows = [row for row in all_manual_rows if row["planning_need"]["has_blocker"]]
    draft_summary = _build_draft_summary_from_parts(
        draft_items,
        [row["planning_need"] for row in all_manual_rows],
        departments,
    )
    draft_summary["high_risk"] = high_risk_count
    draft_summary["conflicts"] = conflict_count
    return {
        "year": year,
        "collection": collection,
        "schedule": schedule,
        "draft_exists": schedule is not None,
        "draft_url": schedule_draft_url(year),
        "draft_create_url": schedule_draft_create_url(year),
        "draft_auto_place_url": reverse("schedule_draft_auto_place", args=[year]),
        "draft_auto_place_preview_url": reverse("schedule_draft_auto_place_preview", args=[year]),
        "draft_auto_place_next_url": schedule_draft_url(year),
        "readiness_url": reverse("preference_collection_readiness", args=[year]),
        "placed_rows": placed_rows,
        "manual_rows": manual_rows,
        "query": query,
        "result_count": len(placed_rows) + len(manual_rows),
        "visible_placed_count": len(placed_rows),
        "visible_manual_count": len(manual_rows),
        "placed_count_label": _schedule_draft_visible_count_label(len(placed_rows), len(all_placed_rows), "записей"),
        "manual_count_label": (
            f"{format_staff_count(len(manual_rows))} из {format_staff_count(len(all_manual_rows))}"
            if len(manual_rows) != len(all_manual_rows)
            else format_staff_count(len(all_manual_rows))
        ),
        "planning_need_by_employee": planning_need_by_employee,
        "draft_summary": draft_summary,
        "draft_status": {
            "label": "Черновик создан" if schedule else "Черновик не создан",
            "icon": "edit_calendar" if schedule else "pending_actions",
        },
        "approval_blocked": bool(blocking_rows),
    }


def _get_draft_schedule_for_preview(year):
    schedule = VacationSchedule.objects.filter(year=year, status=VacationSchedule.STATUS_DRAFT).first()
    if schedule is None:
        raise ValidationError("Черновик графика за этот год не найден.")
    return schedule


def _manual_package_target_candidates(context, employee, current_items, planning_need):
    _, planning_end = _planning_year_bounds(context.year)
    open_required_days = planning_need["open_required_days"]
    if open_required_days <= 0:
        return []

    latest_end = planning_need.get("nearest_deadline") if planning_need.get("has_blocker") else planning_end
    latest_end = latest_end or planning_end
    urgent = bool(planning_need.get("has_blocker"))
    target_days = [
        open_required_days,
        min(open_required_days, Decimal(AUTO_DRAFT_FALLBACK_CHUNK_DAYS)),
        min(open_required_days, Decimal("21")),
        min(open_required_days, Decimal("14")),
    ]
    candidates = []
    backup_candidate = _backup_preference_candidate(
        context,
        employee,
        planning_need,
        metadata={"manual_package_seed": True},
    )
    if backup_candidate is not None:
        candidates.append(backup_candidate)
    candidates.extend(_build_auto_generation_candidates(context, employee, current_items, planning_need))
    for target in target_days:
        if target <= 0:
            continue
        payloads = list(
            _iter_auto_candidate_payloads_for_need(
                employee,
                context.year,
                context.placements,
                target,
                latest_end,
                urgent=urgent,
                allow_short_parts=target < MIN_CONTINUOUS_PAID_LEAVE_DAYS,
                max_chargeable_days=target,
                limit=AUTO_DRAFT_MAX_CANDIDATES_PER_STRATEGY,
            )
        )
        candidates.extend(
            _auto_generation_candidates_from_payloads(
                employee,
                payloads,
                kind=DRAFT_CANDIDATE_AUTO_URGENT if urgent else DRAFT_CANDIDATE_AUTO,
                comment="Предложение модуля для ручного распределения.",
                planning_need=planning_need,
                metadata={"manual_package_seed": True},
            )
        )
    ranked_candidates = _rank_generation_candidates(_dedupe_generation_candidates(candidates))
    return sorted(
        ranked_candidates,
        key=lambda candidate: 1 if candidate.metadata.get("is_preference_candidate") else 0,
        reverse=True,
    )


def _backup_preference_candidate(context, employee, planning_need, *, metadata=None):
    pair = context.preference_pair_by_employee.get(employee.id) or {}
    preference = pair.get(VacationPreference.PRIORITY_BACKUP)
    if not preference or not preference.start_date or not preference.end_date:
        return None

    open_required_days = _decimal_to_whole_days(planning_need.get("open_required_days"))
    if open_required_days <= 0:
        return None

    backup_days = _decimal_to_whole_days(get_chargeable_leave_days(preference.start_date, preference.end_date, "paid"))
    if backup_days <= 0:
        return None

    target_days = min(open_required_days, backup_days)
    start_date = preference.start_date
    end_date = preference.end_date
    preference_match = "backup"
    preference_match_label = "Запасное пожелание"
    if target_days < backup_days:
        partial_end = _end_date_for_chargeable_days(start_date, target_days, preference.end_date)
        if partial_end is None:
            return None
        end_date = partial_end
        preference_match = "backup_partial"
        preference_match_label = "Часть запасного пожелания"

    candidate = DraftGenerationCandidate(
        employee=employee,
        start_date=start_date,
        end_date=end_date,
        kind=DRAFT_CANDIDATE_BACKUP_PREFERENCE,
        source=VacationScheduleItem.SOURCE_GENERATED,
        comment=f"Предложение модуля: {preference_match_label.lower()}.",
        preference=preference,
        metadata={
            "priority": VacationPreference.PRIORITY_BACKUP,
            "target_chargeable_days": Decimal(target_days),
            "preference_match": preference_match,
            "preference_match_label": preference_match_label,
            "is_preference_candidate": True,
            **(metadata or {}),
        },
    )
    _apply_planning_need_metadata(candidate, planning_need, context.year)
    return _assess_generation_candidate_hard_rules(candidate, context.year, context.placements, max_chargeable_days=Decimal(target_days))


def _manual_package_context_after_candidate(context, employee, current_items, candidate):
    next_context = DraftGenerationContext(
        year=context.year,
        schedule=context.schedule,
        eligible_employees=context.eligible_employees,
        draft_items_by_employee={key: list(value) for key, value in context.draft_items_by_employee.items()},
        preference_pair_by_employee=context.preference_pair_by_employee,
        preference_state_by_employee=context.preference_state_by_employee,
        placements=list(context.placements),
        planning_need_by_employee=dict(context.planning_need_by_employee),
    )
    next_items = list(current_items)
    chargeable_days = candidate.metadata.get("chargeable_days") or candidate.assessment.get("chargeable_days") or 0
    next_items.append(
        _virtual_draft_item(
            employee,
            candidate.start_date,
            candidate.end_date,
            chargeable_days,
            source=candidate.source,
            selected_candidate=candidate,
        )
    )
    next_context.draft_items_by_employee[employee.id] = next_items
    next_context.placements.append(DraftPlacement(employee.id, candidate.start_date, candidate.end_date, None))
    next_context.planning_need_by_employee[employee.id] = _current_employee_planning_need(next_context, employee)
    return next_context, next_items


def _build_manual_candidate_package_from_seed(context, employee, seed_candidate):
    candidates = [seed_candidate]
    current_context = context
    current_items = list(context.draft_items_by_employee.get(employee.id, []))

    if not _candidate_passed_hard_rules(seed_candidate):
        return DraftGenerationCandidatePackage(
            employee=employee,
            candidates=candidates,
            source=VacationScheduleItem.SOURCE_GENERATED,
            explanation="Пакет заблокирован жесткими правилами.",
            metadata={"package_kind": "manual_suggestion"},
        )

    current_context, current_items = _manual_package_context_after_candidate(current_context, employee, current_items, seed_candidate)
    while len(candidates) < MANUAL_DRAFT_MAX_PACKAGE_PERIODS:
        planning_need = _current_employee_planning_need(current_context, employee)
        if not planning_need["needs_manual_attention"]:
            break
        _, planning_end = _planning_year_bounds(current_context.year)
        latest_end = planning_need.get("nearest_deadline") if planning_need.get("has_blocker") else planning_end
        latest_end = latest_end or planning_end
        target_days = min(planning_need["open_required_days"], Decimal(AUTO_DRAFT_FALLBACK_CHUNK_DAYS))
        payloads = list(
            _iter_auto_candidate_payloads_for_need(
                employee,
                current_context.year,
                current_context.placements,
                target_days,
                latest_end,
                urgent=bool(planning_need.get("has_blocker")),
                allow_short_parts=True,
                max_chargeable_days=planning_need["open_required_days"],
                limit=AUTO_DRAFT_MAX_CANDIDATES_PER_STRATEGY,
            )
        )
        next_candidates = _rank_generation_candidates(
            _auto_generation_candidates_from_payloads(
                employee,
                payloads,
                kind=DRAFT_CANDIDATE_AUTO_URGENT if planning_need.get("has_blocker") else DRAFT_CANDIDATE_AUTO,
                comment="Предложение модуля: следующая часть отпуска.",
                planning_need=planning_need,
                metadata={"manual_package_continuation": True},
            )
        )
        next_candidate = _select_first_passed_generation_candidate(next_candidates)
        if next_candidate is None:
            break
        candidates.append(next_candidate)
        current_context, current_items = _manual_package_context_after_candidate(
            current_context,
            employee,
            current_items,
            next_candidate,
        )

    total_days = sum((_feature_decimal(candidate.metadata.get("chargeable_days")) for candidate in candidates), Decimal("0.00"))
    remaining_need = _current_employee_planning_need(current_context, employee)
    explanation = (
        "Пакет полностью закрывает плановую потребность."
        if not remaining_need["needs_manual_attention"]
        else "Пакет закрывает часть дней, остаток останется вручную."
    )
    return DraftGenerationCandidatePackage(
        employee=employee,
        candidates=candidates,
        source=VacationScheduleItem.SOURCE_GENERATED,
        explanation=explanation,
        metadata={
            "package_kind": "manual_suggestion",
            "total_chargeable_days": total_days,
            "periods_count": len(candidates),
            "remaining_after_package": remaining_need["open_required_days"],
            "package_closes_need": not remaining_need["needs_manual_attention"],
        },
    )


def _manual_package_key(package):
    return tuple((candidate.start_date, candidate.end_date) for candidate in package.candidates)


def _rank_manual_candidate_packages(packages):
    def package_rank(package):
        candidates = package.candidates
        has_preference_candidate = any(candidate.metadata.get("is_preference_candidate") for candidate in candidates)
        has_exact_preference = any(candidate.metadata.get("preference_match") == "backup" for candidate in candidates)
        total_days = sum((_feature_decimal(candidate.metadata.get("chargeable_days")) for candidate in candidates), Decimal("0.00"))
        max_risk = max((int(candidate.metadata.get("risk_score") or 0) for candidate in candidates), default=0)
        closes_need = bool(package.metadata.get("package_closes_need"))
        return (
            1 if has_preference_candidate else 0,
            1 if has_exact_preference else 0,
            1 if closes_need else 0,
            _manual_package_quality_score(package),
            total_days,
            Decimal("100.00") - Decimal(max_risk),
            -len(candidates),
        )

    return sorted(packages, key=package_rank, reverse=True)


def _manual_candidate_packages(context, employee, limit=MANUAL_DRAFT_MAX_PACKAGE_SUGGESTIONS):
    current_items = context.draft_items_by_employee.get(employee.id, [])
    planning_need = _current_employee_planning_need(context, employee)
    seeds = [
        candidate
        for candidate in _manual_package_target_candidates(context, employee, current_items, planning_need)
        if _candidate_passed_hard_rules(candidate)
    ]
    packages = []
    seen = set()
    for seed in seeds:
        package = _build_manual_candidate_package_from_seed(context, employee, seed)
        key = _manual_package_key(package)
        if not key or key in seen:
            continue
        seen.add(key)
        packages.append(package)
        if len(packages) >= limit * 2:
            break
    return _rank_manual_candidate_packages(packages)[:limit]


def _manual_package_payload(package, *, rank=None):
    candidates = [_apply_candidate_scoring(candidate) for candidate in package.candidates]
    period_payloads = [_generation_candidate_payload(candidate, rank=index) for index, candidate in enumerate(candidates, start=1)]
    preference_payload = next((payload for payload in period_payloads if payload.get("is_preference_candidate")), None)
    total_days = sum((_feature_decimal(payload["chargeable_days"]) for payload in period_payloads), Decimal("0.00"))
    package_score = _manual_package_quality_score(package)
    avg_confidence = (
        sum((_feature_decimal(payload["confidence"]) for payload in period_payloads), Decimal("0.00"))
        / Decimal(len(period_payloads))
        if period_payloads
        else Decimal("0.00")
    )
    highest_risk = max(
        period_payloads,
        key=lambda payload: (_risk_level_rank(payload["risk_level"]), int(payload["risk_score"] or 0)),
        default={},
    )
    first_period = period_payloads[0] if period_payloads else {}
    return {
        "rank": rank,
        "periods_count": len(period_payloads),
        "periods": period_payloads,
        "period_label": " + ".join(payload["period_label"] for payload in period_payloads),
        "full_period_label": " + ".join(payload["full_period_label"] for payload in period_payloads),
        "start_date": first_period.get("start_date", ""),
        "end_date": first_period.get("end_date", ""),
        "chargeable_days": int(total_days),
        "chargeable_days_label": _days_label(total_days),
        "kind": "manual_package",
        "kind_label": f"{len(period_payloads)} период(а)",
        "passed_hard_rules": all(payload["passed_hard_rules"] for payload in period_payloads),
        "can_apply": all(payload["can_apply"] for payload in period_payloads),
        "preference_match": preference_payload.get("preference_match", "") if preference_payload else "",
        "preference_match_label": preference_payload.get("preference_match_label", "") if preference_payload else "",
        "is_preference_candidate": preference_payload is not None,
        "risk_score": int(highest_risk.get("risk_score") or 0),
        "risk_level": highest_risk.get("risk_level") or VacationScheduleItem.RISK_LOW,
        "risk_label": highest_risk.get("risk_label") or "Низкий",
        "risk_tone": highest_risk.get("risk_tone") or "low",
        "score": package_score,
        "score_label": _percent_label(package_score),
        "confidence": avg_confidence,
        "confidence_label": _percent_label(avg_confidence),
        "recommendation_label": "Можно применить",
        "explanation": package.explanation,
        "message": package.explanation,
    }


def _manual_preference_option_payload(candidate):
    if candidate is None:
        return None

    candidate = _apply_candidate_scoring(candidate)
    payload = _generation_candidate_payload(candidate)
    can_apply = _candidate_passed_hard_rules(candidate)
    block_reason = candidate.metadata.get("block_reason") or candidate.metadata.get("block_reason_detail") or ""
    payload.update(
        {
            "can_apply": can_apply,
            "status_label": "Учтено в предложениях" if can_apply else "Не подходит",
            "status_tone": "ready" if can_apply else "blocked",
            "reason": (
                "Запасной период прошел проверки и добавлен в список предложений."
                if can_apply
                else block_reason or payload.get("explanation") or "Запасной период не прошел проверку."
            ),
        }
    )
    return payload


def _json_safe_manual_suggestion_payload(payload):
    return json.loads(json.dumps(payload, cls=DjangoJSONEncoder))


def _limit_manual_suggestion_payload(payload, limit):
    limit = max(1, min(int(limit or MANUAL_DRAFT_VISIBLE_PACKAGE_SUGGESTIONS), MANUAL_DRAFT_MAX_PACKAGE_SUGGESTIONS))
    payload = dict(payload or {})
    all_options = list(payload.get("options") or [])
    visible_options = all_options[:limit]
    total_candidates = int(payload.get("total_candidates") or len(all_options))
    payload["visible_limit"] = limit
    payload["options"] = visible_options
    payload["shown_candidates"] = len(visible_options)
    payload["has_more_options"] = total_candidates > len(visible_options)
    return payload


def _manual_suggestion_payload_from_context(
    context,
    employee,
    *,
    limit=MANUAL_DRAFT_VISIBLE_PACKAGE_SUGGESTIONS,
    planning_need=None,
):
    limit = max(1, min(int(limit or MANUAL_DRAFT_VISIBLE_PACKAGE_SUGGESTIONS), MANUAL_DRAFT_MAX_PACKAGE_SUGGESTIONS))
    planning_need = planning_need or _current_employee_planning_need(context, employee)
    preference_option = _manual_preference_option_payload(
        _backup_preference_candidate(
            context,
            employee,
            planning_need,
            metadata={"manual_package_seed": True},
        )
    )
    packages = _manual_candidate_packages(context, employee, limit=MANUAL_DRAFT_MAX_PACKAGE_SUGGESTIONS)
    visible_packages = packages[:limit]
    return {
        "suggestion_schema_version": MANUAL_DRAFT_SUGGESTION_CACHE_SCHEMA_VERSION,
        "employee_id": employee.id,
        "employee_name": employee.full_name,
        "needed_label": planning_need["manual_task_label"],
        "status_label": planning_need["status"]["label"],
        "target_days_label": planning_need["target_days_label"],
        "placed_days_label": planning_need["placed_days_label"],
        "planning_year": context.year,
        "date_min": date(context.year, 1, 1).isoformat(),
        "date_max": date(context.year, 12, 31).isoformat(),
        "preference_option": preference_option,
        "visible_limit": limit,
        "options": [
            _manual_package_payload(package, rank=index)
            for index, package in enumerate(visible_packages, start=1)
        ],
        "total_candidates": len(packages),
        "safe_candidates": len(packages),
        "shown_candidates": len(visible_packages),
        "has_more_options": len(packages) > len(visible_packages),
    }


def _get_current_manual_suggestion_cache(schedule, employee_id):
    version = int(schedule.manual_suggestion_cache_version or 0)
    if version <= 0:
        return None
    cache = VacationScheduleManualSuggestionCache.objects.filter(
        schedule=schedule,
        employee_id=employee_id,
        version=version,
    ).first()
    if cache is None:
        return None
    if int((cache.payload or {}).get("suggestion_schema_version") or 0) != MANUAL_DRAFT_SUGGESTION_CACHE_SCHEMA_VERSION:
        cache.delete()
        return None
    return cache


def _rebuild_schedule_draft_manual_suggestion_cache(schedule):
    if schedule is None:
        return 0

    context = _build_draft_generation_context(schedule.year, schedule)
    next_version = int(schedule.manual_suggestion_cache_version or 0) + 1
    rebuilt_at = timezone.now()
    cache_rows = []

    schedule.manual_suggestion_cache_version = next_version
    schedule.manual_suggestion_cache_rebuilt_at = rebuilt_at
    schedule.save(update_fields=["manual_suggestion_cache_version", "manual_suggestion_cache_rebuilt_at"])

    for employee in context.eligible_employees:
        planning_need = _current_employee_planning_need(context, employee)
        context.planning_need_by_employee[employee.id] = planning_need
        if not planning_need["needs_manual_attention"]:
            continue

        payload = _manual_suggestion_payload_from_context(
            context,
            employee,
            limit=MANUAL_DRAFT_MAX_PACKAGE_SUGGESTIONS,
            planning_need=planning_need,
        )
        cache_rows.append(
            VacationScheduleManualSuggestionCache(
                schedule=schedule,
                employee=employee,
                version=next_version,
                payload=_json_safe_manual_suggestion_payload(payload),
                created_at=rebuilt_at,
                updated_at=rebuilt_at,
            )
        )

    VacationScheduleManualSuggestionCache.objects.filter(schedule=schedule).delete()
    if cache_rows:
        VacationScheduleManualSuggestionCache.objects.bulk_create(cache_rows)
    return len(cache_rows)


def _invalidate_schedule_draft_manual_suggestion_cache(schedule):
    if schedule is None:
        return 0

    next_version = int(schedule.manual_suggestion_cache_version or 0) + 1
    invalidated_at = timezone.now()
    schedule.manual_suggestion_cache_version = next_version
    schedule.manual_suggestion_cache_rebuilt_at = invalidated_at
    schedule.save(update_fields=["manual_suggestion_cache_version", "manual_suggestion_cache_rebuilt_at"])
    deleted_count, _ = VacationScheduleManualSuggestionCache.objects.filter(schedule=schedule).delete()
    return deleted_count


def _ensure_manual_suggestion_cache_version(schedule):
    version = int(schedule.manual_suggestion_cache_version or 0)
    if version > 0:
        return version

    schedule.manual_suggestion_cache_version = 1
    schedule.manual_suggestion_cache_rebuilt_at = timezone.now()
    schedule.save(update_fields=["manual_suggestion_cache_version", "manual_suggestion_cache_rebuilt_at"])
    return schedule.manual_suggestion_cache_version


def _build_schedule_draft_manual_suggestion_cache_for_employee(schedule, employee_id):
    if schedule is None:
        return None

    context = _build_draft_generation_context(schedule.year, schedule)
    employee = next((candidate for candidate in context.eligible_employees if candidate.id == employee_id), None)
    if employee is None:
        raise ValidationError("Сотрудник не найден в сборе пожеланий за этот год.")

    planning_need = _current_employee_planning_need(context, employee)
    context.planning_need_by_employee[employee.id] = planning_need
    if not planning_need["needs_manual_attention"]:
        return None

    version = _ensure_manual_suggestion_cache_version(schedule)
    payload = _manual_suggestion_payload_from_context(
        context,
        employee,
        limit=MANUAL_DRAFT_MAX_PACKAGE_SUGGESTIONS,
        planning_need=planning_need,
    )
    cache, _ = VacationScheduleManualSuggestionCache.objects.update_or_create(
        schedule=schedule,
        employee=employee,
        defaults={
            "version": version,
            "payload": _json_safe_manual_suggestion_payload(payload),
        },
    )
    return cache


def _manual_suggestion_cache_payload(cache, limit):
    payload = _limit_manual_suggestion_payload(cache.payload, limit)
    payload.update(
        {
            "from_cache": True,
            "cache_version": cache.version,
            "cached_at": cache.updated_at.isoformat() if cache.updated_at else "",
        }
    )
    return payload


@transaction.atomic
def build_schedule_draft_manual_suggestions(*, year, employee_id, limit=MANUAL_DRAFT_VISIBLE_PACKAGE_SUGGESTIONS):
    limit = max(1, min(int(limit or MANUAL_DRAFT_VISIBLE_PACKAGE_SUGGESTIONS), MANUAL_DRAFT_MAX_PACKAGE_SUGGESTIONS))
    schedule = VacationSchedule.objects.select_for_update().filter(
        year=year,
        status=VacationSchedule.STATUS_DRAFT,
    ).first()
    if schedule is None:
        raise ValidationError("Черновик графика за этот год не найден.")

    cache = _get_current_manual_suggestion_cache(schedule, employee_id)
    if cache is None:
        cache = _build_schedule_draft_manual_suggestion_cache_for_employee(schedule, employee_id)
    if cache is None:
        context = _build_draft_generation_context(year, schedule)
        employee = next((candidate for candidate in context.eligible_employees if candidate.id == employee_id), None)
        if employee is None:
            raise ValidationError("Сотрудник не найден в сборе пожеланий за этот год.")

        payload = _manual_suggestion_payload_from_context(context, employee, limit=limit)
        payload.update(
            {
                "from_cache": False,
                "cache_version": schedule.manual_suggestion_cache_version,
                "cached_at": "",
            }
        )
        return payload
    if cache.version != int(schedule.manual_suggestion_cache_version or 0):
        cache = _get_current_manual_suggestion_cache(schedule, employee_id)
    if cache is not None:
        return _manual_suggestion_cache_payload(cache, limit)


def build_schedule_draft_auto_place_preview(*, year, limit=8):
    schedule = _get_draft_schedule_for_preview(year)
    context = _build_draft_generation_context(year, schedule)
    preview_options = []
    placed_count = 0
    blocked_count = 0
    high_risk_count = 0
    auto_employees = _sort_auto_place_employees(context.eligible_employees, context.planning_need_by_employee)
    initial_unresolved_count = len(auto_employees)
    processed_employee_ids = set()
    max_preview_employees = max(int(limit or 0) * 3, 8)

    for employee in auto_employees:
        if len(preview_options) >= limit:
            break
        if preview_options and len(processed_employee_ids) >= max_preview_employees:
            break
        processed_employee_ids.add(employee.id)
        chunks_count = 0
        while chunks_count < AUTO_DRAFT_MAX_CHUNKS_PER_EMPLOYEE:
            if len(preview_options) >= limit:
                break
            planning_need = _current_employee_planning_need(context, employee)
            if not planning_need["needs_manual_attention"]:
                context.planning_need_by_employee[employee.id] = planning_need
                break

            packages, selected_package = _auto_candidate_packages_for_employee(
                context,
                employee,
                package_limit=AUTO_DRAFT_PREVIEW_PACKAGE_SUGGESTIONS,
            )
            if selected_package is None:
                blocked_count += 1
                context.planning_need_by_employee[employee.id] = planning_need
                break

            package_periods_count = len(selected_package.candidates)
            placed_count += package_periods_count
            chunks_count += package_periods_count
            high_risk_count += sum(
                1
                for candidate in selected_package.candidates
                if candidate.metadata.get("risk_level") == VacationScheduleItem.RISK_HIGH
            )
            if len(preview_options) < limit:
                day_calculation = build_schedule_day_calculation_payload(employee, year, planning_need)
                package_payload = _manual_package_payload(selected_package, rank=len(preview_options) + 1)
                proposal_text = (
                    package_payload.get("preference_match_label")
                    or package_payload.get("kind_label")
                    or "пакет периодов"
                )
                preview_options.append(
                    {
                        "employee_id": employee.id,
                        "employee_name": employee.full_name,
                        "department_name": _employee_org_payload(employee)["department_name"],
                        "day_calculation": day_calculation,
                        "calculation_note": (
                            f"Осталось {day_calculation['open_required_days_label']}. "
                            f"{day_calculation['short_reason']}"
                        ),
                        "proposal_note": f"Предложение: {proposal_text.lower()}.",
                        **package_payload,
                    }
                )

            _register_virtual_candidate_package(context, selected_package)

        if chunks_count >= AUTO_DRAFT_MAX_CHUNKS_PER_EMPLOYEE:
            planning_need = _current_employee_planning_need(context, employee)
            if planning_need["needs_manual_attention"]:
                blocked_count += 1

    processed_unresolved_after = sum(
        1
        for employee in context.eligible_employees
        if employee.id in processed_employee_ids
        and _current_employee_planning_need(context, employee)["needs_manual_attention"]
    )
    unresolved_after = (initial_unresolved_count - len(processed_employee_ids)) + processed_unresolved_after
    return {
        "placed_count": placed_count,
        "unresolved_count": unresolved_after,
        "blocked_count": blocked_count,
        "high_risk_count": high_risk_count,
        "options": preview_options,
        "has_more_options": len(processed_employee_ids) < initial_unresolved_count or placed_count > len(preview_options),
    }
