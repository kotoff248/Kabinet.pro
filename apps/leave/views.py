import json
import secrets
from datetime import date
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.formats import date_format
from django.utils.http import url_has_allowed_host_and_scheme

from apps.core.services.navigation import build_explicit_back_link
from apps.accounts.services import (
    can_access_analytics,
    can_access_applications,
    can_approve_leave_for_employee,
    can_initiate_schedule_change_for_item,
    can_review_schedule_change_request,
    can_view_employee,
    employee_required,
    get_managed_department_id,
    get_current_employee,
    get_user_context,
    is_authorized_person_employee,
    is_department_head_employee,
    is_enterprise_head_employee,
    is_hr_employee,
)
from apps.employees.models import Employees
from apps.employees.services import update_context_with_departments
from apps.leave.models import (
    VACATION_TYPE_CHOICES,
    VacationPreference,
    VacationPreferenceCollection,
    VacationRequest,
    VacationSchedule,
    VacationScheduleAutoPlaceJob,
    VacationScheduleChangeRequest,
    VacationScheduleItem,
    VacationUrgentClosureRequest,
)

from .forms import ScheduleChangeRequestCreateForm, VacationPreferenceResponseForm, VacationRequestCreateForm
from .services.calendar import get_calendar_redirect_url
from .services.candidate_feedback import build_schedule_candidate_feedback_context, submit_schedule_candidate_feedback
from .services.constants import LEAVE_ADVANCE_MONTHS
from .services.dates import add_months_safe, get_chargeable_leave_days
from .services.ledger import get_employee_available_balance, get_employee_entitlement_source_preview
from .services.page_contexts import (
    build_analytics_page_context,
    build_applications_json_payload,
    build_applications_page_context,
    build_calendar_page_context,
    build_schedule_change_detail_context,
    build_urgent_closure_detail_context,
    build_vacation_detail_context,
)
from .services.preferences import (
    employee_can_join_preference_collection,
    build_calendar_preference_collection_context,
    build_preference_collection_readiness_context,
    finish_preference_collection,
    get_employee_preference_page_context,
    get_preference_planning_year,
    start_preference_collection,
    submit_employee_preferences,
)
from .services.querysets import get_vacation_requests_queryset
from .services.risk import (
    build_vacation_request_risk_explanation,
    calculate_schedule_change_risk,
    calculate_vacation_request_risk,
)
from .services.requests import (
    approve_vacation_request,
    create_vacation_request,
    delete_pending_vacation_request,
    reject_vacation_request,
)
from .services.schedule_drafts import (
    build_manual_schedule_draft_package_preview,
    build_manual_schedule_draft_preview,
    build_schedule_draft_auto_place_preview,
    build_schedule_draft_day_calculation,
    build_schedule_draft_item_review_context,
    build_schedule_draft_manual_suggestions,
    build_schedule_draft_page_context,
    create_schedule_draft_from_preferences,
    get_schedule_draft_status,
    place_manual_schedule_draft_item,
    place_manual_schedule_draft_items,
)
from .services.schedule_auto_place_jobs import (
    get_or_create_schedule_auto_place_job,
    schedule_auto_place_job_payload,
    start_schedule_auto_place_process,
)
from .services.schedule_planning import (
    build_schedule_planning_page_context,
    can_access_schedule_planning,
    get_schedule_planning_year,
    schedule_planning_url,
)
from .services.schedule_changes import (
    approve_schedule_change_request,
    create_schedule_change_request,
    get_schedule_change_requests_queryset,
    is_manager_initiated_schedule_change,
    reject_schedule_change_request,
)
from .services.urgent_closures import (
    accept_urgent_closure_by_employee,
    apply_urgent_closure_demo_responses,
    approve_urgent_closure_by_manager,
    build_urgent_closure_preview,
    can_department_review_urgent_closure,
    can_employee_review_urgent_closure,
    can_finalize_urgent_closure,
    can_view_urgent_closure_request,
    create_urgent_closure_request,
    finalize_urgent_closure,
    get_urgent_closure_requests_queryset,
    propose_urgent_closure_period_by_employee,
    reject_urgent_closure,
)
from .services.validation import validate_schedule_change_request, validate_vacation_request_for_employee


def _form_errors_to_messages(form):
    errors = []
    for field_errors in form.errors.values():
        errors.extend(field_errors)
    return " ".join(str(error) for error in errors)


def _validation_error_message(exc):
    return " ".join(exc.messages) if getattr(exc, "messages", None) else str(exc)


def _request_wants_json(request):
    return (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
    )


def _urgent_closure_create_success_message(demo_result):
    if not demo_result or not demo_result.get("manager_approved"):
        return "Согласование срочного остатка отправлено руководителю отдела."
    if demo_result.get("employee_proposed"):
        return "Демо-ответы применены: сотрудник предложил другой период, задача снова у руководителя."
    if demo_result.get("employee_accepted"):
        return "Демо-ответы применены: руководитель и сотрудник подтвердили период, ожидается финализация HR."
    return "Демо-ответ применен: руководитель подтвердил период, ожидается ответ сотрудника."


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


def _parse_manual_periods_payload(raw_periods):
    if not raw_periods:
        raise ValidationError("Добавьте хотя бы один период отпуска.")
    if isinstance(raw_periods, str):
        try:
            raw_periods = json.loads(raw_periods)
        except json.JSONDecodeError:
            raise ValidationError("Не удалось разобрать список периодов.")
    if not isinstance(raw_periods, list):
        raise ValidationError("Список периодов должен быть массивом.")

    periods = []
    for index, period in enumerate(raw_periods, start=1):
        if not isinstance(period, dict):
            raise ValidationError(f"Период {index} заполнен некорректно.")
        periods.append(
            {
                "start_date": _parse_preview_date(period.get("start_date"), f"Дата начала {index}"),
                "end_date": _parse_preview_date(period.get("end_date"), f"Дата окончания {index}"),
            }
        )
    return periods


def _manual_package_preview_json(preview):
    return {
        "can_submit": preview["can_submit"],
        "message": preview["message"],
        "calendar_days": preview["calendar_days"],
        "chargeable_days": _json_number(preview["chargeable_days"]),
        "remaining_after_placement": _json_number(preview["remaining_after_placement"]),
        "target_days": _json_number(preview["planning_need"]["target_days"]),
        "placed_days": _json_number(preview["planning_need"]["placed_days"]),
        "open_required_days": _json_number(preview["planning_need"]["open_required_days"]),
        "blocking_after_placement": _json_number(preview.get("blocking_after_placement", 0)),
        "annual_remaining_after_placement": _json_number(preview.get("annual_remaining_after_placement", 0)),
        "risk_label": preview["risk_label"],
        "risk_score": preview["risk_score"],
        "risk_level": preview["risk_level"],
        "risk_tone": preview["risk_tone"],
        "risk_short_reason": preview["risk_short_reason"],
        "risk_recommended_action": preview["risk_recommended_action"],
        "risk_is_conflict": preview["risk_is_conflict"],
        "periods": [
            {
                "order": period["order"],
                "start_date": period["start_date_iso"],
                "end_date": period["end_date_iso"],
                "period_label": period["period_label"],
                "full_period_label": period["full_period_label"],
                "calendar_days": period["calendar_days"],
                "chargeable_days": _json_number(period["chargeable_days"]),
                "chargeable_days_label": period["chargeable_days_label"],
                "can_place": period["can_place"],
                "message": period["message"],
                "risk_label": period["risk_label"],
                "risk_score": period["risk_score"],
                "risk_level": period["risk_level"],
                "risk_tone": period["risk_tone"],
                "risk_short_reason": period["risk_short_reason"],
                "risk_recommended_action": period["risk_recommended_action"],
                "risk_is_conflict": period["risk_is_conflict"],
                "remaining_after_period": _json_number(period["remaining_after_period"]),
            }
            for period in preview["periods"]
        ],
    }


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

    try:
        validate_vacation_request_for_employee(employee, start_date, end_date, vacation_type)
    except ValidationError as exc:
        can_submit = False
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
    return payload


def _format_chargeable_days_delta(delta):
    if delta < 0:
        return f"Освободится {abs(delta):g} д."
    if delta > 0:
        return f"Добавится {delta:g} д."
    return "Без изменения"


def _empty_schedule_change_preview_payload(schedule_item, message):
    old_calendar_days = (schedule_item.end_date - schedule_item.start_date).days + 1
    return {
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
    message = "Перенос можно отправить: даты, баланс и пересечения проверены."

    try:
        validate_schedule_change_request(schedule_item, new_start_date, new_end_date)
    except ValidationError as exc:
        can_submit = False
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

    return {
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
    if request.GET.get("from") == "schedule_planning" and can_access_schedule_planning(current_user):
        context["sidebar_section"] = "schedule_planning"
    calendar_period_label = context["calendar_period_label"]
    calendar_period_description = context["calendar_period_description"]
    calendar_details = context["calendar_details"]
    calendar_month_details = context["calendar_month_details"]

    if request.method == "GET" and request.headers.get("x-requested-with") == "XMLHttpRequest":
        detail_employee_id = request.GET.get("calendar_detail_employee")
        if detail_employee_id:
            return JsonResponse(
                {
                    "calendar_detail": calendar_details.get(str(detail_employee_id)),
                }
            )

        detail_month = request.GET.get("calendar_detail_month")
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
                "calendar_details": calendar_details,
                "calendar_month_details": calendar_month_details,
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


def _calendar_year_redirect(year):
    return f"{reverse('calendar')}?view=year&year={year}"


def _can_view_schedule_draft_item(current_employee, schedule_item):
    if is_hr_employee(current_employee) or is_enterprise_head_employee(current_employee):
        return True
    if is_department_head_employee(current_employee):
        managed_department_id = get_managed_department_id(current_employee)
        return bool(managed_department_id and schedule_item.employee.department_id == managed_department_id)
    return False


def _can_view_schedule_draft_employee(current_employee, employee):
    if is_hr_employee(current_employee) or is_enterprise_head_employee(current_employee):
        return True
    if is_department_head_employee(current_employee):
        managed_department_id = get_managed_department_id(current_employee)
        return bool(managed_department_id and employee.department_id == managed_department_id)
    return False


def _parse_collection_year(value):
    try:
        year = int(value)
    except (TypeError, ValueError):
        raise ValidationError("Выберите корректный год сбора пожеланий.")
    if year < 2000 or year > 2100:
        raise ValidationError("Выберите корректный год сбора пожеланий.")
    return year


def _parse_deadline(value):
    if not value:
        raise ValidationError("Укажите срок заполнения пожеланий.")
    try:
        deadline = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValidationError("Укажите корректный срок заполнения пожеланий.")
    if deadline < date.today():
        raise ValidationError("Срок заполнения не может быть в прошлом.")
    return deadline


def _safe_next_url(request, fallback):
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        return next_url
    return fallback


def _relative_return_path(url):
    split_url = urlsplit(url)
    if split_url.scheme or split_url.netloc:
        return urlunsplit(("", "", split_url.path or "/", split_url.query, split_url.fragment))
    return url


def _schedule_draft_return_source(return_url):
    query = dict(parse_qsl(urlsplit(return_url).query, keep_blank_values=True))
    return "schedule_planning" if query.get("from") == "schedule_planning" else "calendar"


def _url_with_query_params(url, **params):
    split_url = urlsplit(url)
    query = dict(parse_qsl(split_url.query, keep_blank_values=True))
    for key, value in params.items():
        if value in (None, ""):
            query.pop(key, None)
        else:
            query[key] = str(value)
    return urlunsplit(
        (
            split_url.scheme,
            split_url.netloc,
            split_url.path,
            urlencode(query),
            split_url.fragment,
        )
    )


def _url_with_fragment(url, fragment):
    split_url = urlsplit(url)
    return urlunsplit(
        (
            split_url.scheme,
            split_url.netloc,
            split_url.path,
            split_url.query,
            fragment,
        )
    )


def _planning_nested_url(url, year, stage):
    return _url_with_query_params(
        url,
        **{
            "from": "schedule_planning",
            "back_url": schedule_planning_url(year, stage),
            "back_label": "К планированию",
        },
    )


@employee_required
def start_vacation_preferences_collection(request):
    current_employee = get_current_employee(request)
    if not is_hr_employee(current_employee):
        messages.error(request, "Запустить сбор пожеланий может только HR.")
        return redirect("calendar")
    if request.method != "POST":
        return redirect("calendar")

    try:
        year = _parse_collection_year(request.POST.get("year"))
        deadline = _parse_deadline(request.POST.get("deadline"))
    except ValidationError as exc:
        messages.error(request, _validation_error_message(exc))
        return redirect(_safe_next_url(request, "calendar"))

    planning_year = get_preference_planning_year()
    if year != planning_year:
        messages.error(request, f"Сбор пожеланий сейчас открывается только на {planning_year} год.")
        return redirect(_safe_next_url(request, _calendar_year_redirect(planning_year)))

    demo_autofill = request.POST.get("demo_autofill") == "on"
    stats = start_preference_collection(
        year=year,
        deadline=deadline,
        actor=current_employee,
        demo_autofill=demo_autofill,
    )
    messages.success(
        request,
        (
            f"Сбор пожеланий на {year} год открыт. "
            f"Демо-ответов: {stats['demo_filled_count']}, "
            f"без пожеланий: {stats['demo_skipped_count']}, "
            f"ожидают ответа: {stats['notified_count']}."
        ),
    )
    return redirect(_safe_next_url(request, _calendar_year_redirect(year)))


@employee_required
def finish_vacation_preferences_collection(request, year):
    current_employee = get_current_employee(request)
    if not is_hr_employee(current_employee):
        messages.error(request, "Завершить сбор пожеланий может только HR.")
        return redirect("calendar")
    if request.method != "POST":
        return redirect(_calendar_year_redirect(year))

    planning_year = get_preference_planning_year()
    if year != planning_year:
        messages.error(request, f"Сбор пожеланий сейчас ведётся только на {planning_year} год.")
        return redirect(_calendar_year_redirect(planning_year))

    try:
        finish_preference_collection(year=year, actor=current_employee)
    except VacationPreferenceCollection.DoesNotExist:
        messages.error(request, "Сбор пожеланий за этот год не найден.")
        return redirect(_calendar_year_redirect(year))

    messages.success(request, f"Сбор пожеланий на {year} год завершён.")
    return redirect(_safe_next_url(request, _calendar_year_redirect(year)))


@employee_required
def preference_collection_readiness(request, year):
    current_employee = get_current_employee(request)
    if not (is_hr_employee(current_employee) or is_enterprise_head_employee(current_employee)):
        messages.error(request, "Готовность сбора доступна только HR и руководителю предприятия.")
        if is_authorized_person_employee(current_employee):
            return redirect("applications")
        return redirect("calendar")

    context = get_user_context(request)
    context = update_context_with_departments(request, context)
    readiness_context = build_preference_collection_readiness_context(year, request.GET)
    draft_status = get_schedule_draft_status(year)
    explicit_back_link = build_explicit_back_link(request.GET, section="schedule_planning")
    source = request.GET.get("from", "")
    is_planning_source = source == "schedule_planning" and can_access_schedule_planning(current_employee)
    current_path = request.get_full_path()
    can_manage_collection = is_hr_employee(current_employee)
    can_start_collection = (
        can_manage_collection
        and readiness_context["collection"] is None
        and year == get_preference_planning_year()
    )
    if is_planning_source:
        readiness_context["readiness_url"] = _planning_nested_url(
            readiness_context["readiness_url"],
            year,
            "collection",
        )
        for status_filter in readiness_context["status_filters"]:
            status_filter["url"] = _planning_nested_url(status_filter["url"], year, "collection")
        draft_status["url"] = _planning_nested_url(draft_status["url"], year, "draft")
    draft_status["create_next_url"] = draft_status["url"]
    context.update(readiness_context)
    context.update(
        {
            "can_manage_collection": can_manage_collection,
            "can_start_collection": can_start_collection,
            "calendar_preference_collection": build_calendar_preference_collection_context(
                current_employee,
                year,
                start_next_url=current_path,
            ),
            "draft_status": draft_status,
            "current_path": current_path,
            "readiness_subtitle": f"Ответы сотрудников на сбор пожеланий по отпуску на {year} год",
            "sidebar_section": "schedule_planning" if is_planning_source else "calendar",
            "page_header_back_link": explicit_back_link
            or (
                {
                    "url": schedule_planning_url(year, "collection"),
                    "label": "К планированию",
                }
                if is_planning_source
                else {
                    "url": _calendar_year_redirect(year),
                    "label": "К графику",
                    "use_calendar_memory": True,
                }
            ),
        }
    )
    return render(request, "vacation_preference_readiness.html", context)


@employee_required
def create_schedule_draft(request, year):
    current_employee = get_current_employee(request)
    if not is_hr_employee(current_employee):
        messages.error(request, "Создать черновик графика может только HR.")
        if is_authorized_person_employee(current_employee):
            return redirect("applications")
        return redirect("calendar")
    if request.method != "POST":
        return redirect("preference_collection_readiness", year=year)

    redirect_on_error = _safe_next_url(request, reverse("preference_collection_readiness", args=[year]))
    redirect_after_create = _safe_next_url(request, reverse("schedule_draft_detail", args=[year]))
    try:
        result = create_schedule_draft_from_preferences(year=year, actor=current_employee)
    except ValidationError as exc:
        messages.error(request, _validation_error_message(exc))
        return redirect(redirect_on_error)

    if result["created"]:
        messages.success(request, f"Черновик графика на {year} год создан. Размещено: {result['placed_count']}.")
    else:
        messages.info(request, f"Черновик графика на {year} год уже создан.")
    return redirect(redirect_after_create)


@employee_required
def auto_place_schedule_draft_remaining(request, year):
    current_employee = get_current_employee(request)
    wants_json = _request_wants_json(request)
    if not is_hr_employee(current_employee):
        message = "Добрать незакрытые дни может только HR."
        if wants_json:
            return JsonResponse({"ok": False, "message": message}, status=403)
        messages.error(request, message)
        if is_authorized_person_employee(current_employee):
            return redirect("applications")
        return redirect("calendar")
    if request.method != "POST":
        return redirect("schedule_draft_detail", year=year)

    redirect_after_action = _safe_next_url(request, reverse("schedule_draft_detail", args=[year]))
    try:
        job, created = get_or_create_schedule_auto_place_job(year=year, actor=current_employee)
        if created:
            start_schedule_auto_place_process(job)
    except ValidationError as exc:
        message = _validation_error_message(exc)
        if wants_json:
            return JsonResponse({"ok": False, "message": message}, status=400)
        messages.error(request, message)
        return redirect(redirect_after_action)
    except Exception as exc:
        message = f"Не удалось запустить действие «Добрать незакрытые дни»: {exc}"
        if wants_json:
            return JsonResponse({"ok": False, "message": message}, status=500)
        messages.error(request, message)
        return redirect(redirect_after_action)

    status_url = f"{reverse('schedule_draft_auto_place_status', args=[year, job.id])}?token={job.token}"
    payload = schedule_auto_place_job_payload(job)
    payload.update(
        {
            "token": job.token,
            "status_url": status_url,
            "message": (
                "Действие «Добрать незакрытые дни» запущено в фоне."
                if created
                else "Действие «Добрать незакрытые дни» уже выполняется."
            ),
        }
    )
    if wants_json:
        return JsonResponse(payload)

    if created:
        messages.info(request, "Действие «Добрать незакрытые дни» запущено в фоне. Обновите страницу через несколько секунд.")
    else:
        messages.info(request, "Действие «Добрать незакрытые дни» уже выполняется. Дождитесь завершения текущей задачи.")
    return redirect(redirect_after_action)


@employee_required
def auto_place_schedule_draft_status(request, year, job_id):
    current_employee = get_current_employee(request)
    if not is_hr_employee(current_employee):
        return JsonResponse(
            {"ok": False, "message": "Статус действия «Добрать незакрытые дни» может открыть только HR."},
            status=403,
        )

    token = request.GET.get("token", "")
    job = get_object_or_404(VacationScheduleAutoPlaceJob, id=job_id, year=year)
    if not token or not secrets.compare_digest(token, job.token):
        return JsonResponse({"ok": False, "message": "Некорректный токен статуса."}, status=403)

    return JsonResponse(schedule_auto_place_job_payload(job))


@employee_required
def auto_place_schedule_draft_preview(request, year):
    current_employee = get_current_employee(request)
    if request.method != "GET":
        return JsonResponse(
            {"ok": False, "message": "Предпросмотр действия «Добрать незакрытые дни» доступен только GET-запросом."},
            status=405,
        )
    if not is_hr_employee(current_employee):
        return JsonResponse(
            {"ok": False, "message": "Предпросмотр действия «Добрать незакрытые дни» может открыть только HR."},
            status=403,
        )

    try:
        preview = build_schedule_draft_auto_place_preview(year=year)
    except ValidationError as exc:
        return JsonResponse({"ok": False, "message": _validation_error_message(exc)}, status=400)

    return JsonResponse({"ok": True, **preview})


@employee_required
def schedule_draft_day_calculation(request, year, employee_id):
    current_employee = get_current_employee(request)
    if request.method != "GET":
        return JsonResponse({"ok": False, "message": "Расчёт дней доступен только GET-запросом."}, status=405)

    employee = get_object_or_404(
        Employees.objects.select_related(
            "department",
            "employee_position",
            "employee_position__production_group",
        ).exclude(role__in=Employees.SERVICE_ROLES),
        pk=employee_id,
    )
    if not _can_view_schedule_draft_employee(current_employee, employee):
        return JsonResponse({"ok": False, "message": "Нет доступа к расчёту дней этого сотрудника."}, status=403)

    try:
        calculation = build_schedule_draft_day_calculation(year=year, employee_id=employee_id)
    except ValidationError as exc:
        return JsonResponse({"ok": False, "message": _validation_error_message(exc)}, status=400)

    return JsonResponse({"ok": True, **calculation})


@employee_required
def schedule_draft_item_review(request, year, item_id):
    current_employee = get_current_employee(request)
    if request.method != "GET":
        return JsonResponse({"ok": False, "message": "Проверка пункта черновика доступна только GET-запросом."}, status=405)

    schedule_item = get_object_or_404(
        VacationScheduleItem.objects.select_related(
            "schedule",
            "employee",
            "employee__department",
            "employee__employee_position",
            "employee__employee_position__production_group",
            "generation_run",
            "selected_candidate",
            "selected_candidate__generation_run",
        ),
        pk=item_id,
        schedule__year=year,
        schedule__status=VacationSchedule.STATUS_DRAFT,
    )
    if not _can_view_schedule_draft_item(current_employee, schedule_item):
        return JsonResponse({"ok": False, "message": "Нет доступа к проверке этого пункта черновика."}, status=403)

    context = build_schedule_draft_item_review_context(schedule_item, actor=current_employee)
    context["current_path"] = reverse("schedule_draft_detail", args=[year])
    html = render_to_string("includes/schedule_draft_review_modal_content.html", context, request=request)
    return JsonResponse(
        {
            "ok": True,
            "html": html,
            "title": f"Проверка: {context['employee_name']}",
            "subtitle": f"Назначено {context['short_period_label']} · {context['chargeable_days_label']}",
        }
    )


@employee_required
def manual_schedule_draft_suggestions(request, year, employee_id):
    current_employee = get_current_employee(request)
    if request.method != "GET":
        return JsonResponse({"ok": False, "message": "Предложения доступны только GET-запросом."}, status=405)
    if not is_hr_employee(current_employee):
        return JsonResponse({"ok": False, "message": "Предложения для ручного размещения может открыть только HR."}, status=403)

    before_items_count = VacationScheduleItem.objects.count()
    try:
        limit = int(request.GET.get("limit") or 3)
        suggestions = build_schedule_draft_manual_suggestions(year=year, employee_id=employee_id, limit=limit)
    except ValidationError as exc:
        return JsonResponse({"ok": False, "message": _validation_error_message(exc)}, status=400)
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "message": "Некорректный лимит предложений."}, status=400)

    return JsonResponse(
        {
            "ok": True,
            **suggestions,
            "db_items_unchanged": before_items_count == VacationScheduleItem.objects.count(),
        }
    )


@employee_required
def manual_schedule_draft_preview(request, year, employee_id):
    current_employee = get_current_employee(request)
    if request.method != "GET":
        return JsonResponse(
            {"can_submit": False, "message": "Проверка ручного размещения доступна только GET-запросом."},
            status=405,
        )
    if not is_hr_employee(current_employee):
        return JsonResponse(
            {"can_submit": False, "message": "Вручную размещать пункты черновика может только HR."},
            status=403,
        )

    try:
        start_date = _parse_preview_date(request.GET.get("start_date"), "Дата начала")
        end_date = _parse_preview_date(request.GET.get("end_date"), "Дата окончания")
        preview = build_manual_schedule_draft_preview(
            year=year,
            employee_id=employee_id,
            start_date=start_date,
            end_date=end_date,
        )
    except ValidationError as exc:
        return JsonResponse(
            {
                "can_submit": False,
                "message": _validation_error_message(exc),
                "calendar_days": 0,
                "chargeable_days": 0,
                "merged_calendar_days": 0,
                "merged_chargeable_days": 0,
                "remaining_after_placement": 0,
                "risk_label": "Низкий",
                "risk_score": 0,
                "risk_short_reason": "",
                "risk_recommended_action": "",
                "risk_is_conflict": False,
                "will_merge": False,
                "merged_period_label": "",
                "short_gap_warning": False,
            }
        )

    return JsonResponse(
        {
            "can_submit": preview["can_submit"],
            "message": preview["message"],
            "calendar_days": preview["calendar_days"],
            "chargeable_days": _json_number(preview["chargeable_days"]),
            "merged_calendar_days": preview["merged_calendar_days"],
            "merged_chargeable_days": _json_number(preview["merged_chargeable_days"]),
            "remaining_after_placement": _json_number(preview["remaining_after_placement"]),
            "target_days": _json_number(preview["planning_need"]["target_days"]),
            "placed_days": _json_number(preview["planning_need"]["placed_days"]),
            "open_required_days": _json_number(preview["planning_need"]["open_required_days"]),
            "blocking_after_placement": _json_number(preview.get("blocking_after_placement", 0)),
            "annual_remaining_after_placement": _json_number(preview.get("annual_remaining_after_placement", 0)),
            "risk_label": preview["risk_label"],
            "risk_score": preview["risk_score"],
            "risk_short_reason": preview["risk_short_reason"],
            "risk_recommended_action": preview["risk_recommended_action"],
            "risk_is_conflict": preview["risk_is_conflict"],
            "will_merge": preview["will_merge"],
            "merged_period_label": preview["merged_period_label"],
            "short_gap_warning": preview["short_gap_warning"],
        }
    )


@employee_required
def manual_schedule_draft_package_preview(request, year, employee_id):
    current_employee = get_current_employee(request)
    if request.method != "POST":
        return JsonResponse(
            {"can_submit": False, "message": "Пакетная проверка доступна только POST-запросом."},
            status=405,
        )
    if not is_hr_employee(current_employee):
        return JsonResponse(
            {"can_submit": False, "message": "Вручную размещать пункты черновика может только HR."},
            status=403,
        )

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
        periods = _parse_manual_periods_payload(payload.get("periods"))
        preview = build_manual_schedule_draft_package_preview(
            year=year,
            employee_id=employee_id,
            periods=periods,
        )
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"can_submit": False, "message": "Не удалось разобрать запрос."}, status=400)
    except ValidationError as exc:
        return JsonResponse(
            {
                "can_submit": False,
                "message": _validation_error_message(exc),
                "calendar_days": 0,
                "chargeable_days": 0,
                "remaining_after_placement": 0,
                "risk_label": "Низкий",
                "risk_score": 0,
                "risk_level": "low",
                "risk_tone": "low",
                "risk_short_reason": "",
                "risk_recommended_action": "",
                "risk_is_conflict": False,
                "periods": [],
            },
            status=400,
        )

    return JsonResponse(_manual_package_preview_json(preview))


@employee_required
def manual_place_schedule_draft_item(request, year, employee_id):
    current_employee = get_current_employee(request)
    if not is_hr_employee(current_employee):
        messages.error(request, "Вручную размещать пункты черновика может только HR.")
        if is_authorized_person_employee(current_employee):
            return redirect("applications")
        return redirect("calendar")
    if request.method != "POST":
        return redirect("schedule_draft_detail", year=year)

    redirect_after_action = _safe_next_url(request, reverse("schedule_draft_detail", args=[year]))
    try:
        if request.POST.get("periods_json"):
            periods = _parse_manual_periods_payload(request.POST.get("periods_json"))
            place_manual_schedule_draft_items(
                year=year,
                employee_id=employee_id,
                periods=periods,
                actor=current_employee,
            )
        else:
            start_date = _parse_preview_date(request.POST.get("start_date"), "Дата начала")
            end_date = _parse_preview_date(request.POST.get("end_date"), "Дата окончания")
            place_manual_schedule_draft_item(
                year=year,
                employee_id=employee_id,
                start_date=start_date,
                end_date=end_date,
                actor=current_employee,
            )
    except ValidationError as exc:
        messages.error(request, _validation_error_message(exc))
        return redirect(redirect_after_action)

    messages.success(request, "Периоды черновика добавлены вручную.")
    return redirect(redirect_after_action)


def _parse_decimal_days(value, field_label):
    if value in (None, ""):
        raise ValidationError(f"Укажите поле «{field_label}».")
    try:
        parsed = Decimal(str(value).replace(",", "."))
    except Exception:
        raise ValidationError(f"Некорректное значение в поле «{field_label}».")
    if parsed <= 0:
        raise ValidationError(f"Поле «{field_label}» должно быть больше нуля.")
    return parsed


def _parse_urgent_closure_selected_dates(post_data):
    manual_start = post_data.get("manual_start_date")
    manual_end = post_data.get("manual_end_date")
    if manual_start or manual_end:
        return (
            _parse_preview_date(manual_start, "Дата начала"),
            _parse_preview_date(manual_end, "Дата окончания"),
        )

    selected_option = post_data.get("selected_option") or ""
    try:
        start_value, end_value = selected_option.split("|", 1)
    except ValueError:
        raise ValidationError("Выберите предложенный период или укажите даты вручную.")
    return (
        _parse_preview_date(start_value, "Дата начала"),
        _parse_preview_date(end_value, "Дата окончания"),
    )


def _empty_urgent_closure_preview_payload(message):
    return {
        "can_submit": False,
        "message": message,
        "calendar_days": 0,
        "chargeable_days": 0,
        "period_label": "",
        "risk_label": "Низкий",
        "risk_score": 0,
        "risk_short_reason": "",
        "risk_recommended_action": "",
        "risk_is_conflict": False,
    }


@employee_required
def urgent_closure_preview(request, year, employee_id):
    current_employee = get_current_employee(request)
    if request.method != "GET":
        return JsonResponse(
            _empty_urgent_closure_preview_payload("Проверка срочного остатка доступна только GET-запросом."),
            status=405,
        )
    if not is_hr_employee(current_employee):
        return JsonResponse(
            _empty_urgent_closure_preview_payload("Проверить срочный остаток может только HR."),
            status=403,
        )

    employee = get_object_or_404(Employees.objects.exclude(role__in=Employees.SERVICE_ROLES), pk=employee_id)
    try:
        required_days = _parse_decimal_days(request.GET.get("required_days"), "Списываемые дни")
        deadline = _parse_preview_date(request.GET.get("deadline"), "Срок использования")
        start_date = _parse_preview_date(request.GET.get("start_date"), "Дата начала")
        end_date = _parse_preview_date(request.GET.get("end_date"), "Дата окончания")
        preview = build_urgent_closure_preview(
            employee=employee,
            planning_year=year,
            required_days=required_days,
            deadline=deadline,
            start_date=start_date,
            end_date=end_date,
        )
    except ValidationError as exc:
        return JsonResponse(_empty_urgent_closure_preview_payload(_validation_error_message(exc)))

    return JsonResponse(
        {
            "can_submit": preview["can_submit"],
            "message": preview["message"],
            "calendar_days": preview["calendar_days"],
            "chargeable_days": _json_number(preview["chargeable_days"]),
            "period_label": preview["period_label"],
            "risk_label": preview["risk_label"],
            "risk_score": preview["risk_score"],
            "risk_short_reason": preview["risk_short_reason"],
            "risk_recommended_action": preview["risk_recommended_action"],
            "risk_is_conflict": preview["risk_is_conflict"],
        }
    )


@employee_required
def create_urgent_closure(request, year, employee_id):
    current_employee = get_current_employee(request)
    redirect_after_action = _safe_next_url(request, reverse("schedule_draft_detail", args=[year]))
    if not is_hr_employee(current_employee):
        messages.error(request, "Запустить закрытие срочного остатка может только HR.")
        if is_authorized_person_employee(current_employee):
            return redirect("applications")
        return redirect("calendar")
    if request.method != "POST":
        return redirect(redirect_after_action)

    employee = get_object_or_404(Employees.objects.exclude(role__in=Employees.SERVICE_ROLES), pk=employee_id)
    try:
        required_days = _parse_decimal_days(request.POST.get("required_days"), "Списываемые дни")
        deadline = _parse_preview_date(request.POST.get("deadline"), "Срок использования")
        start_date, end_date = _parse_urgent_closure_selected_dates(request.POST)
        closure_request = create_urgent_closure_request(
            employee=employee,
            planning_year=year,
            required_days=required_days,
            deadline=deadline,
            start_date=start_date,
            end_date=end_date,
            actor=current_employee,
            reason=request.POST.get("reason", ""),
        )
        demo_manager_approve = request.POST.get("demo_manager_approve") == "on"
        demo_employee_reply = demo_manager_approve and request.POST.get("demo_employee_reply") == "on"
        demo_employee_response = request.POST.get("demo_employee_response") or "accept"
        demo_result = apply_urgent_closure_demo_responses(
            closure_request,
            auto_manager=demo_manager_approve,
            auto_employee=demo_employee_reply,
            employee_response=demo_employee_response,
        )
        closure_request.refresh_from_db()
    except ValidationError as exc:
        error_message = _validation_error_message(exc)
        messages.error(request, error_message)
        return redirect(
            _url_with_query_params(
                redirect_after_action,
                open_modal=request.POST.get("modal_id"),
                modal_error=error_message,
            )
        )

    messages.success(request, _urgent_closure_create_success_message(demo_result))
    if demo_result.get("employee_skipped_reason"):
        messages.warning(
            request,
            f"Заявка создана, но сотрудник не ответил автоматически: {demo_result['employee_skipped_reason']}.",
        )
    draft_return_url = _relative_return_path(redirect_after_action)
    return redirect(
        _url_with_query_params(
            reverse("urgent_closure_detail", args=[closure_request.id]),
            **{
                "from": _schedule_draft_return_source(draft_return_url),
                "back_url": draft_return_url,
                "back_label": "К черновику",
            },
        )
    )


@employee_required
def schedule_draft_candidate_feedback(request, year, item_id):
    current_employee = get_current_employee(request)
    redirect_after_action = _safe_next_url(request, reverse("schedule_draft_detail", args=[year]))
    wants_json = request.headers.get("x-requested-with") == "XMLHttpRequest" or "application/json" in request.headers.get("accept", "")
    if request.method != "POST":
        if wants_json:
            return JsonResponse({"ok": False, "message": "Отзыв можно сохранить только POST-запросом."}, status=405)
        return redirect(redirect_after_action)

    schedule_item = get_object_or_404(
        VacationScheduleItem.objects.select_related(
            "schedule",
            "employee",
            "employee__department",
            "generation_run",
            "selected_candidate",
        ),
        pk=item_id,
        schedule__year=year,
    )
    try:
        submit_schedule_candidate_feedback(
            schedule_item=schedule_item,
            actor=current_employee,
            decision=request.POST.get("decision", ""),
            comment=request.POST.get("comment", ""),
        )
    except ValidationError as exc:
        error_message = _validation_error_message(exc)
        if wants_json:
            return JsonResponse({"ok": False, "message": error_message}, status=400)
        messages.error(request, error_message)
        return redirect(_url_with_fragment(redirect_after_action, f"draft-item-{schedule_item.id}"))

    if wants_json:
        feedback_context = build_schedule_candidate_feedback_context([schedule_item], actor=current_employee).get(schedule_item.id, {})
        return JsonResponse(
            {
                "ok": True,
                "message": "Отзыв по рекомендации сохранён.",
                "feedback": feedback_context,
            }
        )

    messages.success(request, "Отзыв по рекомендации сохранён.")
    return redirect(_url_with_fragment(redirect_after_action, f"draft-item-{schedule_item.id}"))


@employee_required
def schedule_draft_detail(request, year):
    current_employee = get_current_employee(request)
    if not (
        is_hr_employee(current_employee)
        or is_enterprise_head_employee(current_employee)
        or is_department_head_employee(current_employee)
    ):
        messages.error(request, "Черновик графика доступен только HR и руководителям.")
        if is_authorized_person_employee(current_employee):
            return redirect("applications")
        return redirect("calendar")

    context = get_user_context(request)
    context = update_context_with_departments(request, context)
    context.update(build_schedule_draft_page_context(year, actor=current_employee, query_params=request.GET))
    explicit_back_link = build_explicit_back_link(request.GET, section="schedule_planning")
    source = request.GET.get("from", "")
    is_planning_source = source == "schedule_planning" and can_access_schedule_planning(current_employee)
    draft_return_source = "schedule_planning" if is_planning_source else "calendar"
    if is_planning_source:
        context["readiness_url"] = _planning_nested_url(context["readiness_url"], year, "collection")
        context["draft_url"] = _planning_nested_url(context["draft_url"], year, "draft")
        context["draft_auto_place_next_url"] = context["draft_url"]
    context.update(
        {
            "can_manage_draft": is_hr_employee(current_employee),
            "current_path": request.get_full_path(),
            "draft_subtitle": f"Рабочая версия графика отпусков на {year} год",
            "draft_return_source": draft_return_source,
            "draft_back_label": "К черновику",
            "sidebar_section": "schedule_planning" if is_planning_source else "calendar",
            "page_header_back_link": explicit_back_link
            or {
                "url": _planning_nested_url(reverse("preference_collection_readiness", args=[year]), year, "collection")
                if is_planning_source
                else reverse("preference_collection_readiness", args=[year]),
                "label": "К сбору",
            },
        }
    )
    return render(request, "vacation_schedule_draft.html", context)


@employee_required
def schedule_planning_current(request):
    target_url = reverse("schedule_planning", args=[get_schedule_planning_year()])
    query = request.GET.urlencode()
    if query:
        target_url = f"{target_url}?{query}"
    return redirect(target_url)


@employee_required
def schedule_planning(request, year):
    current_employee = get_current_employee(request)
    if not can_access_schedule_planning(current_employee):
        messages.error(request, "Планирование графика доступно только участникам подготовки годового графика.")
        if is_authorized_person_employee(current_employee):
            return redirect("applications")
        return redirect("calendar")

    context = get_user_context(request)
    context = update_context_with_departments(request, context)
    context.update(build_schedule_planning_page_context(year, current_employee, request.GET))
    context.update(
        {
            "planning_subtitle": f"Подготовка графика отпусков на {year} год",
            "sidebar_section": "schedule_planning",
        }
    )
    return render(request, "vacation_schedule_planning.html", context)


@employee_required
def vacation_preferences(request, year):
    context = get_user_context(request)
    context = update_context_with_departments(request, context)
    current_employee = get_current_employee(request)
    if is_authorized_person_employee(current_employee):
        messages.error(request, "Уполномоченное лицо не заполняет пожелания по отпуску.")
        return redirect("applications")

    collection = get_object_or_404(VacationPreferenceCollection, year=year)
    if not employee_can_join_preference_collection(current_employee, year):
        messages.error(request, "Для этого года пожелания по отпуску вам недоступны.")
        return redirect("main")

    preference_context = get_employee_preference_page_context(current_employee, collection)
    primary = preference_context["primary_preference"]
    backup = preference_context["backup_preference"]
    has_saved_preferences = preference_context["preference_state"] in {
        VacationPreference.STATUS_FILLED,
        VacationPreference.STATUS_SKIPPED,
    }
    editing_requested = request.GET.get("edit") == "1" or request.POST.get("editing") == "1"
    is_editing_preferences = not has_saved_preferences or (
        preference_context["editable"] and editing_requested
    )
    initial = {
        "primary_start_date": primary.start_date if primary else None,
        "primary_end_date": primary.end_date if primary else None,
        "backup_start_date": backup.start_date if backup else None,
        "backup_end_date": backup.end_date if backup else None,
        "comment": (primary.comment if primary and primary.comment else backup.comment if backup else ""),
        "no_preferences": preference_context["preference_state"] == VacationPreference.STATUS_SKIPPED,
        "remainder_policy": preference_context["remainder_policy"],
    }

    if request.method == "POST":
        if not preference_context["editable"]:
            messages.error(request, "Сбор пожеланий закрыт, изменить ответ уже нельзя.")
            return redirect("vacation_preferences", year=year)
        if has_saved_preferences and not editing_requested:
            messages.info(request, "Пожелания уже сохранены. Чтобы изменить ответ, нажмите «Изменить».")
            return redirect("vacation_preferences", year=year)

        form = VacationPreferenceResponseForm(
            request.POST,
            employee=current_employee,
            collection=collection,
        )
        if form.is_valid():
            try:
                submit_employee_preferences(
                    collection=collection,
                    employee=current_employee,
                    primary_start=form.cleaned_data.get("primary_start_date"),
                    primary_end=form.cleaned_data.get("primary_end_date"),
                    backup_start=form.cleaned_data.get("backup_start_date"),
                    backup_end=form.cleaned_data.get("backup_end_date"),
                    comment=form.cleaned_data.get("comment", ""),
                    no_preferences=form.cleaned_data.get("no_preferences"),
                    remainder_policy=form.cleaned_data.get("remainder_policy"),
                )
            except ValidationError as exc:
                messages.error(request, _validation_error_message(exc))
                return redirect("vacation_preferences", year=year)
            messages.success(
                request,
                "Пожелания по отпуску обновлены." if has_saved_preferences else "Пожелания по отпуску сохранены.",
            )
            return redirect("vacation_preferences", year=year)
        messages.error(request, _form_errors_to_messages(form) or "Не удалось сохранить пожелания.")
    else:
        form = VacationPreferenceResponseForm(
            initial=initial,
            employee=current_employee,
            collection=collection,
        )

    if not preference_context["editable"]:
        for field in form.fields.values():
            field.disabled = True

    context.update(preference_context)
    context.update(
        {
            "form": form,
            "employee": current_employee,
            "preference_year": year,
            "has_saved_preferences": has_saved_preferences,
            "is_editing_preferences": is_editing_preferences,
            "preferences_edit_url": f"{reverse('vacation_preferences', args=[year])}?edit=1",
            "preferences_view_url": reverse("vacation_preferences", args=[year]),
            "sidebar_section": "calendar",
            "page_header_back_link": {
                "url": _calendar_year_redirect(year),
                "label": "К графику",
                "use_calendar_memory": True,
            },
        }
    )
    return render(request, "vacation_preferences.html", context)


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

    context.update(
        build_vacation_detail_context(
            vacation,
            current_employee,
            source=request.GET.get("from", ""),
            query_params=request.GET,
        )
    )
    return render(request, "vacation_detail.html", context)


@employee_required
def schedule_change_detail(request, pk):
    context = get_user_context(request)
    context = update_context_with_departments(request, context)
    try:
        change_request = get_schedule_change_requests_queryset().get(pk=pk)
    except VacationScheduleChangeRequest.DoesNotExist:
        messages.error(request, "Перенос удалён или больше недоступен.")
        return redirect("applications")
    current_employee = get_current_employee(request)
    if not can_view_employee(current_employee, change_request.employee):
        messages.error(request, "У вас нет прав для просмотра этого переноса.")
        return redirect("main")

    context.update(
        build_schedule_change_detail_context(
            change_request,
            current_employee,
            source=request.GET.get("from", ""),
            query_params=request.GET,
        )
    )
    return render(request, "schedule_change_detail.html", context)


@employee_required
def urgent_closure_detail(request, pk):
    context = get_user_context(request)
    context = update_context_with_departments(request, context)
    try:
        closure_request = get_urgent_closure_requests_queryset().get(pk=pk)
    except VacationUrgentClosureRequest.DoesNotExist:
        messages.error(request, "Согласование срочного остатка удалено или больше недоступно.")
        return redirect("applications")
    current_employee = get_current_employee(request)
    if not can_view_urgent_closure_request(current_employee, closure_request):
        messages.error(request, "У вас нет прав для просмотра этого согласования.")
        return redirect("main")

    context.update(
        build_urgent_closure_detail_context(
            closure_request,
            current_employee,
            source=request.GET.get("from", ""),
            query_params=request.GET,
        )
    )
    return render(request, "urgent_closure_detail.html", context)


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
        change_request = create_schedule_change_request(
            schedule_item_id=schedule_item.id,
            requested_by=current_employee,
            new_start_date=form.cleaned_data["new_start_date"],
            new_end_date=form.cleaned_data["new_end_date"],
            reason=form.cleaned_data.get("reason", ""),
        )
        if is_manager_initiated_schedule_change(change_request):
            messages.success(request, "Предложение переноса отправлено сотруднику.")
        else:
            messages.success(request, "Запрос переноса отправлен на согласование.")
    except ValidationError as exc:
        messages.error(request, _validation_error_message(exc))
    return redirect(redirect_url)


@employee_required
def approve_schedule_change(request, pk):
    change_request = get_object_or_404(get_schedule_change_requests_queryset(), pk=pk)
    current_employee = get_current_employee(request)

    if not can_review_schedule_change_request(current_employee, change_request):
        if is_manager_initiated_schedule_change(change_request):
            messages.error(request, "Принять или отклонить предложение переноса может только сотрудник.")
        else:
            messages.error(request, "У вас нет прав для согласования этого переноса.")
        return redirect("schedule_change_detail", pk=pk)

    if request.method == "POST":
        try:
            approve_schedule_change_request(pk, reviewer=current_employee)
            if is_manager_initiated_schedule_change(change_request):
                messages.success(request, "Предложение переноса принято.")
            else:
                messages.success(request, "Перенос отпуска согласован.")
        except ValidationError as exc:
            messages.error(request, _validation_error_message(exc))
    return redirect("schedule_change_detail", pk=pk)


@employee_required
def reject_schedule_change(request, pk):
    change_request = get_object_or_404(get_schedule_change_requests_queryset(), pk=pk)
    current_employee = get_current_employee(request)

    if not can_review_schedule_change_request(current_employee, change_request):
        if is_manager_initiated_schedule_change(change_request):
            messages.error(request, "Принять или отклонить предложение переноса может только сотрудник.")
        else:
            messages.error(request, "У вас нет прав для согласования этого переноса.")
        return redirect("schedule_change_detail", pk=pk)

    if request.method == "POST":
        try:
            reject_schedule_change_request(pk, reviewer=current_employee)
            if is_manager_initiated_schedule_change(change_request):
                messages.success(request, "Предложение переноса отклонено.")
            else:
                messages.success(request, "Перенос отпуска отклонён.")
        except ValidationError as exc:
            messages.error(request, _validation_error_message(exc))
    return redirect("schedule_change_detail", pk=pk)


@employee_required
def approve_urgent_closure_manager(request, pk):
    closure_request = get_object_or_404(get_urgent_closure_requests_queryset(), pk=pk)
    current_employee = get_current_employee(request)
    if not can_department_review_urgent_closure(current_employee, closure_request):
        messages.error(request, "Проверить период может только согласующий руководитель.")
        return redirect("urgent_closure_detail", pk=pk)

    if request.method == "POST":
        start_date = end_date = None
        try:
            if request.POST.get("manager_start_date") or request.POST.get("manager_end_date"):
                start_date = _parse_preview_date(request.POST.get("manager_start_date"), "Дата начала")
                end_date = _parse_preview_date(request.POST.get("manager_end_date"), "Дата окончания")
            approve_urgent_closure_by_manager(
                pk,
                reviewer=current_employee,
                comment=request.POST.get("comment", ""),
                start_date=start_date,
                end_date=end_date,
            )
            messages.success(request, "Период проверен и отправлен сотруднику.")
        except ValidationError as exc:
            messages.error(request, _validation_error_message(exc))
    return redirect("urgent_closure_detail", pk=pk)


@employee_required
def accept_urgent_closure_employee(request, pk):
    closure_request = get_object_or_404(get_urgent_closure_requests_queryset(), pk=pk)
    current_employee = get_current_employee(request)
    if not can_employee_review_urgent_closure(current_employee, closure_request):
        messages.error(request, "Принять период может только сотрудник, которому предложен отпуск.")
        return redirect("urgent_closure_detail", pk=pk)

    if request.method == "POST":
        try:
            accept_urgent_closure_by_employee(
                pk,
                employee=current_employee,
                comment=request.POST.get("comment", ""),
            )
            messages.success(request, "Период принят и отправлен HR на финализацию.")
        except ValidationError as exc:
            messages.error(request, _validation_error_message(exc))
    return redirect("urgent_closure_detail", pk=pk)


@employee_required
def propose_urgent_closure_employee(request, pk):
    closure_request = get_object_or_404(get_urgent_closure_requests_queryset(), pk=pk)
    current_employee = get_current_employee(request)
    if not can_employee_review_urgent_closure(current_employee, closure_request):
        messages.error(request, "Предложить другой период может только сотрудник, которому предложен отпуск.")
        return redirect("urgent_closure_detail", pk=pk)

    if request.method == "POST":
        try:
            start_date = _parse_preview_date(request.POST.get("propose_start_date"), "Дата начала")
            end_date = _parse_preview_date(request.POST.get("propose_end_date"), "Дата окончания")
            propose_urgent_closure_period_by_employee(
                pk,
                employee=current_employee,
                start_date=start_date,
                end_date=end_date,
                comment=request.POST.get("comment", ""),
            )
            messages.success(request, "Новый период отправлен руководителю на повторную проверку.")
        except ValidationError as exc:
            messages.error(request, _validation_error_message(exc))
    return redirect("urgent_closure_detail", pk=pk)


@employee_required
def finalize_urgent_closure_hr(request, pk):
    closure_request = get_object_or_404(get_urgent_closure_requests_queryset(), pk=pk)
    current_employee = get_current_employee(request)
    if not can_finalize_urgent_closure(current_employee, closure_request):
        messages.error(request, "Финализировать закрытие срочного остатка может только HR.")
        return redirect("urgent_closure_detail", pk=pk)

    if request.method == "POST":
        try:
            finalize_urgent_closure(pk, actor=current_employee, comment=request.POST.get("comment", ""))
            messages.success(request, "Срочный остаток закрыт: пункт графика создан в предыдущем году.")
        except ValidationError as exc:
            messages.error(request, _validation_error_message(exc))
    return redirect("urgent_closure_detail", pk=pk)


@employee_required
def reject_urgent_closure_request(request, pk):
    closure_request = get_object_or_404(get_urgent_closure_requests_queryset(), pk=pk)
    current_employee = get_current_employee(request)
    if not (
        can_department_review_urgent_closure(current_employee, closure_request)
        or can_employee_review_urgent_closure(current_employee, closure_request)
        or can_finalize_urgent_closure(current_employee, closure_request)
    ):
        messages.error(request, "У вас нет прав для отклонения этого согласования.")
        return redirect("urgent_closure_detail", pk=pk)

    if request.method == "POST":
        try:
            reject_urgent_closure(pk, actor=current_employee, comment=request.POST.get("comment", ""))
            messages.success(request, "Согласование срочного остатка отклонено.")
        except ValidationError as exc:
            messages.error(request, _validation_error_message(exc))
    return redirect("urgent_closure_detail", pk=pk)


@employee_required
def analytics(request):
    current_employee = get_current_employee(request)
    if not can_access_analytics(current_employee):
        messages.error(request, "Раздел аналитики доступен только руководителям.")
        return redirect("main")

    context = get_user_context(request)
    context = update_context_with_departments(request, context)
    context.update(build_analytics_page_context(current_employee, request.GET))
    return render(request, "analytics.html", context)
