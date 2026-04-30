import calendar
from datetime import date, timedelta

from django.urls import reverse
from django.utils import timezone

from apps.employees.models import Employees
from apps.leave.models import VacationRequest, VacationScheduleItem

from .constants import (
    CALENDAR_VISIBLE_STATUSES,
    DISPLAY_FREE,
    DISPLAY_MIXED,
    DISPLAY_REQUEST_APPROVED,
    DISPLAY_REQUEST_PENDING,
    DISPLAY_REQUEST_REJECTED,
    DISPLAY_SCHEDULE_APPROVED,
    DISPLAY_SCHEDULE_CANCELLED,
    DISPLAY_SCHEDULE_PLANNED,
    DISPLAY_SCHEDULE_TRANSFERRED,
    DISPLAY_STATUS_PRIORITY,
    DISPLAY_STATUS_UI,
    REQUEST_STATUS_TO_DISPLAY_STATUS,
    REQUEST_STATUS_UI,
    RUSSIAN_MONTH_NAMES,
    RUSSIAN_MONTH_SHORT_NAMES,
    SCHEDULE_STATUS_TO_CALENDAR_STATUS,
    SCHEDULE_STATUS_TO_DISPLAY_STATUS,
    VACATION_STATUS_META,
)
from .dates import clip_period_to_range, format_period_label, get_month_end, get_requested_days, iterate_dates
from .querysets import exclude_converted_paid_requests, get_vacation_requests_queryset

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
            "detail_url": reverse("vacation_detail", args=[record.id]),
            "detail_label": "Открыть заявку",
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

    schedule_items = VacationScheduleItem.objects.select_related(
        "employee",
        "employee__department",
        "schedule",
        "created_from_vacation_request",
    ).filter(
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
            "detail_url": reverse("vacation_detail", args=[item.created_from_vacation_request_id])
            if item.created_from_vacation_request_id
            else "",
            "detail_label": "Открыть заявку" if item.created_from_vacation_request_id else "",
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
        "detail_url": entry.get("detail_url", ""),
        "detail_label": entry.get("detail_label", ""),
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
                "profile_url": reverse("employee_profile", args=[employee.id]),
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
            "profile_url": reverse("employee_profile", args=[employee.id]),
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
