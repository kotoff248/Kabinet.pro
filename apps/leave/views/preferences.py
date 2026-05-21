from datetime import date

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import (
    get_object_or_404,
    redirect,
    render,
)
from django.urls import reverse

from apps.core.services.navigation import build_explicit_back_link
from apps.accounts.services import (
    employee_required,
    get_current_employee,
    get_user_context,
    is_authorized_person_employee,
    is_enterprise_head_employee,
    is_hr_employee,
)
from apps.employees.services import update_context_with_departments
from apps.leave.models import (
    VacationPreference,
    VacationPreferenceCollection,
)
from apps.leave.forms import VacationPreferenceResponseForm
from apps.leave.ml.preference_support import build_vacation_preference_ai_comparison
from apps.leave.services.preferences import (
    employee_can_join_preference_collection,
    build_calendar_preference_collection_context,
    build_preference_collection_readiness_context,
    finish_preference_collection,
    get_employee_preference_page_context,
    get_preference_planning_year,
    start_preference_collection,
    submit_employee_preferences,
)
from apps.leave.services.schedule_drafts.utils import get_schedule_draft_status
from apps.leave.services.schedule_planning import (
    can_access_schedule_planning,
    schedule_planning_url,
)
from apps.leave.views.common import (
    _form_errors_to_messages,
    _validation_error_message,
    _calendar_year_redirect,
    _safe_next_url,
    _planning_nested_url,
)


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


def _parse_optional_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


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
def vacation_preferences_ai_preview(request, year):
    current_employee = get_current_employee(request)
    if is_authorized_person_employee(current_employee):
        return JsonResponse({"ok": False, "message": "Уполномоченное лицо не заполняет пожелания."}, status=403)
    if request.method != "GET":
        return JsonResponse({"ok": False, "message": "Оценка пожеланий доступна только GET-запросом."}, status=405)

    collection = get_object_or_404(VacationPreferenceCollection, year=year)
    if not employee_can_join_preference_collection(current_employee, year):
        return JsonResponse({"ok": False, "message": "Для этого года пожелания по отпуску недоступны."}, status=403)

    preference_context = get_employee_preference_page_context(current_employee, collection)
    if not preference_context["editable"]:
        return JsonResponse({"ok": False, "message": "Сбор пожеланий закрыт для редактирования."}, status=403)

    payload = build_vacation_preference_ai_comparison(
        current_employee,
        year,
        primary_start=_parse_optional_date(request.GET.get("primary_start_date")),
        primary_end=_parse_optional_date(request.GET.get("primary_end_date")),
        backup_start=_parse_optional_date(request.GET.get("backup_start_date")),
        backup_end=_parse_optional_date(request.GET.get("backup_end_date")),
        remainder_policy=request.GET.get("remainder_policy") or VacationPreference.REMAINDER_AUTO,
    )
    return JsonResponse(payload)


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
