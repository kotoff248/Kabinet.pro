import json

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import (
    redirect,
    render,
)
from django.urls import reverse

from apps.accounts.services import (
    employee_required,
    get_current_employee,
    get_user_context,
    is_authorized_person_employee,
    is_hr_employee,
)
from apps.employees.services import update_context_with_departments
from apps.leave.services.schedule_drafts.department_rework import (
    build_schedule_department_rework_package_preview,
    build_schedule_department_rework_suggestions,
    get_schedule_department_rework_approval,
    replace_department_rework_employee_package,
)
from apps.leave.services.schedule_drafts.constants import MANUAL_DRAFT_VISIBLE_PACKAGE_SUGGESTIONS
from apps.leave.services.schedule_drafts.page_context import build_schedule_draft_page_context
from apps.leave.services.schedule_approvals import (
    approve_schedule_enterprise_review as approve_schedule_enterprise_review_service,
    approve_schedule_department_review as approve_schedule_department_review_service,
    open_department_rework_from_enterprise_return,
    resubmit_schedule_department_review as resubmit_schedule_department_review_service,
    return_schedule_enterprise_review as return_schedule_enterprise_review_service,
    return_schedule_department_review as return_schedule_department_review_service,
    submit_schedule_for_enterprise_review,
    submit_schedule_for_department_review,
)
from apps.leave.services.schedule_planning import schedule_planning_url
from apps.leave.services.planning_cycles import is_active_planning_year
from apps.leave.views.common import (
    _validation_error_message,
    _parse_manual_periods_payload,
    _manual_package_preview_json,
    _safe_next_url,
    _inactive_planning_year_message,
)


@employee_required
def start_schedule_department_review(request, year):
    current_employee = get_current_employee(request)
    redirect_after_action = _safe_next_url(request, schedule_planning_url(year, "draft"))
    if request.method != "POST":
        return redirect(schedule_planning_url(year, "draft"))
    if not is_active_planning_year(year):
        messages.error(request, _inactive_planning_year_message(year))
        return redirect(redirect_after_action)
    if not is_hr_employee(current_employee):
        messages.error(request, "Отправить график на проверку отделов может только HR.")
        if is_authorized_person_employee(current_employee):
            return redirect("applications")
        return redirect("calendar")

    try:
        result = submit_schedule_for_department_review(year=year, actor=current_employee)
    except ValidationError as exc:
        messages.error(request, _validation_error_message(exc))
        return redirect(redirect_after_action)

    messages.success(
        request,
        (
            f"Черновик графика на {year} год отправлен руководителям отделов. "
            f"Отделов: {result['departments_count']}, пунктов графика: {result['planned_items_count']}."
        ),
    )
    return redirect(redirect_after_action)


@employee_required
def approve_schedule_department_review(request, year, approval_id):
    current_employee = get_current_employee(request)
    redirect_after_action = _safe_next_url(request, schedule_planning_url(year, "review"))
    if request.method != "POST":
        return redirect(schedule_planning_url(year, "review"))

    try:
        approve_schedule_department_review_service(
            approval_id=approval_id,
            actor=current_employee,
            comment=request.POST.get("comment", ""),
        )
    except ValidationError as exc:
        messages.error(request, _validation_error_message(exc))
        return redirect(redirect_after_action)

    messages.success(request, "График по вашему отделу согласован.")
    return redirect(redirect_after_action)


@employee_required
def return_schedule_department_review(request, year, approval_id):
    current_employee = get_current_employee(request)
    redirect_after_action = _safe_next_url(request, schedule_planning_url(year, "review"))
    if request.method != "POST":
        return redirect(schedule_planning_url(year, "review"))

    try:
        return_schedule_department_review_service(
            approval_id=approval_id,
            actor=current_employee,
            comment=request.POST.get("comment", ""),
        )
    except ValidationError as exc:
        messages.error(request, _validation_error_message(exc))
        return redirect(redirect_after_action)

    messages.success(request, "График по вашему отделу возвращён HR на доработку.")
    return redirect(redirect_after_action)


@employee_required
def schedule_department_review_rework(request, year, approval_id):
    current_employee = get_current_employee(request)
    if not is_hr_employee(current_employee):
        messages.error(request, "Доработать возвращённый отдел может только HR.")
        if is_authorized_person_employee(current_employee):
            return redirect("applications")
        return redirect("calendar")

    try:
        approval = get_schedule_department_rework_approval(year=year, approval_id=approval_id, actor=current_employee)
    except ValidationError as exc:
        messages.error(request, _validation_error_message(exc))
        return redirect(schedule_planning_url(year, "review"))

    context = get_user_context(request)
    context = update_context_with_departments(request, context)
    context.update(
        build_schedule_draft_page_context(
            year,
            actor=current_employee,
            query_params=request.GET,
            department_rework_approval=approval,
        )
    )
    context.update(
        {
            "can_manage_draft": True,
            "current_path": request.get_full_path(),
            "draft_url": reverse("schedule_department_review_rework", args=[year, approval.id]),
            "draft_subtitle": f"Доработка отдела «{approval.department.name}» по графику на {year} год",
            "draft_return_source": "schedule_planning",
            "draft_back_label": "К проверке",
            "planning_review_url": schedule_planning_url(year, "review"),
            "department_review_start_url": reverse("schedule_department_review_start", args=[year]),
            "department_review_start_next_url": schedule_planning_url(year, "review"),
            "can_start_department_review": False,
            "department_review_start_block_reason": "",
            "sidebar_section": "schedule_planning",
            "page_header_back_link": {
                "url": schedule_planning_url(year, "review"),
                "label": "К проверке",
            },
        }
    )
    return render(request, "vacation_schedule_draft.html", context)


@employee_required
def schedule_department_review_rework_suggestions(request, year, approval_id, employee_id):
    current_employee = get_current_employee(request)
    if request.method != "GET":
        return JsonResponse({"ok": False, "message": "Предложения доступны только GET-запросом."}, status=405)
    if not is_hr_employee(current_employee):
        return JsonResponse({"ok": False, "message": "Доработать возвращённый отдел может только HR."}, status=403)

    try:
        limit = int(request.GET.get("limit") or MANUAL_DRAFT_VISIBLE_PACKAGE_SUGGESTIONS)
        suggestions = build_schedule_department_rework_suggestions(
            year=year,
            approval_id=approval_id,
            employee_id=employee_id,
            actor=current_employee,
            limit=limit,
        )
    except ValidationError as exc:
        return JsonResponse({"ok": False, "message": _validation_error_message(exc)}, status=400)
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "message": "Некорректный лимит предложений."}, status=400)

    return JsonResponse({"ok": True, **suggestions})


@employee_required
def schedule_department_review_rework_package_preview(request, year, approval_id, employee_id):
    current_employee = get_current_employee(request)
    if request.method != "POST":
        return JsonResponse({"can_submit": False, "message": "Пакетная проверка доступна только POST-запросом."}, status=405)
    if not is_hr_employee(current_employee):
        return JsonResponse({"can_submit": False, "message": "Доработать возвращённый отдел может только HR."}, status=403)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
        periods = _parse_manual_periods_payload(payload.get("periods"))
        preview = build_schedule_department_rework_package_preview(
            year=year,
            approval_id=approval_id,
            employee_id=employee_id,
            periods=periods,
            actor=current_employee,
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
def schedule_department_review_rework_place(request, year, approval_id, employee_id):
    current_employee = get_current_employee(request)
    redirect_after_action = _safe_next_url(
        request,
        reverse("schedule_department_review_rework", args=[year, approval_id]),
    )
    if not is_hr_employee(current_employee):
        messages.error(request, "Доработать возвращённый отдел может только HR.")
        if is_authorized_person_employee(current_employee):
            return redirect("applications")
        return redirect("calendar")
    if request.method != "POST":
        return redirect(redirect_after_action)

    try:
        periods = _parse_manual_periods_payload(request.POST.get("periods_json"))
        result = replace_department_rework_employee_package(
            year=year,
            approval_id=approval_id,
            employee_id=employee_id,
            periods=periods,
            actor=current_employee,
        )
    except ValidationError as exc:
        messages.error(request, _validation_error_message(exc))
        return redirect(redirect_after_action)

    messages.success(
        request,
        f"Пакет сотрудника доработан: заменено {result['old_items_count']} пункт(а), добавлено {len(result['items'])}.",
    )
    return redirect(redirect_after_action)


@employee_required
def resubmit_schedule_department_review(request, year, approval_id):
    current_employee = get_current_employee(request)
    redirect_after_action = _safe_next_url(request, schedule_planning_url(year, "review"))
    if request.method != "POST":
        return redirect(schedule_planning_url(year, "review"))
    if not is_hr_employee(current_employee):
        messages.error(request, "Повторно отправить отдел на проверку может только HR.")
        if is_authorized_person_employee(current_employee):
            return redirect("applications")
        return redirect("calendar")

    try:
        approval = resubmit_schedule_department_review_service(approval_id=approval_id, actor=current_employee)
    except ValidationError as exc:
        messages.error(request, _validation_error_message(exc))
        return redirect(redirect_after_action)

    messages.success(request, f"Отдел «{approval.department.name}» повторно отправлен руководителю на проверку.")
    return redirect(redirect_after_action)


@employee_required
def submit_schedule_final_review(request, year):
    current_employee = get_current_employee(request)
    redirect_after_action = _safe_next_url(request, schedule_planning_url(year, "final"))
    if request.method != "POST":
        return redirect(schedule_planning_url(year, "final"))
    if not is_active_planning_year(year):
        messages.error(request, _inactive_planning_year_message(year))
        return redirect(redirect_after_action)
    if not is_hr_employee(current_employee):
        messages.error(request, "Отправить график на финальное утверждение может только HR.")
        if is_authorized_person_employee(current_employee):
            return redirect("applications")
        return redirect("calendar")

    try:
        approval = submit_schedule_for_enterprise_review(year=year, actor=current_employee)
    except ValidationError as exc:
        messages.error(request, _validation_error_message(exc))
        return redirect(redirect_after_action)

    reviewer_name = approval.enterprise_head.full_name if approval.enterprise_head else "руководителю предприятия"
    messages.success(request, f"График отправлен на финальное утверждение: {reviewer_name}.")
    return redirect(redirect_after_action)


@employee_required
def approve_schedule_final_review(request, year, approval_id):
    current_employee = get_current_employee(request)
    redirect_after_action = _safe_next_url(request, schedule_planning_url(year, "final"))
    if request.method != "POST":
        return redirect(schedule_planning_url(year, "final"))

    try:
        approve_schedule_enterprise_review_service(
            approval_id=approval_id,
            actor=current_employee,
            comment=request.POST.get("comment", ""),
        )
    except ValidationError as exc:
        messages.error(request, _validation_error_message(exc))
        return redirect(redirect_after_action)

    messages.success(request, "График отпусков финально утверждён.")
    return redirect(redirect_after_action)


@employee_required
def return_schedule_final_review(request, year, approval_id):
    current_employee = get_current_employee(request)
    redirect_after_action = _safe_next_url(request, schedule_planning_url(year, "final"))
    if request.method != "POST":
        return redirect(schedule_planning_url(year, "final"))

    try:
        return_schedule_enterprise_review_service(
            approval_id=approval_id,
            actor=current_employee,
            comment=request.POST.get("comment", ""),
        )
    except ValidationError as exc:
        messages.error(request, _validation_error_message(exc))
        return redirect(redirect_after_action)

    messages.success(request, "График возвращён HR на доработку.")
    return redirect(redirect_after_action)


@employee_required
def open_schedule_final_department_rework(request, year, approval_id, department_id):
    current_employee = get_current_employee(request)
    redirect_after_action = _safe_next_url(request, schedule_planning_url(year, "final"))
    if request.method != "POST":
        return redirect(schedule_planning_url(year, "final"))
    if not is_hr_employee(current_employee):
        messages.error(request, "Открыть доработку отдела может только HR.")
        if is_authorized_person_employee(current_employee):
            return redirect("applications")
        return redirect("calendar")

    try:
        department_approval = open_department_rework_from_enterprise_return(
            approval_id=approval_id,
            department_id=department_id,
            actor=current_employee,
        )
    except ValidationError as exc:
        messages.error(request, _validation_error_message(exc))
        return redirect(redirect_after_action)

    messages.success(request, f"Отдел «{department_approval.department.name}» открыт для доработки.")
    return redirect(reverse("schedule_department_review_rework", args=[year, department_approval.id]))
