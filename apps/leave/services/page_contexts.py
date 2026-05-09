import calendar
from datetime import date
from urllib.parse import urlencode

from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.accounts.services import (
    can_access_applications,
    can_approve_leave_for_employee,
    can_review_schedule_change_request,
    get_accessible_departments,
    is_authorized_person_employee,
    is_enterprise_head_employee,
    is_hr_employee,
)
from apps.core.services.navigation import build_explicit_back_link
from apps.employees.models import Employees
from apps.employees.role_presentation import get_employee_role_card_meta
from apps.employees.services import resolve_production_group_filter_context
from apps.leave.models import (
    VACATION_TYPE_CHOICES,
    VacationPreference,
    VacationPreferenceCollection,
    VacationRequest,
    VacationScheduleChangeRequest,
    VacationScheduleItem,
    VacationUrgentClosureRequest,
)

from .analytics import build_analytics_payload
from .approval_routes import get_expected_vacation_approver
from .calendar import (
    build_calendar_base_data,
    build_calendar_month_details,
    build_calendar_month_totals,
    build_calendar_rows,
    build_calendar_summary,
)
from .constants import LEAVE_ADVANCE_MONTHS, RUSSIAN_MONTH_NAMES, RUSSIAN_MONTH_SHORT_NAMES, WEEKDAY_SHORT_NAMES
from .dates import add_months_safe, get_chargeable_leave_days, get_russian_holiday_iso_dates
from .ledger import (
    get_employee_available_balance,
    get_employee_entitlement_rows,
    get_employee_entitlement_source_preview,
    get_employee_leave_summary,
    get_employee_remaining_balance,
)
from .preferences import build_calendar_preference_collection_context
from .metrics import sync_employee_vacation_metrics
from .querysets import get_vacation_requests_queryset
from .request_history import get_vacation_request_history
from .requests import enrich_vacation_request, serialize_vacation_request_row
from .schedule_changes import (
    enrich_schedule_change_request,
    get_schedule_change_requests_queryset,
    is_manager_initiated_schedule_change,
    serialize_schedule_change_request_row,
)
from .scopes import (
    filter_by_employee_name,
    get_visible_employee_ids,
    normalize_employee_search_query,
    restrict_change_requests_queryset_for_employee,
    restrict_requests_queryset_for_employee,
    restrict_urgent_closure_requests_queryset_for_employee,
)
from .urgent_closures import (
    can_department_review_urgent_closure,
    can_employee_review_urgent_closure,
    can_finalize_urgent_closure,
    enrich_urgent_closure_request,
    get_urgent_closure_requests_queryset,
    serialize_urgent_closure_request_row,
    urgent_closure_review_status,
)
from .validation import get_paid_request_eligibility_for_year


def _get_calendar_available_years(current_year, selected_year=None):
    years = set(VacationRequest.objects.values_list("start_date__year", flat=True))
    years.update(VacationRequest.objects.values_list("end_date__year", flat=True))
    years.update(VacationScheduleItem.objects.values_list("start_date__year", flat=True))
    years.update(VacationScheduleItem.objects.values_list("end_date__year", flat=True))
    years.update(VacationPreference.objects.values_list("year", flat=True))
    years.update(VacationPreferenceCollection.objects.values_list("year", flat=True))
    years.update({current_year, current_year + 1})
    available_years = sorted((year for year in years if year), reverse=True)
    return available_years or [current_year]

def _filter_calendar_employees_by_name(queryset, search_query):
    for token in search_query.split():
        queryset = queryset.filter(
            Q(last_name__icontains=token)
            | Q(first_name__icontains=token)
            | Q(middle_name__icontains=token)
        )
    return queryset

def _build_month_day_headers(year, month, today, calendar_rows):
    day_count = calendar.monthrange(year, month)[1]
    issue_counts_by_day = {
        day: {"risk_count": 0, "conflict_count": 0}
        for day in range(1, day_count + 1)
    }

    for row in calendar_rows:
        for cell in row.get("cells") or []:
            day = cell.get("day")
            if not day or day not in issue_counts_by_day:
                continue
            if cell.get("has_conflict"):
                issue_counts_by_day[day]["conflict_count"] += 1
            elif cell.get("has_high_risk"):
                issue_counts_by_day[day]["risk_count"] += 1

    headers = []
    for day in range(1, day_count + 1):
        current_date = date(year, month, day)
        issue_counts = issue_counts_by_day[day]
        issue_tooltip_parts = []
        if issue_counts["conflict_count"]:
            issue_tooltip_parts.append(f'Конфликт: {issue_counts["conflict_count"]}')
        if issue_counts["risk_count"]:
            issue_tooltip_parts.append(f'Высокий риск: {issue_counts["risk_count"]}')
        issue_tooltip = f' • {" • ".join(issue_tooltip_parts)}' if issue_tooltip_parts else ""
        has_conflict = bool(issue_counts["conflict_count"])
        has_high_risk = bool(issue_counts["risk_count"])
        headers.append(
            {
                "day": day,
                "weekday": WEEKDAY_SHORT_NAMES[current_date.weekday()],
                "is_weekend": current_date.weekday() >= 5,
                "is_today": current_date == today,
                "has_high_risk": has_high_risk,
                "has_conflict": has_conflict,
                "issue_icon": "⚔" if has_conflict else ("bolt" if has_high_risk else ""),
                "issue_icon_type": "symbol" if has_conflict else ("material" if has_high_risk else ""),
                "issue_label": "Конфликт" if has_conflict else ("Высокий риск" if has_high_risk else ""),
                "risk_count": issue_counts["risk_count"],
                "conflict_count": issue_counts["conflict_count"],
                "tooltip": f"{day:02d}.{month:02d}.{year} • {WEEKDAY_SHORT_NAMES[current_date.weekday()]}{issue_tooltip}",
            }
        )

    return headers


def build_calendar_page_context(current_employee, query_params):
    today = timezone.localdate()
    current_year = today.year

    selected_year = query_params.get("year", current_year)
    selected_month = query_params.get("month", today.month)
    calendar_view_mode = query_params.get("view", "year")
    selected_employee_id = query_params.get("employee")
    selected_department = query_params.get("department", "all")
    search_query = normalize_employee_search_query(query_params.get("search", ""))
    selected_issue = query_params.get("issue", "all")

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
        calendar_view_mode = "year"
    if selected_issue not in {"all", "risk", "conflict"}:
        selected_issue = "all"
    available_years = _get_calendar_available_years(current_year, selected_year)
    if selected_year not in available_years:
        selected_year = current_year if current_year in available_years else max(available_years)

    sync_employee_vacation_metrics(current_employee)
    current_employee.refresh_from_db()
    current_employee_leave_summary = get_employee_leave_summary(current_employee)
    current_employee_final_balance = current_employee_leave_summary["available"]

    context = {}
    visible_employee_ids = get_visible_employee_ids(current_employee)
    accessible_departments = list(get_accessible_departments(current_employee))
    accessible_department_ids = {department.id for department in accessible_departments}
    display_employees_qs = Employees.objects.filter(id__in=visible_employee_ids)
    issue_scope_employees_qs = display_employees_qs
    if selected_department != "all":
        try:
            selected_department_id = int(selected_department)
        except (TypeError, ValueError):
            selected_department = "all"
        else:
            if selected_department_id in accessible_department_ids:
                display_employees_qs = display_employees_qs.filter(department_id=selected_department_id)
                issue_scope_employees_qs = issue_scope_employees_qs.filter(department_id=selected_department_id)
                selected_department = str(selected_department_id)
            else:
                selected_department = "all"

    if search_query:
        display_employees_qs = _filter_calendar_employees_by_name(display_employees_qs, search_query)

    display_employee_ids = list(display_employees_qs.values_list("id", flat=True))
    issue_scope_employee_ids = list(issue_scope_employees_qs.values_list("id", flat=True))
    employees, employee_day_status, employee_entries = build_calendar_base_data(
        selected_year,
        employee_ids=display_employee_ids,
    )
    _, _, issue_employee_entries = build_calendar_base_data(
        selected_year,
        employee_ids=issue_scope_employee_ids,
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
        issue_employee_entries=issue_employee_entries,
        issue_filter=selected_issue,
    )
    employee_ids = {row["employee_id"] for row in calendar_rows}
    visible_employee_entries = {
        employee_id: entries
        for employee_id, entries in employee_entries.items()
        if employee_id in employee_ids
    }
    calendar_summary = build_calendar_summary(
        visible_employee_entries,
        selected_year,
        selected_month,
        calendar_view_mode,
    )
    calendar_month_totals = build_calendar_month_totals(calendar_rows) if calendar_view_mode == "year" else []
    calendar_month_details = (
        build_calendar_month_details(calendar_rows, calendar_details, selected_year)
        if calendar_view_mode == "year"
        else {}
    )

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
                "selected_department": selected_department,
                "search_query": search_query,
                "selected_issue": selected_issue,
                "department_options": accessible_departments,
                "show_department_filter": len(accessible_departments) > 1,
                "available_years": available_years,
                "available_months": [
                    {"value": index + 1, "label": month_name}
                    for index, month_name in enumerate(RUSSIAN_MONTH_NAMES)
                ],
            },
            "calendar_summary": calendar_summary,
            "calendar_preference_collection": build_calendar_preference_collection_context(
                current_employee,
                selected_year,
            ),
            "calendar_month_totals": calendar_month_totals,
            "calendar_month_details": calendar_month_details,
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
                {
                    "group": "Проблемы графика",
                    "items": [
                        {"status": "issue-risk", "label": "Высокий риск", "icon": "bolt", "icon_type": "material"},
                        {"status": "issue-conflict", "label": "Конфликт", "icon": "⚔", "icon_type": "symbol"},
                    ],
                },
            ],
            "calendar_rows": calendar_rows,
            "calendar_details": calendar_details,
            "selected_employee_id": selected_employee_id,
            "selected_employee_detail": selected_employee_detail,
            "selected_month_name": selected_month_label,
            "year_short_headers": RUSSIAN_MONTH_SHORT_NAMES,
            "month_day_headers": _build_month_day_headers(selected_year, selected_month, today, calendar_rows),
            "today_iso": today.isoformat(),
        }
    )

    return context


def build_applications_page_context(current_employee, query_params):
    status_filter = query_params.get("status", "all")
    show_task_scope_filter = is_enterprise_head_employee(current_employee)
    task_scope = query_params.get("task_scope", "all")
    if task_scope not in {"all", "mine"} or not show_task_scope_filter:
        task_scope = "all"
    department_id = query_params.get("department", "all")
    selected_group = query_params.get("group", "all")
    selected_vacation_type = query_params.get("vacation_type", "all")
    search_query = normalize_employee_search_query(query_params.get("search", ""))
    requests_qs = restrict_requests_queryset_for_employee(
        get_vacation_requests_queryset().order_by("-created_at"),
        current_employee,
    )
    change_requests_qs = restrict_change_requests_queryset_for_employee(
        get_schedule_change_requests_queryset().order_by("-created_at"),
        current_employee,
    )
    urgent_closure_qs = restrict_urgent_closure_requests_queryset_for_employee(
        get_urgent_closure_requests_queryset().order_by("-created_at"),
        current_employee,
    )

    if status_filter in {
        VacationRequest.STATUS_APPROVED,
        VacationRequest.STATUS_PENDING,
        VacationRequest.STATUS_REJECTED,
    }:
        requests_qs = requests_qs.filter(status=status_filter)
        change_requests_qs = change_requests_qs.filter(status=status_filter)
        if status_filter == VacationRequest.STATUS_APPROVED:
            urgent_closure_qs = urgent_closure_qs.filter(status=VacationUrgentClosureRequest.STATUS_COMPLETED)
        elif status_filter == VacationRequest.STATUS_REJECTED:
            urgent_closure_qs = urgent_closure_qs.filter(status=VacationUrgentClosureRequest.STATUS_REJECTED)
        else:
            urgent_closure_qs = urgent_closure_qs.filter(status__in=VacationUrgentClosureRequest.ACTIVE_STATUSES)

    vacation_type_options = [{"value": "all", "label": "Все отпуска"}] + [
        {"value": value, "label": label}
        for value, label in VACATION_TYPE_CHOICES
    ]
    allowed_vacation_types = {option["value"] for option in vacation_type_options}
    if selected_vacation_type not in allowed_vacation_types:
        selected_vacation_type = "all"
    if selected_vacation_type != "all":
        requests_qs = requests_qs.filter(vacation_type=selected_vacation_type)

    group_filter = resolve_production_group_filter_context(
        current_employee,
        selected_department=department_id,
        selected_group=selected_group,
    )
    department_id = group_filter["selected_department"]
    group_id = group_filter["selected_group_id"]

    if department_id != "all":
        requests_qs = requests_qs.filter(employee__department_id=department_id)
        change_requests_qs = change_requests_qs.filter(employee__department_id=department_id)
        urgent_closure_qs = urgent_closure_qs.filter(employee__department_id=department_id)
    if group_id is not None:
        requests_qs = requests_qs.filter(employee__employee_position__production_group_id=group_id)
        change_requests_qs = change_requests_qs.filter(employee__employee_position__production_group_id=group_id)
        urgent_closure_qs = urgent_closure_qs.filter(employee__employee_position__production_group_id=group_id)

    if search_query:
        requests_qs = filter_by_employee_name(requests_qs, search_query)
        change_requests_qs = filter_by_employee_name(change_requests_qs, search_query)
        urgent_closure_qs = filter_by_employee_name(urgent_closure_qs, search_query)

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
            and can_review_schedule_change_request(current_employee, change_request)
        )
        change_request.decision_locked = (
            change_request.status == VacationScheduleChangeRequest.STATUS_PENDING
            and not change_request.can_approve
        )
        if change_request.decision_locked and change_request.is_manager_initiated:
            change_request.decision_locked_icon = "hourglass_top"
            change_request.decision_locked_label = "Ожидает сотрудника"
            change_request.decision_locked_tooltip_title = "Ожидается решение сотрудника"
            change_request.decision_locked_tooltip_text = (
                "Предложение переноса должен принять или отклонить сотрудник, которому предложили новый период."
            )
        else:
            change_request.decision_locked_icon = "lock"
            change_request.decision_locked_label = "Недоступно"
            change_request.decision_locked_tooltip_title = "Решение недоступно"
            change_request.decision_locked_tooltip_text = (
                "Перенос должен согласовать пользователь с другим уровнем доступа или назначенный руководитель."
            )

    urgent_closures = [enrich_urgent_closure_request(closure_request) for closure_request in urgent_closure_qs]
    for closure_request in urgent_closures:
        closure_request.can_approve = (
            can_department_review_urgent_closure(current_employee, closure_request)
            or can_employee_review_urgent_closure(current_employee, closure_request)
            or can_finalize_urgent_closure(current_employee, closure_request)
        )
        closure_request.decision_locked = (
            urgent_closure_review_status(closure_request) == VacationRequest.STATUS_PENDING
            and not closure_request.can_approve
        )
        if closure_request.status == VacationUrgentClosureRequest.STATUS_DEPARTMENT_REVIEW:
            closure_request.decision_locked_icon = "supervisor_account"
            closure_request.decision_locked_label = "У руководителя"
            closure_request.decision_locked_tooltip_title = "Ожидается руководитель"
            closure_request.decision_locked_tooltip_text = "Период должен проверить руководитель отдела сотрудника."
        elif closure_request.status == VacationUrgentClosureRequest.STATUS_EMPLOYEE_REVIEW:
            closure_request.decision_locked_icon = "person"
            closure_request.decision_locked_label = "У сотрудника"
            closure_request.decision_locked_tooltip_title = "Ожидается сотрудник"
            closure_request.decision_locked_tooltip_text = "Сотрудник должен принять период или предложить другой."
        elif closure_request.status == VacationUrgentClosureRequest.STATUS_HR_FINALIZATION:
            closure_request.decision_locked_icon = "verified_user"
            closure_request.decision_locked_label = "У HR"
            closure_request.decision_locked_tooltip_title = "Ожидается HR"
            closure_request.decision_locked_tooltip_text = "HR должен финализировать согласованный период."
        else:
            closure_request.decision_locked_icon = "task_alt"
            closure_request.decision_locked_label = "Завершено"
            closure_request.decision_locked_tooltip_title = "Маршрут закрыт"
            closure_request.decision_locked_tooltip_text = "Согласование срочного остатка уже завершено."

    if task_scope == "mine":
        vacations = [
            vacation
            for vacation in vacations
            if vacation.status == VacationRequest.STATUS_PENDING and vacation.can_approve
        ]
        change_requests = [
            change_request
            for change_request in change_requests
            if change_request.status == VacationScheduleChangeRequest.STATUS_PENDING
            and change_request.can_approve
        ]
        urgent_closures = [
            closure_request
            for closure_request in urgent_closures
            if urgent_closure_review_status(closure_request) == VacationRequest.STATUS_PENDING
            and closure_request.can_approve
        ]

    change_requests = sorted(
        [*change_requests, *urgent_closures],
        key=lambda item: (item.created_at, item.id),
        reverse=True,
    )

    return {
        "vacations": vacations,
        "change_requests": change_requests,
        "selected_status": status_filter,
        "selected_task_scope": task_scope,
        "selected_department": str(department_id),
        "selected_group": group_filter["selected_group"],
        "selected_vacation_type": selected_vacation_type,
        "vacation_type_options": vacation_type_options,
        "group_options": group_filter["group_options"],
        "search_query": search_query,
        "show_task_scope_filter": show_task_scope_filter,
        "show_group_filter": not is_authorized_person_employee(current_employee),
        "show_department_filter": is_hr_employee(current_employee) or is_enterprise_head_employee(current_employee),
        "show_group_department_labels": group_filter["show_group_department_labels"],
    }


def build_applications_json_payload(vacations, change_requests):
    return {
        "vacations": [serialize_vacation_request_row(vacation) for vacation in vacations],
        "change_requests": [
            serialize_urgent_closure_request_row(change_request)
            if isinstance(change_request, VacationUrgentClosureRequest)
            else serialize_schedule_change_request_row(change_request)
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
    value_mod_100 = value % 100
    value_mod_10 = value % 10
    if value_mod_100 not in range(11, 15) and value_mod_10 == 1:
        return f"{value} сотрудник"
    if value_mod_100 not in range(11, 15) and 2 <= value_mod_10 <= 4:
        return f"{value} сотрудника"
    return f"{value} сотрудников"


def _format_staffing_ratio(remaining_staff, required_staff):
    remaining_staff = int(remaining_staff or 0)
    required_staff = int(required_staff or 0)
    if required_staff:
        return f"{remaining_staff} из минимума {required_staff}"
    return "Минимум не задан"


def _format_workload_label(load_level):
    load_level = int(load_level or 1)
    if load_level >= 5:
        return f"{load_level}/5 · пиковая"
    if load_level == 4:
        return f"{load_level}/5 · высокая"
    if load_level == 3:
        return f"{load_level}/5 · средняя"
    return f"{load_level}/5 · спокойная"


def _format_leave_days_delta(delta):
    delta = int(delta or 0)
    if delta > 0:
        return f"+{delta} д."
    if delta < 0:
        return f"{delta} д."
    return "Без изменения"


def _get_decision_context_variant(risk_explanation):
    if risk_explanation.get("is_conflict"):
        return "conflict"
    if risk_explanation.get("level") == VacationRequest.RISK_HIGH:
        return "risk"
    if risk_explanation.get("level") == VacationRequest.RISK_MEDIUM:
        return "medium"
    return "planned"


def _get_decision_context_title(risk_explanation):
    if risk_explanation.get("is_conflict"):
        return "Есть конфликт состава"
    if risk_explanation.get("level") == VacationRequest.RISK_HIGH:
        return "Высокий риск"
    if risk_explanation.get("level") == VacationRequest.RISK_MEDIUM:
        return "Средний риск"
    return "Критичных проблем нет"


def _get_detail_impact_label(detail):
    kind = detail.get("kind")
    if detail.get("missing_staff"):
        return f"Не хватает: {_format_employee_count(detail['missing_staff'])}"
    if detail.get("absent_staff") is not None and detail.get("max_absent") is not None:
        absent_staff = int(detail.get("absent_staff") or 0)
        max_absent = int(detail.get("max_absent") or 0)
        if absent_staff > max_absent:
            return f"Превышение: {_format_employee_count(absent_staff - max_absent)}"
        return f"Лимит: {_format_employee_count(max_absent)}"
    if detail.get("covered_staff"):
        return f"Покрывает: {_format_employee_count(detail['covered_staff'])}"
    if detail.get("remaining_staff") is not None and detail.get("required_staff") is not None:
        return _format_staffing_ratio(detail.get("remaining_staff"), detail.get("required_staff"))
    if kind == "department_load":
        return _format_workload_label(detail.get("department_load_level"))
    if kind == "overlapping_absences":
        return _format_employee_count(detail.get("overlapping_absences_count"))
    return ""


def _same_staffing_scope(first_detail, second_detail):
    first_group = first_detail.get("affected_group") or ""
    second_group = second_detail.get("affected_group") or ""
    first_department = first_detail.get("affected_department") or ""
    second_department = second_detail.get("affected_department") or ""
    return (
        first_group == second_group
        and first_department == second_department
        and (first_detail.get("affected_employee_label") or "") == (second_detail.get("affected_employee_label") or "")
    )


def _build_combined_staffing_card(shortage_detail, limit_detail, *, is_group):
    title = "Группа не проходит по составу" if is_group else "Отдел не проходит по составу"
    scope_name = (
        shortage_detail.get("affected_group")
        or limit_detail.get("affected_group")
        or shortage_detail.get("affected_department")
        or limit_detail.get("affected_department")
        or ("Группа" if is_group else "Отдел")
    )
    absent_staff = limit_detail.get("absent_staff")
    max_absent = limit_detail.get("max_absent")
    remaining_staff = shortage_detail.get("remaining_staff")
    required_staff = shortage_detail.get("required_staff")
    missing_staff = shortage_detail.get("missing_staff")

    text_parts = []
    if absent_staff is not None and max_absent is not None:
        text_parts.append(f"отсутствуют {_format_employee_count(absent_staff)} при лимите {_format_employee_count(max_absent)}")
    if remaining_staff is not None and required_staff is not None:
        text_parts.append(f"останется {_format_employee_count(remaining_staff)} при минимуме {_format_employee_count(required_staff)}")
    text = f"{scope_name}: {', '.join(text_parts)}." if text_parts else shortage_detail.get("text", "")
    impact_label = (
        f"Не хватает: {_format_employee_count(missing_staff)}"
        if missing_staff
        else _get_detail_impact_label(limit_detail)
    )

    return {
        "severity": "conflict",
        "title": title,
        "text": text,
        "impact_label": impact_label,
        "people_label": shortage_detail.get("affected_employee_label") or limit_detail.get("affected_employee_label") or "",
        "tooltip_title": title,
        "tooltip_text": "Система объединила минимум состава и лимит отсутствующих, потому что они описывают одну управленческую проблему.",
    }


def _build_decision_rule_cards(risk_explanation):
    details = list(risk_explanation.get("details") or [])
    cards = []
    consumed_indexes = set()

    for index, detail in enumerate(details):
        if index in consumed_indexes:
            continue

        kind = detail.get("kind")
        paired_index = None
        is_group_shortage = kind == "group_shortage"
        is_department_shortage = kind == "department_staff_shortage"
        is_group_limit = kind == "group_absence_limit"
        is_department_limit = kind == "department_absence_limit"
        if is_group_shortage or is_department_shortage or is_group_limit or is_department_limit:
            paired_kind = {
                "group_shortage": "group_absence_limit",
                "department_staff_shortage": "department_absence_limit",
                "group_absence_limit": "group_shortage",
                "department_absence_limit": "department_staff_shortage",
            }[kind]
            for candidate_index, candidate in enumerate(details):
                if candidate_index == index:
                    continue
                if candidate_index in consumed_indexes:
                    continue
                if candidate.get("kind") == paired_kind and _same_staffing_scope(detail, candidate):
                    paired_index = candidate_index
                    break

        if paired_index is not None:
            consumed_indexes.update({index, paired_index})
            shortage_detail = details[paired_index] if is_group_limit or is_department_limit else detail
            limit_detail = detail if is_group_limit or is_department_limit else details[paired_index]
            cards.append(
                _build_combined_staffing_card(
                    shortage_detail,
                    limit_detail,
                    is_group=is_group_shortage or is_group_limit,
                )
            )
            continue

        consumed_indexes.add(index)
        people_label = detail.get("affected_employee_label") or ""
        tooltip_text = detail.get("text") or "Пояснение к фактору риска."
        if kind == "substitution_used" and detail.get("substitute_groups"):
            tooltip_text = f"{tooltip_text} Доступное замещение: {detail['substitute_groups']}."
        cards.append(
            {
                "severity": detail.get("severity", "info"),
                "title": detail.get("title", "Фактор риска"),
                "text": detail.get("text", ""),
                "impact_label": _get_detail_impact_label(detail),
                "people_label": people_label,
                "tooltip_title": detail.get("title", "Фактор риска"),
                "tooltip_text": tooltip_text,
            }
        )

    return cards


def _build_substitution_context(risk_explanation):
    details = list(risk_explanation.get("details") or [])
    substitution_detail = next((detail for detail in details if detail.get("kind") == "substitution_used"), None)
    if substitution_detail:
        substitute_groups = substitution_detail.get("substitute_groups") or "группа замещения"
        covered_staff = substitution_detail.get("covered_staff") or 0
        return {
            "variant": "risk",
            "label": "Замещение задействовано",
            "value": f"Покрывает {_format_employee_count(covered_staff)}",
            "hint": f"Дефицит закрывает: {substitute_groups}.",
            "tooltip": "Замещение снижает конфликт до высокого риска: формально состав закрыт, но решение лучше проверить вручную.",
        }

    has_shortage = any(
        detail.get("kind") in {"department_staff_shortage", "group_shortage"}
        for detail in details
    )
    if has_shortage:
        return {
            "variant": "conflict",
            "label": "Замещение не покрывает",
            "value": "Нужна ручная проверка",
            "hint": "В доступных правилах замещения не нашлось свободного резерва на весь дефицит.",
            "tooltip": "Если правила замещения настроены, система учитывает только свободный резерв замещающих групп.",
        }

    if risk_explanation.get("is_conflict"):
        return {
            "variant": "conflict",
            "label": "Замещение не решает",
            "value": "Проверьте лимит",
            "hint": "Конфликт связан не с дефицитом группы, а с другим правилом состава.",
            "tooltip": "Для превышения лимита отсутствующих обычно нужно менять период, лимит или состав графика, а не только добавлять замещение.",
        }

    return {
        "variant": "planned",
        "label": "Замещение",
        "value": "Не требуется",
        "hint": "По текущему расчету отпуск не создает дефицит, который нужно закрывать замещением.",
        "tooltip": "Замещение применяется только когда уход сотрудника опускает группу ниже минимального состава.",
    }


def _build_calendar_period_url(period_start, *, period_end=None, employee_id=None, issue_focus=False):
    period_end = period_end or period_start
    query = {
        "view": "year",
        "year": period_start.year,
        "issue": "all",
    }
    if employee_id:
        query.update(
            {
                "employee": employee_id,
                "calendar_focus_employee": employee_id,
                "calendar_focus_start": period_start.isoformat(),
                "calendar_focus_end": period_end.isoformat(),
            }
        )
    if issue_focus:
        query["focus"] = "issues"
    return f"{reverse('calendar')}?{urlencode(query)}"


def _build_leave_decision_context(risk_explanation, *, period_start, period_end=None, employee_id=None, calendar_action_label):
    overlapping_count = int(risk_explanation.get("overlapping_absences_count") or 0)
    overlapping_label = risk_explanation.get("overlapping_employee_label") or _format_employee_count(overlapping_count)
    substitution_context = _build_substitution_context(risk_explanation)
    variant = _get_decision_context_variant(risk_explanation)
    primary_detail = next(
        (
            detail
            for detail in (risk_explanation.get("details") or [])
            if detail.get("severity") in {"conflict", "high", "medium"}
        ),
        {},
    )
    remaining_staff = int(primary_detail.get("remaining_staff") or risk_explanation.get("remaining_staff") or 0)
    required_staff = int(primary_detail.get("required_staff") or risk_explanation.get("required_staff") or 0)
    department = risk_explanation.get("affected_department") or "Отдел не указан"
    affected_group = risk_explanation.get("affected_group") or ""
    if primary_detail.get("affected_group"):
        staffing_target = f"Группа: {primary_detail['affected_group']}"
    elif primary_detail.get("affected_department"):
        staffing_target = f"Отдел: {primary_detail['affected_department']}"
    else:
        staffing_target = f"Группа: {affected_group}" if affected_group else f"Отдел: {department}"

    return {
        "variant": variant,
        "title": _get_decision_context_title(risk_explanation),
        "summary": risk_explanation.get("short_reason", ""),
        "score_label": f"{risk_explanation.get('label')} · {int(risk_explanation.get('score') or 0)}%",
        "recommended_action": risk_explanation.get("recommended_action", ""),
        "staffing_target": staffing_target,
        "staffing_ratio": _format_staffing_ratio(remaining_staff, required_staff),
        "staffing_tooltip": "Показывает, сколько сотрудников останется в отделе или ключевой группе после учета этой заявки и уже известных отсутствий.",
        "overlap_label": _format_employee_count(overlapping_count) if overlapping_count else "Нет",
        "overlap_people_label": overlapping_label if overlapping_count else "Пересечений нет",
        "overlap_tooltip": "Сотрудники, которые уже отсутствуют в тот же период по утвержденному графику, заявкам или переносам.",
        "workload_label": _format_workload_label(risk_explanation.get("department_load_level")),
        "workload_tooltip": "Месячная нагрузка отдела уточняет базовые лимиты состава и усиливает риск в напряженные месяцы.",
        "substitution": substitution_context,
        "rule_cards": _build_decision_rule_cards(risk_explanation),
        "calendar_url": _build_calendar_period_url(
            period_start,
            period_end=period_end,
            employee_id=employee_id,
            issue_focus=risk_explanation.get("is_conflict"),
        ),
        "calendar_action_label": calendar_action_label,
    }


def _get_vacation_risk_summary(vacation):
    risk_explanation = getattr(vacation, "risk_explanation", None)
    if risk_explanation:
        return risk_explanation["short_reason"]

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


def _get_current_role_label(employee):
    if employee is None:
        return "Роль не определена"
    return get_employee_role_card_meta(employee).get("label") or "Роль не определена"


def _get_vacation_approval_route(vacation, current_employee, can_approve_vacation):
    expected = get_expected_vacation_approver(vacation.employee)
    current_role = _get_current_role_label(current_employee)
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


def _get_schedule_change_approval_route(change_request, current_employee, can_approve_change):
    if is_manager_initiated_schedule_change(change_request):
        reviewer_name = change_request.employee.full_name or change_request.employee.login
        if change_request.status != VacationScheduleChangeRequest.STATUS_PENDING:
            availability = "Предложение переноса уже рассмотрено."
        elif can_approve_change:
            availability = "Текущий пользователь может принять или отклонить предложение."
        else:
            availability = "Решение ожидается от сотрудника, которому предложен перенос."
        return {
            "role_label": "Сотрудник",
            "reviewer_name": reviewer_name,
            "reason": "Предложение руководителя подтверждает сам сотрудник.",
            "availability": availability,
        }

    expected = get_expected_vacation_approver(change_request.employee)
    current_role = _get_current_role_label(current_employee)
    reviewer = expected.employee
    reviewer_name = (reviewer.full_name or reviewer.login) if reviewer else ""
    if change_request.status != VacationScheduleChangeRequest.STATUS_PENDING:
        availability = "Перенос уже рассмотрен, маршрут закрыт."
    elif can_approve_change:
        availability = "Текущий пользователь находится на нужном уровне согласования."
    else:
        availability = f"Решение недоступно: нужна роль «{expected.role_label}», текущая роль — «{current_role}»."

    return {
        "role_label": expected.role_label,
        "reviewer_name": reviewer_name or "согласующий не назначен",
        "reason": f"Перенос утвержденного отпуска проходит по маршруту: {expected.reason}",
        "availability": availability,
    }


def _get_urgent_closure_approval_route(closure_request, current_employee, can_act):
    current_role = _get_current_role_label(current_employee)
    if closure_request.status == VacationUrgentClosureRequest.STATUS_EMPLOYEE_REVIEW:
        reviewer_name = closure_request.employee.full_name or closure_request.employee.login
        if can_act:
            availability = "Сотрудник может принять предложенный период или предложить другой."
        else:
            availability = "Решение ожидается от сотрудника, чей срочный остаток закрывается."
        return {
            "role_label": "Сотрудник",
            "reviewer_name": reviewer_name,
            "reason": "После проверки руководителя сотрудник подтверждает период, потому что отпуск нельзя ставить без его участия.",
            "availability": availability,
        }

    if closure_request.status == VacationUrgentClosureRequest.STATUS_HR_FINALIZATION:
        reviewer_name = current_employee.full_name if can_act and current_employee else "HR"
        availability = (
            "HR может финализировать согласованный период и создать пункт графика."
            if can_act
            else f"Решение недоступно: нужна роль «HR», текущая роль — «{current_role}»."
        )
        return {
            "role_label": "HR",
            "reviewer_name": reviewer_name,
            "reason": "HR завершает маршрут только после согласия руководителя и сотрудника.",
            "availability": availability,
        }

    expected = get_expected_vacation_approver(closure_request.employee)
    reviewer = expected.employee
    reviewer_name = (reviewer.full_name or reviewer.login) if reviewer else ""
    if closure_request.status in VacationUrgentClosureRequest.TERMINAL_STATUSES:
        availability = "Согласование срочного остатка уже завершено."
    elif can_act:
        availability = "Текущий пользователь может проверить период для отдела."
    else:
        availability = f"Решение недоступно: нужна роль «{expected.role_label}», текущая роль — «{current_role}»."
    return {
        "role_label": expected.role_label,
        "reviewer_name": reviewer_name or "согласующий не назначен",
        "reason": "Руководитель сначала проверяет, не ломает ли период состав отдела.",
        "availability": availability,
    }


def _get_urgent_closure_history(closure_request):
    history = [
        {
            "title": "Согласование создано",
            "description": (
                f"HR предложил закрыть {closure_request.required_days_label} до "
                f"{closure_request.deadline_label}: {closure_request.period_label}."
            ),
            "actor": closure_request.created_by,
            "created_at": closure_request.created_at,
        }
    ]
    if closure_request.department_reviewed_at or closure_request.department_reviewer_id:
        history.append(
            {
                "title": "Руководитель проверил период",
                "description": closure_request.department_comment or "Период передан сотруднику без комментария.",
                "actor": closure_request.department_reviewer,
                "created_at": closure_request.department_reviewed_at or closure_request.created_at,
            }
        )
    if closure_request.employee_responded_at:
        history.append(
            {
                "title": (
                    "Сотрудник предложил другой период"
                    if closure_request.status == VacationUrgentClosureRequest.STATUS_DEPARTMENT_REVIEW
                    else "Сотрудник ответил"
                ),
                "description": closure_request.employee_comment or "Ответ сотрудника без комментария.",
                "actor": closure_request.employee,
                "created_at": closure_request.employee_responded_at,
            }
        )
    if closure_request.finalized_at or closure_request.finalized_by_id:
        history.append(
            {
                "title": "HR финализировал закрытие",
                "description": closure_request.final_comment or "Пункт графика создан без комментария.",
                "actor": closure_request.finalized_by,
                "created_at": closure_request.finalized_at or closure_request.updated_at,
            }
        )
    if closure_request.rejected_at or closure_request.rejected_by_id:
        history.append(
            {
                "title": "Согласование отклонено",
                "description": closure_request.rejection_comment or "Отклонено без комментария.",
                "actor": closure_request.rejected_by,
                "created_at": closure_request.rejected_at or closure_request.updated_at,
            }
        )
    return history


def _build_saved_vacation_risk_snapshot(vacation):
    overlapping_absences_count = int(vacation.overlapping_absences_count or 0)
    remaining_staff_count = int(vacation.remaining_staff_count or 0)
    min_staff_required = int(vacation.min_staff_required or 0)
    department_load_level = int(vacation.department_load_level or 1)
    return {
        "risk_level": vacation.risk_level,
        "risk_label": vacation.get_risk_level_display(),
        "risk_score": int(vacation.risk_score or 0),
        "overlapping_absences_count": overlapping_absences_count,
        "overlapping_absences_label": _format_employee_count(overlapping_absences_count),
        "remaining_staff_count": remaining_staff_count,
        "required_staff_count": min_staff_required,
        "department_load_level": department_load_level,
    }


def _build_live_vacation_risk_context(risk_explanation):
    overlapping_absences_count = int(risk_explanation.get("overlapping_absences_count") or 0)
    return {
        "risk_level": risk_explanation["level"],
        "risk_label": risk_explanation["label"],
        "risk_score": int(risk_explanation["score"] or 0),
        "overlapping_absences_count": overlapping_absences_count,
        "overlapping_absences_label": _format_employee_count(overlapping_absences_count),
        "remaining_staff_count": int(risk_explanation.get("remaining_staff") or 0),
        "required_staff_count": int(risk_explanation.get("required_staff") or 0),
        "department_load_level": int(risk_explanation.get("department_load_level") or 1),
    }


def _build_saved_schedule_change_risk_snapshot(change_request):
    overlapping_absences_count = int(change_request.overlapping_absences_count or 0)
    remaining_staff_count = int(change_request.remaining_staff_count or 0)
    min_staff_required = int(change_request.min_staff_required or 0)
    department_load_level = int(change_request.department_load_level or 1)
    return {
        "risk_level": change_request.risk_level,
        "risk_label": change_request.get_risk_level_display(),
        "risk_score": int(change_request.risk_score or 0),
        "overlapping_absences_count": overlapping_absences_count,
        "overlapping_absences_label": _format_employee_count(overlapping_absences_count),
        "remaining_staff_count": remaining_staff_count,
        "required_staff_count": min_staff_required,
        "department_load_level": department_load_level,
    }


def _risk_snapshot_has_changed(saved_snapshot, live_context):
    compared_fields = (
        "risk_level",
        "risk_score",
        "overlapping_absences_count",
        "remaining_staff_count",
        "required_staff_count",
        "department_load_level",
    )
    return any(saved_snapshot[field] != live_context[field] for field in compared_fields)


def _get_schedule_change_history(change_request):
    history = [
        {
            "title": "Перенос создан",
            "description": f"{change_request.origin_label}: {change_request.old_period_label} → {change_request.new_period_label}.",
            "actor": change_request.requested_by,
            "created_at": change_request.created_at,
        }
    ]
    if change_request.reviewed_at or change_request.reviewed_by_id:
        status_text = "Перенос согласован" if change_request.status == change_request.STATUS_APPROVED else "Перенос отклонен"
        description = change_request.review_comment or "Решение принято без комментария."
        history.append(
            {
                "title": status_text,
                "description": description,
                "actor": change_request.reviewed_by,
                "created_at": change_request.reviewed_at or change_request.created_at,
            }
        )
    return history


def _get_schedule_change_risk_summary(change_request):
    risk_explanation = getattr(change_request, "risk_explanation", None)
    if risk_explanation:
        return risk_explanation["short_reason"]

    label = change_request.risk_label.lower()
    if change_request.min_staff_required:
        summary = (
            f"Риск {label}: в отделе останется {_format_employee_count(change_request.remaining_staff_count)} "
            f"при минимуме {_format_employee_count(change_request.min_staff_required)}."
        )
    else:
        summary = f"Риск {label}: нагрузка отдела оценивается как {change_request.department_load_level}/5."
    if change_request.overlapping_absences_count:
        summary += f" Одновременно отсутствуют: {_format_employee_count(change_request.overlapping_absences_count)}."
    return summary


def _get_section_back_links():
    return {
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


def build_vacation_detail_context(vacation, current_employee, source="", query_params=None):
    saved_risk_snapshot = _build_saved_vacation_risk_snapshot(vacation)
    enrich_vacation_request(vacation, include_live_risk_explanation=True)
    live_risk_context = _build_live_vacation_risk_context(vacation.risk_explanation)
    saved_risk_snapshot_changed = _risk_snapshot_has_changed(saved_risk_snapshot, live_risk_context)
    saved_risk_snapshot_title = (
        "Риск изменился после подачи заявки"
        if vacation.status == VacationRequest.STATUS_PENDING
        else "Актуальный риск отличается от сохраненного расчета"
    )
    saved_risk_snapshot_caption = (
        "На момент подачи"
        if vacation.status == VacationRequest.STATUS_PENDING
        else "На момент решения"
    )
    vacation_decision_context = _build_leave_decision_context(
        vacation.risk_explanation,
        period_start=vacation.start_date,
        period_end=vacation.end_date,
        employee_id=vacation.employee_id,
        calendar_action_label="Открыть период в графике",
    )
    can_approve_vacation = (
        vacation.status == VacationRequest.STATUS_PENDING
        and can_approve_leave_for_employee(current_employee, vacation.employee)
    )
    can_delete = vacation.status == VacationRequest.STATUS_PENDING and (
        vacation.employee_id == (current_employee.id if current_employee else None) or can_approve_vacation
    )
    decision_state = ""
    decision_state_icon = ""
    if vacation.status != VacationRequest.STATUS_PENDING:
        decision_state = "Заявка уже рассмотрена"
        decision_state_icon = "task_alt"
    elif not can_approve_vacation:
        decision_state = "Решение недоступно для вашей роли"
        decision_state_icon = "lock"
    section_back_links = _get_section_back_links()
    source = source if source in section_back_links else ""
    if source == "applications" and not can_access_applications(current_employee):
        source = ""
    default_source = "applications" if can_access_applications(current_employee) else ""
    navigation_source = source or default_source
    explicit_back_link = build_explicit_back_link(query_params or {}, section=navigation_source)
    employee_profile_query = {}
    if navigation_source:
        employee_profile_query["from"] = navigation_source
        employee_profile_query["return_to"] = "vacation"
        employee_profile_query["vacation_id"] = vacation.id
    employee_profile_url = reverse("employee_profile", args=[vacation.employee_id])
    if employee_profile_query:
        employee_profile_url = f"{employee_profile_url}?{urlencode(employee_profile_query)}"
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
        "vacation_risk_explanation": vacation.risk_explanation,
        "vacation_risk_summary": _get_vacation_risk_summary(vacation),
        "vacation_decision_context": vacation_decision_context,
        "vacation_live_risk_context": live_risk_context,
        "vacation_saved_risk_snapshot": saved_risk_snapshot,
        "vacation_saved_risk_snapshot_changed": saved_risk_snapshot_changed,
        "vacation_saved_risk_snapshot_title": saved_risk_snapshot_title,
        "vacation_saved_risk_snapshot_caption": saved_risk_snapshot_caption,
        "overlapping_absences_employee_label": live_risk_context["overlapping_absences_label"],
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
        "vacation_decision_state": decision_state,
        "vacation_decision_state_icon": decision_state_icon,
        "sidebar_section": navigation_source,
        "vacation_detail_back_link": explicit_back_link or section_back_links.get(navigation_source),
        "vacation_detail_employee_profile_url": employee_profile_url,
    }


def build_schedule_change_detail_context(change_request, current_employee, source="", query_params=None):
    saved_risk_snapshot = _build_saved_schedule_change_risk_snapshot(change_request)
    enrich_schedule_change_request(change_request, include_live_risk_explanation=True)
    live_risk_context = _build_live_vacation_risk_context(change_request.risk_explanation)
    saved_risk_snapshot_changed = _risk_snapshot_has_changed(saved_risk_snapshot, live_risk_context)
    schedule_change_decision_context = _build_leave_decision_context(
        change_request.risk_explanation,
        period_start=change_request.new_start_date,
        period_end=change_request.new_end_date,
        employee_id=change_request.employee_id,
        calendar_action_label="Открыть новый период в графике",
    )
    can_approve_change = (
        change_request.status == VacationScheduleChangeRequest.STATUS_PENDING
        and can_review_schedule_change_request(current_employee, change_request)
    )
    section_back_links = _get_section_back_links()
    source = source if source in section_back_links else ""
    if source == "applications" and not can_access_applications(current_employee):
        source = ""
    default_source = "applications" if can_access_applications(current_employee) else "calendar"
    navigation_source = source or default_source
    explicit_back_link = build_explicit_back_link(query_params or {}, section=navigation_source)
    employee_profile_query = {}
    if navigation_source:
        employee_profile_query["from"] = navigation_source
        employee_profile_query["return_to"] = "transfer"
        employee_profile_query["transfer_id"] = change_request.id
    employee_profile_url = reverse("employee_profile", args=[change_request.employee_id])
    if employee_profile_query:
        employee_profile_url = f"{employee_profile_url}?{urlencode(employee_profile_query)}"

    schedule_item = change_request.schedule_item
    old_chargeable_days = int(schedule_item.chargeable_days or 0)
    new_chargeable_days = get_chargeable_leave_days(
        change_request.new_start_date,
        change_request.new_end_date,
        schedule_item.vacation_type,
    )
    chargeable_days_delta = new_chargeable_days - old_chargeable_days
    decision_state = ""
    decision_state_icon = ""
    if change_request.status != VacationScheduleChangeRequest.STATUS_PENDING:
        decision_state = "Предложение уже рассмотрено." if change_request.is_manager_initiated else "Перенос уже рассмотрен."
        decision_state_icon = "task_alt"
    elif not can_approve_change:
        if change_request.is_manager_initiated and current_employee and current_employee.id == change_request.requested_by_id:
            decision_state = "Ожидается решение сотрудника."
            decision_state_icon = "hourglass_top"
        else:
            decision_state = "Решение недоступно для вашей роли."
            decision_state_icon = "lock"

    return {
        "change_request": change_request,
        "employee": change_request.employee,
        "schedule_item": schedule_item,
        "status": change_request.status,
        "status_label": change_request.status_label,
        "status_icon": change_request.status_icon,
        "status_css_class": change_request.status_css_class,
        "old_chargeable_days": old_chargeable_days,
        "new_chargeable_days": new_chargeable_days,
        "schedule_change_days_delta": chargeable_days_delta,
        "schedule_change_days_delta_label": _format_leave_days_delta(chargeable_days_delta),
        "vacation_type_label": schedule_item.get_vacation_type_display(),
        "schedule_year": schedule_item.schedule.year,
        "schedule_change_risk_explanation": change_request.risk_explanation,
        "schedule_change_risk_summary": _get_schedule_change_risk_summary(change_request),
        "schedule_change_decision_context": schedule_change_decision_context,
        "schedule_change_live_risk_context": live_risk_context,
        "schedule_change_saved_risk_snapshot": saved_risk_snapshot,
        "schedule_change_saved_risk_snapshot_changed": saved_risk_snapshot_changed,
        "overlapping_absences_employee_label": live_risk_context["overlapping_absences_label"],
        "approval_route": _get_schedule_change_approval_route(change_request, current_employee, can_approve_change),
        "schedule_change_history": _get_schedule_change_history(change_request),
        "system_recommendation_text": change_request.risk_recommended_action,
        "can_approve_schedule_change": can_approve_change,
        "schedule_change_approve_label": "Принять перенос" if change_request.is_manager_initiated else "Одобрить",
        "schedule_change_reject_label": "Отклонить предложение" if change_request.is_manager_initiated else "Отклонить",
        "schedule_change_action_label": (
            "Решение по предложению переноса"
            if change_request.is_manager_initiated
            else "Решение по переносу"
        ),
        "schedule_change_decision_state": decision_state,
        "schedule_change_decision_state_icon": decision_state_icon,
        "sidebar_section": navigation_source,
        "schedule_change_detail_back_link": explicit_back_link or section_back_links.get(navigation_source),
        "schedule_change_detail_employee_profile_url": employee_profile_url,
    }


def build_urgent_closure_detail_context(closure_request, current_employee, source="", query_params=None):
    enrich_urgent_closure_request(closure_request)
    urgent_closure_decision_context = _build_leave_decision_context(
        closure_request.risk_explanation,
        period_start=closure_request.proposed_start_date,
        period_end=closure_request.proposed_end_date,
        employee_id=closure_request.employee_id,
        calendar_action_label="Открыть период в графике",
    )
    can_manager_approve = can_department_review_urgent_closure(current_employee, closure_request)
    can_employee_review = can_employee_review_urgent_closure(current_employee, closure_request)
    can_hr_finalize = can_finalize_urgent_closure(current_employee, closure_request)
    can_act = can_manager_approve or can_employee_review or can_hr_finalize
    can_reject = can_act and closure_request.status in VacationUrgentClosureRequest.ACTIVE_STATUSES

    decision_state = ""
    decision_state_icon = ""
    if closure_request.status in VacationUrgentClosureRequest.TERMINAL_STATUSES:
        decision_state = "Согласование уже завершено."
        decision_state_icon = "task_alt"
    elif not can_act:
        decision_state = "Решение недоступно для вашей роли."
        decision_state_icon = "lock"

    section_back_links = _get_section_back_links()
    source = source if source in section_back_links else ""
    if source == "applications" and not can_access_applications(current_employee):
        source = ""
    default_source = "applications" if can_access_applications(current_employee) else "calendar"
    navigation_source = source or default_source
    explicit_back_link = build_explicit_back_link(query_params or {}, section=navigation_source)
    employee_profile_query = {}
    if navigation_source:
        employee_profile_query["from"] = navigation_source
        employee_profile_query["return_to"] = "urgent_closure"
        employee_profile_query["urgent_closure_id"] = closure_request.id
    employee_profile_url = reverse("employee_profile", args=[closure_request.employee_id])
    if employee_profile_query:
        employee_profile_url = f"{employee_profile_url}?{urlencode(employee_profile_query)}"

    return {
        "urgent_closure": closure_request,
        "employee": closure_request.employee,
        "status": closure_request.status,
        "status_label": closure_request.status_label,
        "status_icon": closure_request.status_icon,
        "status_css_class": closure_request.status_css_class,
        "urgent_closure_period_label": closure_request.period_label,
        "urgent_closure_risk_summary": _get_schedule_change_risk_summary(closure_request),
        "urgent_closure_decision_context": urgent_closure_decision_context,
        "approval_route": _get_urgent_closure_approval_route(closure_request, current_employee, can_act),
        "urgent_closure_history": _get_urgent_closure_history(closure_request),
        "system_recommendation_text": closure_request.risk_recommended_action
        or "Система предлагает закрыть только ту часть остатка, которую нельзя корректно поставить в график следующего года.",
        "can_manager_approve_urgent_closure": can_manager_approve,
        "can_employee_review_urgent_closure": can_employee_review,
        "can_hr_finalize_urgent_closure": can_hr_finalize,
        "can_reject_urgent_closure": can_reject,
        "urgent_closure_decision_state": decision_state,
        "urgent_closure_decision_state_icon": decision_state_icon,
        "sidebar_section": navigation_source,
        "urgent_closure_detail_back_link": explicit_back_link or section_back_links.get(navigation_source),
        "urgent_closure_detail_employee_profile_url": employee_profile_url,
    }


def build_analytics_page_context(current_employee, query_params=None):
    query_params = query_params or {}
    today = timezone.localdate()
    current_year = today.year
    selected_year = query_params.get("year", current_year)
    try:
        selected_year = int(selected_year)
    except (TypeError, ValueError):
        selected_year = current_year

    available_years = sorted(
        set(_get_calendar_available_years(current_year, selected_year))
        | {current_year, current_year + 1, selected_year},
        reverse=True,
    )

    visible_employee_ids = get_visible_employee_ids(current_employee)
    accessible_departments = list(get_accessible_departments(current_employee))
    accessible_department_ids = {department.id for department in accessible_departments}
    selected_department = query_params.get("department", "all")
    selected_department_id = None
    if selected_department != "all":
        try:
            selected_department_id = int(selected_department)
        except (TypeError, ValueError):
            selected_department = "all"
        else:
            if selected_department_id not in accessible_department_ids:
                selected_department = "all"
                selected_department_id = None

    if selected_department_id is not None:
        visible_employee_ids = list(
            Employees.objects.filter(
                id__in=visible_employee_ids,
                department_id=selected_department_id,
            ).values_list("id", flat=True)
        )

    context = build_analytics_payload(employee_ids=visible_employee_ids, year=selected_year)
    context.update(
        {
            "default_annual_leave_days": 52,
            "analytics_filters": {
                "selected_year": selected_year,
                "selected_department": selected_department,
            },
            "analytics_available_years": available_years,
            "analytics_department_options": accessible_departments,
        }
    )
    return context
