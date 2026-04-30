import calendar
from datetime import date

from django.utils import timezone

from apps.accounts.services import (
    ROLE_LABELS,
    can_approve_leave_for_employee,
    get_accessible_departments,
    is_authorized_person_employee,
)
from apps.employees.models import Employees
from apps.leave.models import VacationRequest, VacationScheduleChangeRequest, VacationScheduleItem

from .analytics import build_analytics_payload
from .approval_routes import get_expected_vacation_approver
from .calendar import build_calendar_base_data, build_calendar_rows, build_calendar_summary
from .constants import LEAVE_ADVANCE_MONTHS, RUSSIAN_MONTH_NAMES, RUSSIAN_MONTH_SHORT_NAMES, WEEKDAY_SHORT_NAMES
from .dates import add_months_safe, get_chargeable_leave_days, get_russian_holiday_iso_dates
from .ledger import (
    get_employee_available_balance,
    get_employee_entitlement_rows,
    get_employee_entitlement_source_preview,
    get_employee_leave_summary,
    get_employee_remaining_balance,
)
from .metrics import sync_employee_vacation_metrics
from .querysets import get_vacation_requests_queryset
from .request_history import get_vacation_request_history
from .requests import enrich_vacation_request, serialize_vacation_request_row
from .schedule_changes import (
    enrich_schedule_change_request,
    get_schedule_change_requests_queryset,
    serialize_schedule_change_request_row,
)
from .scopes import (
    filter_by_employee_name,
    get_visible_employee_ids,
    normalize_employee_search_query,
    restrict_change_requests_queryset_for_employee,
    restrict_requests_queryset_for_employee,
)
from .validation import get_paid_request_eligibility_for_year


def _get_calendar_available_years(current_year, selected_year=None):
    years = set(VacationRequest.objects.values_list("start_date__year", flat=True))
    years.update(VacationRequest.objects.values_list("end_date__year", flat=True))
    years.update(VacationScheduleItem.objects.values_list("start_date__year", flat=True))
    years.update(VacationScheduleItem.objects.values_list("end_date__year", flat=True))
    available_years = sorted(year for year in years if year)
    return available_years or [current_year]


def build_calendar_page_context(current_employee, query_params):
    today = timezone.localdate()
    current_year = today.year

    selected_year = query_params.get("year", current_year)
    selected_month = query_params.get("month", today.month)
    calendar_view_mode = query_params.get("view", "month")
    selected_employee_id = query_params.get("employee")

    try:
        selected_year = int(selected_year)
    except (TypeError, ValueError):
        selected_year = current_year

    try:
        selected_month = int(selected_month)
    except (TypeError, ValueError):
        selected_month = today.month

    try:
        selected_employee_id = int(selected_employee_id) if selected_employee_id else None
    except (TypeError, ValueError):
        selected_employee_id = None

    if selected_month < 1 or selected_month > 12:
        selected_month = today.month
    if calendar_view_mode not in ("year", "month"):
        calendar_view_mode = "month"
    available_years = _get_calendar_available_years(current_year, selected_year)
    if selected_year not in available_years:
        selected_year = current_year if current_year in available_years else max(available_years)

    sync_employee_vacation_metrics(current_employee)
    current_employee.refresh_from_db()
    current_employee_leave_summary = get_employee_leave_summary(current_employee)
    current_employee_final_balance = current_employee_leave_summary["available"]

    context = {}
    visible_employee_ids = get_visible_employee_ids(current_employee)
    employees, employee_day_status, employee_entries = build_calendar_base_data(
        selected_year,
        employee_ids=visible_employee_ids,
    )
    calendar_rows, calendar_details = build_calendar_rows(
        employees,
        employee_day_status,
        employee_entries,
        selected_year,
        selected_month,
        calendar_view_mode,
        today,
        current_employee=current_employee,
    )
    calendar_summary = build_calendar_summary(
        employee_entries,
        selected_year,
        selected_month,
        calendar_view_mode,
    )

    employee_ids = {row["employee_id"] for row in calendar_rows}
    if selected_employee_id not in employee_ids:
        selected_employee_id = current_employee.id if current_employee and current_employee.id in employee_ids else None
    if selected_employee_id not in employee_ids and calendar_rows:
        selected_employee_id = calendar_rows[0]["employee_id"]

    selected_employee_detail = calendar_details.get(str(selected_employee_id)) if selected_employee_id else None
    selected_month_label = RUSSIAN_MONTH_NAMES[selected_month - 1]
    calendar_period_label = (
        f"График отпусков на {selected_month_label.lower()} {selected_year}"
        if calendar_view_mode == "month"
        else f"График отпусков на {selected_year} год"
    )
    calendar_period_description = (
        "Детали по сотруднику открываются кликом по строке."
        if calendar_view_mode == "month"
        else "Обзор отпусков по месяцам за выбранный год."
    )
    paid_request_allowed, paid_request_hint = get_paid_request_eligibility_for_year(current_employee, selected_year)
    paid_leave_available_from = add_months_safe(current_employee.date_joined, LEAVE_ADVANCE_MONTHS)
    paid_leave_waiting_period_active = today < paid_leave_available_from

    context.update(
        {
            "current_user": current_employee,
            "current_user_leave_summary": current_employee_leave_summary,
            "current_user_final_balance": current_employee_final_balance,
            "calendar_charge_preview": {
                "holiday_dates": get_russian_holiday_iso_dates(range(min(available_years), max(available_years) + 1)),
                "available_balance": float(current_employee_final_balance),
                "paid_request_allowed": paid_request_allowed,
            },
            "paid_request_allowed": paid_request_allowed,
            "paid_request_hint": paid_request_hint,
            "paid_leave_available_from": paid_leave_available_from,
            "paid_leave_waiting_period_active": paid_leave_waiting_period_active,
            "calendar_view_mode": calendar_view_mode,
            "calendar_period_label": calendar_period_label,
            "calendar_period_description": calendar_period_description,
            "calendar_filters": {
                "selected_year": selected_year,
                "selected_month": selected_month,
                "available_years": available_years,
                "available_months": [
                    {"value": index + 1, "label": month_name}
                    for index, month_name in enumerate(RUSSIAN_MONTH_NAMES)
                ],
            },
            "calendar_summary": calendar_summary,
            "calendar_legend": [
                {
                    "group": "Годовой график",
                    "items": [
                        {"status": "schedule-approved", "label": "График утвержден"},
                        {"status": "schedule-planned", "label": "Запланировано"},
                        {"status": "schedule-transferred", "label": "Перенесено"},
                        {"status": "schedule-cancelled", "label": "Отменено"},
                    ],
                },
                {
                    "group": "Заявки и изменения",
                    "items": [
                        {"status": "request-approved", "label": "Внеплановая заявка"},
                        {"status": "request-pending", "label": "Заявка ожидает"},
                        {"status": "request-rejected", "label": "Заявка отклонена"},
                    ],
                },
            ],
            "calendar_rows": calendar_rows,
            "calendar_details": calendar_details,
            "selected_employee_id": selected_employee_id,
            "selected_employee_detail": selected_employee_detail,
            "selected_month_name": selected_month_label,
            "year_short_headers": RUSSIAN_MONTH_SHORT_NAMES,
            "month_day_headers": [
                {
                    "day": day,
                    "weekday": WEEKDAY_SHORT_NAMES[date(selected_year, selected_month, day).weekday()],
                    "is_weekend": date(selected_year, selected_month, day).weekday() >= 5,
                    "is_today": date(selected_year, selected_month, day) == today,
                }
                for day in range(1, calendar.monthrange(selected_year, selected_month)[1] + 1)
            ],
            "today_iso": today.isoformat(),
        }
    )

    return context


def build_applications_page_context(current_employee, query_params):
    status_filter = query_params.get("status", "all")
    department_id = query_params.get("department", "all")
    search_query = normalize_employee_search_query(query_params.get("search", ""))
    requests_qs = restrict_requests_queryset_for_employee(
        get_vacation_requests_queryset().order_by("-created_at"),
        current_employee,
    )
    change_requests_qs = restrict_change_requests_queryset_for_employee(
        get_schedule_change_requests_queryset().order_by("-created_at"),
        current_employee,
    )

    if status_filter in {
        VacationRequest.STATUS_APPROVED,
        VacationRequest.STATUS_PENDING,
        VacationRequest.STATUS_REJECTED,
    }:
        requests_qs = requests_qs.filter(status=status_filter)
        change_requests_qs = change_requests_qs.filter(status=status_filter)

    accessible_departments = list(get_accessible_departments(current_employee))
    accessible_department_ids = {department.id for department in accessible_departments}
    if department_id != "all":
        try:
            department_id_int = int(department_id)
        except (TypeError, ValueError):
            department_id = "all"
        else:
            if department_id_int in accessible_department_ids:
                requests_qs = requests_qs.filter(employee__department_id=department_id_int)
                change_requests_qs = change_requests_qs.filter(employee__department_id=department_id_int)
            else:
                department_id = "all"

    if search_query:
        requests_qs = filter_by_employee_name(requests_qs, search_query)
        change_requests_qs = filter_by_employee_name(change_requests_qs, search_query)

    vacations = [enrich_vacation_request(request_obj) for request_obj in requests_qs]
    for vacation in vacations:
        vacation.can_approve = (
            vacation.status == VacationRequest.STATUS_PENDING
            and can_approve_leave_for_employee(current_employee, vacation.employee)
        )
        vacation.decision_locked = vacation.status == VacationRequest.STATUS_PENDING and not vacation.can_approve

    change_requests = [enrich_schedule_change_request(change_request) for change_request in change_requests_qs]
    for change_request in change_requests:
        change_request.can_approve = (
            change_request.status == VacationScheduleChangeRequest.STATUS_PENDING
            and can_approve_leave_for_employee(current_employee, change_request.employee)
        )
        change_request.decision_locked = (
            change_request.status == VacationScheduleChangeRequest.STATUS_PENDING
            and not change_request.can_approve
        )

    return {
        "vacations": vacations,
        "change_requests": change_requests,
        "selected_status": status_filter,
        "selected_department": str(department_id),
        "search_query": search_query,
        "show_department_filter": not is_authorized_person_employee(current_employee),
    }


def build_applications_json_payload(vacations, change_requests):
    return {
        "vacations": [serialize_vacation_request_row(vacation) for vacation in vacations],
        "change_requests": [
            serialize_schedule_change_request_row(change_request)
            for change_request in change_requests
        ],
    }


def _get_balance_notice_for_vacation(vacation):
    if vacation.vacation_type == "unpaid":
        return (
            "Оплачиваемый баланс не используется",
            "Неоплачиваемый отпуск оформляется без сохранения заработной платы и не уменьшает остаток ежегодного оплачиваемого отпуска.",
        )
    if vacation.vacation_type == "study":
        return (
            "Оплачиваемый баланс не используется",
            "Учебный отпуск не уменьшает остаток ежегодного оплачиваемого отпуска.",
        )
    return "", ""


def _format_employee_count(value):
    value = int(value or 0)
    if value == 1:
        return "1 сотрудник"
    if 2 <= value <= 4:
        return f"{value} сотрудника"
    return f"{value} сотрудников"


def _get_vacation_risk_summary(vacation):
    label = vacation.risk_label.lower()
    if vacation.min_staff_required:
        summary = (
            f"Риск {label}: в отделе останется {_format_employee_count(vacation.remaining_staff_count)} "
            f"при минимуме {_format_employee_count(vacation.min_staff_required)}."
        )
    else:
        summary = f"Риск {label}: нагрузка отдела оценивается как {vacation.department_load_level}/5."
    if vacation.overlapping_absences_count:
        summary += f" Одновременно отсутствуют: {_format_employee_count(vacation.overlapping_absences_count)}."
    return summary


def _get_vacation_approval_route(vacation, current_employee, can_approve_vacation):
    expected = get_expected_vacation_approver(vacation.employee)
    current_role = ROLE_LABELS.get(getattr(current_employee, "role", ""), "роль не определена")
    reviewer = expected.employee
    reviewer_name = (reviewer.full_name or reviewer.login) if reviewer else ""
    if vacation.status != VacationRequest.STATUS_PENDING:
        availability = "Заявка уже рассмотрена, маршрут закрыт."
    elif can_approve_vacation:
        availability = "Текущий пользователь находится на нужном уровне согласования."
    else:
        availability = f"Решение недоступно: нужна роль «{expected.role_label}», текущая роль — «{current_role}»."

    return {
        "role_label": expected.role_label,
        "reviewer_name": reviewer_name or "согласующий не назначен",
        "reason": expected.reason,
        "availability": availability,
    }


def build_vacation_detail_context(vacation, current_employee):
    enrich_vacation_request(vacation)
    can_approve_vacation = (
        vacation.status == VacationRequest.STATUS_PENDING
        and can_approve_leave_for_employee(current_employee, vacation.employee)
    )
    can_delete = vacation.status == VacationRequest.STATUS_PENDING and (
        vacation.employee_id == (current_employee.id if current_employee else None) or can_approve_vacation
    )
    employee_leave_summary = get_employee_leave_summary(vacation.employee, as_of_date=vacation.start_date)
    entitlement_rows = get_employee_entitlement_rows(vacation.employee, as_of_date=vacation.start_date)
    current_balance = get_employee_remaining_balance(vacation.employee)
    available_on_start_before_request = get_employee_available_balance(
        vacation.employee,
        as_of_date=vacation.start_date,
        exclude_request_id=vacation.id,
    )
    is_paid_vacation = vacation.vacation_type == "paid"
    entitlement_source_preview = get_employee_entitlement_source_preview(
        vacation.employee,
        vacation.start_date,
        vacation.end_date,
        vacation.vacation_type,
        exclude_request_id=vacation.id,
    )
    balance_notice_title, balance_notice_text = _get_balance_notice_for_vacation(vacation)

    return {
        "vacation": vacation,
        "employee": vacation.employee,
        "status": vacation.status,
        "status_label": vacation.status_label,
        "status_icon": vacation.status_icon,
        "status_css_class": vacation.status_css_class,
        "current_balance": current_balance,
        "available_on_start_before_request": available_on_start_before_request,
        "employee_leave_summary": employee_leave_summary,
        "entitlement_rows": entitlement_rows,
        "entitlement_source_preview": entitlement_source_preview,
        "is_paid_vacation": is_paid_vacation,
        "balance_notice_title": balance_notice_title,
        "balance_notice_text": balance_notice_text,
        "vacation_risk_summary": _get_vacation_risk_summary(vacation),
        "overlapping_absences_employee_label": _format_employee_count(vacation.overlapping_absences_count),
        "approval_route": _get_vacation_approval_route(vacation, current_employee, can_approve_vacation),
        "vacation_history": get_vacation_request_history(vacation),
        "system_recommendation_text": "Рекомендация системы будет доступна после подключения аналитического модуля.",
        "vacation_chargeable_days": get_chargeable_leave_days(
            vacation.start_date,
            vacation.end_date,
            vacation.vacation_type,
        ),
        "can_approve_vacation": can_approve_vacation,
        "can_delete": can_delete,
    }


def build_analytics_page_context(current_employee):
    visible_employee_ids = get_visible_employee_ids(current_employee)
    context = build_analytics_payload(employee_ids=visible_employee_ids)
    context.update({"default_annual_leave_days": 52})
    return context
