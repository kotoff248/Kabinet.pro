import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache

import holidays

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as date_format

from apps.employees.models import Employees
from apps.leave.models import (
    DepartmentWorkload,
    VacationEntitlementAllocation,
    VacationEntitlementPeriod,
    VacationRequest,
    VacationSchedule,
    VacationScheduleChangeRequest,
    VacationScheduleItem,
)


ACTIVE_REQUEST_STATUSES = (
    VacationRequest.STATUS_PENDING,
    VacationRequest.STATUS_APPROVED,
)
CALENDAR_VISIBLE_STATUSES = (
    VacationRequest.STATUS_PENDING,
    VacationRequest.STATUS_APPROVED,
    VacationRequest.STATUS_REJECTED,
)
BALANCE_AFFECTING_TYPES = {"paid"}
SCHEDULE_BALANCE_STATUSES = (
    VacationScheduleItem.STATUS_PLANNED,
    VacationScheduleItem.STATUS_APPROVED,
)
SCHEDULE_STATUS_TO_CALENDAR_STATUS = {
    VacationScheduleItem.STATUS_DRAFT: VacationRequest.STATUS_PENDING,
    VacationScheduleItem.STATUS_PLANNED: VacationRequest.STATUS_PENDING,
    VacationScheduleItem.STATUS_APPROVED: VacationRequest.STATUS_APPROVED,
    VacationScheduleItem.STATUS_TRANSFERRED: VacationRequest.STATUS_APPROVED,
    VacationScheduleItem.STATUS_CANCELLED: VacationRequest.STATUS_REJECTED,
}
DISPLAY_FREE = "free"
DISPLAY_MIXED = "mixed"
DISPLAY_SCHEDULE_PLANNED = "schedule-planned"
DISPLAY_SCHEDULE_APPROVED = "schedule-approved"
DISPLAY_SCHEDULE_TRANSFERRED = "schedule-transferred"
DISPLAY_SCHEDULE_CANCELLED = "schedule-cancelled"
DISPLAY_REQUEST_PENDING = "request-pending"
DISPLAY_REQUEST_APPROVED = "request-approved"
DISPLAY_REQUEST_REJECTED = "request-rejected"

SCHEDULE_STATUS_TO_DISPLAY_STATUS = {
    VacationScheduleItem.STATUS_DRAFT: DISPLAY_SCHEDULE_PLANNED,
    VacationScheduleItem.STATUS_PLANNED: DISPLAY_SCHEDULE_PLANNED,
    VacationScheduleItem.STATUS_APPROVED: DISPLAY_SCHEDULE_APPROVED,
    VacationScheduleItem.STATUS_TRANSFERRED: DISPLAY_SCHEDULE_TRANSFERRED,
    VacationScheduleItem.STATUS_CANCELLED: DISPLAY_SCHEDULE_CANCELLED,
}
REQUEST_STATUS_TO_DISPLAY_STATUS = {
    VacationRequest.STATUS_PENDING: DISPLAY_REQUEST_PENDING,
    VacationRequest.STATUS_APPROVED: DISPLAY_REQUEST_APPROVED,
    VacationRequest.STATUS_REJECTED: DISPLAY_REQUEST_REJECTED,
}
DISPLAY_STATUS_UI = {
    DISPLAY_SCHEDULE_PLANNED: {
        "label": "Запланировано",
        "source_label": "Годовой график",
        "css_class": DISPLAY_SCHEDULE_PLANNED,
        "display_type": "schedule",
    },
    DISPLAY_SCHEDULE_APPROVED: {
        "label": "График утвержден",
        "source_label": "Годовой график",
        "css_class": DISPLAY_SCHEDULE_APPROVED,
        "display_type": "schedule",
    },
    DISPLAY_SCHEDULE_TRANSFERRED: {
        "label": "Перенесено",
        "source_label": "Перенос",
        "css_class": DISPLAY_SCHEDULE_TRANSFERRED,
        "display_type": "schedule",
    },
    DISPLAY_SCHEDULE_CANCELLED: {
        "label": "Отменено",
        "source_label": "Годовой график",
        "css_class": DISPLAY_SCHEDULE_CANCELLED,
        "display_type": "schedule",
    },
    DISPLAY_REQUEST_PENDING: {
        "label": "Заявка ожидает",
        "source_label": "Заявка",
        "css_class": DISPLAY_REQUEST_PENDING,
        "display_type": "request",
    },
    DISPLAY_REQUEST_APPROVED: {
        "label": "Внеплановая заявка одобрена",
        "source_label": "Заявка",
        "css_class": DISPLAY_REQUEST_APPROVED,
        "display_type": "request",
    },
    DISPLAY_REQUEST_REJECTED: {
        "label": "Заявка отклонена",
        "source_label": "Заявка",
        "css_class": DISPLAY_REQUEST_REJECTED,
        "display_type": "request",
    },
    DISPLAY_FREE: {"label": "Свободно", "source_label": "", "css_class": DISPLAY_FREE, "display_type": "free"},
    DISPLAY_MIXED: {
        "label": "Смешанный период",
        "source_label": "",
        "css_class": DISPLAY_MIXED,
        "display_type": "mixed",
    },
}
REQUEST_STATUS_UI = {
    VacationRequest.STATUS_APPROVED: {"label": "Одобрено", "icon": "check_circle", "css_class": "approved"},
    VacationRequest.STATUS_PENDING: {"label": "В ожидании", "icon": "watch_later", "css_class": "pending"},
    VacationRequest.STATUS_REJECTED: {"label": "Отклонено", "icon": "error", "css_class": "rejected"},
}
RUSSIAN_MONTH_NAMES = [
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
]
RUSSIAN_MONTH_SHORT_NAMES = [
    "Янв",
    "Фев",
    "Мар",
    "Апр",
    "Май",
    "Июн",
    "Июл",
    "Авг",
    "Сен",
    "Окт",
    "Ноя",
    "Дек",
]
VACATION_STATUS_META = {
    VacationRequest.STATUS_REJECTED: {"label": "Отклонено", "icon": "error"},
    VacationRequest.STATUS_APPROVED: {"label": "Одобрено", "icon": "check_circle"},
    VacationRequest.STATUS_PENDING: {"label": "В ожидании", "icon": "watch_later"},
    "free": {"label": "Свободно", "icon": "event_available"},
    "mixed": {"label": "Смешанный период", "icon": "layers"},
}
VACATION_STATUS_META.update(
    {
        display_status: {"label": meta["label"], "icon": "event"}
        for display_status, meta in DISPLAY_STATUS_UI.items()
    }
)
STATUS_PRIORITY = {
    "free": 0,
    VacationRequest.STATUS_REJECTED: 1,
    VacationRequest.STATUS_PENDING: 2,
    VacationRequest.STATUS_APPROVED: 3,
}
DISPLAY_STATUS_PRIORITY = {
    DISPLAY_FREE: 0,
    DISPLAY_SCHEDULE_CANCELLED: 1,
    DISPLAY_REQUEST_REJECTED: 2,
    DISPLAY_SCHEDULE_TRANSFERRED: 3,
    DISPLAY_REQUEST_PENDING: 4,
    DISPLAY_SCHEDULE_PLANNED: 5,
    DISPLAY_REQUEST_APPROVED: 6,
    DISPLAY_SCHEDULE_APPROVED: 7,
}
WEEKDAY_SHORT_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

LEAVE_DAY_QUANTIZER = Decimal("0.01")
LEAVE_ADVANCE_MONTHS = 6
VACATION_METRIC_SYNC_ENABLED = True


def get_vacation_requests_queryset():
    return VacationRequest.objects.select_related("employee", "employee__department")


def format_ru_date(value):
    return value.strftime("%d.%m.%Y")


def format_period_label(start_date, end_date):
    return f"{format_ru_date(start_date)} - {format_ru_date(end_date)}"


def add_years_safe(value, years):
    target_year = value.year + years
    last_day = calendar.monthrange(target_year, value.month)[1]
    return value.replace(year=target_year, day=min(value.day, last_day))


def add_months_safe(value, months):
    total_months = (value.year * 12 + (value.month - 1)) + months
    target_year = total_months // 12
    target_month = total_months % 12 + 1
    last_day = calendar.monthrange(target_year, target_month)[1]
    return value.replace(year=target_year, month=target_month, day=min(value.day, last_day))


def quantize_leave_days(value):
    return Decimal(value).quantize(LEAVE_DAY_QUANTIZER, rounding=ROUND_HALF_UP)


def normalize_date_value(value):
    if isinstance(value, str):
        return date.fromisoformat(value)
    return value.date() if isinstance(value, datetime) else value


def get_employee_joined_date(employee):
    return normalize_date_value(employee.date_joined)


def set_vacation_metric_sync_enabled(enabled):
    global VACATION_METRIC_SYNC_ENABLED
    previous_value = VACATION_METRIC_SYNC_ENABLED
    VACATION_METRIC_SYNC_ENABLED = enabled
    return previous_value


def iterate_dates(start_date, end_date):
    current_date = start_date
    while current_date <= end_date:
        yield current_date
        current_date += timedelta(days=1)


def get_month_range(start_date, end_date):
    current_date = start_date.replace(day=1)
    target_date = end_date.replace(day=1)
    while current_date <= target_date:
        yield current_date
        current_date = (current_date + timedelta(days=32)).replace(day=1)


def get_month_end(month_start):
    last_day = calendar.monthrange(month_start.year, month_start.month)[1]
    return month_start.replace(day=last_day)


def clip_period_to_range(start_date, end_date, range_start, range_end):
    clipped_start = max(start_date, range_start)
    clipped_end = min(end_date, range_end)
    if clipped_start > clipped_end:
        return None
    return clipped_start, clipped_end


def get_overlap_days(start_date, end_date, range_start, range_end):
    clipped_period = clip_period_to_range(start_date, end_date, range_start, range_end)
    if clipped_period is None:
        return 0
    clipped_start, clipped_end = clipped_period
    return (clipped_end - clipped_start).days + 1


def get_requested_days(start_date, end_date):
    return (end_date - start_date).days + 1


def get_russian_holiday_dates(start_date, end_date):
    if end_date < start_date:
        return set()

    holiday_dates = set()
    for year in range(start_date.year, end_date.year + 1):
        year_start = date(year, 1, 1)
        year_end = date(year, 12, 31)
        range_start = max(start_date, year_start)
        range_end = min(end_date, year_end)
        if range_start > range_end:
            continue

        holiday_dates.update(
            current_date
            for current_date in _get_russian_holiday_dates_for_year(year)
            if range_start <= current_date <= range_end
        )
    return holiday_dates


def get_russian_holiday_iso_dates(years):
    holiday_dates = set()
    for year in years:
        holiday_dates.update(_get_russian_holiday_dates_for_year(year))
    return sorted(current_date.isoformat() for current_date in holiday_dates)


@lru_cache(maxsize=None)
def _get_russian_holiday_dates_for_year(year):
    holiday_calendar = holidays.country_holidays("RU", years=[year])
    return frozenset(holiday_calendar.keys())


def get_chargeable_leave_days(start_date, end_date, vacation_type):
    if vacation_type not in BALANCE_AFFECTING_TYPES:
        return 0
    holiday_days = len(get_russian_holiday_dates(start_date, end_date))
    return max(get_requested_days(start_date, end_date) - holiday_days, 0)


def get_vacation_day_cost(vacation_type, start_date, end_date):
    return get_chargeable_leave_days(start_date, end_date, vacation_type)


def get_converted_paid_request_ids_queryset(employee_ids=None, start_date=None, end_date=None):
    queryset = VacationScheduleItem.objects.filter(
        created_from_vacation_request__isnull=False,
    )
    if employee_ids is not None:
        queryset = queryset.filter(employee_id__in=employee_ids)
    if start_date is not None:
        queryset = queryset.filter(end_date__gte=start_date)
    if end_date is not None:
        queryset = queryset.filter(start_date__lte=end_date)
    return queryset.values_list("created_from_vacation_request_id", flat=True)


def exclude_converted_paid_requests(queryset, employee_ids=None, start_date=None, end_date=None):
    converted_request_ids = get_converted_paid_request_ids_queryset(
        employee_ids=employee_ids,
        start_date=start_date,
        end_date=end_date,
    )
    return queryset.exclude(
        vacation_type="paid",
        status=VacationRequest.STATUS_APPROVED,
        id__in=converted_request_ids,
    )


def _get_schedule_approval_cutoff(schedule):
    if schedule and schedule.approved_at:
        return timezone.localtime(schedule.approved_at).date()
    if schedule:
        return date(schedule.year - 1, 12, 31)
    return None


def get_paid_request_eligibility_for_year(employee, year, as_of_date=None):
    if employee is None or employee.is_service_account or not employee.is_active_employee:
        return False, "Оплачиваемый отпуск недоступен для служебной или архивной учетной записи."

    schedule = VacationSchedule.objects.filter(year=year).first()
    if schedule is None:
        return False, "Годовой график за выбранный год ещё не утверждён."
    if schedule.status not in {VacationSchedule.STATUS_APPROVED, VacationSchedule.STATUS_ARCHIVED}:
        return False, "Оплачиваемый отпуск доступен только после утверждения годового графика."

    balance_check_date = normalize_date_value(as_of_date or timezone.localdate())
    available_from = add_months_safe(employee.date_joined, LEAVE_ADVANCE_MONTHS)
    if balance_check_date < available_from:
        return False, "Оплачиваемый отпуск доступен после шести месяцев работы."

    available_balance = get_employee_available_balance(employee, as_of_date=balance_check_date)
    if available_balance > 0:
        return True, "Можно запросить оплачиваемый отпуск из свободного остатка. Заявка пройдёт проверку баланса, пересечений и нагрузки отдела."

    approval_cutoff = _get_schedule_approval_cutoff(schedule)
    if approval_cutoff is None or employee.date_joined <= approval_cutoff:
        return False, "Свободного оплачиваемого остатка нет. Изменение уже запланированного отпуска оформляется через перенос графика."

    has_schedule_item = VacationScheduleItem.objects.filter(
        employee=employee,
        schedule__year=year,
        status__in=VacationScheduleItem.ACTIVE_STATUSES,
    ).exists()
    if has_schedule_item:
        return False, "У сотрудника нет свободного оплачиваемого остатка; отпуск уже занят годовым графиком."

    return True, "Оплачиваемый отпуск вне графика доступен новичку, принятому после утверждения графика."


def get_paid_exception_eligibility_for_year(employee, year):
    return get_paid_request_eligibility_for_year(employee, year)


def validate_paid_exception_request(employee, start_date, end_date):
    if start_date.year != end_date.year:
        raise ValidationError("Оплачиваемая заявка должна быть в пределах одного календарного года.")

    is_allowed, reason = get_paid_request_eligibility_for_year(employee, start_date.year, as_of_date=start_date)
    if not is_allowed:
        raise ValidationError(reason)

    available_from = add_months_safe(employee.date_joined, LEAVE_ADVANCE_MONTHS)
    if start_date < available_from:
        raise ValidationError("Оплачиваемый отпуск доступен после шести месяцев работы.")


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


def sync_employee_entitlement_periods(employee, through_date=None):
    through_date = normalize_date_value(through_date or timezone.localdate())
    if employee is None:
        return []
    date_joined = get_employee_joined_date(employee)
    if through_date < date_joined:
        return []

    period_ids = []
    for working_year_number, period_start, period_end in iter_employee_working_years(employee, through_date):
        defaults = {
            "period_start": period_start,
            "period_end": period_end,
            "entitled_days": quantize_leave_days(employee.annual_paid_leave_days),
            "available_from": _entitlement_available_from(period_start, working_year_number),
            "must_use_by": _entitlement_must_use_by(period_end),
        }
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
            }
        )

    schedule_items = VacationScheduleItem.objects.filter(
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
            }
        )

    sources.sort(key=lambda source: (source["start_date"], source["end_date"], source["kind"], source["id"]))
    return sources


def _source_state_for_date(source, as_of_date):
    if source["state"] != VacationEntitlementAllocation.STATE_USED:
        return VacationEntitlementAllocation.STATE_RESERVED
    return (
        VacationEntitlementAllocation.STATE_USED
        if source["start_date"] <= as_of_date
        else VacationEntitlementAllocation.STATE_RESERVED
    )


@transaction.atomic
def rebuild_employee_leave_ledger(employee, as_of_date=None, strict=True):
    if employee is None:
        return []

    as_of_date = normalize_date_value(as_of_date or timezone.localdate())
    sources = _collect_paid_ledger_sources(employee)
    source_horizon = max([as_of_date, *[source["end_date"] for source in sources]], default=as_of_date)
    periods = sync_employee_entitlement_periods(employee, source_horizon)

    VacationEntitlementAllocation.objects.filter(employee=employee).delete()
    allocated_by_period = {period.id: Decimal("0.00") for period in periods}
    allocations = []

    for source in sources:
        days_left = quantize_leave_days(source["days"])
        if days_left <= 0:
            continue

        for period in periods:
            requestable_on_start = _period_requestable_days(period, source["start_date"])
            period_left = quantize_leave_days(requestable_on_start - allocated_by_period[period.id])
            if period_left <= 0:
                continue

            allocated_days = min(days_left, period_left)
            if allocated_days <= 0:
                continue

            allocations.append(
                VacationEntitlementAllocation(
                    employee=employee,
                    entitlement_period=period,
                    vacation_request=source["request"],
                    schedule_item=source["schedule_item"],
                    source_kind=source["kind"],
                    state=_source_state_for_date(source, as_of_date),
                    allocated_days=allocated_days,
                )
            )
            allocated_by_period[period.id] = quantize_leave_days(allocated_by_period[period.id] + allocated_days)
            days_left = quantize_leave_days(days_left - allocated_days)
            if days_left <= 0:
                break

        if days_left > 0 and strict:
            raise ValidationError(
                "Недостаточно отпускных прав по рабочим годам для оплачиваемого отпуска."
            )

    if allocations:
        VacationEntitlementAllocation.objects.bulk_create(allocations)

    return periods


def _ensure_employee_leave_ledger(employee, as_of_date=None):
    as_of_date = normalize_date_value(as_of_date or timezone.localdate())
    sources = _collect_paid_ledger_sources(employee)
    source_horizon = max([as_of_date, *[source["end_date"] for source in sources]], default=as_of_date)
    has_allocations = VacationEntitlementAllocation.objects.filter(employee=employee).exists()

    if as_of_date != timezone.localdate() or (sources and not has_allocations):
        return rebuild_employee_leave_ledger(employee, as_of_date=as_of_date)

    return sync_employee_entitlement_periods(employee, source_horizon)


def _ledger_totals(employee, as_of_date=None, exclude_request_id=None, exclude_schedule_item_id=None):
    as_of_date = normalize_date_value(as_of_date or timezone.localdate())
    periods = _ensure_employee_leave_ledger(employee, as_of_date=as_of_date)
    period_ids = [period.id for period in periods]
    allocations = VacationEntitlementAllocation.objects.filter(
        employee=employee,
        entitlement_period_id__in=period_ids,
    )
    if exclude_request_id is not None:
        allocations = allocations.exclude(vacation_request_id=exclude_request_id)
    if exclude_schedule_item_id is not None:
        allocations = allocations.exclude(schedule_item_id=exclude_schedule_item_id)

    allocations_by_period = {
        period.id: {
            VacationEntitlementAllocation.STATE_USED: Decimal("0.00"),
            VacationEntitlementAllocation.STATE_RESERVED: Decimal("0.00"),
        }
        for period in periods
    }
    for allocation in allocations:
        allocations_by_period[allocation.entitlement_period_id][allocation.state] += Decimal(allocation.allocated_days)

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
        used += allocations_by_period[period.id][VacationEntitlementAllocation.STATE_USED]
        reserved += allocations_by_period[period.id][VacationEntitlementAllocation.STATE_RESERVED]

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


def get_employee_used_paid_days(employee, as_of_date=None):
    return _ledger_totals(employee, as_of_date=as_of_date)["used"]


def get_employee_reserved_paid_days(employee, as_of_date=None, exclude_request_id=None, exclude_schedule_item_id=None):
    return _ledger_totals(
        employee,
        as_of_date=as_of_date,
        exclude_request_id=exclude_request_id,
        exclude_schedule_item_id=exclude_schedule_item_id,
    )["reserved"]


def get_employee_accrued_leave(employee, as_of_date=None):
    as_of_date = normalize_date_value(as_of_date or timezone.localdate())
    if as_of_date < get_employee_joined_date(employee):
        return Decimal("0.00")
    periods = sync_employee_entitlement_periods(employee, as_of_date)
    return quantize_leave_days(sum((_period_accrued_days(period, as_of_date) for period in periods), Decimal("0.00")))


def get_employee_requestable_leave(employee, as_of_date=None):
    as_of_date = normalize_date_value(as_of_date or timezone.localdate())
    if as_of_date < get_employee_joined_date(employee):
        return Decimal("0.00")
    periods = sync_employee_entitlement_periods(employee, as_of_date)
    return quantize_leave_days(sum((_period_requestable_days(period, as_of_date) for period in periods), Decimal("0.00")))


def get_employee_available_balance(employee, as_of_date=None, exclude_request_id=None, exclude_schedule_item_id=None):
    return _ledger_totals(
        employee,
        as_of_date=as_of_date,
        exclude_request_id=exclude_request_id,
        exclude_schedule_item_id=exclude_schedule_item_id,
    )["available"]


def get_employee_leave_summary(employee, as_of_date=None):
    return _ledger_totals(employee, as_of_date=as_of_date)


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

    employee_ids = [employee.id for employee in employees]
    request_sources = VacationRequest.objects.filter(
        employee_id__in=employee_ids,
        vacation_type__in=BALANCE_AFFECTING_TYPES,
        status__in=ACTIVE_REQUEST_STATUSES,
    )
    request_sources = exclude_converted_paid_requests(request_sources, employee_ids=employee_ids)
    employees_with_sources = set(
        request_sources.values_list("employee_id", flat=True)
    )
    employees_with_sources.update(
        VacationScheduleItem.objects.filter(
            employee_id__in=employee_ids,
            vacation_type__in=BALANCE_AFFECTING_TYPES,
            status__in=SCHEDULE_BALANCE_STATUSES,
        ).values_list("employee_id", flat=True)
    )
    employees_with_allocations = set(
        VacationEntitlementAllocation.objects.filter(employee_id__in=employee_ids).values_list("employee_id", flat=True)
    )
    employee_by_id = {employee.id: employee for employee in employees}
    for employee_id in employees_with_sources - employees_with_allocations:
        rebuild_employee_leave_ledger(employee_by_id[employee_id], as_of_date=as_of_date)

    periods = list(
        VacationEntitlementPeriod.objects.filter(
            employee_id__in=employee_ids,
            period_start__lte=as_of_date,
        ).order_by("employee_id", "period_start")
    )
    periods_by_employee = {employee.id: [] for employee in employees}
    for period in periods:
        periods_by_employee[period.employee_id].append(period)

    used_by_period = {period.id: Decimal("0.00") for period in periods}
    reserved_by_period = {period.id: Decimal("0.00") for period in periods}
    for allocation in VacationEntitlementAllocation.objects.filter(
        employee_id__in=employee_ids,
        entitlement_period_id__in=used_by_period.keys(),
    ).values("entitlement_period_id", "state", "allocated_days"):
        if allocation["state"] == VacationEntitlementAllocation.STATE_USED:
            used_by_period[allocation["entitlement_period_id"]] += Decimal(allocation["allocated_days"])
        else:
            reserved_by_period[allocation["entitlement_period_id"]] += Decimal(allocation["allocated_days"])

    summaries = {}
    for employee in employees:
        annual_entitlement = quantize_leave_days(employee.annual_paid_leave_days)
        employee_periods = periods_by_employee[employee.id]
        if employee_periods:
            accrued = quantize_leave_days(
                sum((_period_accrued_days(period, as_of_date) for period in employee_periods), Decimal("0.00"))
            )
            requestable = quantize_leave_days(
                sum((_period_requestable_days(period, as_of_date) for period in employee_periods), Decimal("0.00"))
            )
            used = Decimal("0.00")
            reserved = Decimal("0.00")
            for period in employee_periods:
                if _period_requestable_days(period, as_of_date) <= 0:
                    continue
                used += used_by_period[period.id]
                reserved += reserved_by_period[period.id]
            used = quantize_leave_days(used)
            reserved = quantize_leave_days(reserved)
        else:
            accrued = _calculate_accrued_leave_without_ledger(employee, as_of_date)
            requestable = _calculate_requestable_leave_without_ledger(employee, as_of_date)
            used = Decimal("0.00")
            reserved = Decimal("0.00")
        manual_adjustment = quantize_leave_days(employee.manual_leave_adjustment_days)
        accrued_balance = quantize_leave_days(accrued + manual_adjustment - used - reserved)
        available = quantize_leave_days(max(requestable + manual_adjustment - used - reserved, Decimal("0")))
        advance_available = quantize_leave_days(max(available - max(accrued_balance, Decimal("0")), Decimal("0")))
        summaries[employee.id] = {
            "annual_entitlement": annual_entitlement,
            "accrued": accrued,
            "requestable": requestable,
            "used": used,
            "reserved": reserved,
            "accrued_balance": accrued_balance,
            "advance_available": advance_available,
            "available": available,
            "manual_adjustment": manual_adjustment,
        }

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


def get_employee_entitlement_rows(employee, as_of_date=None, limit=6):
    as_of_date = as_of_date or timezone.localdate()
    periods = _ensure_employee_leave_ledger(employee, as_of_date=as_of_date)
    allocations = VacationEntitlementAllocation.objects.filter(
        employee=employee,
        entitlement_period_id__in=[period.id for period in periods],
    ).values("entitlement_period_id", "state", "allocated_days")

    totals_by_period = {
        period.id: {
            VacationEntitlementAllocation.STATE_USED: Decimal("0.00"),
            VacationEntitlementAllocation.STATE_RESERVED: Decimal("0.00"),
        }
        for period in periods
    }
    for allocation in allocations:
        totals_by_period[allocation["entitlement_period_id"]][allocation["state"]] += Decimal(allocation["allocated_days"])

    rows = []
    for period in periods:
        used_days = quantize_leave_days(totals_by_period[period.id][VacationEntitlementAllocation.STATE_USED])
        reserved_days = quantize_leave_days(totals_by_period[period.id][VacationEntitlementAllocation.STATE_RESERVED])
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

    problem_rows = [row for row in rows if row["status_key"] in {"overdue", "attention"} and row["remaining_days"] > 0]
    recent_rows = rows[-limit:]
    selected = {row["id"]: row for row in [*problem_rows, *recent_rows]}
    return sorted(selected.values(), key=lambda row: row["period_start"], reverse=True)


def get_employee_remaining_balance(employee):
    return get_employee_available_balance(employee)


def get_overlapping_requests(employee, start_date, end_date, exclude_request_id=None, statuses=None):
    if statuses is None:
        statuses = ACTIVE_REQUEST_STATUSES

    queryset = VacationRequest.objects.filter(
        employee=employee,
        status__in=statuses,
        start_date__lte=end_date,
        end_date__gte=start_date,
    )
    if exclude_request_id is not None:
        queryset = queryset.exclude(pk=exclude_request_id)
    queryset = exclude_converted_paid_requests(
        queryset,
        employee_ids=[employee.id],
        start_date=start_date,
        end_date=end_date,
    )
    return queryset


def get_overlapping_schedule_items(employee, start_date, end_date, statuses=None):
    if statuses is None:
        statuses = VacationScheduleItem.ACTIVE_STATUSES

    return VacationScheduleItem.objects.filter(
        employee=employee,
        status__in=statuses,
        start_date__lte=end_date,
        end_date__gte=start_date,
    )


def _risk_level_for_score(risk_score):
    if risk_score >= 70:
        return VacationRequest.RISK_HIGH
    if risk_score >= 40:
        return VacationRequest.RISK_MEDIUM
    return VacationRequest.RISK_LOW


def _get_department_staffing_rule(department):
    if department is None:
        return None

    try:
        return department.staffing_rule
    except department.__class__.staffing_rule.RelatedObjectDoesNotExist:
        return None


def calculate_vacation_request_risk(
    employee,
    start_date,
    end_date,
    vacation_type,
    exclude_request_id=None,
    exclude_schedule_item_id=None,
):
    requested_cost = Decimal(get_vacation_day_cost(vacation_type, start_date, end_date))
    requestable_days = get_employee_requestable_leave(employee, start_date)
    used_days = Decimal(get_employee_used_paid_days(employee, start_date))
    reserved_days = Decimal(
        get_employee_reserved_paid_days(
            employee,
            start_date,
            exclude_request_id=exclude_request_id,
            exclude_schedule_item_id=exclude_schedule_item_id,
        )
    )
    balance_after_request = quantize_leave_days(
        requestable_days
        + Decimal(employee.manual_leave_adjustment_days)
        - used_days
        - reserved_days
        - requested_cost
    )

    department = employee.department
    staffing_rule = _get_department_staffing_rule(department)
    workload = None
    if department is not None:
        workload = DepartmentWorkload.objects.filter(
            department=department,
            year=start_date.year,
            month=start_date.month,
        ).first()

    department_load_level = workload.load_level if workload else 1
    min_staff_required = (
        workload.min_staff_required
        if workload
        else (staffing_rule.min_staff_required if staffing_rule else 0)
    )

    if department is None:
        return {
            "risk_score": 25,
            "risk_level": VacationRequest.RISK_LOW,
            "department_load_level": department_load_level,
            "overlapping_absences_count": 0,
            "remaining_staff_count": 0,
            "min_staff_required": min_staff_required,
            "balance_after_request": balance_after_request,
        }

    department_employee_ids = set(
        Employees.objects.filter(
            department=department,
            is_active_employee=True,
            date_joined__lte=end_date,
        )
        .exclude(role__in=Employees.SERVICE_ROLES)
        .values_list("id", flat=True)
    )

    overlapping_requests = VacationRequest.objects.filter(
        employee_id__in=department_employee_ids,
        status__in=ACTIVE_REQUEST_STATUSES,
        start_date__lte=end_date,
        end_date__gte=start_date,
    )
    if exclude_request_id is not None:
        overlapping_requests = overlapping_requests.exclude(pk=exclude_request_id)
    overlapping_requests = exclude_converted_paid_requests(
        overlapping_requests,
        employee_ids=department_employee_ids,
        start_date=start_date,
        end_date=end_date,
    )
    request_employee_ids = set(overlapping_requests.values_list("employee_id", flat=True))
    schedule_employee_ids = set(
        VacationScheduleItem.objects.filter(
            employee_id__in=department_employee_ids,
            status__in=VacationScheduleItem.ACTIVE_STATUSES,
            start_date__lte=end_date,
            end_date__gte=start_date,
        )
        .exclude(pk=exclude_schedule_item_id)
        .values_list("employee_id", flat=True)
    )
    overlapping_employee_ids = (request_employee_ids | schedule_employee_ids) - {employee.id}
    overlapping_absences_count = len(overlapping_employee_ids)
    remaining_staff_count = max(len(department_employee_ids) - overlapping_absences_count - 1, 0)

    max_absent = workload.max_absent if workload else (staffing_rule.max_absent if staffing_rule else 1)
    criticality_level = staffing_rule.criticality_level if staffing_rule else 3
    role_boost = 16 if employee.role == Employees.ROLE_DEPARTMENT_HEAD else 0
    paid_exception_boost = 12 if vacation_type == "paid" else 0
    staffing_boost = 0
    if min_staff_required and remaining_staff_count < min_staff_required:
        staffing_boost += 28
    if max_absent and overlapping_absences_count + 1 > max_absent:
        staffing_boost += 22
    balance_boost = 18 if vacation_type == "paid" and balance_after_request < 0 else 0

    risk_score = min(
        95,
        8
        + department_load_level * 9
        + overlapping_absences_count * 6
        + criticality_level * 3
        + role_boost
        + paid_exception_boost
        + staffing_boost
        + balance_boost,
    )

    return {
        "risk_score": risk_score,
        "risk_level": _risk_level_for_score(risk_score),
        "department_load_level": department_load_level,
        "overlapping_absences_count": overlapping_absences_count,
        "remaining_staff_count": remaining_staff_count,
        "min_staff_required": min_staff_required,
        "balance_after_request": balance_after_request,
    }


def validate_vacation_request_for_employee(
    employee,
    start_date,
    end_date,
    vacation_type,
    exclude_request_id=None,
    exclude_schedule_item_id=None,
):
    if end_date < start_date:
        raise ValidationError("Дата окончания не может быть раньше даты начала.")

    overlaps_existing = get_overlapping_requests(
        employee=employee,
        start_date=start_date,
        end_date=end_date,
        exclude_request_id=exclude_request_id,
    ).exists()
    if overlaps_existing:
        raise ValidationError("На выбранные даты уже есть активная заявка или одобренный отпуск.")

    overlaps_schedule = get_overlapping_schedule_items(
        employee=employee,
        start_date=start_date,
        end_date=end_date,
    ).exists()
    if exclude_schedule_item_id is not None:
        overlaps_schedule = get_overlapping_schedule_items(
            employee=employee,
            start_date=start_date,
            end_date=end_date,
        ).exclude(pk=exclude_schedule_item_id).exists()
    if overlaps_schedule:
        raise ValidationError("На выбранные даты уже есть отпуск в годовом графике.")

    if vacation_type == "paid" and exclude_schedule_item_id is None:
        validate_paid_exception_request(employee, start_date, end_date)

    requested_cost = get_vacation_day_cost(vacation_type, start_date, end_date)
    if requested_cost > get_employee_available_balance(
        employee,
        as_of_date=start_date,
        exclude_request_id=exclude_request_id,
        exclude_schedule_item_id=exclude_schedule_item_id,
    ):
        raise ValidationError("Выбранный отпуск превышает доступный баланс дней.")


def create_vacation_request(employee, start_date, end_date, vacation_type, reason=""):
    validate_vacation_request_for_employee(employee, start_date, end_date, vacation_type)
    risk_payload = calculate_vacation_request_risk(employee, start_date, end_date, vacation_type)
    return VacationRequest.objects.create(
        employee=employee,
        start_date=start_date,
        end_date=end_date,
        vacation_type=vacation_type,
        status=VacationRequest.STATUS_PENDING,
        reason=reason,
        **risk_payload,
    )


def enrich_vacation_request(request_obj):
    status_meta = REQUEST_STATUS_UI[request_obj.status]
    request_obj.status_label = status_meta["label"]
    request_obj.status_icon = status_meta["icon"]
    request_obj.status_css_class = status_meta["css_class"]
    request_obj.vacation_type_display_label = (
        "Оплачиваемый вне графика"
        if request_obj.vacation_type == "paid"
        else request_obj.get_vacation_type_display()
    )
    request_obj.risk_label = request_obj.get_risk_level_display()
    request_obj.request_type = {
        VacationRequest.STATUS_APPROVED: "vacation",
        VacationRequest.STATUS_PENDING: "pre_holiday",
        VacationRequest.STATUS_REJECTED: "canceled_holiday",
    }[request_obj.status]
    request_obj.start_date_formatted = date_format(request_obj.start_date, "j E Y")
    request_obj.end_date_formatted = date_format(request_obj.end_date, "j E Y")
    return request_obj


def serialize_vacation_request_row(request_obj):
    enrich_vacation_request(request_obj)
    return {
        "id": request_obj.id,
        "employee_name": request_obj.employee.full_name,
        "employee_department": request_obj.employee.department.name if request_obj.employee.department else "Не указан",
        "detail_url": reverse("vacation_detail", args=[request_obj.id]),
        "period_label": format_period_label(request_obj.start_date, request_obj.end_date),
        "start_date_formatted": request_obj.start_date_formatted,
        "end_date_formatted": request_obj.end_date_formatted,
        "vacation_type_label": request_obj.vacation_type_display_label,
        "status": request_obj.status,
        "status_label": request_obj.status_label,
        "status_icon": request_obj.status_icon,
        "status_css_class": request_obj.status_css_class,
        "risk_score": request_obj.risk_score,
        "risk_label": request_obj.risk_label,
    }


def get_employee_vacation_requests(employee):
    requests = list(get_vacation_requests_queryset().filter(employee=employee).order_by("-created_at"))
    return [enrich_vacation_request(request_obj) for request_obj in requests]


def sync_employee_vacation_metrics(employee):
    if employee is None or not VACATION_METRIC_SYNC_ENABLED:
        return

    rebuild_employee_leave_ledger(employee)


def create_schedule_item_from_paid_vacation_request(vacation, risk_payload=None):
    if vacation.vacation_type != "paid" or vacation.status != VacationRequest.STATUS_APPROVED:
        return None

    existing_item = VacationScheduleItem.objects.filter(
        created_from_vacation_request=vacation,
    ).order_by("id").first()
    if existing_item is not None:
        return existing_item

    start_date = normalize_date_value(vacation.start_date)
    end_date = normalize_date_value(vacation.end_date)
    schedule = VacationSchedule.objects.filter(year=start_date.year).first()
    if schedule is None:
        raise ValidationError("Для года оплачиваемой заявки не найден утверждённый график отпусков.")
    if schedule.status not in {VacationSchedule.STATUS_APPROVED, VacationSchedule.STATUS_ARCHIVED}:
        raise ValidationError("Оплачиваемую заявку можно добавить только в утверждённый график отпусков.")

    if risk_payload is None:
        risk_payload = calculate_vacation_request_risk(
            employee=vacation.employee,
            start_date=start_date,
            end_date=end_date,
            vacation_type=vacation.vacation_type,
            exclude_request_id=vacation.id,
        )

    return VacationScheduleItem.objects.create(
        schedule=schedule,
        employee=vacation.employee,
        start_date=start_date,
        end_date=end_date,
        vacation_type=vacation.vacation_type,
        chargeable_days=get_chargeable_leave_days(start_date, end_date, vacation.vacation_type),
        status=VacationScheduleItem.STATUS_APPROVED,
        source=VacationScheduleItem.SOURCE_MANUAL,
        risk_score=risk_payload["risk_score"],
        risk_level=risk_payload["risk_level"],
        generated_by_ai=False,
        was_changed_by_manager=False,
        manager_comment="Создано после одобрения оплачиваемой заявки вне графика.",
        created_from_vacation_request=vacation,
    )


@transaction.atomic
def approve_vacation_request(vacation_id, reviewer=None, review_comment=""):
    vacation = VacationRequest.objects.select_related("employee").select_for_update().get(pk=vacation_id)
    if vacation.status != VacationRequest.STATUS_PENDING:
        raise ValidationError("Одобрить можно только заявку со статусом 'В ожидании'.")

    employee = Employees.objects.select_for_update().get(pk=vacation.employee_id)
    validate_vacation_request_for_employee(
        employee=employee,
        start_date=vacation.start_date,
        end_date=vacation.end_date,
        vacation_type=vacation.vacation_type,
        exclude_request_id=vacation.id,
    )
    risk_payload = calculate_vacation_request_risk(
        employee=employee,
        start_date=vacation.start_date,
        end_date=vacation.end_date,
        vacation_type=vacation.vacation_type,
        exclude_request_id=vacation.id,
    )
    vacation.status = VacationRequest.STATUS_APPROVED
    vacation.reviewed_by = reviewer
    vacation.reviewed_at = timezone.now()
    vacation.review_comment = review_comment
    for field_name, value in risk_payload.items():
        setattr(vacation, field_name, value)
    vacation.save(
        update_fields=[
            "status",
            "reviewed_by",
            "reviewed_at",
            "review_comment",
            "risk_score",
            "risk_level",
            "department_load_level",
            "overlapping_absences_count",
            "remaining_staff_count",
            "min_staff_required",
            "balance_after_request",
        ]
    )
    if vacation.vacation_type == "paid":
        create_schedule_item_from_paid_vacation_request(vacation, risk_payload=risk_payload)
    return vacation


@transaction.atomic
def reject_vacation_request(vacation_id, reviewer=None, review_comment=""):
    vacation = VacationRequest.objects.select_related("employee").select_for_update().get(pk=vacation_id)
    if vacation.status != VacationRequest.STATUS_PENDING:
        raise ValidationError("Отклонить можно только заявку со статусом 'В ожидании'.")

    risk_payload = calculate_vacation_request_risk(
        employee=vacation.employee,
        start_date=vacation.start_date,
        end_date=vacation.end_date,
        vacation_type=vacation.vacation_type,
        exclude_request_id=vacation.id,
    )
    vacation.status = VacationRequest.STATUS_REJECTED
    vacation.reviewed_by = reviewer
    vacation.reviewed_at = timezone.now()
    vacation.review_comment = review_comment
    for field_name, value in risk_payload.items():
        setattr(vacation, field_name, value)
    vacation.save(
        update_fields=[
            "status",
            "reviewed_by",
            "reviewed_at",
            "review_comment",
            "risk_score",
            "risk_level",
            "department_load_level",
            "overlapping_absences_count",
            "remaining_staff_count",
            "min_staff_required",
            "balance_after_request",
        ]
    )
    return vacation


@transaction.atomic
def delete_pending_vacation_request(vacation_id):
    vacation = VacationRequest.objects.select_related("employee").select_for_update().get(pk=vacation_id)
    if vacation.status != VacationRequest.STATUS_PENDING:
        raise ValidationError("Удалить можно только заявку со статусом 'В ожидании'.")

    employee = vacation.employee
    vacation.delete()
    return employee


def get_schedule_change_requests_queryset():
    return VacationScheduleChangeRequest.objects.select_related(
        "employee",
        "employee__department",
        "schedule_item",
        "schedule_item__schedule",
        "requested_by",
        "reviewed_by",
    )


def _change_request_status_meta(change_request):
    return REQUEST_STATUS_UI[change_request.status]


def enrich_schedule_change_request(change_request):
    status_meta = _change_request_status_meta(change_request)
    change_request.status_label = status_meta["label"]
    change_request.status_icon = status_meta["icon"]
    change_request.status_css_class = status_meta["css_class"]
    change_request.risk_label = change_request.get_risk_level_display()
    change_request.old_period_label = format_period_label(change_request.old_start_date, change_request.old_end_date)
    change_request.new_period_label = format_period_label(change_request.new_start_date, change_request.new_end_date)
    change_request.created_at_formatted = date_format(change_request.created_at, "j E Y")
    return change_request


def serialize_schedule_change_request_row(change_request):
    enrich_schedule_change_request(change_request)
    return {
        "id": change_request.id,
        "employee_name": change_request.employee.full_name,
        "employee_department": change_request.employee.department.name if change_request.employee.department else "Не указан",
        "old_period_label": change_request.old_period_label,
        "new_period_label": change_request.new_period_label,
        "status": change_request.status,
        "status_label": change_request.status_label,
        "status_icon": change_request.status_icon,
        "status_css_class": change_request.status_css_class,
        "risk_score": change_request.risk_score,
        "risk_label": change_request.risk_label,
        "can_approve": getattr(change_request, "can_approve", False),
        "approve_url": reverse("schedule_change_approve", args=[change_request.id]),
        "reject_url": reverse("schedule_change_reject", args=[change_request.id]),
    }


def calculate_schedule_change_risk(schedule_item, new_start_date, new_end_date):
    risk_payload = calculate_vacation_request_risk(
        schedule_item.employee,
        new_start_date,
        new_end_date,
        schedule_item.vacation_type,
        exclude_schedule_item_id=schedule_item.id,
    )
    return {
        "risk_score": risk_payload["risk_score"],
        "risk_level": risk_payload["risk_level"],
        "department_load_level": risk_payload["department_load_level"],
        "overlapping_absences_count": risk_payload["overlapping_absences_count"],
        "remaining_staff_count": risk_payload["remaining_staff_count"],
        "min_staff_required": risk_payload["min_staff_required"],
        "balance_after_change": risk_payload["balance_after_request"],
    }


def validate_schedule_change_request(schedule_item, new_start_date, new_end_date, exclude_change_request_id=None):
    today = timezone.localdate()
    if schedule_item.status not in VacationScheduleItem.ACTIVE_STATUSES:
        raise ValidationError("Переносить можно только активный пункт годового графика.")
    if schedule_item.start_date <= today:
        raise ValidationError("Перенос доступен только для будущего отпуска.")
    if new_end_date < new_start_date:
        raise ValidationError("Дата окончания не может быть раньше даты начала.")
    if new_start_date.year != schedule_item.schedule.year or new_end_date.year != schedule_item.schedule.year:
        raise ValidationError("Перенос должен оставаться в пределах года утверждённого графика.")
    pending_changes = VacationScheduleChangeRequest.objects.filter(
        schedule_item=schedule_item,
        status=VacationScheduleChangeRequest.STATUS_PENDING,
    )
    if exclude_change_request_id is not None:
        pending_changes = pending_changes.exclude(pk=exclude_change_request_id)
    if pending_changes.exists():
        raise ValidationError("По этому отпуску уже есть запрос переноса в ожидании.")

    new_chargeable_days = get_chargeable_leave_days(new_start_date, new_end_date, schedule_item.vacation_type)
    if schedule_item.chargeable_days >= 14 and new_chargeable_days < 14:
        raise ValidationError("Перенос не должен нарушать правило части отпуска не меньше 14 дней.")

    validate_vacation_request_for_employee(
        schedule_item.employee,
        new_start_date,
        new_end_date,
        schedule_item.vacation_type,
        exclude_schedule_item_id=schedule_item.id,
    )
    return new_chargeable_days


@transaction.atomic
def create_schedule_change_request(schedule_item_id, requested_by, new_start_date, new_end_date, reason=""):
    schedule_item = VacationScheduleItem.objects.select_related("employee", "schedule").select_for_update().get(
        pk=schedule_item_id
    )
    if requested_by is None or requested_by.id != schedule_item.employee_id:
        raise ValidationError("Запросить перенос может только сотрудник, которому принадлежит отпуск.")

    validate_schedule_change_request(schedule_item, new_start_date, new_end_date)
    risk_payload = calculate_schedule_change_risk(schedule_item, new_start_date, new_end_date)
    return VacationScheduleChangeRequest.objects.create(
        schedule_item=schedule_item,
        employee=schedule_item.employee,
        old_start_date=schedule_item.start_date,
        old_end_date=schedule_item.end_date,
        new_start_date=new_start_date,
        new_end_date=new_end_date,
        reason=reason,
        requested_by=requested_by,
        **risk_payload,
    )


@transaction.atomic
def approve_schedule_change_request(change_request_id, reviewer=None, review_comment=""):
    change_request = get_schedule_change_requests_queryset().select_for_update(of=("self",)).get(pk=change_request_id)
    if change_request.status != VacationScheduleChangeRequest.STATUS_PENDING:
        raise ValidationError("Одобрить можно только запрос переноса в ожидании.")

    schedule_item = VacationScheduleItem.objects.select_related("employee", "schedule").select_for_update().get(
        pk=change_request.schedule_item_id
    )
    new_chargeable_days = validate_schedule_change_request(
        schedule_item,
        change_request.new_start_date,
        change_request.new_end_date,
        exclude_change_request_id=change_request.id,
    )
    risk_payload = calculate_schedule_change_risk(
        schedule_item,
        change_request.new_start_date,
        change_request.new_end_date,
    )

    schedule_item.status = VacationScheduleItem.STATUS_TRANSFERRED
    schedule_item.was_changed_by_manager = True
    schedule_item.manager_comment = "Перенесено по согласованному запросу сотрудника."
    schedule_item.save(update_fields=["status", "was_changed_by_manager", "manager_comment"])

    replacement_item = VacationScheduleItem.objects.create(
        schedule=schedule_item.schedule,
        employee=schedule_item.employee,
        start_date=change_request.new_start_date,
        end_date=change_request.new_end_date,
        vacation_type=schedule_item.vacation_type,
        chargeable_days=new_chargeable_days,
        status=VacationScheduleItem.STATUS_APPROVED,
        source=VacationScheduleItem.SOURCE_TRANSFER,
        risk_score=risk_payload["risk_score"],
        risk_level=risk_payload["risk_level"],
        generated_by_ai=False,
        was_changed_by_manager=True,
        manager_comment="Создано после согласования переноса.",
        previous_item=schedule_item,
        created_from_change_request=change_request,
    )

    change_request.status = VacationScheduleChangeRequest.STATUS_APPROVED
    change_request.reviewed_by = reviewer
    change_request.reviewed_at = timezone.now()
    change_request.review_comment = review_comment
    for field_name, value in risk_payload.items():
        setattr(change_request, field_name, value)
    change_request.save(
        update_fields=[
            "status",
            "reviewed_by",
            "reviewed_at",
            "review_comment",
            "risk_score",
            "risk_level",
            "department_load_level",
            "overlapping_absences_count",
            "remaining_staff_count",
            "min_staff_required",
            "balance_after_change",
        ]
    )
    return replacement_item


@transaction.atomic
def reject_schedule_change_request(change_request_id, reviewer=None, review_comment=""):
    change_request = get_schedule_change_requests_queryset().select_for_update(of=("self",)).get(pk=change_request_id)
    if change_request.status != VacationScheduleChangeRequest.STATUS_PENDING:
        raise ValidationError("Отклонить можно только запрос переноса в ожидании.")

    risk_payload = calculate_schedule_change_risk(
        change_request.schedule_item,
        change_request.new_start_date,
        change_request.new_end_date,
    )
    change_request.status = VacationScheduleChangeRequest.STATUS_REJECTED
    change_request.reviewed_by = reviewer
    change_request.reviewed_at = timezone.now()
    change_request.review_comment = review_comment
    for field_name, value in risk_payload.items():
        setattr(change_request, field_name, value)
    change_request.save(
        update_fields=[
            "status",
            "reviewed_by",
            "reviewed_at",
            "review_comment",
            "risk_score",
            "risk_level",
            "department_load_level",
            "overlapping_absences_count",
            "remaining_staff_count",
            "min_staff_required",
            "balance_after_change",
        ]
    )
    return change_request


def get_calendar_redirect_url(request):
    next_view = request.POST.get("next_view_mode", request.GET.get("view", "month"))
    next_year = request.POST.get("next_year", request.GET.get("year", timezone.localdate().year))
    next_month = request.POST.get("next_month", request.GET.get("month", timezone.localdate().month))
    return f"{request.path}?view={next_view}&year={next_year}&month={next_month}"


def _schedule_item_source_label(item):
    if item.status == VacationScheduleItem.STATUS_TRANSFERRED:
        return "Перенос"
    if item.source == VacationScheduleItem.SOURCE_MANUAL:
        return "Дополнение к графику"
    if item.source == VacationScheduleItem.SOURCE_TRANSFER:
        return "Перенос"
    return "Годовой график"


def build_calendar_base_data(year, employee_ids=None):
    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)
    employees_queryset = Employees.objects.select_related("department").filter(
        is_active_employee=True,
        date_joined__lte=year_end,
    ).order_by(
        "last_name",
        "first_name",
        "middle_name",
    )
    if employee_ids is not None:
        employees_queryset = employees_queryset.filter(id__in=employee_ids)

    employees = list(employees_queryset)
    employee_day_status = {employee.id: {} for employee in employees}
    employee_entries = {employee.id: [] for employee in employees}

    records = get_vacation_requests_queryset().filter(
        start_date__lte=year_end,
        end_date__gte=year_start,
        status__in=CALENDAR_VISIBLE_STATUSES,
    )
    if employee_ids is not None:
        records = records.filter(employee_id__in=employee_ids)
    records = exclude_converted_paid_requests(
        records,
        employee_ids=employee_ids,
        start_date=year_start,
        end_date=year_end,
    )

    for record in records:
        clipped_period = clip_period_to_range(record.start_date, record.end_date, year_start, year_end)
        if clipped_period is None:
            continue

        clipped_start, clipped_end = clipped_period
        employee = record.employee
        display_status = REQUEST_STATUS_TO_DISPLAY_STATUS[record.status]
        display_meta = DISPLAY_STATUS_UI[display_status]
        entry = {
            "employee_id": employee.id,
            "source_kind": "request",
            "source_id": record.id,
            "employee_name": employee.full_name,
            "employee_position": employee.position,
            "department_name": employee.department.name if employee.department else "Не указан",
            "status": record.status,
            "display_status": display_status,
            "display_type": display_meta["display_type"],
            "display_label": display_meta["label"],
            "status_label": display_meta["label"],
            "source_label": display_meta["source_label"],
            "css_class": display_meta["css_class"],
            "status_icon": REQUEST_STATUS_UI[record.status]["icon"],
            "vacation_type_label": record.get_vacation_type_display(),
            "start_date": clipped_start,
            "end_date": clipped_end,
            "days": get_requested_days(clipped_start, clipped_end),
            "period_label": format_period_label(clipped_start, clipped_end),
            "sort_key": clipped_start.toordinal(),
        }
        employee_entries[employee.id].append(entry)

        for current_date in iterate_dates(clipped_start, clipped_end):
            current_status = employee_day_status[employee.id].get(current_date, DISPLAY_FREE)
            if DISPLAY_STATUS_PRIORITY[display_status] >= DISPLAY_STATUS_PRIORITY[current_status]:
                employee_day_status[employee.id][current_date] = display_status

    schedule_items = VacationScheduleItem.objects.select_related("employee", "employee__department", "schedule").filter(
        start_date__lte=year_end,
        end_date__gte=year_start,
        status__in=SCHEDULE_STATUS_TO_DISPLAY_STATUS.keys(),
    )
    if employee_ids is not None:
        schedule_items = schedule_items.filter(employee_id__in=employee_ids)

    for item in schedule_items:
        if item.employee_id not in employee_entries:
            continue

        clipped_period = clip_period_to_range(item.start_date, item.end_date, year_start, year_end)
        if clipped_period is None:
            continue

        clipped_start, clipped_end = clipped_period
        employee = item.employee
        calendar_status = SCHEDULE_STATUS_TO_CALENDAR_STATUS[item.status]
        display_status = SCHEDULE_STATUS_TO_DISPLAY_STATUS[item.status]
        display_meta = DISPLAY_STATUS_UI[display_status]
        entry = {
            "employee_id": employee.id,
            "source_kind": "schedule",
            "source_id": item.id,
            "employee_name": employee.full_name,
            "employee_position": employee.position,
            "department_name": employee.department.name if employee.department else "Не указан",
            "status": calendar_status,
            "schedule_status": item.status,
            "display_status": display_status,
            "display_type": display_meta["display_type"],
            "display_label": display_meta["label"],
            "status_label": display_meta["label"],
            "source_label": _schedule_item_source_label(item),
            "css_class": display_meta["css_class"],
            "status_icon": REQUEST_STATUS_UI[calendar_status]["icon"],
            "vacation_type_label": item.get_vacation_type_display(),
            "start_date": clipped_start,
            "end_date": clipped_end,
            "days": get_requested_days(clipped_start, clipped_end),
            "period_label": format_period_label(clipped_start, clipped_end),
            "sort_key": clipped_start.toordinal(),
        }
        employee_entries[employee.id].append(entry)

        for current_date in iterate_dates(clipped_start, clipped_end):
            current_status = employee_day_status[employee.id].get(current_date, DISPLAY_FREE)
            if DISPLAY_STATUS_PRIORITY[display_status] >= DISPLAY_STATUS_PRIORITY[current_status]:
                employee_day_status[employee.id][current_date] = display_status

    for employee_id, entries in employee_entries.items():
        entries.sort(key=lambda item: (item["sort_key"], -DISPLAY_STATUS_PRIORITY[item["display_status"]]))

    return employees, employee_day_status, employee_entries


def _empty_calendar_display_counts():
    counts = {
        "schedule_days": 0,
        "request_days": 0,
        "changed_days": 0,
        "total_days": 0,
        "display_statuses": set(),
    }
    for status in (
        VacationRequest.STATUS_APPROVED,
        VacationRequest.STATUS_PENDING,
        VacationRequest.STATUS_REJECTED,
    ):
        counts[status] = 0
    return counts


def _add_entry_to_display_counts(counts, entry, days):
    if days <= 0:
        return

    counts["total_days"] += days
    counts["display_statuses"].add(entry["display_status"])
    if entry["status"] in (
        VacationRequest.STATUS_APPROVED,
        VacationRequest.STATUS_PENDING,
        VacationRequest.STATUS_REJECTED,
    ):
        counts[entry["status"]] += days

    if entry["display_type"] == "request":
        counts["request_days"] += days
    elif entry["display_status"] in (DISPLAY_SCHEDULE_TRANSFERRED, DISPLAY_SCHEDULE_CANCELLED):
        counts["changed_days"] += days
    else:
        counts["schedule_days"] += days


def _get_display_status_from_counts(counts):
    statuses = counts["display_statuses"]
    if len(statuses) > 1:
        return DISPLAY_MIXED
    if statuses:
        return next(iter(statuses))
    return DISPLAY_FREE


def _serialize_calendar_entry(entry, current_employee_id=None, today=None):
    today = today or timezone.localdate()
    can_request_transfer = (
        entry.get("source_kind") == "schedule"
        and entry.get("schedule_status") in VacationScheduleItem.ACTIVE_STATUSES
        and entry["start_date"] > today
        and entry["employee_id"] == current_employee_id
    )
    payload = {
        "source_kind": entry.get("source_kind", ""),
        "source_id": entry.get("source_id"),
        "period_label": entry["period_label"],
        "status_label": entry["status_label"],
        "display_label": entry["display_label"],
        "source_label": entry["source_label"],
        "display_type": entry["display_type"],
        "status": entry["display_status"],
        "css_class": entry["css_class"],
        "vacation_type_label": entry["vacation_type_label"],
        "days": entry["days"],
        "can_request_transfer": can_request_transfer,
    }
    if can_request_transfer:
        payload["transfer_url"] = reverse("schedule_change_request_create", args=[entry["source_id"]])
        payload["transfer_title"] = f'{entry["period_label"]} · {entry["vacation_type_label"]}'
    return payload


def build_month_timeline_cells(day_map, year, month, today):
    days_in_month = calendar.monthrange(year, month)[1]
    cells = []
    for day in range(1, days_in_month + 1):
        current_date = date(year, month, day)
        status = day_map.get(current_date, DISPLAY_FREE)
        previous_status = day_map.get(current_date - timedelta(days=1), DISPLAY_FREE) if day > 1 else DISPLAY_FREE
        next_status = day_map.get(current_date + timedelta(days=1), DISPLAY_FREE) if day < days_in_month else DISPLAY_FREE
        is_start = status != DISPLAY_FREE and previous_status != status
        is_end = status != DISPLAY_FREE and next_status != status
        cells.append(
            {
                "day": day,
                "status": status,
                "display_status": status,
                "css_class": DISPLAY_STATUS_UI[status]["css_class"],
                "is_weekend": current_date.weekday() >= 5,
                "is_today": current_date == today,
                "is_start": is_start,
                "is_end": is_end,
                "is_single": is_start and is_end,
                "tooltip": f'{day:02d}.{month:02d}.{year} • {VACATION_STATUS_META[status]["label"]}',
            }
        )
    return cells


def build_year_month_cells(entries, year):
    month_cells = []
    for month_number in range(1, 13):
        month_start = date(year, month_number, 1)
        month_end = get_month_end(month_start)
        days_in_month = calendar.monthrange(year, month_number)[1]
        counts = _empty_calendar_display_counts()
        segments = []
        for entry in entries:
            overlap = clip_period_to_range(entry["start_date"], entry["end_date"], month_start, month_end)
            if overlap is None:
                continue

            overlap_start, overlap_end = overlap
            overlap_days = get_requested_days(overlap_start, overlap_end)
            _add_entry_to_display_counts(counts, entry, overlap_days)

            segments.append(
                {
                    "status": entry["display_status"],
                    "display_status": entry["display_status"],
                    "css_class": entry["css_class"],
                    "days": overlap_days,
                    "offset_percent": round(((overlap_start.day - 1) / days_in_month) * 100, 1),
                    "width_percent": round((overlap_days / days_in_month) * 100, 1),
                }
            )

        busy_days = counts["total_days"]
        status_key = _get_display_status_from_counts(counts)

        segments.sort(
            key=lambda segment: (
                segment["offset_percent"],
                -DISPLAY_STATUS_PRIORITY.get(segment["status"], 0),
            )
        )

        month_cells.append(
            {
                "month_name": RUSSIAN_MONTH_NAMES[month_number - 1],
                "month_short": RUSSIAN_MONTH_SHORT_NAMES[month_number - 1],
                "busy_days": busy_days,
                "status": status_key,
                "display_status": status_key,
                "schedule_days": counts["schedule_days"],
                "request_days": counts["request_days"],
                "changed_days": counts["changed_days"],
                "segments": segments,
                "approved_days": counts[VacationRequest.STATUS_APPROVED],
                "pending_days": counts[VacationRequest.STATUS_PENDING],
                "rejected_days": counts[VacationRequest.STATUS_REJECTED],
            }
        )

    return month_cells


def build_calendar_rows(
    employees,
    employee_day_status,
    employee_entries,
    year,
    month,
    view_mode,
    today,
    current_employee=None,
):
    period_start = date(year, 1, 1) if view_mode == "year" else date(year, month, 1)
    period_end = date(year, 12, 31) if view_mode == "year" else date(year, month, calendar.monthrange(year, month)[1])
    rows = []
    details = {}

    for employee in employees:
        day_map = employee_day_status.get(employee.id, {})
        entries = employee_entries.get(employee.id, [])

        selected_entries = [
            entry for entry in entries if clip_period_to_range(entry["start_date"], entry["end_date"], period_start, period_end)
        ]
        upcoming_entry = next((entry for entry in entries if entry["end_date"] >= today), None)
        period_counts = _empty_calendar_display_counts()
        year_counts = _empty_calendar_display_counts()
        year_start = date(year, 1, 1)
        year_end = date(year, 12, 31)
        for entry in entries:
            year_overlap = clip_period_to_range(entry["start_date"], entry["end_date"], year_start, year_end)
            if year_overlap is not None:
                _add_entry_to_display_counts(year_counts, entry, get_requested_days(*year_overlap))
            period_overlap = clip_period_to_range(entry["start_date"], entry["end_date"], period_start, period_end)
            if period_overlap is not None:
                _add_entry_to_display_counts(period_counts, entry, get_requested_days(*period_overlap))

        row_status = _get_display_status_from_counts(period_counts)

        rows.append(
            {
                "employee_id": employee.id,
                "employee_name": employee.full_name,
                "position": employee.position,
                "department": employee.department.name if employee.department else "Не указан",
                "status": row_status,
                "display_status": row_status,
                "selected_schedule_days": period_counts["schedule_days"],
                "selected_request_days": period_counts["request_days"],
                "selected_changed_days": period_counts["changed_days"],
                "selected_total_days": period_counts["total_days"],
                "selected_approved_days": period_counts[VacationRequest.STATUS_APPROVED],
                "selected_pending_days": period_counts[VacationRequest.STATUS_PENDING],
                "selected_rejected_days": period_counts[VacationRequest.STATUS_REJECTED],
                "year_schedule_days": year_counts["schedule_days"],
                "year_request_days": year_counts["request_days"],
                "year_changed_days": year_counts["changed_days"],
                "year_total_days": year_counts["total_days"],
                "year_approved_days": year_counts[VacationRequest.STATUS_APPROVED],
                "year_pending_days": year_counts[VacationRequest.STATUS_PENDING],
                "year_rejected_days": year_counts[VacationRequest.STATUS_REJECTED],
                "cells": build_year_month_cells(entries, year)
                if view_mode == "year"
                else build_month_timeline_cells(day_map, year, month, today),
            }
        )

        details[str(employee.id)] = {
            "employee_name": employee.full_name,
            "position": employee.position,
            "department": employee.department.name if employee.department else "Не указан",
            "selected_period_label": f"{RUSSIAN_MONTH_NAMES[month - 1]} {year}" if view_mode == "month" else f"Годовой обзор {year}",
            "selected_schedule_days": period_counts["schedule_days"],
            "selected_request_days": period_counts["request_days"],
            "selected_changed_days": period_counts["changed_days"],
            "selected_total_days": period_counts["total_days"],
            "selected_approved_days": period_counts[VacationRequest.STATUS_APPROVED],
            "selected_pending_days": period_counts[VacationRequest.STATUS_PENDING],
            "selected_rejected_days": period_counts[VacationRequest.STATUS_REJECTED],
            "year_schedule_days": year_counts["schedule_days"],
            "year_request_days": year_counts["request_days"],
            "year_changed_days": year_counts["changed_days"],
            "year_total_days": year_counts["total_days"],
            "year_approved_days": year_counts[VacationRequest.STATUS_APPROVED],
            "year_pending_days": year_counts[VacationRequest.STATUS_PENDING],
            "year_rejected_days": year_counts[VacationRequest.STATUS_REJECTED],
            "upcoming_label": upcoming_entry["period_label"] if upcoming_entry else "Ближайший отпуск не запланирован",
            "upcoming_status": upcoming_entry["status_label"] if upcoming_entry else "",
            "selected_entries": [
                _serialize_calendar_entry(entry, getattr(current_employee, "id", None), today)
                for entry in selected_entries
            ],
            "year_entries": [
                _serialize_calendar_entry(entry, getattr(current_employee, "id", None), today)
                for entry in entries
            ],
        }
        details[str(employee.id)]["selected_period_label"] = (
            f"{RUSSIAN_MONTH_NAMES[month - 1]} {year}" if view_mode == "month" else f"Годовой обзор {year}"
        )
        if upcoming_entry is None:
            details[str(employee.id)]["upcoming_label"] = "Ближайший отпуск не запланирован"

    return rows, details


def build_calendar_summary(employee_entries, year, month, view_mode):
    period_start = date(year, 1, 1) if view_mode == "year" else date(year, month, 1)
    period_end = date(year, 12, 31) if view_mode == "year" else date(year, month, calendar.monthrange(year, month)[1])
    employees_in_period = set()
    counts = _empty_calendar_display_counts()

    for entries in employee_entries.values():
        for entry in entries:
            overlap = clip_period_to_range(entry["start_date"], entry["end_date"], period_start, period_end)
            if overlap is None:
                continue

            overlap_start, overlap_end = overlap
            overlap_days = get_requested_days(overlap_start, overlap_end)
            employees_in_period.add(entry["employee_id"])
            _add_entry_to_display_counts(counts, entry, overlap_days)

    return [
        {
            "icon": "groups",
            "label": "Сотрудников в периоде",
            "value": len(employees_in_period),
            "hint": "У кого есть отпуск или заявка в выбранном диапазоне.",
        },
        {
            "icon": "event_available",
            "label": "По годовому графику",
            "value": counts["schedule_days"],
            "hint": "Дни из утвержденного или запланированного графика отпусков.",
        },
        {
            "icon": "watch_later",
            "label": "Заявки и изменения",
            "value": counts["request_days"] + counts["changed_days"],
            "hint": "Внеплановые заявки, переносы и отмененные пункты графика.",
        },
    ]


def build_analytics_payload(employee_ids=None):
    today = timezone.localdate()
    year = today.year
    employees, employee_day_status, employee_entries = build_calendar_base_data(year, employee_ids=employee_ids)
    rows, _ = build_calendar_rows(
        employees,
        employee_day_status,
        employee_entries,
        year=year,
        month=today.month,
        view_mode="year",
        today=today,
    )

    vacation_counts = [0] * 12
    average_duration_days = [0] * 12
    planned_days = [0] * 12
    duration_totals = [0] * 12
    for entries in employee_entries.values():
        for entry in entries:
            for month_start in get_month_range(entry["start_date"], entry["end_date"]):
                overlap_days = get_overlap_days(entry["start_date"], entry["end_date"], month_start, get_month_end(month_start))
                month_index = month_start.month - 1
                vacation_counts[month_index] += 1
                duration_totals[month_index] += overlap_days
                planned_days[month_index] += overlap_days

    for month_index, vacations_in_month in enumerate(vacation_counts):
        if vacations_in_month:
            average_duration_days[month_index] = round(duration_totals[month_index] / vacations_in_month, 2)

    total_employees = len(employees)
    employee_id_set = {employee.id for employee in employees}
    approved_absence_requests = VacationRequest.objects.filter(
        employee_id__in=employee_id_set,
        status=VacationRequest.STATUS_APPROVED,
        start_date__lte=today,
        end_date__gte=today,
    )
    approved_absence_requests = exclude_converted_paid_requests(
        approved_absence_requests,
        employee_ids=employee_id_set,
        start_date=today,
        end_date=today,
    )
    absent_employee_ids = set(approved_absence_requests.values_list("employee_id", flat=True))
    absent_employee_ids.update(
        VacationScheduleItem.objects.filter(
            employee_id__in=employee_id_set,
            status__in=VacationScheduleItem.ACTIVE_STATUSES,
            start_date__lte=today,
            end_date__gte=today,
        ).values_list("employee_id", flat=True)
    )
    employees_not_on_vacation_count = total_employees - len(absent_employee_ids)
    working_employees = round((employees_not_on_vacation_count / total_employees) * 100) if total_employees else 0
    total_applications_count = VacationRequest.objects.count() if employee_ids is None else VacationRequest.objects.filter(employee_id__in=employee_ids).count()
    canceled_count = (
        VacationRequest.objects.filter(status=VacationRequest.STATUS_REJECTED).count()
        if employee_ids is None
        else VacationRequest.objects.filter(employee_id__in=employee_ids, status=VacationRequest.STATUS_REJECTED).count()
    )
    rejection_percentage = round((canceled_count / total_applications_count) * 100) if total_applications_count else 0
    avg_vacation_days = round(
        sum(employee.annual_paid_leave_days for employee in employees) / total_employees,
        2,
    ) if total_employees else 0

    return {
        "labels": RUSSIAN_MONTH_SHORT_NAMES,
        "values1": vacation_counts,
        "values2": average_duration_days,
        "values3": planned_days,
        "rows": rows,
        "total_employees": total_employees,
        "employees_not_on_vacation_count": employees_not_on_vacation_count,
        "working_employees": working_employees,
        "total_applications_count": total_applications_count,
        "canceled_count": canceled_count,
        "rejection_percentage": rejection_percentage,
        "avg_vacation_days": avg_vacation_days,
    }
