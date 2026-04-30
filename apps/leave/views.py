from datetime import date

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils.formats import date_format

from apps.accounts.services import (
    can_access_analytics,
    can_access_applications,
    can_approve_leave_for_employee,
    can_view_employee,
    employee_required,
    get_current_employee,
    get_user_context,
    is_authorized_person_employee,
)
from apps.employees.services import update_context_with_departments
from apps.leave.models import VACATION_TYPE_CHOICES, VacationRequest, VacationScheduleItem

from .forms import ScheduleChangeRequestCreateForm, VacationRequestCreateForm
from .services.calendar import get_calendar_redirect_url
from .services.constants import LEAVE_ADVANCE_MONTHS
from .services.dates import add_months_safe, get_chargeable_leave_days
from .services.ledger import get_employee_available_balance, get_employee_entitlement_source_preview
from .services.page_contexts import (
    build_analytics_page_context,
    build_applications_json_payload,
    build_applications_page_context,
    build_calendar_page_context,
    build_vacation_detail_context,
)
from .services.querysets import get_vacation_requests_queryset
from .services.risk import calculate_vacation_request_risk
from .services.requests import (
    approve_vacation_request,
    create_vacation_request,
    delete_pending_vacation_request,
    reject_vacation_request,
)
from .services.schedule_changes import (
    approve_schedule_change_request,
    create_schedule_change_request,
    get_schedule_change_requests_queryset,
    reject_schedule_change_request,
)
from .services.validation import validate_vacation_request_for_employee


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


def _json_number(value):
    return float(value or 0)


def _serialize_entitlement_source_preview(preview):
    return {
        "entitlement_source_label": preview["label"],
        "entitlement_allocations": [
            {
                "working_year_number": row["working_year_number"],
                "period_label": row["period_label"],
                "period_start": row["period_start"].isoformat(),
                "period_end": row["period_end"].isoformat(),
                "days": _json_number(row["days"]),
                "balance_before": _json_number(row["balance_before"]),
                "balance_after": _json_number(row["balance_after"]),
            }
            for row in preview["allocations"]
        ],
    }


def _parse_preview_date(value, field_label):
    if not value:
        raise ValidationError(f"Выберите поле «{field_label}».")
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValidationError(f"Некорректная дата в поле «{field_label}».")


def _vacation_preview_message(vacation_type, start_date, employee, can_submit):
    available_from = add_months_safe(employee.date_joined, LEAVE_ADVANCE_MONTHS)
    if vacation_type == "paid" and start_date < available_from:
        return (
            f"Оплачиваемый отпуск доступен с {date_format(available_from, 'j E Y')}. "
            "Выберите дату начала не раньше этой даты."
        )
    if not can_submit:
        return ""
    if vacation_type == "paid":
        return "Заявку можно отправить: право на отпуск и баланс проверены на дату начала отпуска."
    if vacation_type == "study":
        return "Заявку можно отправить. Учебный отпуск не уменьшает оплачиваемый баланс."
    return "Заявку можно отправить. Неоплачиваемый отпуск не уменьшает оплачиваемый баланс."


def _build_vacation_preview_payload(employee, start_date, end_date, vacation_type):
    calendar_days = (end_date - start_date).days + 1 if end_date >= start_date else 0
    chargeable_days = get_chargeable_leave_days(start_date, end_date, vacation_type) if calendar_days else 0
    balance_today = get_employee_available_balance(employee)
    available_on_start = get_employee_available_balance(employee, as_of_date=start_date)
    available_from = add_months_safe(employee.date_joined, LEAVE_ADVANCE_MONTHS)
    risk_payload = calculate_vacation_request_risk(employee, start_date, end_date, vacation_type)
    entitlement_source_preview = get_employee_entitlement_source_preview(
        employee,
        start_date,
        end_date,
        vacation_type,
    )
    can_submit = True
    message = ""

    try:
        validate_vacation_request_for_employee(employee, start_date, end_date, vacation_type)
    except ValidationError as exc:
        can_submit = False
        message = _vacation_preview_message(vacation_type, start_date, employee, False) or _validation_error_message(exc)

    if can_submit:
        message = _vacation_preview_message(vacation_type, start_date, employee, True)

    risk_label = dict(VacationRequest.RISK_CHOICES).get(risk_payload["risk_level"], "Низкий")
    payload = {
        "can_submit": can_submit,
        "message": message,
        "calendar_days": calendar_days,
        "chargeable_days": chargeable_days,
        "balance_today": _json_number(balance_today),
        "available_on_start": _json_number(available_on_start),
        "remaining_after_request": _json_number(risk_payload["balance_after_request"]),
        "available_from": available_from.isoformat(),
        "risk_label": risk_label,
        "risk_score": risk_payload["risk_score"],
    }
    payload.update(_serialize_entitlement_source_preview(entitlement_source_preview))
    return payload


@employee_required
def vacation_request_preview(request):
    current_user = get_current_employee(request)
    if is_authorized_person_employee(current_user):
        return JsonResponse(
            {
                "can_submit": False,
                "message": "Уполномоченное лицо не создаёт заявки через календарь.",
            },
            status=403,
        )
    if request.method != "GET":
        return JsonResponse(
            {"can_submit": False, "message": "Проверка заявки доступна только GET-запросом."},
            status=405,
        )

    vacation_type = request.GET.get("vacation_type") or "paid"
    allowed_types = {choice[0] for choice in VACATION_TYPE_CHOICES}
    if vacation_type not in allowed_types:
        return JsonResponse(
            {
                "can_submit": False,
                "message": "Выберите корректный тип отпуска.",
                "calendar_days": 0,
                "chargeable_days": 0,
                "balance_today": _json_number(get_employee_available_balance(current_user)),
                "available_on_start": 0,
                "remaining_after_request": 0,
                "available_from": add_months_safe(current_user.date_joined, LEAVE_ADVANCE_MONTHS).isoformat(),
                "risk_label": "Низкий",
                "risk_score": 0,
                "entitlement_source_label": "Выберите корректный тип отпуска.",
                "entitlement_allocations": [],
            }
        )

    try:
        start_date = _parse_preview_date(request.GET.get("start_date"), "Дата начала")
        end_date = _parse_preview_date(request.GET.get("end_date"), "Дата окончания")
    except ValidationError as exc:
        return JsonResponse(
            {
                "can_submit": False,
                "message": _validation_error_message(exc),
                "calendar_days": 0,
                "chargeable_days": 0,
                "balance_today": _json_number(get_employee_available_balance(current_user)),
                "available_on_start": 0,
                "remaining_after_request": 0,
                "available_from": add_months_safe(current_user.date_joined, LEAVE_ADVANCE_MONTHS).isoformat(),
                "risk_label": "Низкий",
                "risk_score": 0,
                "entitlement_source_label": "Выберите даты, чтобы определить рабочий год списания.",
                "entitlement_allocations": [],
            }
        )

    return JsonResponse(_build_vacation_preview_payload(current_user, start_date, end_date, vacation_type))


@employee_required
def graphics(request):
    context = get_user_context(request)
    context = update_context_with_departments(request, context)
    current_user = get_current_employee(request)
    if is_authorized_person_employee(current_user):
        messages.error(request, "У вас нет прав для доступа к графику отпусков.")
        return redirect("applications")

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

    context.update(build_calendar_page_context(current_user, request.GET))
    calendar_period_label = context["calendar_period_label"]
    calendar_period_description = context["calendar_period_description"]
    calendar_details = context["calendar_details"]

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
    page_context = build_applications_page_context(current_employee, request.GET)
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        payload = build_applications_json_payload(
            page_context["vacations"],
            page_context["change_requests"],
        )
        payload.update(
            {
                "change_requests_html": render_to_string(
                    "includes/applications/change_requests_list.html",
                    page_context,
                    request=request,
                ),
                "vacations_html": render_to_string(
                    "includes/applications/vacations_list.html",
                    page_context,
                    request=request,
                ),
            }
        )
        return JsonResponse(payload)

    context.update(page_context)
    return render(request, "applications.html", context)


@employee_required
def vacation_detail(request, pk):
    context = get_user_context(request)
    context = update_context_with_departments(request, context)
    try:
        vacation = get_vacation_requests_queryset().get(pk=pk)
    except VacationRequest.DoesNotExist:
        messages.error(request, "Заявка удалена или больше недоступна.")
        return redirect("applications")
    current_employee = get_current_employee(request)
    if not can_view_employee(current_employee, vacation.employee):
        messages.error(request, "У вас нет прав для просмотра этой заявки.")
        return redirect("main")

    context.update(build_vacation_detail_context(vacation, current_employee))
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
            delete_pending_vacation_request(pk, actor=current_employee)
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
    context.update(build_analytics_page_context(current_employee))
    return render(request, "analytics.html", context)
