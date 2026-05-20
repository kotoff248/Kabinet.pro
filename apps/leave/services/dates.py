import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache

import holidays

from .constants import BALANCE_AFFECTING_TYPES, LEAVE_DAY_QUANTIZER

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
    normalized_years = tuple(sorted({int(year) for year in years}))
    return list(_get_russian_holiday_iso_dates_for_years(normalized_years))

@lru_cache(maxsize=None)
def _get_russian_holiday_iso_dates_for_years(years):
    holiday_dates = set()
    for year in years:
        holiday_dates.update(_get_russian_holiday_dates_for_year(year))
    return tuple(sorted(current_date.isoformat() for current_date in holiday_dates))

@lru_cache(maxsize=None)
def _get_russian_holiday_dates_for_year(year):
    holiday_calendar = holidays.country_holidays("RU", years=[year])
    return frozenset(holiday_calendar.keys())

@lru_cache(maxsize=8192)
def get_chargeable_leave_days(start_date, end_date, vacation_type):
    if vacation_type not in BALANCE_AFFECTING_TYPES:
        return 0
    holiday_days = len(get_russian_holiday_dates(start_date, end_date))
    return max(get_requested_days(start_date, end_date) - holiday_days, 0)

def get_vacation_day_cost(vacation_type, start_date, end_date):
    return get_chargeable_leave_days(start_date, end_date, vacation_type)
