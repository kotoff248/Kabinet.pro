from datetime import timedelta
from collections import defaultdict
from decimal import Decimal
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.leave.models import (
    VacationEntitlementAllocation,
    VacationEntitlementPeriod,
    VacationRequest,
    VacationScheduleItem,
)

from .constants import (
    ACTIVE_REQUEST_STATUSES,
    BALANCE_AFFECTING_TYPES,
    LEAVE_ADVANCE_MONTHS,
    SCHEDULE_BALANCE_STATUSES,
)
from .dates import (
    add_months_safe,
    add_years_safe,
    format_ru_date,
    get_chargeable_leave_days,
    get_employee_joined_date,
    normalize_date_value,
    quantize_leave_days,
)
from .querysets import exclude_converted_paid_requests

def get_working_year_bounds(employee, as_of_date=None):
    as_of_date = normalize_date_value(as_of_date or timezone.localdate())
    start_date = get_employee_joined_date(employee)
    if as_of_date < start_date:
        working_year_start = start_date
    else:
        completed_years = as_of_date.year - start_date.year
        anniversary_this_year = add_years_safe(start_date, completed_years)
        if anniversary_this_year > as_of_date:
            completed_years -= 1
        working_year_start = add_years_safe(start_date, max(completed_years, 0))
    working_year_end = add_years_safe(working_year_start, 1) - timedelta(days=1)
    return working_year_start, working_year_end

def _calculate_accrued_leave_without_ledger(employee, as_of_date):
    as_of_date = normalize_date_value(as_of_date)
    date_joined = get_employee_joined_date(employee)
    if as_of_date < date_joined:
        return Decimal("0.00")

    annual_leave = Decimal(employee.annual_paid_leave_days)
    working_year_start, working_year_end = get_working_year_bounds(employee, as_of_date)
    completed_years = max(0, working_year_start.year - date_joined.year)
    while add_years_safe(date_joined, completed_years) > working_year_start:
        completed_years -= 1

    fully_accrued = annual_leave * completed_years
    elapsed_days = (min(as_of_date, working_year_end) - working_year_start).days + 1
    working_year_days = (working_year_end - working_year_start).days + 1
    current_year_accrued = annual_leave * Decimal(elapsed_days) / Decimal(working_year_days)
    return quantize_leave_days(fully_accrued + current_year_accrued)

def _calculate_requestable_leave_without_ledger(employee, as_of_date):
    as_of_date = normalize_date_value(as_of_date)
    date_joined = get_employee_joined_date(employee)
    if as_of_date < date_joined:
        return Decimal("0.00")

    annual_leave = Decimal(employee.annual_paid_leave_days)
    working_year_start, _ = get_working_year_bounds(employee, as_of_date)
    completed_years = max(0, working_year_start.year - date_joined.year)
    while add_years_safe(date_joined, completed_years) > working_year_start:
        completed_years -= 1

    fully_requestable = annual_leave * completed_years
    current_year_accrued = _calculate_accrued_leave_without_ledger(employee, as_of_date) - quantize_leave_days(
        annual_leave * completed_years
    )
    if completed_years == 0:
        first_year_advance_available_from = add_months_safe(date_joined, LEAVE_ADVANCE_MONTHS)
        current_year_requestable = annual_leave if as_of_date >= first_year_advance_available_from else current_year_accrued
    else:
        current_year_requestable = annual_leave
    return quantize_leave_days(fully_requestable + current_year_requestable)

def iter_employee_working_years(employee, through_date=None):
    through_date = through_date or timezone.localdate()
    date_joined = get_employee_joined_date(employee)
    if through_date < date_joined:
        return

    cursor = date_joined
    working_year_number = 1
    while cursor <= through_date:
        period_end = add_years_safe(cursor, 1) - timedelta(days=1)
        yield working_year_number, cursor, period_end
        cursor = period_end + timedelta(days=1)
        working_year_number += 1

def _entitlement_available_from(period_start, working_year_number):
    return add_months_safe(period_start, LEAVE_ADVANCE_MONTHS) if working_year_number == 1 else period_start

def _entitlement_must_use_by(period_end):
    return add_years_safe(period_end, 1)

def _entitlement_period_defaults(employee, working_year_number, period_start, period_end):
    return {
        "period_start": period_start,
        "period_end": period_end,
        "entitled_days": quantize_leave_days(employee.annual_paid_leave_days),
        "available_from": _entitlement_available_from(period_start, working_year_number),
        "must_use_by": _entitlement_must_use_by(period_end),
    }

def _virtual_entitlement_period(employee, working_year_number, period_start, period_end):
    defaults = _entitlement_period_defaults(employee, working_year_number, period_start, period_end)
    return SimpleNamespace(
        id=None,
        pk=None,
        employee=employee,
        employee_id=employee.id,
        working_year_number=working_year_number,
        **defaults,
    )

def sync_employee_entitlement_periods(employee, through_date=None):
    through_date = normalize_date_value(through_date or timezone.localdate())
    if employee is None:
        return []
    date_joined = get_employee_joined_date(employee)
    if through_date < date_joined:
        return []

    period_ids = []
    for working_year_number, period_start, period_end in iter_employee_working_years(employee, through_date):
        defaults = _entitlement_period_defaults(employee, working_year_number, period_start, period_end)
        period, _ = VacationEntitlementPeriod.objects.update_or_create(
            employee=employee,
            working_year_number=working_year_number,
            defaults=defaults,
        )
        period_ids.append(period.id)

    return list(
        VacationEntitlementPeriod.objects.filter(id__in=period_ids)
        .order_by("period_start")
    )

def get_employee_entitlement_periods_for_read(employee, through_date=None):
    through_date = normalize_date_value(through_date or timezone.localdate())
    if employee is None:
        return []
    date_joined = get_employee_joined_date(employee)
    if through_date < date_joined:
        return []

    persisted_by_number = {
        period.working_year_number: period
        for period in VacationEntitlementPeriod.objects.filter(
            employee=employee,
            period_start__lte=through_date,
        ).order_by("period_start")
    }
    periods = []
    for working_year_number, period_start, period_end in iter_employee_working_years(employee, through_date):
        periods.append(
            persisted_by_number.get(working_year_number)
            or _virtual_entitlement_period(employee, working_year_number, period_start, period_end)
        )
    return periods

def get_employee_entitlement_periods_for_read_bulk(employees, through_dates):
    employees = list(employees)
    if not employees:
        return {}

    employee_ids = [employee.id for employee in employees]
    max_horizon = max(through_dates.values(), default=timezone.localdate())
    persisted_by_employee = defaultdict(dict)
    for period in VacationEntitlementPeriod.objects.filter(
        employee_id__in=employee_ids,
        period_start__lte=max_horizon,
    ).order_by("employee_id", "period_start"):
        persisted_by_employee[period.employee_id][period.working_year_number] = period

    periods_by_employee = {}
    for employee in employees:
        through_date = normalize_date_value(through_dates.get(employee.id, timezone.localdate()))
        periods = []
        if through_date >= get_employee_joined_date(employee):
            for working_year_number, period_start, period_end in iter_employee_working_years(employee, through_date):
                periods.append(
                    persisted_by_employee[employee.id].get(working_year_number)
                    or _virtual_entitlement_period(employee, working_year_number, period_start, period_end)
                )
        periods_by_employee[employee.id] = periods
    return periods_by_employee

def _period_accrued_days(period, as_of_date):
    if as_of_date < period.period_start:
        return Decimal("0.00")
    if as_of_date >= period.period_end:
        return quantize_leave_days(period.entitled_days)

    elapsed_days = (as_of_date - period.period_start).days + 1
    working_year_days = (period.period_end - period.period_start).days + 1
    return quantize_leave_days(Decimal(period.entitled_days) * Decimal(elapsed_days) / Decimal(working_year_days))

def _period_requestable_days(period, as_of_date):
    if as_of_date < period.period_start:
        return Decimal("0.00")
    if period.working_year_number == 1 and as_of_date < period.available_from:
        return _period_accrued_days(period, as_of_date)
    return quantize_leave_days(period.entitled_days)

def _schedule_item_reservation_created_at(item):
    if item.created_from_vacation_request_id and item.created_from_vacation_request is not None:
        return item.created_from_vacation_request.created_at
    if item.previous_item_id and item.previous_item is not None:
        return item.previous_item.created_at
    return item.created_at

def _source_allocation_sort_key(source, as_of_date):
    is_preview_source = source.get("marker") == "preview-request"
    if not is_preview_source and source["start_date"] <= as_of_date:
        return (
            0,
            source["start_date"],
            source["end_date"],
            source["kind"],
            source["id"],
        )

    reservation_created_at = source.get("reservation_created_at")
    return (
        1,
        reservation_created_at is None,
        reservation_created_at.isoformat() if reservation_created_at is not None else "",
        source["start_date"],
        source["end_date"],
        source["kind"],
        source["id"],
    )

def _sort_paid_ledger_sources_for_allocation(sources, as_of_date):
    as_of_date = normalize_date_value(as_of_date)
    return sorted(sources, key=lambda source: _source_allocation_sort_key(source, as_of_date))

def _collect_paid_ledger_sources(employee):
    sources = []
    requests = VacationRequest.objects.filter(
        employee=employee,
        vacation_type__in=BALANCE_AFFECTING_TYPES,
        status__in=ACTIVE_REQUEST_STATUSES,
    )
    requests = exclude_converted_paid_requests(requests, employee_ids=[employee.id])
    for request_obj in requests:
        start_date = normalize_date_value(request_obj.start_date)
        end_date = normalize_date_value(request_obj.end_date)
        sources.append(
            {
                "kind": VacationEntitlementAllocation.SOURCE_REQUEST,
                "id": request_obj.id,
                "start_date": start_date,
                "end_date": end_date,
                "days": quantize_leave_days(
                    get_chargeable_leave_days(start_date, end_date, request_obj.vacation_type)
                ),
                "state": (
                    VacationEntitlementAllocation.STATE_USED
                    if request_obj.status == VacationRequest.STATUS_APPROVED
                    else VacationEntitlementAllocation.STATE_RESERVED
                ),
                "request": request_obj,
                "schedule_item": None,
                "reservation_created_at": request_obj.created_at,
            }
        )

    schedule_items = VacationScheduleItem.objects.select_related(
        "created_from_vacation_request",
        "previous_item",
    ).filter(
        employee=employee,
        vacation_type__in=BALANCE_AFFECTING_TYPES,
        status__in=SCHEDULE_BALANCE_STATUSES,
    )
    for item in schedule_items:
        start_date = normalize_date_value(item.start_date)
        end_date = normalize_date_value(item.end_date)
        chargeable_days = item.chargeable_days or get_chargeable_leave_days(start_date, end_date, item.vacation_type)
        sources.append(
            {
                "kind": VacationEntitlementAllocation.SOURCE_SCHEDULE,
                "id": item.id,
                "start_date": start_date,
                "end_date": end_date,
                "days": quantize_leave_days(chargeable_days),
                "state": (
                    VacationEntitlementAllocation.STATE_USED
                    if item.status == VacationScheduleItem.STATUS_APPROVED
                    else VacationEntitlementAllocation.STATE_RESERVED
                ),
                "request": None,
                "schedule_item": item,
                "reservation_created_at": _schedule_item_reservation_created_at(item),
            }
        )

    return sources

def _collect_paid_ledger_sources_for_employees(employees):
    employees = list(employees)
    sources_by_employee = {employee.id: [] for employee in employees}
    if not employees:
        return sources_by_employee

    employee_ids = [employee.id for employee in employees]
    requests = VacationRequest.objects.filter(
        employee_id__in=employee_ids,
        vacation_type__in=BALANCE_AFFECTING_TYPES,
        status__in=ACTIVE_REQUEST_STATUSES,
    )
    requests = exclude_converted_paid_requests(requests, employee_ids=employee_ids)
    for request_obj in requests:
        start_date = normalize_date_value(request_obj.start_date)
        end_date = normalize_date_value(request_obj.end_date)
        sources_by_employee[request_obj.employee_id].append(
            {
                "kind": VacationEntitlementAllocation.SOURCE_REQUEST,
                "id": request_obj.id,
                "start_date": start_date,
                "end_date": end_date,
                "days": quantize_leave_days(
                    get_chargeable_leave_days(start_date, end_date, request_obj.vacation_type)
                ),
                "state": (
                    VacationEntitlementAllocation.STATE_USED
                    if request_obj.status == VacationRequest.STATUS_APPROVED
                    else VacationEntitlementAllocation.STATE_RESERVED
                ),
                "request": request_obj,
                "schedule_item": None,
                "reservation_created_at": request_obj.created_at,
            }
        )

    for item in VacationScheduleItem.objects.select_related(
        "created_from_vacation_request",
        "previous_item",
    ).filter(
        employee_id__in=employee_ids,
        vacation_type__in=BALANCE_AFFECTING_TYPES,
        status__in=SCHEDULE_BALANCE_STATUSES,
    ):
        start_date = normalize_date_value(item.start_date)
        end_date = normalize_date_value(item.end_date)
        chargeable_days = item.chargeable_days or get_chargeable_leave_days(start_date, end_date, item.vacation_type)
        sources_by_employee[item.employee_id].append(
            {
                "kind": VacationEntitlementAllocation.SOURCE_SCHEDULE,
                "id": item.id,
                "start_date": start_date,
                "end_date": end_date,
                "days": quantize_leave_days(chargeable_days),
                "state": (
                    VacationEntitlementAllocation.STATE_USED
                    if item.status == VacationScheduleItem.STATUS_APPROVED
                    else VacationEntitlementAllocation.STATE_RESERVED
                ),
                "request": None,
                "schedule_item": item,
                "reservation_created_at": _schedule_item_reservation_created_at(item),
            }
        )

    return sources_by_employee

def _normalize_excluded_schedule_item_ids(exclude_schedule_item_id=None, exclude_schedule_item_ids=None):
    excluded_ids = set(exclude_schedule_item_ids or [])
    if exclude_schedule_item_id is not None:
        excluded_ids.add(exclude_schedule_item_id)
    return excluded_ids


def _filter_paid_ledger_sources(sources, exclude_request_id=None, exclude_schedule_item_id=None, exclude_schedule_item_ids=None):
    excluded_schedule_item_ids = _normalize_excluded_schedule_item_ids(
        exclude_schedule_item_id,
        exclude_schedule_item_ids,
    )
    filtered_sources = []
    for source in sources:
        if exclude_request_id is not None and source["kind"] == VacationEntitlementAllocation.SOURCE_REQUEST:
            if source["id"] == exclude_request_id:
                continue
        if excluded_schedule_item_ids and source["kind"] == VacationEntitlementAllocation.SOURCE_SCHEDULE:
            if source["id"] in excluded_schedule_item_ids:
                continue
        filtered_sources.append(source)
    return filtered_sources

def _source_horizon(as_of_date, sources):
    return max([as_of_date, *[source["end_date"] for source in sources]], default=as_of_date)

def _allocation_effective_state(allocation, as_of_date):
    if allocation.state != VacationEntitlementAllocation.STATE_USED:
        return VacationEntitlementAllocation.STATE_RESERVED
    if allocation.vacation_request is not None:
        source_start = normalize_date_value(allocation.vacation_request.start_date)
    elif allocation.schedule_item is not None:
        source_start = normalize_date_value(allocation.schedule_item.start_date)
    else:
        return VacationEntitlementAllocation.STATE_RESERVED
    return (
        VacationEntitlementAllocation.STATE_USED
        if source_start <= as_of_date
        else VacationEntitlementAllocation.STATE_RESERVED
    )

def _build_allocation_rows(employee, periods, sources, strict=True, for_save=False):
    allocated_by_period = {period.working_year_number: Decimal("0.00") for period in periods}
    allocations = []

    for source in sources:
        days_left = quantize_leave_days(source["days"])
        if days_left <= 0:
            continue

        for period in periods:
            period_key = period.working_year_number
            source_usable_days = _source_usable_days_for_period(source, period)
            if source_usable_days <= 0:
                continue
            requestable_on_start = _period_requestable_days(period, source["start_date"])
            period_left = quantize_leave_days(requestable_on_start - allocated_by_period[period_key])
            if period_left <= 0:
                continue

            allocated_days = min(days_left, period_left, source_usable_days)
            if allocated_days <= 0:
                continue

            if for_save:
                if period.id is None:
                    raise ValidationError("Невозможно сохранить распределение без рабочего года отпуска.")
                allocations.append(
                    VacationEntitlementAllocation(
                        employee=employee,
                        entitlement_period=period,
                        vacation_request=source["request"],
                        schedule_item=source["schedule_item"],
                        source_kind=source["kind"],
                        state=source["state"],
                        allocated_days=allocated_days,
                    )
                )
            else:
                allocations.append(
                    SimpleNamespace(
                        employee=employee,
                        employee_id=employee.id,
                        entitlement_period=period,
                        entitlement_period_id=period.id,
                        vacation_request=source["request"],
                        vacation_request_id=source["request"].id if source["request"] is not None else None,
                        schedule_item=source["schedule_item"],
                        schedule_item_id=source["schedule_item"].id if source["schedule_item"] is not None else None,
                        source_kind=source["kind"],
                        source_marker=source.get("marker"),
                        state=source["state"],
                        allocated_days=allocated_days,
                        period_balance_before=period_left,
                        period_balance_after=quantize_leave_days(period_left - allocated_days),
                    )
                )
            allocated_by_period[period_key] = quantize_leave_days(allocated_by_period[period_key] + allocated_days)
            days_left = quantize_leave_days(days_left - allocated_days)
            if days_left <= 0:
                break

        if days_left > 0 and strict:
            raise ValidationError(
                "Недостаточно отпускных прав по рабочим годам для оплачиваемого отпуска."
            )

    return allocations


def _source_usable_days_for_period(source, period):
    deadline = normalize_date_value(period.must_use_by)
    source_start = normalize_date_value(source["start_date"])
    source_end = normalize_date_value(source["end_date"])
    source_days = quantize_leave_days(source["days"])
    if source_end <= deadline:
        return source_days
    usable_end = min(source_end, deadline)
    if usable_end < source_start:
        return Decimal("0.00")
    return min(
        source_days,
        quantize_leave_days(get_chargeable_leave_days(source_start, usable_end, "paid")),
    )

def _empty_entitlement_source_preview(label="Оплачиваемый баланс не списывается"):
    return {
        "label": label,
        "allocations": [],
    }

def get_employee_entitlement_source_preview(
    employee,
    start_date,
    end_date,
    vacation_type,
    exclude_request_id=None,
    exclude_schedule_item_id=None,
):
    start_date = normalize_date_value(start_date)
    end_date = normalize_date_value(end_date)
    if vacation_type not in BALANCE_AFFECTING_TYPES:
        return _empty_entitlement_source_preview()

    chargeable_days = quantize_leave_days(get_chargeable_leave_days(start_date, end_date, vacation_type))
    if chargeable_days <= 0:
        return _empty_entitlement_source_preview("Оплачиваемые дни не списываются")

    sources = _filter_paid_ledger_sources(
        _collect_paid_ledger_sources(employee),
        exclude_request_id=exclude_request_id,
        exclude_schedule_item_id=exclude_schedule_item_id,
    )
    source_horizon = _source_horizon(end_date, sources)
    periods = get_employee_entitlement_periods_for_read(employee, source_horizon)
    preview_marker = "preview-request"
    preview_sources = [
        *sources,
        {
            "kind": VacationEntitlementAllocation.SOURCE_REQUEST,
            "id": 0,
            "start_date": start_date,
            "end_date": end_date,
            "days": chargeable_days,
            "state": VacationEntitlementAllocation.STATE_RESERVED,
            "request": None,
            "schedule_item": None,
            "marker": preview_marker,
            "reservation_created_at": None,
        },
    ]
    preview_sources = _sort_paid_ledger_sources_for_allocation(preview_sources, start_date)

    try:
        preview_allocations = _build_allocation_rows(employee, periods, preview_sources, strict=True, for_save=False)
    except ValidationError:
        return _empty_entitlement_source_preview("Недостаточно доступных дней для списания")

    rows = []
    for allocation in preview_allocations:
        if getattr(allocation, "source_marker", None) != preview_marker:
            continue
        period = allocation.entitlement_period
        days = quantize_leave_days(allocation.allocated_days)
        rows.append(
            {
                "working_year_number": period.working_year_number,
                "period_label": f"{format_ru_date(period.period_start)} - {format_ru_date(period.period_end)}",
                "period_start": period.period_start,
                "period_end": period.period_end,
                "days": days,
                "balance_before": quantize_leave_days(allocation.period_balance_before),
                "balance_after": quantize_leave_days(allocation.period_balance_after),
            }
        )

    if not rows:
        return _empty_entitlement_source_preview("Недостаточно доступных дней для списания")

    label = (
        f"Дни будут списаны из рабочего года {rows[0]['period_label']}"
        if len(rows) == 1
        else "Дни будут списаны из нескольких рабочих годов"
    )
    return {
        "label": label,
        "allocations": rows,
    }

@transaction.atomic
def rebuild_employee_leave_ledger(employee, as_of_date=None, strict=True):
    if employee is None:
        return []

    as_of_date = normalize_date_value(as_of_date or timezone.localdate())
    sources = _collect_paid_ledger_sources(employee)
    source_horizon = _source_horizon(as_of_date, sources)
    periods = sync_employee_entitlement_periods(employee, source_horizon)
    sources = _sort_paid_ledger_sources_for_allocation(sources, as_of_date)
    allocations = _build_allocation_rows(employee, periods, sources, strict=strict, for_save=True)

    VacationEntitlementAllocation.objects.filter(employee=employee).delete()
    if allocations:
        VacationEntitlementAllocation.objects.bulk_create(allocations)

    return periods

def _ensure_employee_leave_ledger(employee, as_of_date=None):
    as_of_date = normalize_date_value(as_of_date or timezone.localdate())
    sources = _collect_paid_ledger_sources(employee)
    source_horizon = _source_horizon(as_of_date, sources)
    return get_employee_entitlement_periods_for_read(employee, source_horizon)

def _calculate_ledger_totals(employee, as_of_date, sources, periods, *, strict=True):
    sources = _sort_paid_ledger_sources_for_allocation(sources, as_of_date)
    allocations = _build_allocation_rows(employee, periods, sources, strict=strict, for_save=False)

    allocations_by_period = {
        period.working_year_number: {
            VacationEntitlementAllocation.STATE_USED: Decimal("0.00"),
            VacationEntitlementAllocation.STATE_RESERVED: Decimal("0.00"),
        }
        for period in periods
    }
    for allocation in allocations:
        period_key = allocation.entitlement_period.working_year_number
        effective_state = _allocation_effective_state(allocation, as_of_date)
        allocations_by_period[period_key][effective_state] += Decimal(allocation.allocated_days)

    accrued = Decimal("0.00")
    requestable = Decimal("0.00")
    used = Decimal("0.00")
    reserved = Decimal("0.00")
    for period in periods:
        period_requestable = _period_requestable_days(period, as_of_date)
        accrued += _period_accrued_days(period, as_of_date)
        requestable += period_requestable
        if period_requestable <= 0:
            continue
        used += allocations_by_period[period.working_year_number][VacationEntitlementAllocation.STATE_USED]
        reserved += allocations_by_period[period.working_year_number][VacationEntitlementAllocation.STATE_RESERVED]

    manual_adjustment = quantize_leave_days(employee.manual_leave_adjustment_days)
    accrued = quantize_leave_days(accrued)
    requestable = quantize_leave_days(requestable)
    used = quantize_leave_days(used)
    reserved = quantize_leave_days(reserved)
    accrued_balance = quantize_leave_days(accrued + manual_adjustment - used - reserved)
    available = quantize_leave_days(max(requestable + manual_adjustment - used - reserved, Decimal("0")))
    advance_available = quantize_leave_days(max(available - max(accrued_balance, Decimal("0")), Decimal("0")))

    return {
        "annual_entitlement": quantize_leave_days(employee.annual_paid_leave_days),
        "accrued": accrued,
        "requestable": requestable,
        "reserved": reserved,
        "used": used,
        "accrued_balance": accrued_balance,
        "advance_available": advance_available,
        "available": available,
        "manual_adjustment": manual_adjustment,
    }

def _ledger_totals(
    employee,
    as_of_date=None,
    exclude_request_id=None,
    exclude_schedule_item_id=None,
    exclude_schedule_item_ids=None,
    strict=True,
):
    as_of_date = normalize_date_value(as_of_date or timezone.localdate())
    sources = _filter_paid_ledger_sources(
        _collect_paid_ledger_sources(employee),
        exclude_request_id=exclude_request_id,
        exclude_schedule_item_id=exclude_schedule_item_id,
        exclude_schedule_item_ids=exclude_schedule_item_ids,
    )
    periods = get_employee_entitlement_periods_for_read(employee, _source_horizon(as_of_date, sources))
    return _calculate_ledger_totals(employee, as_of_date, sources, periods, strict=strict)

def get_employee_used_paid_days(
    employee,
    as_of_date=None,
    exclude_request_id=None,
    exclude_schedule_item_id=None,
    exclude_schedule_item_ids=None,
):
    return _ledger_totals(
        employee,
        as_of_date=as_of_date,
        exclude_request_id=exclude_request_id,
        exclude_schedule_item_id=exclude_schedule_item_id,
        exclude_schedule_item_ids=exclude_schedule_item_ids,
    )["used"]

def get_employee_reserved_paid_days(
    employee,
    as_of_date=None,
    exclude_request_id=None,
    exclude_schedule_item_id=None,
    exclude_schedule_item_ids=None,
):
    return _ledger_totals(
        employee,
        as_of_date=as_of_date,
        exclude_request_id=exclude_request_id,
        exclude_schedule_item_id=exclude_schedule_item_id,
        exclude_schedule_item_ids=exclude_schedule_item_ids,
    )["reserved"]

def get_employee_accrued_leave(employee, as_of_date=None):
    as_of_date = normalize_date_value(as_of_date or timezone.localdate())
    if as_of_date < get_employee_joined_date(employee):
        return Decimal("0.00")
    periods = get_employee_entitlement_periods_for_read(employee, as_of_date)
    return quantize_leave_days(sum((_period_accrued_days(period, as_of_date) for period in periods), Decimal("0.00")))

def get_employee_requestable_leave(employee, as_of_date=None):
    as_of_date = normalize_date_value(as_of_date or timezone.localdate())
    if as_of_date < get_employee_joined_date(employee):
        return Decimal("0.00")
    periods = get_employee_entitlement_periods_for_read(employee, as_of_date)
    return quantize_leave_days(sum((_period_requestable_days(period, as_of_date) for period in periods), Decimal("0.00")))

def get_employee_available_balance(
    employee,
    as_of_date=None,
    exclude_request_id=None,
    exclude_schedule_item_id=None,
    exclude_schedule_item_ids=None,
):
    return _ledger_totals(
        employee,
        as_of_date=as_of_date,
        exclude_request_id=exclude_request_id,
        exclude_schedule_item_id=exclude_schedule_item_id,
        exclude_schedule_item_ids=exclude_schedule_item_ids,
    )["available"]

def get_employee_leave_summary(employee, as_of_date=None):
    try:
        return _ledger_totals(employee, as_of_date=as_of_date)
    except ValidationError:
        summary = _ledger_totals(employee, as_of_date=as_of_date, strict=False)
        summary["has_allocation_error"] = True
        return summary

def get_employee_leave_summaries(employees, as_of_date=None):
    as_of_date = normalize_date_value(as_of_date or timezone.localdate())
    employees = list(employees)
    if not employees:
        return {}
    return get_employee_list_leave_summaries(employees, as_of_date)

def get_employee_list_leave_summaries(employees, as_of_date=None):
    as_of_date = normalize_date_value(as_of_date or timezone.localdate())
    employees = list(employees)
    if not employees:
        return {}
    sources_by_employee = _collect_paid_ledger_sources_for_employees(employees)
    horizons = {
        employee.id: _source_horizon(as_of_date, sources_by_employee[employee.id])
        for employee in employees
    }
    periods_by_employee = get_employee_entitlement_periods_for_read_bulk(employees, horizons)
    summaries = {}
    for employee in employees:
        sources = sources_by_employee[employee.id]
        periods = periods_by_employee[employee.id]
        try:
            summaries[employee.id] = _calculate_ledger_totals(employee, as_of_date, sources, periods)
        except ValidationError:
            summaries[employee.id] = _calculate_ledger_totals(employee, as_of_date, sources, periods, strict=False)
            summaries[employee.id]["has_allocation_error"] = True
    return summaries

def _entitlement_status_for_row(period, remaining_days, as_of_date):
    if as_of_date < period.period_start:
        return "future", "Будущий"
    if remaining_days <= 0:
        return "closed", "Закрыт"
    if period.must_use_by < as_of_date:
        return "overdue", "Просрочен"
    if period.period_start <= as_of_date <= period.period_end:
        return "current", "Текущий"
    if period.must_use_by <= as_of_date + timedelta(days=90):
        return "attention", "Скоро срок"
    return "remaining", "Остаток"

def _build_employee_entitlement_rows(employee, periods, sources, as_of_date, limit=6):
    sources = _sort_paid_ledger_sources_for_allocation(sources, as_of_date)
    try:
        allocations = _build_allocation_rows(employee, periods, sources, strict=True, for_save=False)
    except ValidationError:
        allocations = _build_allocation_rows(employee, periods, sources, strict=False, for_save=False)

    totals_by_period = {
        period.working_year_number: {
            VacationEntitlementAllocation.STATE_USED: Decimal("0.00"),
            VacationEntitlementAllocation.STATE_RESERVED: Decimal("0.00"),
        }
        for period in periods
    }
    for allocation in allocations:
        period_key = allocation.entitlement_period.working_year_number
        effective_state = _allocation_effective_state(allocation, as_of_date)
        totals_by_period[period_key][effective_state] += Decimal(allocation.allocated_days)

    rows = []
    for period in periods:
        period_key = period.working_year_number
        used_days = quantize_leave_days(totals_by_period[period_key][VacationEntitlementAllocation.STATE_USED])
        reserved_days = quantize_leave_days(totals_by_period[period_key][VacationEntitlementAllocation.STATE_RESERVED])
        remaining_days = quantize_leave_days(max(Decimal(period.entitled_days) - used_days - reserved_days, Decimal("0")))
        status_key, status_label = _entitlement_status_for_row(period, remaining_days, as_of_date)
        rows.append(
            {
                "id": period.id,
                "working_year_number": period.working_year_number,
                "period_label": f"{format_ru_date(period.period_start)} - {format_ru_date(period.period_end)}",
                "period_start": period.period_start,
                "period_end": period.period_end,
                "entitled_days": quantize_leave_days(period.entitled_days),
                "used_days": used_days,
                "reserved_days": reserved_days,
                "remaining_days": remaining_days,
                "available_now_days": quantize_leave_days(
                    max(_period_requestable_days(period, as_of_date) - used_days - reserved_days, Decimal("0"))
                ),
                "available_from": period.available_from,
                "must_use_by": period.must_use_by,
                "status_key": status_key,
                "status_label": status_label,
            }
        )

    visible_rows = [
        row
        for row in rows
        if not (
            row["status_key"] == "future"
            and row["used_days"] <= 0
            and row["reserved_days"] <= 0
        )
    ]
    problem_rows = [
        row
        for row in visible_rows
        if row["status_key"] in {"overdue", "attention"} and row["remaining_days"] > 0
    ]
    recent_rows = visible_rows[-limit:]
    selected = {row["working_year_number"]: row for row in [*problem_rows, *recent_rows]}
    return sorted(selected.values(), key=lambda row: row["period_start"], reverse=True)


def get_employee_entitlement_rows(employee, as_of_date=None, limit=6):
    as_of_date = normalize_date_value(as_of_date or timezone.localdate())
    sources = _collect_paid_ledger_sources(employee)
    periods = get_employee_entitlement_periods_for_read(employee, _source_horizon(as_of_date, sources))
    return _build_employee_entitlement_rows(employee, periods, sources, as_of_date, limit=limit)


def get_employee_entitlement_rows_bulk(employees, as_of_date=None, limit=6):
    as_of_date = normalize_date_value(as_of_date or timezone.localdate())
    employees = list(employees)
    if not employees:
        return {}

    sources_by_employee = _collect_paid_ledger_sources_for_employees(employees)
    horizons = {
        employee.id: _source_horizon(as_of_date, sources_by_employee[employee.id])
        for employee in employees
    }
    periods_by_employee = get_employee_entitlement_periods_for_read_bulk(employees, horizons)
    return {
        employee.id: _build_employee_entitlement_rows(
            employee,
            periods_by_employee[employee.id],
            sources_by_employee[employee.id],
            as_of_date,
            limit=limit,
        )
        for employee in employees
    }

def get_employee_remaining_balance(employee):
    return get_employee_available_balance(employee)
