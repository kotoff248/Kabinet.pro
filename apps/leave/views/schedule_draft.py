import json
import secrets
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import (
    get_object_or_404,
    redirect,
    render,
)
from django.template.loader import render_to_string
from django.urls import reverse

from apps.core.services.navigation import build_explicit_back_link
from apps.accounts.services import (
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
    VacationSchedule,
    VacationScheduleAutoPlaceJob,
    VacationScheduleItem,
)
from apps.leave.services.candidate_feedback import (
    build_schedule_candidate_feedback_context,
    submit_schedule_candidate_feedback,
)
from apps.leave.services.schedule_drafts.auto_place import create_schedule_draft_from_preferences
from apps.leave.services.schedule_drafts.manual import (
    build_manual_schedule_draft_package_preview,
    place_manual_schedule_draft_item,
    place_manual_schedule_draft_items,
)
from apps.leave.services.schedule_drafts.manual_suggestions import (
    build_schedule_draft_auto_place_preview,
    build_schedule_draft_manual_suggestions,
    build_schedule_draft_urgent_closure_options,
)
from apps.leave.services.schedule_drafts.constants import MANUAL_DRAFT_VISIBLE_PACKAGE_SUGGESTIONS
from apps.leave.services.schedule_drafts.page_context import (
    build_manual_schedule_draft_preview,
    build_schedule_draft_item_review_context,
    build_schedule_draft_page_context,
)
from apps.leave.services.schedule_drafts.planning_need import build_schedule_draft_day_calculation
from apps.leave.services.schedule_auto_place_jobs import (
    get_or_create_schedule_auto_place_job,
    schedule_auto_place_job_payload,
    start_schedule_auto_place_process,
)
from apps.leave.services.schedule_approvals import get_schedule_department_review_start_state
from apps.leave.services.schedule_planning import (
    can_access_schedule_planning,
    schedule_planning_url,
)
from apps.leave.services.planning_cycles import is_active_planning_year
from apps.leave.services.urgent_closures import (
    apply_urgent_closure_demo_responses,
    build_urgent_closure_preview,
    create_urgent_closure_request,
)
from apps.leave.views.common import (
    _validation_error_message,
    _request_wants_json,
    _urgent_closure_create_success_message,
    _json_number,
    _parse_preview_date,
    _parse_manual_periods_payload,
    _manual_package_preview_json,
    _safe_next_url,
    _relative_return_path,
    _schedule_draft_return_source,
    _url_with_query_params,
    _url_with_fragment,
    _planning_nested_url,
    _inactive_planning_year_message,
    _empty_urgent_closure_preview_payload,
    _urgent_closure_option_json,
    _urgent_closure_preview_json,
)


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
    if not is_active_planning_year(year):
        messages.error(request, _inactive_planning_year_message(year))
        return redirect(schedule_planning_url(year, "draft"))

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
    if not is_active_planning_year(year):
        message = _inactive_planning_year_message(year)
        if wants_json:
            return JsonResponse({"ok": False, "message": message}, status=400)
        messages.error(request, message)
        return redirect(schedule_planning_url(year, "draft"))

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
    if not (
        is_hr_employee(current_employee)
        or is_enterprise_head_employee(current_employee)
        or is_department_head_employee(current_employee)
    ):
        return JsonResponse(
            {"ok": False, "message": "Статус действия «Добрать незакрытые дни» доступен только HR и руководителям."},
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
        schedule__status__in=[
            VacationSchedule.STATUS_DRAFT,
            VacationSchedule.STATUS_DEPARTMENT_REVIEW,
        ],
    )
    if not _can_view_schedule_draft_item(current_employee, schedule_item):
        return JsonResponse({"ok": False, "message": "Нет доступа к проверке этого пункта черновика."}, status=403)

    context = build_schedule_draft_item_review_context(schedule_item, actor=current_employee)
    context["current_path"] = reverse("schedule_draft_detail", args=[year])
    html = render_to_string("includes/schedule_draft/schedule_draft_review_modal_content.html", context, request=request)
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
        limit = int(request.GET.get("limit") or MANUAL_DRAFT_VISIBLE_PACKAGE_SUGGESTIONS)
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


@employee_required
def urgent_closure_options(request, year, employee_id):
    current_employee = get_current_employee(request)
    if request.method != "GET":
        return JsonResponse(
            {"ok": False, "message": "Предложения срочного закрытия доступны только GET-запросом."},
            status=405,
        )
    if not is_hr_employee(current_employee):
        return JsonResponse(
            {"ok": False, "message": "Подобрать срочное закрытие может только HR."},
            status=403,
        )

    try:
        urgent_closure = build_schedule_draft_urgent_closure_options(year=year, employee_id=employee_id)
    except ValidationError as exc:
        return JsonResponse({"ok": False, "message": _validation_error_message(exc)}, status=400)

    return JsonResponse(
        {
            "ok": True,
            "required_days": _json_number(urgent_closure["required_days"]),
            "required_days_label": urgent_closure["required_days_label"],
            "deadline": urgent_closure["deadline"].isoformat(),
            "deadline_label": urgent_closure["deadline_label"],
            "options": [_urgent_closure_option_json(option) for option in urgent_closure["options"]],
        }
    )


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

    return JsonResponse(_urgent_closure_preview_json(preview))


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
    department_review_start_state = get_schedule_department_review_start_state(year, current_employee)
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
            "department_review_start_url": reverse("schedule_department_review_start", args=[year]),
            "department_review_start_next_url": schedule_planning_url(year, "review"),
            "can_start_department_review": department_review_start_state.get("can_start", False),
            "department_review_start_block_reason": department_review_start_state.get("reason", ""),
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
