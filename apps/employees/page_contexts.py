from collections import Counter
from datetime import date
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from apps.accounts.services import (
    can_access_applications,
    can_delete_employee,
    can_edit_employee_data,
    get_managed_department_id,
    is_department_head_employee,
    is_enterprise_head_employee,
    is_hr_employee,
)
from apps.core.services.navigation import build_explicit_back_link
from apps.employees.models import Departments, Employees, ProductionGroup
from apps.employees.role_presentation import get_employee_role_card_meta
from apps.employees.services import resolve_production_group_filter_context
from apps.employees.tenure import build_new_hire_badge
from apps.leave.models import DepartmentWorkload, VacationRequest, VacationScheduleChangeRequest, VacationScheduleItem
from apps.leave.services.calendar import build_employee_schedule_status_map
from apps.leave.services.dates import format_period_label, get_chargeable_leave_days, get_requested_days, quantize_leave_days
from apps.leave.services.ledger import (
    get_employee_entitlement_rows,
    get_employee_list_leave_summaries,
    get_employee_leave_summary,
)
from apps.leave.services.querysets import exclude_converted_paid_requests
from apps.leave.services.requests import get_employee_vacation_requests
from apps.leave.services.schedule_changes import build_schedule_change_transfer_action, enrich_schedule_change_request
from apps.leave.services.schedule_items import get_schedule_item_detail_reference
from apps.leave.services.staffing import (
    build_department_group_staffing_forecast_map,
    build_department_staffing_forecast_map,
    format_staff_count,
)

EMPLOYEE_SCHEDULE_STATUS_FILTER_OPTIONS = (
    {"value": "all", "label": "Все графики"},
    {"value": "conflict", "label": "Конфликт"},
    {"value": "risk", "label": "Риск"},
    {"value": "planned", "label": "График есть"},
    {"value": "empty", "label": "Нет отпуска"},
)
EMPLOYEE_SCHEDULE_STATUS_FILTER_VALUES = {
    option["value"]
    for option in EMPLOYEE_SCHEDULE_STATUS_FILTER_OPTIONS
}
CALENDAR_RETURN_CONTEXT_SOURCES = {
    "profile",
    "employees",
    "applications",
    "departments",
    "analytics",
    "staffing",
    "notifications",
    "calendar",
}


def _format_days(value):
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _format_vacation_count_label(value):
    value = int(value)
    last_two_digits = value % 100
    last_digit = value % 10
    if 11 <= last_two_digits <= 14:
        word = "отпусков"
    elif last_digit == 1:
        word = "отпуск"
    elif 2 <= last_digit <= 4:
        word = "отпуска"
    else:
        word = "отпусков"
    return f"{value} {word}"


def _format_short_date(value):
    return date_format(value, "j E", use_l10n=True)


def _format_short_period(start_date, end_date):
    return f"{_format_short_date(start_date)} - {_format_short_date(end_date)}"


def _empty_vacation_display():
    return {
        "is_currently_on_vacation": False,
        "current_vacation_end": None,
        "upcoming_vacation_label": "Не запланирован",
    }


def _get_employee_department_deputy(employee):
    return getattr(employee, "deputy_department", None)


def _get_employee_management_badges(employee, department_deputy=None):
    badges = []
    role_meta = get_employee_role_card_meta(employee)
    if employee.role in {
        Employees.ROLE_HR,
        Employees.ROLE_DEPARTMENT_HEAD,
        Employees.ROLE_ENTERPRISE_HEAD,
    }:
        badges.append(
            {
                "label": role_meta["label"],
                "icon": role_meta["icon"],
                "icon_type": role_meta["icon_type"],
                "variant": role_meta["variant"],
            }
        )
    if employee.role == Employees.ROLE_DEPARTMENT_HEAD:
        badges[-1]["label"] = "Руководитель отдела"
    if department_deputy is not None:
        badges.append(
            {
                "label": "Заместитель отдела",
                "icon": "supervisor_account",
                "icon_type": "material",
                "variant": "department-deputy",
            }
        )
    if employee.is_enterprise_deputy:
        badges.append(
            {
                "label": "Заместитель предприятия",
                "icon": "workspace_premium",
                "icon_type": "material",
                "variant": "enterprise-deputy",
            }
        )
    return badges


def _get_employee_list_role_meta(employee, base_role_meta, department_deputy=None):
    role_meta = base_role_meta.copy()
    if employee.role == Employees.ROLE_EMPLOYEE and department_deputy is not None:
        role_meta.update(
            {
                "icon": "supervisor_account",
                "icon_type": "material",
                "label": "Заместитель отдела",
                "variant": "department-deputy",
            }
        )
    elif employee.role == Employees.ROLE_EMPLOYEE and employee.is_enterprise_deputy:
        role_meta.update(
            {
                "icon": "workspace_premium",
                "icon_type": "material",
                "label": "Заместитель предприятия",
                "variant": "enterprise-deputy",
            }
        )
    return role_meta


def _collect_employee_vacation_display(employee_ids, as_of_date=None):
    employee_ids = list(dict.fromkeys(employee_ids))
    display_by_employee = {employee_id: _empty_vacation_display() for employee_id in employee_ids}
    if not employee_ids:
        return display_by_employee

    today = as_of_date or timezone.localdate()
    entries_by_employee = {employee_id: [] for employee_id in employee_ids}
    current_requests = VacationRequest.objects.filter(
        employee_id__in=employee_ids,
        status=VacationRequest.STATUS_APPROVED,
        end_date__gte=today,
    ).only("employee_id", "start_date", "end_date", "vacation_type", "status")
    current_requests = exclude_converted_paid_requests(
        current_requests,
        employee_ids=employee_ids,
        start_date=today,
    )
    for request_obj in current_requests:
        entries_by_employee[request_obj.employee_id].append(
            {
                "start_date": request_obj.start_date,
                "end_date": request_obj.end_date,
            }
        )

    schedule_items = VacationScheduleItem.objects.filter(
        employee_id__in=employee_ids,
        status__in=VacationScheduleItem.ACTIVE_STATUSES,
        end_date__gte=today,
    ).only("employee_id", "start_date", "end_date", "status")
    for item in schedule_items:
        entries_by_employee[item.employee_id].append(
            {
                "start_date": item.start_date,
                "end_date": item.end_date,
            }
        )

    for employee_id, entries in entries_by_employee.items():
        if not entries:
            continue

        current_entries = [
            entry
            for entry in entries
            if entry["start_date"] <= today <= entry["end_date"]
        ]
        if current_entries:
            current_end = max(entry["end_date"] for entry in current_entries)
            display_by_employee[employee_id]["is_currently_on_vacation"] = True
            display_by_employee[employee_id]["current_vacation_end"] = current_end

        upcoming = sorted(
            entries,
            key=lambda entry: (
                0 if entry["start_date"] <= today <= entry["end_date"] else 1,
                entry["start_date"],
                entry["end_date"],
            ),
        )[0]
        display_by_employee[employee_id]["upcoming_vacation_label"] = _format_short_period(
            upcoming["start_date"],
            upcoming["end_date"],
        )

    return display_by_employee


def _get_current_vacation_employee_ids(employee_ids, as_of_date=None):
    vacation_display = _collect_employee_vacation_display(employee_ids, as_of_date=as_of_date)
    return {
        employee_id
        for employee_id, display in vacation_display.items()
        if display["is_currently_on_vacation"]
    }


def _serialize_employee_row(employee, leave_summary, vacation_display=None, schedule_status=None):
    vacation_display = vacation_display or _collect_employee_vacation_display([employee.id]).get(
        employee.id,
        _empty_vacation_display(),
    )
    role_meta = get_employee_role_card_meta(employee)
    production_group = (
        employee.employee_position.production_group
        if getattr(employee, "employee_position_id", None) and employee.employee_position
        else None
    )
    department_deputy = _get_employee_department_deputy(employee)
    role_meta = _get_employee_list_role_meta(employee, role_meta, department_deputy=department_deputy)
    is_working_now = not vacation_display["is_currently_on_vacation"]
    status_label = "Работает"
    if not is_working_now:
        current_vacation_end = vacation_display.get("current_vacation_end")
        status_label = (
            f"В отпуске до {_format_short_date(current_vacation_end)}"
            if current_vacation_end
            else "В отпуске"
        )
    return {
        "id": employee.id,
        "name": employee.full_name,
        "position": employee.position,
        "department_name": employee.department.name if employee.department else "Не указан",
        "production_group_label": production_group.name if production_group else "Не указана",
        "management_badges": _get_employee_management_badges(employee, department_deputy=department_deputy),
        "new_hire_badge": build_new_hire_badge(employee),
        "date_joined": date_format(employee.date_joined, "j E Y", use_l10n=True),
        "available_days": _format_days(leave_summary["available"]),
        "role_icon": role_meta["icon"],
        "role_icon_type": role_meta["icon_type"],
        "role_label": role_meta["label"],
        "role_variant": role_meta["variant"],
        "upcoming_vacation_label": vacation_display["upcoming_vacation_label"],
        "is_working": is_working_now,
        "status_label": status_label,
        "schedule_status": schedule_status or {},
        "profile_url": f"{reverse('employee_profile', args=[employee.id])}?from=employees",
    }


def _get_employee_status_context(employee):
    vacation_display = _collect_employee_vacation_display([employee.id]).get(employee.id, _empty_vacation_display())
    is_working_now = not vacation_display["is_currently_on_vacation"]
    status_label = "Работает"
    if not is_working_now:
        current_vacation_end = vacation_display.get("current_vacation_end")
        status_label = (
            f"В отпуске до {_format_short_date(current_vacation_end)}"
            if current_vacation_end
            else "В отпуске"
        )
    return {
        "employee_is_working": is_working_now,
        "employee_status_label": status_label,
    }


def _get_period_years(start_date, end_date):
    return list(range(start_date.year, end_date.year + 1))


def _schedule_item_source_label(item):
    if item.source == VacationScheduleItem.SOURCE_MANUAL:
        return "Дополнение к графику"
    if item.source == VacationScheduleItem.SOURCE_TRANSFER:
        return "Перенос"
    return "Годовой график"


def _schedule_item_status_label(item):
    if item.status == VacationScheduleItem.STATUS_APPROVED:
        return "График утвержден"
    return "Запланировано"


def _vacation_stage_meta(start_date, end_date, today=None):
    today = today or timezone.localdate()
    if end_date < today:
        return {
            "stage": "past",
            "stage_label": "Прошел",
            "stage_icon": "task_alt",
        }
    if start_date <= today <= end_date:
        return {
            "stage": "current",
            "stage_label": "Идет сейчас",
            "stage_icon": "beach_access",
        }
    return {
        "stage": "upcoming",
        "stage_label": "Предстоит",
        "stage_icon": "event",
    }


def _build_profile_calendar_url(employee_id, start_date, end_date):
    calendar_query = urlencode({
        "view": "month",
        "year": start_date.year,
        "month": start_date.month,
        "employee": employee_id,
        "calendar_focus_employee": employee_id,
        "calendar_focus_start": start_date.isoformat(),
        "calendar_focus_end": end_date.isoformat(),
    })
    return f'{reverse("calendar")}?{calendar_query}'


def _append_calendar_return_params(calendar_url, *, source, back_url, back_label):
    if not calendar_url or not source or not back_url or not back_label:
        return calendar_url

    parsed = urlsplit(calendar_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(
        {
            "from": source,
            "back_url": back_url,
            "back_label": back_label,
        }
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _employee_profile_context_url(employee_id, source="", back_url="", back_label=""):
    profile_url = reverse("employee_profile", args=[employee_id])
    query = {}
    if source:
        query["from"] = source
    if back_url and back_label:
        query["back_url"] = back_url
        query["back_label"] = back_label
    if query:
        return f"{profile_url}?{urlencode(query)}"
    return profile_url


def _employees_list_context_url(query_params):
    if hasattr(query_params, "urlencode"):
        query_string = query_params.urlencode() if query_params else ""
    else:
        query_string = urlencode(query_params) if query_params else ""
    if query_string:
        return f"{reverse('employees')}?{query_string}"
    return reverse("employees")


def _apply_calendar_return_context(context, *, source, back_url, back_label):
    seen_entries = set()
    planned_vacations = context.get("planned_vacations") or {}
    for entry in list(planned_vacations.get("entries") or []) + list(planned_vacations.get("initial_entries") or []):
        if not entry or id(entry) in seen_entries:
            continue
        seen_entries.add(id(entry))
        entry["calendar_url"] = _append_calendar_return_params(
            entry.get("calendar_url", ""),
            source=source,
            back_url=back_url,
            back_label=back_label,
        )

    upcoming = planned_vacations.get("upcoming")
    if upcoming and id(upcoming) not in seen_entries:
        upcoming["calendar_url"] = _append_calendar_return_params(
            upcoming.get("calendar_url", ""),
            source=source,
            back_url=back_url,
            back_label=back_label,
        )

    schedule_status = (context.get("profile_summary") or {}).get("schedule_status") or {}
    if schedule_status.get("calendar_url"):
        schedule_status["calendar_url"] = _append_calendar_return_params(
            schedule_status["calendar_url"],
            source=source,
            back_url=back_url,
            back_label=back_label,
        )


def _serialize_profile_schedule_item(item, current_employee=None, today=None):
    period_years = _get_period_years(item.start_date, item.end_date)
    stage_meta = _vacation_stage_meta(item.start_date, item.end_date, today=today)
    detail_reference = get_schedule_item_detail_reference(item)
    prefetched_change_requests = getattr(item, "_prefetched_objects_cache", {}).get("change_requests")
    has_pending_change_request = (
        any(
            change_request.status == VacationScheduleChangeRequest.STATUS_PENDING
            for change_request in prefetched_change_requests
        )
        if prefetched_change_requests is not None
        else VacationScheduleChangeRequest.objects.filter(
            schedule_item_id=item.id,
            status=VacationScheduleChangeRequest.STATUS_PENDING,
        ).exists()
    )
    transfer_action = build_schedule_change_transfer_action(
        actor=current_employee,
        employee=item.employee,
        schedule_item_id=item.id,
        start_date=item.start_date,
        end_date=item.end_date,
        vacation_type_label=item.get_vacation_type_display(),
        schedule_status=item.status,
        today=today,
        pending_change_exists=has_pending_change_request,
    )
    return {
        "id": f"schedule-{item.id}",
        "period_label": format_period_label(item.start_date, item.end_date),
        "source_label": _schedule_item_source_label(item),
        "source_kind": "schedule",
        "vacation_type": item.vacation_type,
        "vacation_type_label": item.get_vacation_type_display(),
        "status": f"schedule-{item.status}",
        "status_label": _schedule_item_status_label(item),
        "stage": stage_meta["stage"],
        "stage_label": stage_meta["stage_label"],
        "stage_icon": stage_meta["stage_icon"],
        "days": get_requested_days(item.start_date, item.end_date),
        "calendar_url": _build_profile_calendar_url(item.employee_id, item.start_date, item.end_date),
        "detail_url": detail_reference["detail_url"],
        "detail_label": detail_reference["detail_label"],
        "start_date": item.start_date,
        "end_date": item.end_date,
        "years": period_years,
        "years_attr": " ".join(str(year) for year in period_years),
        "sort_key": item.start_date.toordinal(),
    } | transfer_action


def _serialize_profile_approved_request(request_obj, today=None):
    period_years = _get_period_years(request_obj.start_date, request_obj.end_date)
    stage_meta = _vacation_stage_meta(request_obj.start_date, request_obj.end_date, today=today)
    return {
        "id": f"request-{request_obj.id}",
        "period_label": format_period_label(request_obj.start_date, request_obj.end_date),
        "source_label": "Одобренная заявка",
        "source_kind": "request",
        "vacation_type": request_obj.vacation_type,
        "vacation_type_label": request_obj.get_vacation_type_display(),
        "status": "request-approved",
        "status_label": "Одобрено",
        "stage": stage_meta["stage"],
        "stage_label": stage_meta["stage_label"],
        "stage_icon": stage_meta["stage_icon"],
        "days": get_requested_days(request_obj.start_date, request_obj.end_date),
        "calendar_url": _build_profile_calendar_url(
            request_obj.employee_id,
            request_obj.start_date,
            request_obj.end_date,
        ),
        "detail_url": reverse("vacation_detail", args=[request_obj.id]),
        "detail_label": "Открыть заявку",
        "start_date": request_obj.start_date,
        "end_date": request_obj.end_date,
        "years": period_years,
        "years_attr": " ".join(str(year) for year in period_years),
        "sort_key": request_obj.start_date.toordinal(),
        "can_request_transfer": False,
        "transfer_url": "",
        "transfer_preview_url": "",
        "transfer_title": "",
        "transfer_action_label": "",
        "transfer_submit_label": "",
        "transfer_hint": "",
        "transfer_modal_title": "",
        "transfer_modal_subtitle": "",
    }


def _build_planned_vacations_context(employee, current_employee=None, year=None):
    today = timezone.localdate()
    year = year or today.year
    schedule_items = VacationScheduleItem.objects.select_related(
        "employee",
        "employee__department",
        "schedule",
        "created_from_vacation_request",
        "created_from_change_request",
    ).filter(
        employee=employee,
        status__in=VacationScheduleItem.ACTIVE_STATUSES,
    ).prefetch_related("change_requests")
    request_qs = VacationRequest.objects.filter(
        employee=employee,
        status=VacationRequest.STATUS_APPROVED,
    )
    request_qs = exclude_converted_paid_requests(request_qs, employee_ids=[employee.id])

    rows = [
        _serialize_profile_schedule_item(item, current_employee=current_employee, today=today)
        for item in schedule_items
    ]
    rows.extend(
        _serialize_profile_approved_request(request_obj, today=today)
        for request_obj in request_qs
    )
    rows.sort(
        key=lambda row: (
            row["start_date"],
            row["end_date"],
            row["id"],
        ),
        reverse=True,
    )

    available_years = {
        row_year
        for row in rows
        for row_year in row["years"]
    }
    available_years.add(year)

    initial_entries = [
        row
        for row in rows
        if year in row["years"]
    ]
    upcoming_candidates = [row for row in rows if row["end_date"] >= today]
    upcoming = (
        min(
            upcoming_candidates,
            key=lambda row: (
                0 if row["start_date"] <= today <= row["end_date"] else 1,
                row["start_date"],
                row["end_date"],
            ),
        )
        if upcoming_candidates
        else None
    )
    return {
        "year": year,
        "entries": rows,
        "initial_entries": initial_entries,
        "initial_count": len(initial_entries),
        "available_years": sorted(available_years, reverse=True),
        "upcoming": upcoming,
    }


def _get_employee_schedule_change_rows(employee):
    change_requests = VacationScheduleChangeRequest.objects.select_related(
        "employee",
        "employee__department",
        "schedule_item",
        "schedule_item__schedule",
        "requested_by",
        "reviewed_by",
    ).filter(employee=employee).order_by("-created_at")
    rows = []
    for change_request in change_requests:
        row = enrich_schedule_change_request(change_request)
        period_years = sorted(
            set(_get_period_years(row.old_start_date, row.old_end_date))
            | set(_get_period_years(row.new_start_date, row.new_end_date))
        )
        row.years = period_years
        row.years_attr = " ".join(str(year) for year in period_years)
        rows.append(row)
    rows.sort(
        key=lambda row: (
            row.new_start_date,
            row.old_start_date,
            row.id,
        ),
        reverse=True,
    )
    return rows


def _get_schedule_item_chargeable_days(item):
    if item.chargeable_days:
        return item.chargeable_days
    return get_chargeable_leave_days(item.start_date, item.end_date, item.vacation_type)


def _get_profile_scheduled_paid_days(employee, year):
    schedule_items = VacationScheduleItem.objects.filter(
        employee=employee,
        schedule__year=year,
        vacation_type="paid",
        status__in=VacationScheduleItem.ACTIVE_STATUSES,
    ).only("start_date", "end_date", "vacation_type", "chargeable_days")
    return quantize_leave_days(
        sum(
            (Decimal(_get_schedule_item_chargeable_days(item)) for item in schedule_items),
            Decimal("0.00"),
        )
    )


def _get_profile_pending_paid_request_days(employee, year):
    pending_requests = VacationRequest.objects.filter(
        employee=employee,
        status=VacationRequest.STATUS_PENDING,
        vacation_type="paid",
        start_date__year=year,
    ).only("start_date", "end_date", "vacation_type")
    return quantize_leave_days(
        sum(
            (
                Decimal(get_chargeable_leave_days(request_obj.start_date, request_obj.end_date, request_obj.vacation_type))
                for request_obj in pending_requests
            ),
            Decimal("0.00"),
        )
    )


def _build_profile_summary_context(employee, leave_summary, planned_vacations):
    vacation_display = _collect_employee_vacation_display([employee.id]).get(
        employee.id,
        _empty_vacation_display(),
    )
    role_meta = get_employee_role_card_meta(employee)
    planned_entries = planned_vacations["initial_entries"]
    planned_days = sum((entry["days"] for entry in planned_entries), 0)
    planned_vacation_count = len(planned_entries)
    today = timezone.localdate()
    current_year_start = date(planned_vacations["year"], 1, 1)
    current_year_end = date(planned_vacations["year"], 12, 31)
    remaining_entries = []
    remaining_days = 0
    for entry in planned_entries:
        remaining_start = max(entry["start_date"], current_year_start, today)
        remaining_end = min(entry["end_date"], current_year_end)
        if remaining_start > remaining_end:
            continue
        remaining_entries.append(entry)
        remaining_days += get_requested_days(remaining_start, remaining_end)
    pending_requests_count = VacationRequest.objects.filter(
        employee=employee,
        status=VacationRequest.STATUS_PENDING,
    ).count()
    pending_change_requests_count = VacationScheduleChangeRequest.objects.filter(
        employee=employee,
        status=VacationScheduleChangeRequest.STATUS_PENDING,
    ).count()
    scheduled_paid_days = _get_profile_scheduled_paid_days(employee, planned_vacations["year"])
    pending_paid_request_days = _get_profile_pending_paid_request_days(employee, planned_vacations["year"])
    available_now_days = quantize_leave_days(leave_summary["available"])
    production_group = (
        employee.employee_position.production_group
        if getattr(employee, "employee_position_id", None) and employee.employee_position
        else None
    )
    department_deputy = _get_employee_department_deputy(employee)
    role_meta = _get_employee_list_role_meta(employee, role_meta, department_deputy=department_deputy)
    department_deputy_name = department_deputy.name if department_deputy else ""
    schedule_status = build_employee_schedule_status_map(
        [employee.id],
        planned_vacations["year"],
    ).get(employee.id, {})
    return {
        "role_icon": role_meta["icon"],
        "role_icon_type": role_meta["icon_type"],
        "role_label": role_meta["label"],
        "role_variant": role_meta["variant"],
        "management_badges": _get_employee_management_badges(employee, department_deputy=department_deputy),
        "new_hire_badge": build_new_hire_badge(employee),
        "production_group_label": production_group.name if production_group else "Не указана",
        "is_department_deputy": bool(department_deputy_name),
        "department_deputy_label": department_deputy_name,
        "is_enterprise_deputy": employee.is_enterprise_deputy,
        "upcoming_vacation_label": vacation_display["upcoming_vacation_label"],
        "planned_vacation_days": planned_days,
        "planned_vacation_count": planned_vacation_count,
        "planned_vacation_count_label": _format_vacation_count_label(planned_vacation_count),
        "remaining_year_vacation_days": remaining_days,
        "remaining_year_vacation_count": len(remaining_entries),
        "remaining_year_vacation_count_label": _format_vacation_count_label(len(remaining_entries)),
        "pending_requests_count": pending_requests_count + pending_change_requests_count,
        "available_now_days": available_now_days,
        "scheduled_paid_days": scheduled_paid_days,
        "pending_paid_request_days": pending_paid_request_days,
        "schedule_status": schedule_status,
    }


def _get_visible_employees_queryset(current_employee):
    queryset = Employees.objects.select_related(
        "department",
        "managed_department",
        "deputy_department",
        "employee_position",
        "employee_position__production_group",
    ).filter(is_active_employee=True).exclude(
        role__in=Employees.SERVICE_ROLES
    ).order_by(
        "last_name",
        "first_name",
        "middle_name",
    )
    if current_employee is None:
        return queryset.none()
    if is_hr_employee(current_employee) or is_enterprise_head_employee(current_employee):
        return queryset
    if is_department_head_employee(current_employee):
        managed_department_id = get_managed_department_id(current_employee)
        return queryset.filter(department_id=managed_department_id) if managed_department_id else queryset.none()
    if current_employee.department_id:
        return queryset.filter(department_id=current_employee.department_id)
    return queryset.filter(pk=current_employee.pk)


def _normalize_employee_search_query(value):
    return " ".join((value or "").split())


def _filter_employees_by_name(queryset, search_query):
    for token in search_query.split():
        queryset = queryset.filter(
            Q(last_name__icontains=token)
            | Q(first_name__icontains=token)
            | Q(middle_name__icontains=token)
        )
    return queryset


def _build_leave_profile_context(employee, current_employee=None):
    leave_summary = get_employee_leave_summary(employee)
    planned_vacations = _build_planned_vacations_context(employee, current_employee=current_employee or employee)
    schedule_change_requests = _get_employee_schedule_change_rows(employee)
    available_years = set(planned_vacations["available_years"])
    for change_request in schedule_change_requests:
        available_years.update(change_request.years)
    planned_vacations["available_years"] = sorted(available_years, reverse=True)
    context = {
        "employee": employee,
        "all_requests": get_employee_vacation_requests(employee),
        "leave_summary": leave_summary,
        "entitlement_rows": get_employee_entitlement_rows(employee),
        "planned_vacations": planned_vacations,
        "schedule_change_requests": schedule_change_requests,
        "profile_summary": _build_profile_summary_context(employee, leave_summary, planned_vacations),
        "total_balance": leave_summary["available"],
    }
    context.update(_get_employee_status_context(employee))
    return context


def build_main_profile_context(employee):
    can_edit = can_edit_employee_data(employee)
    context = _build_leave_profile_context(employee, current_employee=employee)
    _apply_calendar_return_context(
        context,
        source="profile",
        back_url=reverse("main"),
        back_label="К профилю",
    )
    context.update(
        {
            "can_edit_employee": can_edit,
            "show_manager_fields": can_edit,
            "sidebar_section": "profile",
            "employee_profile_detail_source": "profile",
            "employee_profile_detail_back_label": "К профилю",
        }
    )
    return context


def build_employee_profile_context(
    current_employee,
    employee,
    source="",
    return_to="",
    vacation_id="",
    transfer_id="",
    query_params=None,
):
    can_edit = can_edit_employee_data(current_employee) and employee.is_active_employee
    back_links = {
        "profile": {
            "label": "К профилю",
            "url": reverse("main"),
            "section": "profile",
            "use_remembered_list": False,
        },
        "calendar": {
            "label": "К графику",
            "url": reverse("calendar"),
            "section": "calendar",
            "use_remembered_list": False,
        },
        "preferences": {
            "label": "К сбору",
            "url": reverse("calendar"),
            "section": "calendar",
            "use_remembered_list": False,
        },
        "applications": {
            "label": "К заявкам",
            "url": reverse("applications"),
            "section": "applications",
            "use_remembered_list": True,
        },
        "employees": {
            "label": "К сотрудникам",
            "url": reverse("employees"),
            "section": "employees",
            "use_remembered_list": True,
        },
        "departments": {
            "label": "К отделам",
            "url": reverse("departments"),
            "section": "departments",
            "use_remembered_list": True,
        },
        "analytics": {
            "label": "К аналитике",
            "url": reverse("analytics"),
            "section": "analytics",
            "use_remembered_list": False,
        },
        "staffing": {
            "label": "К правилам состава",
            "url": reverse("staffing_rules"),
            "section": "staffing",
            "use_remembered_list": False,
        },
        "notifications": {
            "label": "К уведомлениям",
            "url": reverse("notifications"),
            "section": "notifications",
            "use_remembered_list": False,
        },
    }
    source = source if source in back_links else ""
    if source == "applications" and not can_access_applications(current_employee):
        source = ""
    sidebar_section = "employees" if current_employee and current_employee.id != employee.id else "profile"
    explicit_back_link = build_explicit_back_link(query_params or {}, section=source)
    if source and return_to == "vacation" and str(vacation_id).isdigit():
        vacation_url = reverse("vacation_detail", args=[int(vacation_id)])
        if source:
            vacation_url = f"{vacation_url}?{urlencode({'from': source})}"
        back_links[source] = {
            "label": "К заявке",
            "url": vacation_url,
            "section": source,
            "use_remembered_list": False,
        }
    if source and return_to == "transfer" and str(transfer_id).isdigit():
        transfer_url = reverse("schedule_change_detail", args=[int(transfer_id)])
        if source:
            transfer_url = f"{transfer_url}?{urlencode({'from': source})}"
        back_links[source] = {
            "label": "К переносу",
            "url": transfer_url,
            "section": source,
            "use_remembered_list": False,
        }
    context = _build_leave_profile_context(employee, current_employee=current_employee)
    navigation_source = "calendar" if source == "preferences" else source or sidebar_section
    profile_context_url = _employee_profile_context_url(
        employee.id,
        navigation_source,
        (query_params or {}).get("back_url", ""),
        (query_params or {}).get("back_label", ""),
    )
    calendar_source = navigation_source if navigation_source in CALENDAR_RETURN_CONTEXT_SOURCES else sidebar_section
    if calendar_source in CALENDAR_RETURN_CONTEXT_SOURCES:
        _apply_calendar_return_context(
            context,
            source=calendar_source,
            back_url=profile_context_url,
            back_label="К сотруднику",
        )
    context.update(
        {
            "can_edit_employee": can_edit,
            "can_delete_employee": can_delete_employee(current_employee, employee),
            "show_manager_fields": can_edit,
            "sidebar_section": navigation_source,
            "employee_profile_back_link": explicit_back_link or back_links.get(source),
            "employee_profile_detail_source": navigation_source,
            "employee_profile_detail_back_label": "К профилю",
        }
    )
    return context


def build_employees_page_context(current_employee, query_params, session):
    employees_qs = _get_visible_employees_queryset(current_employee)
    department_id = "all"
    if is_hr_employee(current_employee) or is_enterprise_head_employee(current_employee):
        department_id = query_params.get("department", session.get("selected_department", "all"))
    elif is_department_head_employee(current_employee):
        managed_department_id = get_managed_department_id(current_employee)
        department_id = str(managed_department_id) if managed_department_id else "all"
    elif current_employee and current_employee.department_id:
        department_id = str(current_employee.department_id)

    group_filter = resolve_production_group_filter_context(
        current_employee,
        selected_department=department_id,
        selected_group=query_params.get("group", "all"),
    )
    department_id = group_filter["selected_department"]
    group_id = group_filter["selected_group_id"]

    if department_id and department_id != "all":
        employees_qs = employees_qs.filter(department_id=department_id)
    if group_id is not None:
        employees_qs = employees_qs.filter(employee_position__production_group_id=group_id)

    status = query_params.get("status", "None")
    selected_schedule_status = query_params.get("schedule_status", "all")
    if selected_schedule_status not in EMPLOYEE_SCHEDULE_STATUS_FILTER_VALUES:
        selected_schedule_status = "all"
    search_query = _normalize_employee_search_query(query_params.get("search", ""))
    if search_query:
        employees_qs = _filter_employees_by_name(employees_qs, search_query)

    employees_qs = list(employees_qs)
    vacation_display_by_employee = _collect_employee_vacation_display(employee.id for employee in employees_qs)
    current_vacation_employee_ids = {
        employee_id
        for employee_id, vacation_display in vacation_display_by_employee.items()
        if vacation_display["is_currently_on_vacation"]
    }
    if status == "True":
        employees_qs = [employee for employee in employees_qs if employee.id not in current_vacation_employee_ids]
    elif status == "False":
        employees_qs = [employee for employee in employees_qs if employee.id in current_vacation_employee_ids]

    schedule_status_by_employee = build_employee_schedule_status_map(
        [employee.id for employee in employees_qs],
        timezone.localdate().year,
    )
    employees_back_url = _employees_list_context_url(query_params)
    for schedule_status_item in schedule_status_by_employee.values():
        if schedule_status_item.get("calendar_url"):
            schedule_status_item["calendar_url"] = _append_calendar_return_params(
                schedule_status_item["calendar_url"],
                source="employees",
                back_url=employees_back_url,
                back_label="К сотрудникам",
            )
    if selected_schedule_status != "all":
        employees_qs = [
            employee
            for employee in employees_qs
            if schedule_status_by_employee.get(employee.id, {}).get("key") == selected_schedule_status
        ]

    leave_summaries = get_employee_list_leave_summaries(employees_qs)
    employees_list = [
        _serialize_employee_row(
            employee,
            leave_summaries[employee.id],
            vacation_display=vacation_display_by_employee.get(employee.id),
            schedule_status=schedule_status_by_employee.get(employee.id),
        )
        for employee in employees_qs
    ]

    return {
        "employees": employees_list,
        "employees_count": len(employees_list),
        "selected_status": status,
        "selected_schedule_status": selected_schedule_status,
        "schedule_status_options": EMPLOYEE_SCHEDULE_STATUS_FILTER_OPTIONS,
        "selected_department": department_id,
        "selected_group": group_filter["selected_group"],
        "group_options": group_filter["group_options"],
        "show_group_filter": (
            is_hr_employee(current_employee)
            or is_enterprise_head_employee(current_employee)
            or is_department_head_employee(current_employee)
        ),
        "show_department_filter": is_hr_employee(current_employee) or is_enterprise_head_employee(current_employee),
        "show_group_department_labels": group_filter["show_group_department_labels"],
        "search_query": search_query,
    }


def build_departments_queryset(current_employee):
    departments_qs = Departments.objects.select_related("head").annotate(
        employee_count=Count("employees", filter=Q(employees__is_active_employee=True))
    ).order_by("name")
    if is_department_head_employee(current_employee):
        managed_department_id = get_managed_department_id(current_employee)
        departments_qs = departments_qs.filter(id=managed_department_id) if managed_department_id else departments_qs.none()
    return departments_qs


def _get_department_workload_label(load_level):
    labels = {
        1: "Низкая",
        2: "Спокойная",
        3: "Средняя",
        4: "Высокая",
        5: "Критичная",
    }
    return labels.get(load_level, "Нет данных")


def _decorate_departments_for_page(departments_qs):
    departments = list(departments_qs)
    department_ids = [department.id for department in departments]
    if not department_ids:
        return departments

    today = timezone.localdate()
    employees = list(
        Employees.objects.filter(
            department_id__in=department_ids,
            is_active_employee=True,
        )
        .exclude(role__in=Employees.SERVICE_ROLES)
        .values("id", "department_id")
    )
    employee_ids = [employee["id"] for employee in employees]
    employee_department = {employee["id"]: employee["department_id"] for employee in employees}

    current_vacation_employee_ids = _get_current_vacation_employee_ids(employee_ids, as_of_date=today)
    current_vacation_counts = Counter(
        employee_department[employee_id]
        for employee_id in current_vacation_employee_ids
        if employee_id in employee_department
    )
    pending_request_counts = Counter(
        VacationRequest.objects.filter(
            employee__department_id__in=department_ids,
            status=VacationRequest.STATUS_PENDING,
        ).values_list("employee__department_id", flat=True)
    )
    pending_change_counts = Counter(
        VacationScheduleChangeRequest.objects.filter(
            employee__department_id__in=department_ids,
            status=VacationScheduleChangeRequest.STATUS_PENDING,
        ).values_list("employee__department_id", flat=True)
    )
    workloads = {
        workload.department_id: workload
        for workload in DepartmentWorkload.objects.filter(
            department_id__in=department_ids,
            year=today.year,
            month=today.month,
        )
    }
    staffing_forecasts = build_department_staffing_forecast_map(departments, start_date=today)

    for department in departments:
        workload = workloads.get(department.id)
        workload_level = workload.load_level if workload else None
        staffing_forecast = staffing_forecasts.get(department.id, {})
        department.head_position_label = department.head.position if department.head and department.head.position else ""
        department.current_vacation_count = current_vacation_counts[department.id]
        department.pending_applications_count = pending_request_counts[department.id] + pending_change_counts[department.id]
        department.workload_level = workload_level
        department.workload_label = _get_department_workload_label(workload_level)
        department.staffing_forecast_level = staffing_forecast.get("level", "ok")
        department.staffing_forecast_label = staffing_forecast.get("label", "Состав стабилен")
        department.staffing_forecast_icon = staffing_forecast.get("icon", "verified")
        department.staffing_forecast_window_label = staffing_forecast.get("window_label", "30 дней")
        department.staffing_forecast_summary = staffing_forecast.get(
            "summary",
            "Критичных рисков на 30 дней не найдено.",
        )
        department.staffing_forecast_reasons = staffing_forecast.get("reasons", [])
        department.staffing_forecast_primary_reason = staffing_forecast.get(
            "primary_reason",
            "30 дней · критичных рисков нет",
        )
        department.staffing_forecast_has_risk = staffing_forecast.get("has_risk", False)
        department.staffing_peak_absent_count = staffing_forecast.get("peak_absent_count", 0)
        department.staffing_peak_absent_label = staffing_forecast.get("peak_absent_label", "0 сотрудников")
        department.staffing_min_remaining_count = staffing_forecast.get("min_remaining_staff_count", 0)
        department.staffing_min_remaining_label = staffing_forecast.get("min_remaining_label", "0 сотрудников")
        department.staffing_min_reserve_count = staffing_forecast.get("min_reserve_count", 0)
        department.staffing_min_reserve_label = staffing_forecast.get("min_reserve_label", "нет резерва")
        department.staffing_conflict_days_count = staffing_forecast.get("conflict_days_count", 0)

    return departments


def serialize_departments_queryset(departments_qs):
    return list(departments_qs.values("id", "name", "date_added"))


def build_departments_page_context(departments_qs, department_create_form, department_modal_open, can_create_department):
    departments = _decorate_departments_for_page(departments_qs)
    return {
        "departments": departments,
        "departments_count": len(departments),
        "can_create_department": can_create_department,
        "department_create_form": department_create_form,
        "department_head_candidates": department_create_form.fields["head"].queryset,
        "department_modal_open": department_modal_open,
    }


def _build_department_detail_employee_context(current_employee, department, query_params, session):
    employee_query_params = {
        "department": str(department.id),
        "group": query_params.get("group", "all"),
        "status": query_params.get("status", "None"),
        "schedule_status": query_params.get("schedule_status", "all"),
        "search": query_params.get("search", ""),
    }
    return build_employees_page_context(current_employee, employee_query_params, session)


def _get_department_detail_url(department_id, group_id=None):
    url = reverse("department_detail", args=[department_id])
    if group_id:
        return f"{url}?{urlencode({'group': group_id})}"
    return url


def _build_group_count_map(queryset, group_field):
    return {
        row[group_field]: row["count"]
        for row in queryset.values(group_field).annotate(count=Count("id"))
        if row[group_field] is not None
    }


def _decorate_department_groups_for_detail(department, selected_group):
    today = timezone.localdate()
    groups = list(
        ProductionGroup.objects.filter(department=department)
        .prefetch_related("positions")
        .order_by("name")
    )
    if not groups:
        return []

    group_ids = {group.id for group in groups}
    active_employees = list(
        Employees.objects.select_related("employee_position", "employee_position__production_group")
        .filter(department=department, is_active_employee=True)
        .exclude(role__in=Employees.SERVICE_ROLES)
        .only("id", "employee_position_id", "employee_position__production_group_id")
    )
    employee_ids = [employee.id for employee in active_employees]
    employee_group_ids = {
        employee.id: employee.employee_position.production_group_id
        for employee in active_employees
        if employee.employee_position_id
        and employee.employee_position
        and employee.employee_position.production_group_id in group_ids
    }
    employee_counts = Counter(employee_group_ids.values())
    current_vacation_employee_ids = _get_current_vacation_employee_ids(employee_ids, as_of_date=today)
    current_vacation_counts = Counter(
        employee_group_ids[employee_id]
        for employee_id in current_vacation_employee_ids
        if employee_id in employee_group_ids
    )
    pending_request_counts = _build_group_count_map(
        VacationRequest.objects.filter(
            employee__department=department,
            status=VacationRequest.STATUS_PENDING,
        ),
        "employee__employee_position__production_group_id",
    )
    pending_change_counts = _build_group_count_map(
        VacationScheduleChangeRequest.objects.filter(
            employee__department=department,
            status=VacationScheduleChangeRequest.STATUS_PENDING,
        ),
        "employee__employee_position__production_group_id",
    )
    staffing_forecasts = build_department_group_staffing_forecast_map(
        department,
        groups=groups,
        start_date=today,
    )

    for group in groups:
        staffing_forecast = staffing_forecasts.get(group.id, {})
        group.employee_count = employee_counts[group.id]
        group.current_vacation_count = current_vacation_counts[group.id]
        group.pending_applications_count = pending_request_counts.get(group.id, 0) + pending_change_counts.get(group.id, 0)
        group.workload_level = getattr(department, "workload_level", None)
        group.workload_label = getattr(department, "workload_label", "Нет данных")
        group.detail_url = _get_department_detail_url(department.id, group.id)
        group.is_selected = str(group.id) == str(selected_group)
        group.staffing_forecast_level = staffing_forecast.get("level", "ok")
        group.staffing_forecast_label = staffing_forecast.get("label", "Состав стабилен")
        group.staffing_forecast_icon = staffing_forecast.get("icon", "verified")
        group.staffing_forecast_window_label = staffing_forecast.get("window_label", "30 дней")
        group.staffing_forecast_primary_reason = staffing_forecast.get(
            "primary_reason",
            "30 дней · критичных рисков нет",
        )
        group.staffing_forecast_has_rule = staffing_forecast.get("has_rule", False)
        group.staffing_peak_absent_label = staffing_forecast.get("peak_absent_label", format_staff_count(0))
        group.staffing_min_reserve_label = staffing_forecast.get("min_reserve_label", "нет резерва")
        group.staffing_min_staff_label = staffing_forecast.get("min_staff_label", "Правило не задано")
        group.staffing_max_absent_label = staffing_forecast.get("max_absent_label", "Правило не задано")

    return groups


def build_department_detail_page_context(current_employee, department, query_params, session):
    decorated_departments = _decorate_departments_for_page([department])
    department = decorated_departments[0] if decorated_departments else department
    employees_context = _build_department_detail_employee_context(current_employee, department, query_params, session)
    selected_group = employees_context["selected_group"]
    department_groups = _decorate_department_groups_for_detail(department, selected_group)
    selected_group_label = ""
    if selected_group != "all":
        selected_group_label = next(
            (group.name for group in department_groups if str(group.id) == str(selected_group)),
            "",
        )

    context = {
        "sidebar_section": "departments",
        "department": department,
        "department_detail_back_link": {
            "label": "К отделам",
            "url": reverse("departments"),
            "section": "departments",
            "use_remembered_list": True,
        },
        "department_groups": department_groups,
        "department_groups_count": len(department_groups),
        "selected_group_label": selected_group_label,
        "department_detail_all_groups_url": _get_department_detail_url(department.id),
    }
    context.update(employees_context)
    return context
