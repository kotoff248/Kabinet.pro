from datetime import date

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import (
    get_object_or_404,
    redirect,
    render,
)
from django.template.loader import render_to_string
from django.utils.formats import date_format

from apps.accounts.services import (
    can_initiate_schedule_change_for_item,
    employee_required,
    get_current_employee,
    get_user_context,
    is_authorized_person_employee,
)
from apps.employees.services import update_context_with_departments
from apps.leave.models import (
    VACATION_TYPE_CHOICES,
    VacationRequest,
    VacationScheduleItem,
)
from apps.leave.forms import VacationRequestCreateForm
from apps.leave.services.calendar import get_calendar_redirect_url
from apps.leave.services.constants import LEAVE_ADVANCE_MONTHS
from apps.leave.services.dates import (
    add_months_safe,
    clip_period_to_range,
    format_period_label,
    get_chargeable_leave_days,
    get_russian_holiday_iso_dates,
)
from apps.leave.services.ledger import (
    get_employee_available_balance,
    get_employee_entitlement_source_preview,
)
from apps.leave.services.page_contexts import build_calendar_page_context
from apps.leave.services.querysets import (
    exclude_converted_paid_requests,
    get_vacation_requests_queryset,
)
from apps.leave.services.risk import (
    build_vacation_request_risk_explanation,
    calculate_schedule_change_risk,
    calculate_vacation_request_risk,
)
from apps.leave.services.requests import create_vacation_request
from apps.leave.ml.request_support import build_schedule_change_ai_support, build_vacation_request_ai_support
from apps.leave.services.schedule_planning import can_access_schedule_planning
from apps.leave.services.scopes import get_visible_employee_ids
from apps.leave.services.validation import (
    validate_schedule_change_request,
    validate_vacation_request_for_employee,
)
from apps.leave.views.common import (
    _form_errors_to_messages,
    _validation_error_message,
    _json_number,
    _serialize_entitlement_source_preview,
    _serialize_vacation_request_ai_support,
    _parse_preview_date,
)


DATE_PICKER_SCHEDULE_STATUSES = (
    VacationScheduleItem.STATUS_DRAFT,
    VacationScheduleItem.STATUS_PLANNED,
    VacationScheduleItem.STATUS_APPROVED,
)


def _normalize_vacation_form_data(post_data):
    data = post_data.copy()
    if "type_vacation" in data and "vacation_type" not in data:
        data["vacation_type"] = data.get("type_vacation")
    return data


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


def _vacation_preview_risk_message(risk_explanation):
    if risk_explanation["is_conflict"]:
        return f"Есть конфликт состава: {risk_explanation['short_reason']} {risk_explanation['recommended_action']}"
    if risk_explanation["level"] == VacationRequest.RISK_HIGH:
        return f"Высокий риск: {risk_explanation['short_reason']} {risk_explanation['recommended_action']}"
    if risk_explanation["level"] == VacationRequest.RISK_MEDIUM:
        return f"Средний риск: {risk_explanation['short_reason']}"
    return ""


def _build_vacation_preview_payload(employee, start_date, end_date, vacation_type):
    calendar_days = (end_date - start_date).days + 1 if end_date >= start_date else 0
    chargeable_days = get_chargeable_leave_days(start_date, end_date, vacation_type) if calendar_days else 0
    balance_today = get_employee_available_balance(employee)
    available_on_start = get_employee_available_balance(employee, as_of_date=start_date)
    available_from = add_months_safe(employee.date_joined, LEAVE_ADVANCE_MONTHS)
    risk_payload = calculate_vacation_request_risk(employee, start_date, end_date, vacation_type)
    risk_explanation = build_vacation_request_risk_explanation(employee, start_date, end_date, vacation_type)
    entitlement_source_preview = get_employee_entitlement_source_preview(
        employee,
        start_date,
        end_date,
        vacation_type,
    )
    can_submit = True
    message = ""
    block_reason_key = ""

    try:
        validate_vacation_request_for_employee(employee, start_date, end_date, vacation_type)
    except ValidationError as exc:
        can_submit = False
        block_reason_key = "validation_error"
        message = _vacation_preview_message(vacation_type, start_date, employee, False) or _validation_error_message(exc)

    if can_submit:
        message = _vacation_preview_risk_message(risk_explanation) or _vacation_preview_message(
            vacation_type,
            start_date,
            employee,
            True,
        )

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
        "risk_explanation": risk_explanation,
        "risk_short_reason": risk_explanation["short_reason"],
        "risk_recommended_action": risk_explanation["recommended_action"],
        "risk_is_conflict": risk_explanation["is_conflict"],
    }
    payload.update(_serialize_entitlement_source_preview(entitlement_source_preview))
    ai_support = build_vacation_request_ai_support(
        employee,
        start_date,
        end_date,
        vacation_type,
        can_submit=can_submit,
        risk_payload=risk_payload,
        risk_explanation=risk_explanation,
        block_reason_key=block_reason_key,
    )
    payload.update(_serialize_vacation_request_ai_support(ai_support))
    return payload


def _format_chargeable_days_delta(delta):
    if delta < 0:
        return f"Освободится {abs(delta):g} д."
    if delta > 0:
        return f"Добавится {delta:g} д."
    return "Без изменения"


def _empty_schedule_change_ai_payload(message="Выберите новые даты, чтобы получить подсказку нейромодуля."):
    return {
        "module_score": 0,
        "module_score_label": "",
        "module_confidence": 0,
        "module_confidence_label": "",
        "module_model_version": "",
        "module_recommendation": "blocked",
        "module_recommendation_label": "Выберите даты",
        "module_action": "Подсказка не заменяет жесткие правила переноса.",
        "module_explanation": message,
        "module_scorer_kind": "",
        "module_alternatives": [],
    }


def _empty_schedule_change_preview_payload(schedule_item, message):
    old_calendar_days = (schedule_item.end_date - schedule_item.start_date).days + 1
    payload = {
        "can_submit": False,
        "message": message,
        "old_calendar_days": old_calendar_days,
        "new_calendar_days": 0,
        "old_chargeable_days": _json_number(schedule_item.chargeable_days),
        "new_chargeable_days": 0,
        "chargeable_days_delta": 0,
        "chargeable_days_delta_label": "Выберите новые даты",
        "balance_after_change": _json_number(get_employee_available_balance(schedule_item.employee)),
        "risk_label": "Низкий",
        "risk_score": 0,
        "risk_explanation": {},
        "risk_short_reason": "",
        "risk_recommended_action": "",
        "risk_is_conflict": False,
    }
    payload.update(_empty_schedule_change_ai_payload(message))
    return payload


def _build_schedule_change_preview_payload(schedule_item, new_start_date, new_end_date):
    old_calendar_days = (schedule_item.end_date - schedule_item.start_date).days + 1
    new_calendar_days = (new_end_date - new_start_date).days + 1 if new_end_date >= new_start_date else 0
    old_chargeable_days = _json_number(schedule_item.chargeable_days)
    new_chargeable_days = (
        get_chargeable_leave_days(new_start_date, new_end_date, schedule_item.vacation_type)
        if new_calendar_days
        else 0
    )
    chargeable_days_delta = _json_number(new_chargeable_days) - old_chargeable_days
    can_submit = True
    block_reason_key = ""
    message = "Перенос можно отправить: даты, баланс и пересечения проверены."

    try:
        validate_schedule_change_request(schedule_item, new_start_date, new_end_date)
    except ValidationError as exc:
        can_submit = False
        block_reason_key = "validation_error"
        message = _validation_error_message(exc)

    risk_payload = calculate_schedule_change_risk(schedule_item, new_start_date, new_end_date) if new_calendar_days else {
        "risk_score": 0,
        "risk_level": VacationRequest.RISK_LOW,
        "balance_after_change": get_employee_available_balance(schedule_item.employee),
    }
    risk_explanation = (
        build_vacation_request_risk_explanation(
            schedule_item.employee,
            new_start_date,
            new_end_date,
            schedule_item.vacation_type,
            exclude_schedule_item_id=schedule_item.id,
        )
        if new_calendar_days
        else {}
    )
    risk_label = dict(VacationRequest.RISK_CHOICES).get(risk_payload["risk_level"], "Низкий")
    if can_submit:
        if risk_explanation.get("is_conflict"):
            message = "Перенос можно отправить, но есть конфликт состава."
        elif risk_payload["risk_level"] == VacationRequest.RISK_HIGH:
            message = "Перенос можно отправить, но риск высокий."

    ai_support = build_schedule_change_ai_support(
        schedule_item,
        new_start_date,
        new_end_date,
        can_submit=can_submit,
        risk_payload=risk_payload,
        risk_explanation=risk_explanation,
        block_reason_key=block_reason_key,
    )
    payload = {
        "can_submit": can_submit,
        "message": message,
        "old_calendar_days": old_calendar_days,
        "new_calendar_days": new_calendar_days,
        "old_chargeable_days": old_chargeable_days,
        "new_chargeable_days": _json_number(new_chargeable_days),
        "chargeable_days_delta": chargeable_days_delta,
        "chargeable_days_delta_label": _format_chargeable_days_delta(chargeable_days_delta),
        "balance_after_change": _json_number(risk_payload["balance_after_change"]),
        "risk_label": risk_label,
        "risk_score": risk_payload["risk_score"],
        "risk_explanation": risk_explanation,
        "risk_short_reason": risk_explanation.get("short_reason", ""),
        "risk_recommended_action": risk_explanation.get("recommended_action", ""),
        "risk_is_conflict": risk_explanation.get("is_conflict", False),
    }
    payload.update(_serialize_vacation_request_ai_support(ai_support))
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
def schedule_change_preview(request, item_id):
    current_employee = get_current_employee(request)
    schedule_item = get_object_or_404(
        VacationScheduleItem.objects.select_related("employee", "schedule"),
        pk=item_id,
    )
    if request.method != "GET":
        return JsonResponse(
            {"can_submit": False, "message": "Проверка переноса доступна только GET-запросом."},
            status=405,
        )
    if not can_initiate_schedule_change_for_item(current_employee, schedule_item):
        return JsonResponse(
            {
                "can_submit": False,
                "message": "У вас нет прав для создания переноса по этому отпуску.",
            },
            status=403,
        )

    try:
        new_start_date = _parse_preview_date(request.GET.get("new_start_date"), "Новая дата начала")
        new_end_date = _parse_preview_date(request.GET.get("new_end_date"), "Новая дата окончания")
    except ValidationError as exc:
        return JsonResponse(_empty_schedule_change_preview_payload(schedule_item, _validation_error_message(exc)))

    return JsonResponse(_build_schedule_change_preview_payload(schedule_item, new_start_date, new_end_date))


def _parse_positive_int(value):
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _empty_date_picker_payload(year, employee_id=None):
    return {
        "employee_id": employee_id,
        "year": year,
        "holiday_dates": get_russian_holiday_iso_dates([year]),
        "periods": [],
    }


def _serialize_date_picker_period(*, start_date, end_date, label, status, status_label, source_kind):
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "label": label,
        "status": status,
        "status_label": status_label,
        "source_kind": source_kind,
    }


@employee_required
def calendar_date_picker_periods(request):
    current_employee = get_current_employee(request)
    if request.method != "GET":
        return JsonResponse(_empty_date_picker_payload(date.today().year), status=405)

    requested_year = _parse_positive_int(request.GET.get("year"))
    year = requested_year if requested_year and 2000 <= requested_year <= 2100 else date.today().year
    raw_employee_id = request.GET.get("employee_id")
    employee_id = _parse_positive_int(raw_employee_id)
    if raw_employee_id and employee_id is None:
        return JsonResponse(_empty_date_picker_payload(year))
    if employee_id is None:
        employee_id = current_employee.id

    visible_employee_ids = set(get_visible_employee_ids(current_employee))
    if employee_id not in visible_employee_ids:
        return JsonResponse(
            _empty_date_picker_payload(year, employee_id),
            status=403,
        )

    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)
    exclude_schedule_item_id = _parse_positive_int(request.GET.get("exclude_schedule_item"))
    periods = []

    requests = get_vacation_requests_queryset().filter(
        employee_id=employee_id,
        status__in=VacationRequest.ACTIVE_STATUSES,
        start_date__lte=year_end,
        end_date__gte=year_start,
    )
    requests = exclude_converted_paid_requests(
        requests,
        employee_ids=[employee_id],
        start_date=year_start,
        end_date=year_end,
    )
    for vacation in requests:
        clipped_period = clip_period_to_range(vacation.start_date, vacation.end_date, year_start, year_end)
        if clipped_period is None:
            continue
        clipped_start, clipped_end = clipped_period
        periods.append(
            _serialize_date_picker_period(
                start_date=clipped_start,
                end_date=clipped_end,
                label=format_period_label(clipped_start, clipped_end),
                status=vacation.status,
                status_label=vacation.get_status_display(),
                source_kind="request",
            )
        )

    schedule_items = VacationScheduleItem.objects.filter(
        employee_id=employee_id,
        status__in=DATE_PICKER_SCHEDULE_STATUSES,
        start_date__lte=year_end,
        end_date__gte=year_start,
    ).select_related("schedule")
    if exclude_schedule_item_id:
        schedule_items = schedule_items.exclude(pk=exclude_schedule_item_id)

    for item in schedule_items:
        clipped_period = clip_period_to_range(item.start_date, item.end_date, year_start, year_end)
        if clipped_period is None:
            continue
        clipped_start, clipped_end = clipped_period
        periods.append(
            _serialize_date_picker_period(
                start_date=clipped_start,
                end_date=clipped_end,
                label=format_period_label(clipped_start, clipped_end),
                status=item.status,
                status_label=item.get_status_display(),
                source_kind="schedule",
            )
        )

    periods.sort(key=lambda period: (period["start_date"], period["end_date"], period["source_kind"]))
    return JsonResponse(
        {
            "employee_id": employee_id,
            "year": year,
            "holiday_dates": get_russian_holiday_iso_dates([year]),
            "periods": periods,
        }
    )


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

    is_calendar_xhr = request.method == "GET" and request.headers.get("x-requested-with") == "XMLHttpRequest"
    detail_employee_id = request.GET.get("calendar_detail_employee")
    detail_month = request.GET.get("calendar_detail_month")
    initial_month = request.GET.get("calendar_month") if request.GET.get("calendar_modal") == "month_summary" else None
    should_build_month_detail = bool(detail_month or initial_month)
    detail_employee_ids = None
    if is_calendar_xhr and not should_build_month_detail:
        detail_employee_ids = [detail_employee_id] if detail_employee_id else []
    context.update(
        build_calendar_page_context(
            current_user,
            request.GET,
            include_month_details=False,
            month_detail_numbers=[detail_month or initial_month] if (detail_month or initial_month) else None,
            detail_employee_ids=detail_employee_ids,
        )
    )
    if request.GET.get("from") == "schedule_planning" and can_access_schedule_planning(current_user):
        context["sidebar_section"] = "schedule_planning"
    calendar_period_label = context["calendar_period_label"]
    calendar_period_description = context["calendar_period_description"]
    calendar_details = context["calendar_details"]
    calendar_month_details = context["calendar_month_details"]

    if is_calendar_xhr:
        if detail_employee_id:
            return JsonResponse(
                {
                    "calendar_detail": calendar_details.get(str(detail_employee_id)),
                }
            )

        if detail_month:
            return JsonResponse(
                {
                    "calendar_month_detail": calendar_month_details.get(str(detail_month)),
                }
            )

        return JsonResponse(
                {
                    "board_html": render_to_string("includes/calendar/board_content.html", context, request=request),
                    "period_label": calendar_period_label,
                    "period_description": calendar_period_description,
                    "calendar_details": {},
                    "calendar_month_details": {},
                }
            )

    calendar_details_payload = {}
    calendar_month_details_payload = {}
    selected_employee_detail = context.get("selected_employee_detail")
    selected_employee_id = context.get("selected_employee_id")
    if selected_employee_id and selected_employee_detail:
        calendar_details_payload[str(selected_employee_id)] = selected_employee_detail

    if request.GET.get("calendar_modal") == "month_summary":
        calendar_month = request.GET.get("calendar_month")
        if calendar_month and calendar_month in calendar_month_details:
            calendar_month_details_payload[str(calendar_month)] = calendar_month_details[str(calendar_month)]

    context["calendar_details_payload"] = calendar_details_payload
    context["calendar_month_details_payload"] = calendar_month_details_payload

    return render(request, "calendar.html", context)
