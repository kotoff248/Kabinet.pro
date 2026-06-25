from collections import Counter
from datetime import timedelta
import secrets

from django.contrib import messages
from django.conf import settings
from django.contrib.auth import logout as auth_logout, update_session_auth_hash
from django.db import transaction
from django.db.models import Prefetch
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from apps.accounts.services import (
    can_access_departments_page,
    can_access_staffing_page,
    can_delete_employee,
    can_edit_employee_data,
    can_edit_staffing_rules,
    can_view_employee,
    employee_required,
    get_accessible_departments,
    get_current_employee,
    get_user_context,
    is_enterprise_head_employee,
    is_authorized_person_employee,
    is_hr_employee,
)
from apps.core.services.demo_baseline import (
    DemoBaselineMissingError,
    DemoBaselineResetInProgressError,
    reset_demo_to_baseline,
)
from apps.core.models import DemoDataResetJob
from apps.core.services.demo_reset_jobs import (
    DemoDataResetInProgressError,
    demo_data_reset_preset_options,
    demo_data_reset_job_payload,
    get_or_create_demo_data_reset_job,
    normalize_demo_data_reset_preset,
    start_demo_data_reset_process,
)
from apps.employees.models import (
    DepartmentCoverageRule,
    Departments,
    EmployeePosition,
    Employees,
    ProductionGroup,
    ProductionGroupSubstitutionRule,
)
from apps.leave.models import DepartmentStaffingRule, DepartmentWorkload
from apps.leave.services.constants import RUSSIAN_MONTH_SHORT_NAMES
from apps.leave.services.preferences import attach_employee_to_open_preference_collections
from apps.leave.services.staffing import (
    build_department_group_staffing_forecast_map,
    build_department_staffing_context_map,
    build_department_staffing_forecast_map,
    format_staff_count,
)

from .forms import DepartmentCreateForm, EmployeeCreateForm, EmployeeUpdateForm
from .page_contexts import (
    build_department_detail_page_context,
    build_departments_page_context,
    build_departments_queryset,
    build_employee_profile_context,
    build_employees_page_context,
    build_main_profile_context,
    serialize_departments_queryset,
)
from .services import archive_employee, update_context_with_departments
from .tenure import build_new_hire_badge


STAFFING_WORKLOAD_ACTIONS = {"save_workload_year", "fill_workload_year"}
STAFFING_WORKLOAD_LEVEL_LABELS = {
    1: "Низкая",
    2: "Спокойная",
    3: "Средняя",
    4: "Высокая",
    5: "Пиковая",
}
STAFFING_WORKLOAD_LEVEL_OPTIONS = [
    {"value": value, "label": f"{value} / {label}"}
    for value, label in STAFFING_WORKLOAD_LEVEL_LABELS.items()
]
STAFFING_QUALITY_LEVELS = {
    "ok": {"label": "Стабильно", "icon": "verified"},
    "info": {"label": "Проверьте", "icon": "rule"},
    "medium": {"label": "На минимуме", "icon": "balance"},
    "high": {"label": "Нужен резерв", "icon": "support_agent"},
    "conflict": {"label": "Слишком жёстко", "icon": "warning"},
}
STAFFING_QUALITY_PRIORITY = {
    "ok": 0,
    "info": 1,
    "medium": 2,
    "high": 3,
    "conflict": 4,
}
DEMO_SEED_MAX_VALUE = 999_999


def _form_errors_to_messages(form):
    errors = []
    for field_errors in form.errors.values():
        errors.extend(field_errors)
    return " ".join(str(error) for error in errors)


def _normalize_employee_form_data(post_data):
    data = post_data.copy()
    field_map = {
        "employee_last_name": "last_name",
        "employee_first_name": "first_name",
        "employee_middle_name": "middle_name",
        "employee_position_id": "employee_position",
        "employee_date_joined": "date_joined",
        "employee_vacation_days": "annual_paid_leave_days",
        "employee_annual_paid_leave_days": "annual_paid_leave_days",
        "vacation_days": "annual_paid_leave_days",
        "employee_department": "department",
        "employee_role": "role",
    }
    for old_name, new_name in field_map.items():
        if old_name in data and new_name not in data:
            data[new_name] = data.get(old_name)
    return data


def _get_employee_redirect_response(request, employee_id):
    next_path = request.POST.get("next_path")
    if next_path and url_has_allowed_host_and_scheme(
        next_path,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_path)
    return redirect("employee_profile", employee_id=employee_id)


@employee_required
def main(request):
    context = get_user_context(request)
    context = update_context_with_departments(request, context)
    employee = get_current_employee(request)
    if is_authorized_person_employee(employee):
        return redirect("applications")

    context.update(build_main_profile_context(employee))
    return render(request, "main.html", context)


@employee_required
def employee_profile(request, employee_id):
    context = get_user_context(request)
    context = update_context_with_departments(request, context)
    current_employee = get_current_employee(request)
    if is_authorized_person_employee(current_employee):
        return redirect("applications")
    employee = get_object_or_404(
        Employees.objects.select_related(
            "department",
            "managed_department",
            "employee_position",
            "employee_position__production_group",
        ).exclude(role__in=Employees.SERVICE_ROLES),
        id=employee_id,
    )

    if not can_view_employee(current_employee, employee):
        messages.error(request, "У вас нет прав для просмотра этого профиля.")
        return redirect("main")

    context.update(
        build_employee_profile_context(
            current_employee,
            employee,
            source=request.GET.get("from", ""),
            return_to=request.GET.get("return_to", ""),
            vacation_id=request.GET.get("vacation_id", ""),
            transfer_id=request.GET.get("transfer_id", ""),
            query_params=request.GET,
        )
    )
    return render(request, "employee_profile.html", context)


@employee_required
def update_employee(request, employee_id):
    current_employee = get_current_employee(request)
    if not can_edit_employee_data(current_employee):
        messages.error(request, "Только HR может редактировать карточки сотрудников.")
        return redirect("main")

    employee = get_object_or_404(Employees.objects.exclude(role__in=Employees.SERVICE_ROLES), id=employee_id)
    if not employee.is_active_employee:
        messages.error(request, "Архивного сотрудника нельзя редактировать.")
        return redirect("employee_profile", employee_id=employee_id)

    if request.method != "POST":
        return redirect("employee_profile", employee_id=employee_id)

    redirect_response = _get_employee_redirect_response(request, employee_id)
    form = EmployeeUpdateForm(_normalize_employee_form_data(request.POST), instance=employee)
    if not form.is_valid():
        messages.error(request, _form_errors_to_messages(form) or "Не удалось обновить данные сотрудника.")
        return redirect_response

    updated_employee = form.save()
    if form.cleaned_data.get("password") and request.user.pk == updated_employee.user_id and updated_employee.user is not None:
        update_session_auth_hash(request, updated_employee.user)

    messages.success(request, "Данные сотрудника обновлены.")
    return redirect_response


@employee_required
def delete_employee(request, employee_id):
    current_employee = get_current_employee(request)
    employee = get_object_or_404(
        Employees.objects.select_related("user").exclude(role__in=Employees.SERVICE_ROLES),
        id=employee_id,
    )

    if request.method != "POST":
        return redirect("employee_profile", employee_id=employee_id)

    if not can_delete_employee(current_employee, employee):
        messages.error(request, "У вас нет прав для удаления этого сотрудника.")
        return redirect("employee_profile", employee_id=employee_id)

    archive_employee(employee)
    messages.success(request, "Сотрудник удалён из активного состава.")
    return redirect("employees")


@employee_required
def employees(request):
    context = get_user_context(request)
    context = update_context_with_departments(request, context)
    current_employee = get_current_employee(request)
    if is_authorized_person_employee(current_employee):
        messages.error(request, "У вас нет прав для доступа к списку сотрудников.")
        return redirect("applications")
    can_edit = can_edit_employee_data(current_employee)

    if request.method == "POST" and not can_edit:
        messages.error(request, "Только HR может добавлять сотрудников.")
        return redirect("employees")

    if request.method == "POST" and can_edit and ("last_name" in request.POST or "employee_last_name" in request.POST):
        form = EmployeeCreateForm(_normalize_employee_form_data(request.POST))
        if form.is_valid():
            employee = form.save()
            attached_years = attach_employee_to_open_preference_collections(employee, actor=current_employee)
            if attached_years:
                years_label = ", ".join(str(year) for year in attached_years)
                messages.success(request, f"Сотрудник создан и подключён к сбору пожеланий на {years_label} год.")
            else:
                messages.success(request, "Сотрудник создан.")
        else:
            messages.error(request, _form_errors_to_messages(form) or "Не удалось создать сотрудника.")
        return redirect("employees")

    employees_context = build_employees_page_context(current_employee, request.GET, request.session)

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"employees": employees_context["employees"]})

    employees_context.update(
        {
            "can_manage_employees": can_edit,
            "show_manager_fields": can_edit,
        }
    )
    context.update(employees_context)
    return render(request, "employees.html", context)


@employee_required
def departments(request):
    context = get_user_context(request)
    context = update_context_with_departments(request, context)
    current_employee = get_current_employee(request)
    can_create_department = is_hr_employee(current_employee)

    if not can_access_departments_page(current_employee):
        messages.error(request, "У вас нет прав для доступа к разделу отделов.")
        return redirect("main")

    department_modal_open = False
    if request.method == "POST":
        if not can_create_department:
            messages.error(request, "Только HR может создавать отделы.")
            return redirect("departments")

        department_create_form = DepartmentCreateForm(request.POST)
        if department_create_form.is_valid():
            department_create_form.save()
            messages.success(request, "Отдел создан.")
            return redirect("departments")

        department_modal_open = True
        messages.error(request, _form_errors_to_messages(department_create_form) or "Не удалось создать отдел.")
    else:
        department_create_form = DepartmentCreateForm()

    departments_qs = build_departments_queryset(current_employee)

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({"departments": serialize_departments_queryset(departments_qs)})

    context.update(
        build_departments_page_context(
            departments_qs,
            department_create_form,
            department_modal_open,
            can_create_department,
        )
    )
    return render(request, "departments.html", context)


@employee_required
def department_detail(request, department_id):
    context = get_user_context(request)
    context = update_context_with_departments(request, context)
    current_employee = get_current_employee(request)

    if not can_access_departments_page(current_employee):
        messages.error(request, "У вас нет прав для доступа к разделу отделов.")
        return redirect("main")

    department = build_departments_queryset(current_employee).filter(id=department_id).first()
    if department is None:
        messages.error(request, "Отдел недоступен или не найден.")
        return redirect("departments")

    context.update(
        build_department_detail_page_context(
            current_employee,
            department,
            request.GET,
            request.session,
        )
    )
    return render(request, "department_detail.html", context)


def _get_staffing_departments(current_employee):
    return get_accessible_departments(current_employee).select_related(
        "head",
        "deputy",
        "staffing_rule",
    ).prefetch_related(
        "production_groups__positions",
        Prefetch(
            "employee_positions",
            queryset=EmployeePosition.objects.select_related("production_group").order_by(
                "production_group__name",
                "title",
            ),
        ),
        "coverage_rules__production_group",
        "substitution_rules__source_group",
        "substitution_rules__substitute_group",
        "employees",
    )


def _staffing_department_or_none(current_employee, department_id):
    try:
        department_id = int(department_id)
    except (TypeError, ValueError):
        return None
    return _get_staffing_departments(current_employee).filter(id=department_id).first()


def _staffing_employee_or_none(department, employee_id):
    if not employee_id:
        return None
    try:
        employee_id = int(employee_id)
    except (TypeError, ValueError):
        return None
    return Employees.objects.filter(
        id=employee_id,
        department=department,
        is_active_employee=True,
    ).exclude(role__in=Employees.SERVICE_ROLES).first()


def _staffing_group_or_none(department, group_id):
    try:
        group_id = int(group_id)
    except (TypeError, ValueError):
        return None
    return ProductionGroup.objects.filter(id=group_id, department=department).first()


def _staffing_position_or_none(department, position_id):
    try:
        position_id = int(position_id)
    except (TypeError, ValueError):
        return None
    return EmployeePosition.objects.filter(id=position_id, department=department).first()


def _staffing_coverage_or_none(department, coverage_id):
    try:
        coverage_id = int(coverage_id)
    except (TypeError, ValueError):
        return None
    return DepartmentCoverageRule.objects.filter(id=coverage_id, department=department).first()


def _staffing_substitution_or_none(department, substitution_id):
    try:
        substitution_id = int(substitution_id)
    except (TypeError, ValueError):
        return None
    return ProductionGroupSubstitutionRule.objects.filter(id=substitution_id, department=department).first()


def _normalize_staffing_text(value):
    return " ".join((value or "").split())


def _positive_small_int(value, default=1, minimum=1):
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _bounded_int(value, default, minimum, maximum=None):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _selected_staffing_workload_year(request):
    current_year = timezone.localdate().year
    raw_year = request.POST.get("year") or request.GET.get("year") or current_year
    return _bounded_int(raw_year, current_year, 2000, 2100)


def _staffing_rules_redirect_url(request, selected_year):
    if request.POST.get("action") in STAFFING_WORKLOAD_ACTIONS:
        return f"{reverse('staffing_rules')}?year={selected_year}"
    return reverse("staffing_rules")


def _demo_data_tools_enabled():
    return bool(getattr(settings, "DEMO_DATA_TOOLS_ENABLED", settings.DEBUG))


def _can_reset_demo_data(employee):
    return bool(
        _demo_data_tools_enabled()
        and (is_enterprise_head_employee(employee) or is_hr_employee(employee))
    )


def _staffing_rule_or_none(department):
    try:
        return department.staffing_rule
    except DepartmentStaffingRule.DoesNotExist:
        return None


def _staffing_workload_base_limits(department):
    staffing_rule = _staffing_rule_or_none(department)
    return {
        "min_staff_required": staffing_rule.min_staff_required if staffing_rule else 0,
        "max_absent": staffing_rule.max_absent if staffing_rule else 1,
    }


def _staffing_workload_year_options(selected_year):
    current_year = timezone.localdate().year
    years = set(DepartmentWorkload.objects.values_list("year", flat=True))
    years.update(range(current_year - 1, current_year + 4))
    years.add(selected_year)
    return sorted(years)


def _staffing_workload_fallbacks(departments, selected_year):
    fallback_by_department_month = {}
    for workload in DepartmentWorkload.objects.filter(department__in=departments).exclude(year=selected_year):
        key = (workload.department_id, workload.month)
        current = fallback_by_department_month.get(key)
        current_distance = abs(current.year - selected_year) if current is not None else None
        workload_distance = abs(workload.year - selected_year)
        if current is None or (workload_distance, workload.year > selected_year) < (
            current_distance,
            current.year > selected_year,
        ):
            fallback_by_department_month[key] = workload
    return fallback_by_department_month


def _decorate_departments_with_workload(departments, selected_year):
    workload_by_department_month = {
        (workload.department_id, workload.month): workload
        for workload in DepartmentWorkload.objects.filter(
            department__in=departments,
            year=selected_year,
        )
    }
    fallback_by_department_month = _staffing_workload_fallbacks(departments, selected_year)
    for department in departments:
        base_limits = _staffing_workload_base_limits(department)
        workload_months = []
        for month_number, month_short in enumerate(RUSSIAN_MONTH_SHORT_NAMES, start=1):
            workload = workload_by_department_month.get((department.id, month_number))
            fallback_workload = None if workload else fallback_by_department_month.get((department.id, month_number))
            source_workload = workload or fallback_workload
            load_level = source_workload.load_level if source_workload else 1
            min_staff_required = (
                source_workload.min_staff_required if source_workload else base_limits["min_staff_required"]
            )
            max_absent = source_workload.max_absent if source_workload else base_limits["max_absent"]
            workload_months.append(
                {
                    "month": month_number,
                    "month_short": month_short,
                    "load_level": load_level,
                    "load_label": STAFFING_WORKLOAD_LEVEL_LABELS.get(load_level, "Нагрузка"),
                    "min_staff_required": min_staff_required,
                    "max_absent": max_absent,
                    "is_configured": workload is not None,
                    "is_inherited": fallback_workload is not None,
                }
            )
        department.staffing_workload_months = workload_months


def _staffing_quality(level, label=None, icon=None, hint=""):
    meta = STAFFING_QUALITY_LEVELS.get(level, STAFFING_QUALITY_LEVELS["ok"])
    return {
        "level": level if level in STAFFING_QUALITY_LEVELS else "ok",
        "label": label or meta["label"],
        "icon": icon or meta["icon"],
        "hint": hint,
    }


def _format_staffing_reserve(value):
    value = int(value or 0)
    if value < 0:
        return f"дефицит {format_staff_count(abs(value))}"
    if value == 0:
        return "нет резерва"
    return format_staff_count(value)


def _quality_for_staffing_rule(staff_count, min_staff_required, max_absent, *, has_rule=True):
    staff_count = int(staff_count or 0)
    min_staff_required = int(min_staff_required or 0)
    max_absent = int(max_absent or 0)
    reserve_count = staff_count - min_staff_required

    if not has_rule:
        return _staffing_quality(
            "info",
            "Правило не задано",
            "rule_folder",
            "Для группы не задан минимум состава и лимит отсутствующих.",
        )
    if staff_count <= 0:
        return _staffing_quality(
            "high",
            "Нет сотрудников",
            "groups",
            "В группе пока нет активных сотрудников, правило нельзя проверить на практике.",
        )
    if min_staff_required > staff_count:
        return _staffing_quality(
            "conflict",
            "Слишком жёстко",
            "warning",
            "Минимум больше текущего состава группы.",
        )
    if reserve_count <= 0:
        return _staffing_quality(
            "medium",
            "Нет резерва",
            "balance",
            "Минимум равен текущему составу, любой отпуск выводит группу на границу.",
        )
    if max_absent > reserve_count:
        return _staffing_quality(
            "info",
            "Проверьте лимит",
            "rule",
            "Лимит отсутствующих выше резерва по минимальному составу.",
        )
    return _staffing_quality(
        "ok",
        f"Резерв {format_staff_count(reserve_count)}",
        "verified",
        "Правило согласуется с текущим составом группы.",
    )


def _merge_quality_with_forecast(quality, forecast):
    forecast_level = forecast.get("level", "ok")
    if STAFFING_QUALITY_PRIORITY.get(forecast_level, 0) <= STAFFING_QUALITY_PRIORITY.get(quality["level"], 0):
        return quality
    return _staffing_quality(
        forecast_level,
        forecast.get("label") or quality["label"],
        forecast.get("icon") or quality["icon"],
        forecast.get("primary_reason") or forecast.get("summary") or quality["hint"],
    )


def _decorate_staffing_quality(departments, active_employees):
    today = timezone.localdate()
    staff_count_by_department = Counter(employee.department_id for employee in active_employees)
    staff_count_by_group = Counter(
        employee.employee_position.production_group_id
        for employee in active_employees
        if employee.employee_position_id
        and employee.employee_position
        and employee.employee_position.production_group_id
    )
    staffing_contexts = build_department_staffing_context_map(departments, today + timedelta(days=29))
    department_forecasts = build_department_staffing_forecast_map(
        departments,
        start_date=today,
        staffing_contexts=staffing_contexts,
    )

    for department in departments:
        staff_count = staff_count_by_department[department.id]
        department_forecast = department_forecasts.get(department.id, {})
        department.staffing_quality_level = department_forecast.get("level", "ok")
        department.staffing_quality_label = department_forecast.get("label", STAFFING_QUALITY_LEVELS["ok"]["label"])
        department.staffing_quality_icon = department_forecast.get("icon", STAFFING_QUALITY_LEVELS["ok"]["icon"])
        department.staffing_quality_reason = department_forecast.get(
            "primary_reason",
            "30 дней · критичных рисков нет",
        )
        department.staffing_quality_facts = [
            {
                "icon": "groups",
                "label": "Состав",
                "value": format_staff_count(staff_count),
                "hint": "Активные сотрудники отдела без сервисных ролей. Это база для оценки покрытия и рисков.",
            },
            {
                "icon": "shield",
                "label": "Резерв",
                "value": department_forecast.get("min_reserve_label", _format_staffing_reserve(staff_count)),
                "hint": "Минимальный запас людей сверх обязательного минимума по прогнозу на ближайшие 30 дней.",
            },
            {
                "icon": "beach_access",
                "label": "Пик отсутствий",
                "value": department_forecast.get("peak_absent_label", format_staff_count(0)),
                "hint": "Максимум сотрудников отдела, которые одновременно отсутствуют в ближайшие 30 дней.",
            },
        ]

        groups = list(department.production_groups.all())
        group_forecasts = build_department_group_staffing_forecast_map(
            department,
            groups=groups,
            start_date=today,
            staffing_context=staffing_contexts.get(department.id),
        )
        coverage_rules = list(department.coverage_rules.all())
        coverage_by_group = {rule.production_group_id: rule for rule in coverage_rules}

        for group in groups:
            group_staff_count = staff_count_by_group[group.id]
            group_positions = list(group.positions.all())
            coverage_rule = coverage_by_group.get(group.id)
            forecast = group_forecasts.get(group.id, {})
            base_quality = _quality_for_staffing_rule(
                group_staff_count,
                coverage_rule.min_staff_required if coverage_rule else 0,
                coverage_rule.max_absent if coverage_rule else 0,
                has_rule=coverage_rule is not None,
            )
            group_quality = _merge_quality_with_forecast(base_quality, forecast)
            group.staffing_employee_count = group_staff_count
            group.staffing_employee_count_label = format_staff_count(group_staff_count)
            group.staffing_position_count = len(group_positions)
            group.staffing_reserve_label = forecast.get(
                "min_reserve_label",
                _format_staffing_reserve(group_staff_count - (coverage_rule.min_staff_required if coverage_rule else 0)),
            )
            group.staffing_rule_label = "правило задано" if coverage_rule else "правило не задано"
            group.staffing_quality_level = group_quality["level"]
            group.staffing_quality_label = group_quality["label"]
            group.staffing_quality_icon = group_quality["icon"]
            group.staffing_quality_hint = group_quality["hint"]

        for rule in coverage_rules:
            group_staff_count = staff_count_by_group[rule.production_group_id]
            reserve_count = group_staff_count - int(rule.min_staff_required or 0)
            quality = _quality_for_staffing_rule(
                group_staff_count,
                rule.min_staff_required,
                rule.max_absent,
                has_rule=True,
            )
            rule.staffing_employee_count_label = format_staff_count(group_staff_count)
            rule.staffing_reserve_label = _format_staffing_reserve(reserve_count)
            rule.staffing_quality_level = quality["level"]
            rule.staffing_quality_label = quality["label"]
            rule.staffing_quality_icon = quality["icon"]
            rule.staffing_quality_hint = quality["hint"]

        for rule in department.substitution_rules.all():
            source_count = staff_count_by_group[rule.source_group_id]
            substitute_count = staff_count_by_group[rule.substitute_group_id]
            substitute_coverage = coverage_by_group.get(rule.substitute_group_id)
            substitute_minimum = substitute_coverage.min_staff_required if substitute_coverage else 0
            substitute_reserve = substitute_count - int(substitute_minimum or 0)
            real_capacity = max(0, min(int(rule.max_covered_absences or 0), substitute_reserve))
            if rule.source_group_id == rule.substitute_group_id:
                quality = _staffing_quality(
                    "conflict",
                    "Проверьте связку",
                    "sync_problem",
                    "Группа не должна замещать сама себя.",
                )
            elif substitute_count <= 0:
                quality = _staffing_quality(
                    "high",
                    "Нет замещающих",
                    "groups",
                    "В замещающей группе нет активных сотрудников.",
                )
            elif substitute_reserve <= 0:
                quality = _staffing_quality(
                    "high",
                    "Нет резерва",
                    "support_agent",
                    "Замещающая группа сама стоит на минимуме.",
                )
            elif real_capacity < int(rule.max_covered_absences or 0):
                quality = _staffing_quality(
                    "medium",
                    "Частично покрывает",
                    "balance",
                    "Фактический резерв меньше указанной ёмкости замещения.",
                )
            else:
                quality = _staffing_quality(
                    "ok",
                    "Покрывает",
                    "verified",
                    "У замещающей группы есть резерв под это правило.",
                )
            rule.staffing_source_count_label = format_staff_count(source_count)
            rule.staffing_substitute_count_label = format_staff_count(substitute_count)
            rule.staffing_substitute_reserve_label = _format_staffing_reserve(substitute_reserve)
            rule.staffing_real_capacity_label = format_staff_count(real_capacity)
            rule.staffing_quality_level = quality["level"]
            rule.staffing_quality_label = quality["label"]
            rule.staffing_quality_icon = quality["icon"]
            rule.staffing_quality_hint = quality["hint"]

        for month in getattr(department, "staffing_workload_months", []):
            quality = _quality_for_staffing_rule(
                staff_count,
                month["min_staff_required"],
                month["max_absent"],
                has_rule=True,
            )
            month["quality_level"] = quality["level"]
            month["quality_label"] = quality["label"]
            month["quality_icon"] = quality["icon"]
            month["quality_hint"] = quality["hint"]


@transaction.atomic
def _delete_staffing_department(department):
    Employees.objects.filter(department=department).update(department=None, employee_position=None)
    Employees.objects.filter(employee_position__department=department).update(employee_position=None)
    EmployeePosition.objects.filter(department=department).delete()
    department.delete()


def _coverage_values_from_post(request):
    try:
        return {
            "min_staff_required": max(0, int(request.POST.get("min_staff_required", 1))),
            "max_absent": max(0, int(request.POST.get("max_absent", 1))),
            "criticality_level": min(5, max(1, int(request.POST.get("criticality_level", 3)))),
        }
    except (TypeError, ValueError):
        return None


def _handle_staffing_post(request, current_employee):
    action = request.POST.get("action")
    department = _staffing_department_or_none(current_employee, request.POST.get("department_id"))
    if department is None:
        messages.error(request, "Выберите доступный отдел.")
        return

    if action == "delete_department":
        department_name = department.name
        _delete_staffing_department(department)
        messages.success(request, f'Отдел "{department_name}" удалён.')
        return

    if action == "update_deputy":
        department.deputy = _staffing_employee_or_none(department, request.POST.get("deputy_id"))
        department.save(update_fields=["deputy"])
        messages.success(request, "Заместитель руководителя отдела обновлён.")
        return

    if action == "create_group":
        name = _normalize_staffing_text(request.POST.get("name"))
        code = _normalize_staffing_text(request.POST.get("code"))
        if not name:
            messages.error(request, "Введите название производственной группы.")
            return
        if ProductionGroup.objects.filter(department=department, name=name).exists():
            messages.error(request, "Производственная группа с таким названием уже есть в отделе.")
            return
        ProductionGroup.objects.create(department=department, name=name, code=code)
        messages.success(request, "Производственная группа добавлена.")
        return

    if action == "update_group":
        group = _staffing_group_or_none(department, request.POST.get("group_id"))
        name = _normalize_staffing_text(request.POST.get("name"))
        code = _normalize_staffing_text(request.POST.get("code"))
        if group is None:
            messages.error(request, "Выберите производственную группу.")
            return
        if not name:
            messages.error(request, "Введите название производственной группы.")
            return
        if ProductionGroup.objects.filter(department=department, name=name).exclude(id=group.id).exists():
            messages.error(request, "Производственная группа с таким названием уже есть в отделе.")
            return
        group.name = name
        group.code = code
        group.save(update_fields=["name", "code"])
        messages.success(request, "Производственная группа обновлена.")
        return

    if action == "delete_group":
        group = _staffing_group_or_none(department, request.POST.get("group_id"))
        if group is None:
            messages.error(request, "Выберите производственную группу.")
            return
        if (
            group.positions.exists()
            or group.coverage_rules.exists()
            or group.substitution_sources.exists()
            or group.substitution_targets.exists()
        ):
            messages.error(request, "Нельзя удалить группу, пока она используется в должностях, покрытии или замещении.")
            return
        group_name = group.name
        group.delete()
        messages.success(request, f'Производственная группа "{group_name}" удалена.')
        return

    if action == "create_position":
        title = _normalize_staffing_text(request.POST.get("title"))
        production_group = _staffing_group_or_none(department, request.POST.get("production_group_id"))
        if not title or production_group is None:
            messages.error(request, "Выберите группу и введите должность.")
            return
        if EmployeePosition.objects.filter(department=department, title=title).exists():
            messages.error(request, "Должность с таким названием уже есть в отделе.")
            return
        EmployeePosition.objects.create(department=department, title=title, production_group=production_group)
        messages.success(request, "Должность добавлена в справочник.")
        return

    if action == "update_position":
        position = _staffing_position_or_none(department, request.POST.get("position_id"))
        title = _normalize_staffing_text(request.POST.get("title"))
        production_group = _staffing_group_or_none(department, request.POST.get("production_group_id"))
        if position is None:
            messages.error(request, "Выберите должность.")
            return
        if not title or production_group is None:
            messages.error(request, "Выберите группу и введите должность.")
            return
        if EmployeePosition.objects.filter(department=department, title=title).exclude(id=position.id).exists():
            messages.error(request, "Должность с таким названием уже есть в отделе.")
            return
        position.title = title
        position.production_group = production_group
        position.save(update_fields=["title", "production_group"])
        messages.success(request, "Должность обновлена.")
        return

    if action == "delete_position":
        position = _staffing_position_or_none(department, request.POST.get("position_id"))
        if position is None:
            messages.error(request, "Выберите должность.")
            return
        if position.employees.exists():
            messages.error(request, "Нельзя удалить должность, пока к ней привязаны сотрудники.")
            return
        position_title = position.title
        position.delete()
        messages.success(request, f'Должность "{position_title}" удалена.')
        return

    if action == "save_coverage":
        production_group = _staffing_group_or_none(department, request.POST.get("production_group_id"))
        if production_group is None:
            messages.error(request, "Выберите производственную группу.")
            return
        coverage_values = _coverage_values_from_post(request)
        if coverage_values is None:
            messages.error(request, "Проверьте числовые значения правила покрытия.")
            return
        DepartmentCoverageRule.objects.update_or_create(
            department=department,
            production_group=production_group,
            defaults=coverage_values,
        )
        messages.success(request, "Правило покрытия сохранено.")
        return

    if action == "update_coverage":
        coverage = _staffing_coverage_or_none(department, request.POST.get("coverage_id"))
        production_group = _staffing_group_or_none(department, request.POST.get("production_group_id"))
        coverage_values = _coverage_values_from_post(request)
        if coverage is None:
            messages.error(request, "Выберите правило покрытия.")
            return
        if production_group is None:
            messages.error(request, "Выберите производственную группу.")
            return
        if coverage_values is None:
            messages.error(request, "Проверьте числовые значения правила покрытия.")
            return
        if (
            DepartmentCoverageRule.objects.filter(department=department, production_group=production_group)
            .exclude(id=coverage.id)
            .exists()
        ):
            messages.error(request, "Для этой производственной группы уже есть правило покрытия.")
            return
        coverage.production_group = production_group
        coverage.min_staff_required = coverage_values["min_staff_required"]
        coverage.max_absent = coverage_values["max_absent"]
        coverage.criticality_level = coverage_values["criticality_level"]
        coverage.save(update_fields=["production_group", "min_staff_required", "max_absent", "criticality_level"])
        messages.success(request, "Правило покрытия обновлено.")
        return

    if action == "delete_coverage":
        coverage = _staffing_coverage_or_none(department, request.POST.get("coverage_id"))
        if coverage is None:
            messages.error(request, "Выберите правило покрытия.")
            return
        coverage_name = coverage.production_group.name
        coverage.delete()
        messages.success(request, f'Правило покрытия группы "{coverage_name}" удалено.')
        return

    if action in STAFFING_WORKLOAD_ACTIONS:
        year = _selected_staffing_workload_year(request)
        base_limits = _staffing_workload_base_limits(department)
        for month_number in range(1, 13):
            if action == "fill_workload_year":
                load_level = 3
                min_staff_required = base_limits["min_staff_required"]
                max_absent = base_limits["max_absent"]
            else:
                load_level = _bounded_int(request.POST.get(f"load_level_{month_number}"), 1, 1, 5)
                min_staff_required = _bounded_int(
                    request.POST.get(f"min_staff_required_{month_number}"),
                    base_limits["min_staff_required"],
                    0,
                )
                max_absent = _bounded_int(
                    request.POST.get(f"max_absent_{month_number}"),
                    base_limits["max_absent"],
                    0,
                )
            DepartmentWorkload.objects.update_or_create(
                department=department,
                year=year,
                month=month_number,
                defaults={
                    "load_level": load_level,
                    "min_staff_required": min_staff_required,
                    "max_absent": max_absent,
                },
            )
        if action == "fill_workload_year":
            messages.success(request, f'Нагрузка отдела "{department.name}" на {year} год заполнена базовыми лимитами.')
        else:
            messages.success(request, f'Нагрузка отдела "{department.name}" на {year} год сохранена.')
        return

    if action == "create_substitution":
        source_group = _staffing_group_or_none(department, request.POST.get("source_group_id"))
        substitute_group = _staffing_group_or_none(department, request.POST.get("substitute_group_id"))
        max_covered_absences = _positive_small_int(request.POST.get("max_covered_absences"), default=1)
        if source_group is None or substitute_group is None:
            messages.error(request, "Выберите обе группы для замещения.")
            return
        if source_group.id == substitute_group.id:
            messages.error(request, "Группа не может замещать саму себя.")
            return
        ProductionGroupSubstitutionRule.objects.update_or_create(
            department=department,
            source_group=source_group,
            substitute_group=substitute_group,
            defaults={"max_covered_absences": max_covered_absences},
        )
        messages.success(request, "Правило замещения добавлено.")
        return

    if action == "update_substitution":
        substitution = _staffing_substitution_or_none(department, request.POST.get("substitution_id"))
        if substitution is None:
            messages.error(request, "Выберите правило замещения.")
            return
        source_group = (
            _staffing_group_or_none(department, request.POST.get("source_group_id"))
            if request.POST.get("source_group_id")
            else substitution.source_group
        )
        substitute_group = (
            _staffing_group_or_none(department, request.POST.get("substitute_group_id"))
            if request.POST.get("substitute_group_id")
            else substitution.substitute_group
        )
        if source_group is None or substitute_group is None:
            messages.error(request, "Выберите обе группы для замещения.")
            return
        if source_group.id == substitute_group.id:
            messages.error(request, "Группа не может замещать саму себя.")
            return
        if (
            ProductionGroupSubstitutionRule.objects.filter(
                department=department,
                source_group=source_group,
                substitute_group=substitute_group,
            )
            .exclude(id=substitution.id)
            .exists()
        ):
            messages.error(request, "Такое правило замещения уже есть в отделе.")
            return
        substitution.source_group = source_group
        substitution.substitute_group = substitute_group
        substitution.max_covered_absences = _positive_small_int(request.POST.get("max_covered_absences"), default=1)
        substitution.save(update_fields=["source_group", "substitute_group", "max_covered_absences"])
        messages.success(request, "Правило замещения обновлено.")
        return

    if action == "delete_substitution":
        substitution = _staffing_substitution_or_none(department, request.POST.get("substitution_id"))
        if substitution is None:
            messages.error(request, "Выберите правило замещения.")
            return
        substitution.delete()
        messages.success(request, "Правило замещения удалено.")
        return

    messages.error(request, "Неизвестное действие на странице правил состава.")


@employee_required
def staffing_rules(request):
    context = get_user_context(request)
    context = update_context_with_departments(request, context)
    current_employee = get_current_employee(request)
    if not can_access_staffing_page(current_employee):
        messages.error(request, "У вас нет прав для доступа к правилам состава.")
        return redirect("main")

    can_edit = can_edit_staffing_rules(current_employee)
    selected_workload_year = _selected_staffing_workload_year(request)
    if request.method == "POST":
        if not can_edit:
            messages.error(request, "Редактировать правила состава могут HR и руководитель предприятия.")
            return redirect(_staffing_rules_redirect_url(request, selected_workload_year))

        if request.POST.get("action") == "set_enterprise_deputy":
            deputy_id = request.POST.get("enterprise_deputy_id")
            Employees.objects.update(is_enterprise_deputy=False)
            if deputy_id:
                deputy = Employees.objects.filter(
                    id=deputy_id,
                    is_active_employee=True,
                ).exclude(role__in=Employees.SERVICE_ROLES).first()
                if deputy is not None:
                    deputy.is_enterprise_deputy = True
                    deputy.save(update_fields=["is_enterprise_deputy"])
            messages.success(request, "Заместитель руководителя предприятия обновлён.")
        else:
            _handle_staffing_post(request, current_employee)
        return redirect(_staffing_rules_redirect_url(request, selected_workload_year))

    departments_qs = _get_staffing_departments(current_employee)
    departments = list(departments_qs)
    _decorate_departments_with_workload(departments, selected_workload_year)
    active_employees = list(Employees.objects.select_related(
        "department",
        "employee_position",
        "employee_position__production_group",
    ).filter(
        department__in=departments,
        is_active_employee=True,
    ).exclude(role__in=Employees.SERVICE_ROLES).order_by(
        "department__name",
        "last_name",
        "first_name",
        "middle_name",
    ))
    for employee in active_employees:
        employee.new_hire_badge = build_new_hire_badge(employee)
    _decorate_staffing_quality(departments, active_employees)
    employees_by_department = {}
    for employee in active_employees:
        employees_by_department.setdefault(employee.department_id, []).append(employee)
    for department in departments:
        department.staffing_employees = employees_by_department.get(department.id, [])
        if department.deputy is not None:
            department.deputy.new_hire_badge = build_new_hire_badge(department.deputy)
    current_enterprise_deputy = Employees.objects.filter(
        is_enterprise_deputy=True,
        is_active_employee=True,
    ).exclude(role__in=Employees.SERVICE_ROLES).first()
    if current_enterprise_deputy is not None:
        current_enterprise_deputy.new_hire_badge = build_new_hire_badge(current_enterprise_deputy)

    context.update(
        {
            "sidebar_section": "staffing",
            "staffing_departments": departments,
            "can_edit_staffing": can_edit,
            "selected_workload_year": selected_workload_year,
            "staffing_workload_year_options": _staffing_workload_year_options(selected_workload_year),
            "staffing_workload_level_options": STAFFING_WORKLOAD_LEVEL_OPTIONS,
            "enterprise_deputy_candidates": list(active_employees),
            "current_enterprise_deputy": current_enterprise_deputy,
            "can_reset_demo_data": _can_reset_demo_data(current_employee),
            "demo_data_reset_preset_options": demo_data_reset_preset_options(),
        }
    )
    return render(request, "staffing.html", context)


@employee_required
def reset_demo_data(request):
    current_employee = get_current_employee(request)
    if request.method != "POST":
        return redirect("staffing_rules" if can_access_staffing_page(current_employee) else "main")

    if not _can_reset_demo_data(current_employee):
        messages.error(request, "Пересоздать демо-данные может только руководитель предприятия или HR в демо-режиме.")
        return redirect("staffing_rules" if can_access_staffing_page(current_employee) else "main")

    try:
        seed_value = secrets.randbelow(DEMO_SEED_MAX_VALUE) + 1
        preset = normalize_demo_data_reset_preset(request.POST.get("demo_mode"))
        job, created = get_or_create_demo_data_reset_job(seed_value=seed_value, preset=preset)
        if created:
            start_demo_data_reset_process(job)
    except DemoDataResetInProgressError:
        return JsonResponse(
            {
                "ok": False,
                "message": "Демо-данные уже сбрасываются. Дождитесь завершения текущей операции.",
            },
            status=409,
        )
    except Exception as exc:
        return JsonResponse({"ok": False, "message": f"Не удалось запустить пересоздание демо-данных: {exc}"}, status=500)

    auth_logout(request)
    status_url = f"{reverse('reset_demo_data_status', args=[job.id])}?token={job.token}"
    payload = demo_data_reset_job_payload(job)
    payload.update(
        {
            "token": job.token,
            "status_url": status_url,
            "message": (
                "Пересоздание демо-данных запущено."
                if created
                else "Пересоздание демо-данных уже выполняется."
            ),
        }
    )
    return JsonResponse(
        payload
    )


def reset_demo_data_status(request, job_id):
    if not _demo_data_tools_enabled():
        return JsonResponse({"ok": False, "message": "Статус пересоздания доступен только в демо-режиме."}, status=404)

    token = request.GET.get("token", "")
    job = get_object_or_404(DemoDataResetJob, id=job_id)
    if not token or not secrets.compare_digest(token, job.token):
        return JsonResponse({"ok": False, "message": "Некорректный токен статуса."}, status=403)

    return JsonResponse(demo_data_reset_job_payload(job))


@employee_required
def restore_demo_initial_state(request):
    current_employee = get_current_employee(request)
    if request.method != "POST":
        return redirect("staffing_rules" if can_access_staffing_page(current_employee) else "main")

    if not _can_reset_demo_data(current_employee):
        messages.error(request, "Сбросить демо-состояние может только руководитель предприятия или HR в демо-режиме.")
        return redirect("staffing_rules" if can_access_staffing_page(current_employee) else "main")

    try:
        result = reset_demo_to_baseline(actor=current_employee)
    except DemoBaselineMissingError:
        messages.error(
            request,
            "Быстрый сброс пока недоступен: сначала один раз пересоздайте демо-данные, "
            "чтобы сохранить начальную точку.",
        )
        return redirect("staffing_rules")
    except DemoBaselineResetInProgressError:
        messages.info(request, "Сброс демо-данных уже выполняется. Дождитесь завершения текущей операции.")
        return redirect("staffing_rules")
    except Exception as exc:
        messages.error(request, f"Не удалось сбросить демо-состояние: {exc}")
        return redirect("staffing_rules")

    messages.success(
        request,
        f"Демо-состояние сброшено к началу планирования {result['planning_year']} года.",
    )
    return redirect("staffing_rules")
