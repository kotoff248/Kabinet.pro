from collections import Counter
from urllib.parse import urlencode

from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format

from apps.accounts.services import (
    can_delete_employee,
    can_edit_employee_data,
    get_managed_department_id,
    is_department_head_employee,
    is_enterprise_head_employee,
    is_hr_employee,
)
from apps.employees.models import Departments, Employees
from apps.employees.role_presentation import get_employee_role_card_meta
from apps.leave.models import DepartmentWorkload, VacationRequest, VacationScheduleChangeRequest, VacationScheduleItem
from apps.leave.services.calendar import build_calendar_base_data
from apps.leave.services.ledger import (
    get_employee_entitlement_rows,
    get_employee_list_leave_summaries,
    get_employee_leave_summary,
)
from apps.leave.services.querysets import exclude_converted_paid_requests
from apps.leave.services.requests import get_employee_vacation_requests


def _format_days(value):
    return f"{value:.2f}".rstrip("0").rstrip(".")


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


def _serialize_employee_row(employee, leave_summary, vacation_display=None):
    vacation_display = vacation_display or _collect_employee_vacation_display([employee.id]).get(
        employee.id,
        _empty_vacation_display(),
    )
    role_meta = get_employee_role_card_meta(employee)
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
        "date_joined": date_format(employee.date_joined, "j E Y", use_l10n=True),
        "available_days": _format_days(leave_summary["available"]),
        "role_icon": role_meta["icon"],
        "role_icon_type": role_meta["icon_type"],
        "role_label": role_meta["label"],
        "role_variant": role_meta["variant"],
        "upcoming_vacation_label": vacation_display["upcoming_vacation_label"],
        "is_working": is_working_now,
        "status_label": status_label,
        "profile_url": reverse("employee_profile", args=[employee.id]),
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


def _build_planned_vacations_context(employee, year=None):
    today = timezone.localdate()
    year = year or today.year
    _, _, employee_entries = build_calendar_base_data(year, employee_ids=[employee.id])
    entries = employee_entries.get(employee.id, [])
    rows = []

    for entry in entries:
        calendar_query = urlencode({
            "view": "month",
            "year": entry["start_date"].year,
            "month": entry["start_date"].month,
            "employee": employee.id,
        })
        calendar_url = f'{reverse("calendar")}?{calendar_query}'
        rows.append(
            {
                "period_label": entry["period_label"],
                "source_label": entry["source_label"],
                "vacation_type_label": entry["vacation_type_label"],
                "status_label": entry["status_label"],
                "status": entry["display_status"],
                "days": entry["days"],
                "calendar_url": calendar_url,
                "detail_url": entry.get("detail_url", ""),
                "detail_label": entry.get("detail_label", "Открыть заявку"),
                "start_date": entry["start_date"],
                "end_date": entry["end_date"],
            }
        )

    upcoming = next((row for row in rows if row["end_date"] >= today), None)
    return {
        "year": year,
        "entries": rows,
        "upcoming": upcoming,
    }


def _get_visible_employees_queryset(current_employee):
    queryset = Employees.objects.select_related("department", "managed_department").filter(is_active_employee=True).exclude(
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


def _build_leave_profile_context(employee):
    leave_summary = get_employee_leave_summary(employee)
    context = {
        "employee": employee,
        "all_requests": get_employee_vacation_requests(employee),
        "leave_summary": leave_summary,
        "entitlement_rows": get_employee_entitlement_rows(employee),
        "planned_vacations": _build_planned_vacations_context(employee),
        "total_balance": leave_summary["available"],
    }
    context.update(_get_employee_status_context(employee))
    return context


def build_main_profile_context(employee):
    can_edit = can_edit_employee_data(employee)
    context = _build_leave_profile_context(employee)
    context.update(
        {
            "can_edit_employee": can_edit,
            "show_manager_fields": can_edit,
            "sidebar_section": "profile",
        }
    )
    return context


def build_employee_profile_context(current_employee, employee):
    can_edit = can_edit_employee_data(current_employee) and employee.is_active_employee
    context = _build_leave_profile_context(employee)
    context.update(
        {
            "can_edit_employee": can_edit,
            "can_delete_employee": can_delete_employee(current_employee, employee),
            "show_manager_fields": can_edit,
            "sidebar_section": "employees" if current_employee and current_employee.id != employee.id else "profile",
        }
    )
    return context


def build_employees_page_context(current_employee, query_params, session):
    employees_qs = _get_visible_employees_queryset(current_employee)
    department_id = "all"
    if is_hr_employee(current_employee) or is_enterprise_head_employee(current_employee):
        department_id = query_params.get("department", session.get("selected_department", "all"))
        if department_id and department_id != "all":
            employees_qs = employees_qs.filter(department_id=department_id)
    elif is_department_head_employee(current_employee):
        managed_department_id = get_managed_department_id(current_employee)
        employees_qs = employees_qs.filter(department_id=managed_department_id) if managed_department_id else employees_qs.none()
        department_id = str(managed_department_id) if managed_department_id else "all"
    elif current_employee and current_employee.department_id:
        employees_qs = employees_qs.filter(department_id=current_employee.department_id)
        department_id = str(current_employee.department_id)

    status = query_params.get("status", "None")
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

    leave_summaries = get_employee_list_leave_summaries(employees_qs)
    employees_list = [
        _serialize_employee_row(
            employee,
            leave_summaries[employee.id],
            vacation_display=vacation_display_by_employee.get(employee.id),
        )
        for employee in employees_qs
    ]

    return {
        "employees": employees_list,
        "employees_count": len(employees_list),
        "selected_status": status,
        "selected_department": department_id,
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

    for department in departments:
        workload = workloads.get(department.id)
        workload_level = workload.load_level if workload else None
        department.head_position_label = department.head.position if department.head and department.head.position else ""
        department.current_vacation_count = current_vacation_counts[department.id]
        department.pending_applications_count = pending_request_counts[department.id] + pending_change_counts[department.id]
        department.workload_level = workload_level
        department.workload_label = _get_department_workload_label(workload_level)

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
