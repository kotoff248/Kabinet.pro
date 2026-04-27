import calendar
from datetime import date

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone

from apps.accounts.services import (
    can_access_analytics,
    can_access_applications,
    can_approve_leave_for_employee,
    can_view_employee,
    employee_required,
    get_accessible_departments,
    get_current_employee,
    get_managed_department_id,
    get_user_context,
    is_authorized_person_employee,
    is_department_head_employee,
    is_enterprise_head_employee,
    is_hr_employee,
)
from apps.employees.models import Employees
from apps.employees.services import update_context_with_departments
from apps.leave.models import VacationRequest, VacationScheduleChangeRequest, VacationScheduleItem

from .forms import ScheduleChangeRequestCreateForm, VacationRequestCreateForm
from .services import (
    RUSSIAN_MONTH_NAMES,
    RUSSIAN_MONTH_SHORT_NAMES,
    WEEKDAY_SHORT_NAMES,
    approve_schedule_change_request,
    approve_vacation_request,
    build_analytics_payload,
    build_calendar_base_data,
    build_calendar_rows,
    build_calendar_summary,
    create_vacation_request,
    create_schedule_change_request,
    delete_pending_vacation_request,
    enrich_vacation_request,
    enrich_schedule_change_request,
    get_calendar_redirect_url,
    get_chargeable_leave_days,
    get_employee_entitlement_rows,
    get_employee_leave_summary,
    get_employee_remaining_balance,
    get_paid_request_eligibility_for_year,
    get_russian_holiday_iso_dates,
    get_schedule_change_requests_queryset,
    get_vacation_requests_queryset,
    reject_schedule_change_request,
    reject_vacation_request,
    serialize_schedule_change_request_row,
    serialize_vacation_request_row,
    sync_employee_vacation_metrics,
)


def _form_errors_to_messages(form):
    errors = []
    for field_errors in form.errors.values():
        errors.extend(field_errors)
    return " ".join(str(error) for error in errors)


def _validation_error_message(exc):
    return " ".join(exc.messages) if getattr(exc, "messages", None) else str(exc)


def _normalize_vacation_form_data(post_data):
    data = post_data.copy()
    if "type_vacation" in data and "vacation_type" not in data:
        data["vacation_type"] = data.get("type_vacation")
    return data


def _get_visible_employee_ids(current_employee):
    if current_employee is None:
        return []

    if is_hr_employee(current_employee) or is_enterprise_head_employee(current_employee):
        return list(Employees.objects.exclude(role__in=Employees.SERVICE_ROLES).values_list("id", flat=True))

    if is_department_head_employee(current_employee):
        managed_department_id = get_managed_department_id(current_employee)
        if managed_department_id:
            return list(
                Employees.objects.filter(department_id=managed_department_id).values_list("id", flat=True)
            )

    return [current_employee.id]


def _restrict_requests_queryset_for_employee(queryset, current_employee):
    if current_employee is None:
        return queryset.none()

    if is_hr_employee(current_employee):
        return queryset.exclude(employee__role__in=Employees.SERVICE_ROLES)

    if is_department_head_employee(current_employee):
        managed_department_id = get_managed_department_id(current_employee)
        if managed_department_id:
            return queryset.filter(
                employee__department_id=managed_department_id,
                employee__role=Employees.ROLE_EMPLOYEE,
            )
        return queryset.none()

    if is_enterprise_head_employee(current_employee):
        return queryset.exclude(employee__role__in=Employees.SERVICE_ROLES)

    if is_authorized_person_employee(current_employee):
        return queryset.filter(employee__role=Employees.ROLE_ENTERPRISE_HEAD)

    return queryset.filter(employee=current_employee)


def _restrict_change_requests_queryset_for_employee(queryset, current_employee):
    if current_employee is None:
        return queryset.none()

    if is_hr_employee(current_employee):
        return queryset.exclude(employee__role__in=Employees.SERVICE_ROLES)

    if is_department_head_employee(current_employee):
        managed_department_id = get_managed_department_id(current_employee)
        if managed_department_id:
            return queryset.filter(
                employee__department_id=managed_department_id,
                employee__role=Employees.ROLE_EMPLOYEE,
            )
        return queryset.none()

    if is_enterprise_head_employee(current_employee):
        return queryset.exclude(employee__role__in=Employees.SERVICE_ROLES)

    if is_authorized_person_employee(current_employee):
        return queryset.filter(employee__role=Employees.ROLE_ENTERPRISE_HEAD)

    return queryset.filter(employee=current_employee)


def _normalize_employee_search_query(value):
    return " ".join((value or "").split())


def _filter_by_employee_name(queryset, search_query):
    for token in search_query.split():
        queryset = queryset.filter(
            Q(employee__last_name__icontains=token)
            | Q(employee__first_name__icontains=token)
            | Q(employee__middle_name__icontains=token)
        )
    return queryset


def _get_calendar_available_years(current_year, selected_year=None):
    years = set(VacationRequest.objects.values_list("start_date__year", flat=True))
    years.update(VacationRequest.objects.values_list("end_date__year", flat=True))
    years.update(VacationScheduleItem.objects.values_list("start_date__year", flat=True))
    years.update(VacationScheduleItem.objects.values_list("end_date__year", flat=True))
    available_years = sorted(year for year in years if year)
    return available_years or [current_year]


@employee_required
def graphics(request):
    context = get_user_context(request)
    context = update_context_with_departments(request, context)
    current_user = get_current_employee(request)
    if is_authorized_person_employee(current_user):
        messages.error(request, "У вас нет прав для доступа к графику отпусков.")
        return redirect("applications")

    today = timezone.localdate()
    current_year = today.year

    selected_year = request.GET.get("year", current_year)
    selected_month = request.GET.get("month", today.month)
    calendar_view_mode = request.GET.get("view", "month")
    selected_employee_id = request.GET.get("employee")

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

    sync_employee_vacation_metrics(current_user)
    current_user.refresh_from_db()
    current_user_leave_summary = get_employee_leave_summary(current_user)
    current_user_final_balance = current_user_leave_summary["available"]

    if request.method == "POST":
        form = VacationRequestCreateForm(_normalize_vacation_form_data(request.POST), employee=current_user)
        redirect_url = get_calendar_redirect_url(request)
        if form.is_valid():
            create_vacation_request(
                employee=current_user,
                start_date=form.cleaned_data["start_date"],
                end_date=form.cleaned_data["end_date"],
                vacation_type=form.cleaned_data["vacation_type"],
                reason=form.cleaned_data.get("reason", ""),
            )
            messages.success(request, "Заявка на отпуск успешно добавлена в график.")
        else:
            messages.error(request, _form_errors_to_messages(form) or "Не удалось создать заявку.")
        return redirect(redirect_url)

    visible_employee_ids = _get_visible_employee_ids(current_user)
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
        current_employee=current_user,
    )
    calendar_summary = build_calendar_summary(
        employee_entries,
        selected_year,
        selected_month,
        calendar_view_mode,
    )

    employee_ids = {row["employee_id"] for row in calendar_rows}
    if selected_employee_id not in employee_ids:
        selected_employee_id = current_user.id if current_user and current_user.id in employee_ids else None
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
    paid_request_allowed, paid_request_hint = get_paid_request_eligibility_for_year(current_user, selected_year)

    context.update(
        {
            "current_user": current_user,
            "current_user_leave_summary": current_user_leave_summary,
            "current_user_final_balance": current_user_final_balance,
            "calendar_charge_preview": {
                "holiday_dates": get_russian_holiday_iso_dates(range(min(available_years), max(available_years) + 1)),
                "available_balance": float(current_user_final_balance),
                "paid_request_allowed": paid_request_allowed,
            },
            "paid_request_allowed": paid_request_allowed,
            "paid_request_hint": paid_request_hint,
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

    if request.method == "GET" and request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(
            {
                "board_html": render_to_string("includes/calendar/board_content.html", context, request=request),
                "period_label": calendar_period_label,
                "period_description": calendar_period_description,
                "calendar_details": calendar_details,
            }
        )

    return render(request, "calendar.html", context)


@employee_required
def applications(request):
    current_employee = get_current_employee(request)
    if not can_access_applications(current_employee):
        messages.error(request, "Раздел заявок доступен только согласующим ролям и HR.")
        return redirect("main")

    context = get_user_context(request)
    context = update_context_with_departments(request, context)
    status_filter = request.GET.get("status", "all")
    department_id = request.GET.get("department", "all")
    search_query = _normalize_employee_search_query(request.GET.get("search", ""))
    requests_qs = _restrict_requests_queryset_for_employee(
        get_vacation_requests_queryset().order_by("-created_at"),
        current_employee,
    )
    change_requests_qs = _restrict_change_requests_queryset_for_employee(
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
        requests_qs = _filter_by_employee_name(requests_qs, search_query)
        change_requests_qs = _filter_by_employee_name(change_requests_qs, search_query)

    vacations = [enrich_vacation_request(request_obj) for request_obj in requests_qs]
    change_requests = [enrich_schedule_change_request(change_request) for change_request in change_requests_qs]
    for change_request in change_requests:
        change_request.can_approve = (
            change_request.status == VacationScheduleChangeRequest.STATUS_PENDING
            and can_approve_leave_for_employee(current_employee, change_request.employee)
        )

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(
            {
                "vacations": [serialize_vacation_request_row(vacation) for vacation in vacations],
                "change_requests": [
                    serialize_schedule_change_request_row(change_request)
                    for change_request in change_requests
                ],
            }
        )

    context.update(
        {
            "vacations": vacations,
            "change_requests": change_requests,
            "selected_status": status_filter,
            "selected_department": str(department_id),
            "search_query": search_query,
            "show_department_filter": not is_authorized_person_employee(current_employee),
        }
    )
    return render(request, "applications.html", context)


@employee_required
def vacation_detail(request, pk):
    context = get_user_context(request)
    context = update_context_with_departments(request, context)
    vacation = get_object_or_404(get_vacation_requests_queryset(), pk=pk)
    enrich_vacation_request(vacation)

    current_employee = get_current_employee(request)
    if not can_view_employee(current_employee, vacation.employee):
        messages.error(request, "У вас нет прав для просмотра этой заявки.")
        return redirect("main")

    can_approve_vacation = (
        vacation.status == VacationRequest.STATUS_PENDING
        and can_approve_leave_for_employee(current_employee, vacation.employee)
    )
    can_delete = vacation.status == VacationRequest.STATUS_PENDING and (
        vacation.employee_id == (current_employee.id if current_employee else None) or can_approve_vacation
    )
    employee_leave_summary = get_employee_leave_summary(vacation.employee)
    entitlement_rows = get_employee_entitlement_rows(vacation.employee)
    current_balance = get_employee_remaining_balance(vacation.employee)

    context.update(
        {
            "vacation": vacation,
            "employee": vacation.employee,
            "status": vacation.status,
            "status_label": vacation.status_label,
            "status_icon": vacation.status_icon,
            "status_css_class": vacation.status_css_class,
            "current_balance": current_balance,
            "employee_leave_summary": employee_leave_summary,
            "entitlement_rows": entitlement_rows,
            "vacation_chargeable_days": get_chargeable_leave_days(
                vacation.start_date,
                vacation.end_date,
                vacation.vacation_type,
            ),
            "can_approve_vacation": can_approve_vacation,
            "can_delete": can_delete,
        }
    )
    return render(request, "vacation_detail.html", context)


@employee_required
def approve_vacation(request, pk):
    vacation = get_object_or_404(get_vacation_requests_queryset(), pk=pk)
    current_employee = get_current_employee(request)

    if not can_approve_leave_for_employee(current_employee, vacation.employee):
        messages.error(request, "У вас нет прав для согласования этой заявки.")
        return redirect("vacation_detail", pk=pk)

    if request.method == "POST":
        try:
            approve_vacation_request(pk, reviewer=current_employee)
            messages.success(request, "Заявка успешно одобрена.")
        except ValidationError as exc:
            messages.error(request, _validation_error_message(exc))
    return redirect("applications")


@employee_required
def reject_vacation(request, pk):
    vacation = get_object_or_404(get_vacation_requests_queryset(), pk=pk)
    current_employee = get_current_employee(request)

    if not can_approve_leave_for_employee(current_employee, vacation.employee):
        messages.error(request, "У вас нет прав для согласования этой заявки.")
        return redirect("vacation_detail", pk=pk)

    if request.method == "POST":
        try:
            reject_vacation_request(pk, reviewer=current_employee)
            messages.error(request, "Заявка отклонена.")
        except ValidationError as exc:
            messages.error(request, _validation_error_message(exc))
    return redirect("applications")


@employee_required
def delete_vacation(request, pk):
    vacation = get_object_or_404(VacationRequest, pk=pk, status=VacationRequest.STATUS_PENDING)
    current_employee = get_current_employee(request)

    can_delete = vacation.employee_id == (current_employee.id if current_employee else None) or can_approve_leave_for_employee(
        current_employee,
        vacation.employee,
    )
    if not can_delete:
        messages.error(request, "У вас нет прав для удаления этой заявки.")
        return redirect("vacation_detail", pk=pk)

    if request.method == "POST":
        try:
            delete_pending_vacation_request(pk)
            messages.success(request, "Заявка успешно удалена.")
        except ValidationError as exc:
            messages.error(request, _validation_error_message(exc))
        return redirect("main")

    return redirect("vacation_detail", pk=pk)


@employee_required
def create_schedule_change(request, item_id):
    current_employee = get_current_employee(request)
    schedule_item = get_object_or_404(
        VacationScheduleItem.objects.select_related("employee", "schedule"),
        pk=item_id,
    )
    redirect_url = get_calendar_redirect_url(request)

    if request.method != "POST":
        return redirect(redirect_url)

    form = ScheduleChangeRequestCreateForm(request.POST, schedule_item=schedule_item)
    if not form.is_valid():
        messages.error(request, _form_errors_to_messages(form) or "Не удалось создать запрос переноса.")
        return redirect(redirect_url)

    try:
        create_schedule_change_request(
            schedule_item_id=schedule_item.id,
            requested_by=current_employee,
            new_start_date=form.cleaned_data["new_start_date"],
            new_end_date=form.cleaned_data["new_end_date"],
            reason=form.cleaned_data.get("reason", ""),
        )
        messages.success(request, "Запрос переноса отправлен на согласование.")
    except ValidationError as exc:
        messages.error(request, _validation_error_message(exc))
    return redirect(redirect_url)


@employee_required
def approve_schedule_change(request, pk):
    change_request = get_object_or_404(get_schedule_change_requests_queryset(), pk=pk)
    current_employee = get_current_employee(request)

    if not can_approve_leave_for_employee(current_employee, change_request.employee):
        messages.error(request, "У вас нет прав для согласования этого переноса.")
        return redirect("applications")

    if request.method == "POST":
        try:
            approve_schedule_change_request(pk, reviewer=current_employee)
            messages.success(request, "Перенос отпуска согласован.")
        except ValidationError as exc:
            messages.error(request, _validation_error_message(exc))
    return redirect("applications")


@employee_required
def reject_schedule_change(request, pk):
    change_request = get_object_or_404(get_schedule_change_requests_queryset(), pk=pk)
    current_employee = get_current_employee(request)

    if not can_approve_leave_for_employee(current_employee, change_request.employee):
        messages.error(request, "У вас нет прав для согласования этого переноса.")
        return redirect("applications")

    if request.method == "POST":
        try:
            reject_schedule_change_request(pk, reviewer=current_employee)
            messages.success(request, "Перенос отпуска отклонён.")
        except ValidationError as exc:
            messages.error(request, _validation_error_message(exc))
    return redirect("applications")


@employee_required
def analytics(request):
    current_employee = get_current_employee(request)
    if not can_access_analytics(current_employee):
        messages.error(request, "Раздел аналитики доступен только руководителям.")
        return redirect("main")

    context = get_user_context(request)
    context = update_context_with_departments(request, context)
    visible_employee_ids = _get_visible_employee_ids(current_employee)
    context.update(build_analytics_payload(employee_ids=visible_employee_ids))
    context.update({"default_annual_leave_days": 52})
    return render(request, "analytics.html", context)
