import calendar
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

from apps.employees.models import (
    DepartmentCoverageRule,
    Departments,
    Employees,
    ProductionGroup,
    ProductionGroupSubstitutionRule,
)
from apps.leave.models import DepartmentWorkload, VacationRequest, VacationScheduleChangeRequest, VacationScheduleItem

from .constants import ACTIVE_REQUEST_STATUSES
from .dates import clip_period_to_range, iterate_dates
from .querysets import exclude_converted_paid_requests


STAFFING_FORECAST_WINDOW_DAYS = 30

STAFFING_FORECAST_LEVELS = {
    "ok": {
        "label": "Состав стабилен",
        "icon": "verified",
        "summary": "Критичных рисков на 30 дней не найдено.",
    },
    "info": {
        "label": "Под наблюдением",
        "icon": "visibility",
        "summary": "Есть приближение к лимитам состава, стоит держать отдел под контролем.",
    },
    "medium": {
        "label": "На минимуме",
        "icon": "balance",
        "summary": "Отдел подходит к минимальному составу или лимиту отсутствующих.",
    },
    "high": {
        "label": "Нужен резерв",
        "icon": "support_agent",
        "summary": "Риск закрывается только за счет замещения или дополнительного резерва.",
    },
    "conflict": {
        "label": "Скоро конфликт",
        "icon": "warning",
        "summary": "В ближайшие 30 дней есть дни, где отдел выходит за лимиты состава.",
    },
}

_FORECAST_LEVEL_PRIORITY = {
    "ok": 0,
    "info": 1,
    "medium": 2,
    "high": 3,
    "conflict": 4,
}
_ISSUE_SEVERITY_TO_FORECAST_LEVEL = {
    "info": "info",
    "medium": "medium",
    "high": "high",
    "conflict": "conflict",
}


def _uses_singular_staff_verb(value):
    value = int(value or 0)
    value_mod_100 = value % 100
    value_mod_10 = value % 10
    return value_mod_100 not in range(11, 15) and value_mod_10 == 1


def format_staff_count(value):
    value = int(value or 0)
    value_mod_100 = value % 100
    value_mod_10 = value % 10
    if _uses_singular_staff_verb(value):
        return f"{value} сотрудник"
    if value_mod_100 not in range(11, 15) and 2 <= value_mod_10 <= 4:
        return f"{value} сотрудника"
    return f"{value} сотрудников"


def format_staff_absence(value, tense="present"):
    value = int(value or 0)
    singular = _uses_singular_staff_verb(value)
    verbs = {
        "present": ("отсутствует", "отсутствуют"),
        "future": ("будет отсутствовать", "будут отсутствовать"),
        "past": ("отсутствовал", "отсутствовали"),
    }
    singular_verb, plural_verb = verbs.get(tense, verbs["present"])
    verb = singular_verb if singular else plural_verb
    return f"{verb} {format_staff_count(value)}"


def get_department_staffing_rule(department):
    if department is None:
        return None

    try:
        return department.staffing_rule
    except department.__class__.staffing_rule.RelatedObjectDoesNotExist:
        return None


def _staffing_issue(kind, severity, title, text, **metadata):
    return {
        "kind": kind,
        "severity": severity,
        "title": title,
        "text": text,
        **metadata,
    }


def _criticality_risk_boost(criticality_level, *, step=4):
    return max(0, int(criticality_level or 0) - 3) * step


def iter_month_day_weights(start_date, end_date):
    cursor = date(start_date.year, start_date.month, 1)
    final_month = date(end_date.year, end_date.month, 1)
    while cursor <= final_month:
        month_last_day = date(cursor.year, cursor.month, calendar.monthrange(cursor.year, cursor.month)[1])
        segment_start = max(start_date, cursor)
        segment_end = min(end_date, month_last_day)
        if segment_start <= segment_end:
            yield cursor.year, cursor.month, (segment_end - segment_start).days + 1

        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)


def _round_weighted_metric(total, day_count, *, minimum=0, maximum=None):
    if day_count <= 0:
        return minimum

    value = int((Decimal(total) / Decimal(day_count)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value


def get_weighted_department_workload(department, start_date, end_date, staffing_rule=None):
    if staffing_rule is None:
        staffing_rule = get_department_staffing_rule(department)

    month_weights = list(iter_month_day_weights(start_date, end_date))
    if not month_weights:
        return {
            "department_load_level": 1,
            "min_staff_required": staffing_rule.min_staff_required if staffing_rule else 0,
            "max_absent": staffing_rule.max_absent if staffing_rule else 1,
        }

    workloads = {
        (workload.year, workload.month): workload
        for workload in DepartmentWorkload.objects.filter(
            department=department,
            year__in={year for year, _, _ in month_weights},
            month__in={month for _, month, _ in month_weights},
        )
    }
    fallback_min_staff = staffing_rule.min_staff_required if staffing_rule else 0
    fallback_max_absent = staffing_rule.max_absent if staffing_rule else 1
    total_days = sum(days for _, _, days in month_weights)
    load_total = 0
    min_staff_total = 0
    max_absent_total = 0

    for year, month, days in month_weights:
        workload = workloads.get((year, month))
        load_total += (workload.load_level if workload else 1) * days
        min_staff_total += (workload.min_staff_required if workload else fallback_min_staff) * days
        max_absent_total += (workload.max_absent if workload else fallback_max_absent) * days

    return {
        "department_load_level": _round_weighted_metric(load_total, total_days, minimum=1, maximum=5),
        "min_staff_required": _round_weighted_metric(min_staff_total, total_days, minimum=0),
        "max_absent": _round_weighted_metric(max_absent_total, total_days, minimum=1),
    }


def get_staffing_limits_for_date(department, current_date, workload_by_department_month, staffing_rules):
    workload = workload_by_department_month.get((department.id, current_date.year, current_date.month))
    if workload is not None:
        return workload.min_staff_required, workload.max_absent

    staffing_rule = staffing_rules.get(department.id)
    if staffing_rule is not None:
        return staffing_rule.min_staff_required, staffing_rule.max_absent

    return 0, 1


def get_active_absence_employee_ids(
    employee_ids,
    start_date,
    end_date,
    *,
    exclude_request_id=None,
    exclude_schedule_item_id=None,
):
    employee_ids = set(employee_ids or [])
    if not employee_ids:
        return set()

    overlapping_requests = VacationRequest.objects.filter(
        employee_id__in=employee_ids,
        status__in=ACTIVE_REQUEST_STATUSES,
        start_date__lte=end_date,
        end_date__gte=start_date,
    )
    if exclude_request_id is not None:
        overlapping_requests = overlapping_requests.exclude(pk=exclude_request_id)
    overlapping_requests = exclude_converted_paid_requests(
        overlapping_requests,
        employee_ids=employee_ids,
        start_date=start_date,
        end_date=end_date,
    )
    request_employee_ids = set(overlapping_requests.values_list("employee_id", flat=True))
    schedule_employee_ids = set(
        VacationScheduleItem.objects.filter(
            employee_id__in=employee_ids,
            status__in=VacationScheduleItem.ACTIVE_STATUSES,
            start_date__lte=end_date,
            end_date__gte=start_date,
        )
        .exclude(pk=exclude_schedule_item_id)
        .values_list("employee_id", flat=True)
    )
    return request_employee_ids | schedule_employee_ids


def build_department_staffing_context_map(departments, as_of_date):
    departments = [department for department in departments if department is not None]
    department_ids = [department.id for department in departments]
    if not department_ids:
        return {}

    staff_ids_by_department = defaultdict(set)
    staff_ids_by_department_group = defaultdict(lambda: defaultdict(set))
    staff_members = Employees.objects.select_related(
        "employee_position",
        "employee_position__production_group",
    ).filter(
        department_id__in=department_ids,
        is_active_employee=True,
        date_joined__lte=as_of_date,
    ).exclude(role__in=Employees.SERVICE_ROLES)
    for staff_member in staff_members:
        staff_ids_by_department[staff_member.department_id].add(staff_member.id)
        group_id = (
            staff_member.employee_position.production_group_id
            if staff_member.employee_position_id and staff_member.employee_position
            else None
        )
        if group_id is not None:
            staff_ids_by_department_group[staff_member.department_id][group_id].add(staff_member.id)

    coverage_rules_by_department = defaultdict(list)
    coverage_rule_by_department_group = defaultdict(dict)
    coverage_rules = DepartmentCoverageRule.objects.select_related("production_group").filter(
        department_id__in=department_ids,
    ).order_by("-criticality_level", "production_group__name")
    for rule in coverage_rules:
        coverage_rules_by_department[rule.department_id].append(rule)
        coverage_rule_by_department_group[rule.department_id][rule.production_group_id] = rule

    substitution_rules_by_department_source = defaultdict(lambda: defaultdict(list))
    substitution_rules = ProductionGroupSubstitutionRule.objects.select_related(
        "source_group",
        "substitute_group",
    ).filter(department_id__in=department_ids)
    for rule in substitution_rules:
        substitution_rules_by_department_source[rule.department_id][rule.source_group_id].append(rule)

    head_id_by_department = {department.id: getattr(department, "head_id", None) for department in departments}
    deputy_id_by_department = {department.id: getattr(department, "deputy_id", None) for department in departments}
    missing_head_department_ids = [
        department_id
        for department_id in department_ids
        if not head_id_by_department.get(department_id)
    ]
    if missing_head_department_ids:
        fallback_heads = Employees.objects.filter(
            department_id__in=missing_head_department_ids,
            role=Employees.ROLE_DEPARTMENT_HEAD,
            is_active_employee=True,
            date_joined__lte=as_of_date,
        ).exclude(role__in=Employees.SERVICE_ROLES).order_by("department_id", "id").values_list(
            "department_id",
            "id",
        )
        for department_id, employee_id in fallback_heads:
            head_id_by_department.setdefault(department_id, employee_id)
            if head_id_by_department[department_id] is None:
                head_id_by_department[department_id] = employee_id

    contexts = {}
    for department in departments:
        staff_ids = staff_ids_by_department[department.id]
        contexts[department.id] = {
            "department": department,
            "staff_ids": staff_ids,
            "staff_count": len(staff_ids),
            "staff_ids_by_group": staff_ids_by_department_group[department.id],
            "coverage_rules": coverage_rules_by_department[department.id],
            "coverage_rule_by_group": coverage_rule_by_department_group[department.id],
            "substitution_rules_by_source": substitution_rules_by_department_source[department.id],
            "head_id": head_id_by_department.get(department.id),
            "deputy_id": deputy_id_by_department.get(department.id),
        }
    return contexts


def build_department_staffing_context(department, as_of_date):
    if department is None:
        return None
    return build_department_staffing_context_map([department], as_of_date).get(department.id)


def evaluate_department_staffing_state(
    staffing_context,
    absent_employee_ids,
    *,
    min_staff_required,
    max_absent,
    target_group_id=None,
    target_employee_id=None,
    include_limit_warnings=True,
):
    department = staffing_context["department"]
    staff_ids = staffing_context["staff_ids"]
    absent_employee_ids = set(absent_employee_ids or []) & staff_ids
    staff_count = staffing_context["staff_count"]
    if staff_count:
        min_staff_required = min(int(min_staff_required or 0), staff_count)
        max_absent = min(int(max_absent or 0), staff_count)
    else:
        min_staff_required = 0
        max_absent = 0

    absent_count = len(absent_employee_ids)
    remaining_staff_count = max(staff_count - absent_count, 0)
    issues = []
    hard_conflict = False
    substitution_used = False
    affected_group_name = ""
    staffing_boost = 0
    group_staffing_boost = 0
    leadership_boost = 0

    if min_staff_required and remaining_staff_count < min_staff_required:
        hard_conflict = True
        staffing_boost += 52
        issues.append(
            _staffing_issue(
                "department_staff_shortage",
                "conflict",
                "Недостаток состава отдела",
                (
                    f"В отделе останется {format_staff_count(remaining_staff_count)} "
                    f"при минимуме {format_staff_count(min_staff_required)}."
                ),
                affected_department=department.name,
                affected_employee_ids=set(absent_employee_ids),
                remaining_staff=remaining_staff_count,
                required_staff=min_staff_required,
                missing_staff=max(min_staff_required - remaining_staff_count, 0),
            )
        )
    elif include_limit_warnings and min_staff_required and remaining_staff_count == min_staff_required:
        staffing_boost += 12
        issues.append(
            _staffing_issue(
                "department_staff_minimum_reached",
                "medium",
                "Отдел на минимуме",
                f"После ухода сотрудника отдел останется ровно на минимуме: {format_staff_count(min_staff_required)}.",
                affected_department=department.name,
                affected_employee_ids=set(absent_employee_ids),
                remaining_staff=remaining_staff_count,
                required_staff=min_staff_required,
            )
        )

    if max_absent and absent_count > max_absent:
        hard_conflict = True
        staffing_boost += 44
        issues.append(
            _staffing_issue(
                "department_absence_limit",
                "conflict",
                "Лимит отсутствующих в отделе",
                (
                    f"В отделе {format_staff_absence(absent_count, tense='future')}, "
                    f"лимит — {format_staff_count(max_absent)}."
                ),
                affected_department=department.name,
                affected_employee_ids=set(absent_employee_ids),
                absent_staff=absent_count,
                max_absent=max_absent,
            )
        )
    elif include_limit_warnings and max_absent and absent_count == max_absent:
        staffing_boost += 10
        issues.append(
            _staffing_issue(
                "department_absence_limit_reached",
                "medium",
                "Отдел на лимите",
                f"Отдел выйдет ровно на лимит отсутствующих: {format_staff_count(absent_count)}.",
                affected_department=department.name,
                affected_employee_ids=set(absent_employee_ids),
                absent_staff=absent_count,
                max_absent=max_absent,
            )
        )
    elif include_limit_warnings and max_absent and max_absent >= 3 and absent_count == max_absent - 1:
        staffing_boost += 5
        issues.append(
            _staffing_issue(
                "department_absence_limit_near",
                "info",
                "Отдел близко к лимиту",
                f"До лимита отсутствующих в отделе останется {format_staff_count(max_absent - absent_count)}.",
                affected_department=department.name,
                affected_employee_ids=set(absent_employee_ids),
                absent_staff=absent_count,
                max_absent=max_absent,
            )
        )

    present_ids_by_group = {}
    substitute_capacity_remaining = {}
    for group_id, group_staff_ids in staffing_context["staff_ids_by_group"].items():
        present_ids = group_staff_ids - absent_employee_ids
        present_ids_by_group[group_id] = present_ids
        group_rule = staffing_context["coverage_rule_by_group"].get(group_id)
        group_minimum = group_rule.min_staff_required if group_rule is not None else 0
        substitute_capacity_remaining[group_id] = max(len(present_ids) - group_minimum, 0)

    coverage_rules = staffing_context["coverage_rules"]
    if target_group_id is not None:
        coverage_rules = [rule for rule in coverage_rules if rule.production_group_id == target_group_id]

    for coverage_rule in coverage_rules:
        group_id = coverage_rule.production_group_id
        group_staff_ids = staffing_context["staff_ids_by_group"].get(group_id, set())
        if not group_staff_ids:
            continue

        if target_group_id is not None:
            affected_group_name = coverage_rule.production_group.name

        absent_group_ids = group_staff_ids & absent_employee_ids
        present_primary_ids = present_ids_by_group.get(group_id, set())
        primary_present_count = len(present_primary_ids)
        shortage = max(coverage_rule.min_staff_required - primary_present_count, 0)
        covered_by_substitution = 0
        substitution_names = []

        if shortage:
            for substitution_rule in staffing_context["substitution_rules_by_source"].get(group_id, []):
                substitute_group_id = substitution_rule.substitute_group_id
                available_capacity = substitute_capacity_remaining.get(substitute_group_id, 0)
                if available_capacity <= 0:
                    continue
                covered_now = min(
                    shortage - covered_by_substitution,
                    available_capacity,
                    substitution_rule.max_covered_absences,
                )
                if covered_now <= 0:
                    continue
                covered_by_substitution += covered_now
                substitution_used = True
                substitution_names.append(f"{substitution_rule.substitute_group.name}: {format_staff_count(covered_now)}")
                substitute_capacity_remaining[substitute_group_id] = available_capacity - covered_now
                if covered_by_substitution >= shortage:
                    break

        effective_present_count = primary_present_count + covered_by_substitution
        absent_group_count = len(absent_group_ids)
        affected_employee_ids = set(absent_group_ids or absent_employee_ids)

        if (
            coverage_rule.min_staff_required
            and shortage
            and covered_by_substitution
            and primary_present_count < coverage_rule.min_staff_required <= effective_present_count
        ):
            group_staffing_boost += 56
            issues.append(
                _staffing_issue(
                    "substitution_used",
                    "high",
                    "Нужно замещение",
                    (
                        f"Группа «{coverage_rule.production_group.name}» ниже минимума, но дефицит закрывается замещением "
                        f"({format_staff_count(covered_by_substitution)})."
                    ),
                    affected_group=coverage_rule.production_group.name,
                    affected_employee_ids=affected_employee_ids,
                    substitution_used=True,
                    covered_staff=covered_by_substitution,
                    substitute_groups=", ".join(substitution_names),
                )
            )

        if coverage_rule.min_staff_required and effective_present_count < coverage_rule.min_staff_required:
            hard_conflict = True
            group_staffing_boost += 56
            issues.append(
                _staffing_issue(
                    "group_shortage",
                    "conflict",
                    "Недостаток группы",
                    (
                        f"В группе «{coverage_rule.production_group.name}» останется {format_staff_count(effective_present_count)} "
                        f"при минимуме {format_staff_count(coverage_rule.min_staff_required)}."
                    ),
                    affected_group=coverage_rule.production_group.name,
                    affected_employee_ids=affected_employee_ids,
                    remaining_staff=effective_present_count,
                    required_staff=coverage_rule.min_staff_required,
                    missing_staff=max(coverage_rule.min_staff_required - effective_present_count, 0),
                )
            )

        if absent_group_count > coverage_rule.max_absent:
            hard_conflict = True
            group_staffing_boost += 44
            issues.append(
                _staffing_issue(
                    "group_absence_limit",
                    "conflict",
                    "Лимит отсутствующих в группе",
                    (
                        f"В группе «{coverage_rule.production_group.name}» "
                        f"{format_staff_absence(absent_group_count, tense='future')}, "
                        f"лимит — {format_staff_count(coverage_rule.max_absent)}."
                    ),
                    affected_group=coverage_rule.production_group.name,
                    affected_employee_ids=affected_employee_ids,
                    absent_staff=absent_group_count,
                    max_absent=coverage_rule.max_absent,
                )
            )
        elif include_limit_warnings and coverage_rule.max_absent and absent_group_count == coverage_rule.max_absent:
            group_staffing_boost += 8
            issues.append(
                _staffing_issue(
                    "group_absence_limit_reached",
                    "medium",
                    "Группа на лимите",
                    (
                        f"Группа «{coverage_rule.production_group.name}» выйдет ровно на лимит отсутствующих: "
                        f"{format_staff_count(absent_group_count)}."
                    ),
                    affected_group=coverage_rule.production_group.name,
                    affected_employee_ids=affected_employee_ids,
                    absent_staff=absent_group_count,
                    max_absent=coverage_rule.max_absent,
                )
            )
        if absent_group_count:
            group_staffing_boost += _criticality_risk_boost(coverage_rule.criticality_level, step=3)

    head_id = staffing_context.get("head_id")
    deputy_id = staffing_context.get("deputy_id")
    leadership_pair_absent = head_id and deputy_id and head_id in absent_employee_ids and deputy_id in absent_employee_ids
    target_is_leadership_pair = target_employee_id is None or target_employee_id in {head_id, deputy_id}
    if leadership_pair_absent and target_is_leadership_pair:
        hard_conflict = True
        leadership_boost += 24
        issues.append(
            _staffing_issue(
                "department_leadership_pair",
                "conflict",
                "Нет пары управления отделом",
                "Руководитель отдела и заместитель будут отсутствовать одновременно.",
                affected_department=department.name,
                affected_employee_ids={head_id, deputy_id},
            )
        )

    return {
        "department": department,
        "staff_count": staff_count,
        "absent_employee_ids": absent_employee_ids,
        "absent_count": absent_count,
        "remaining_staff_count": remaining_staff_count,
        "min_staff_required": min_staff_required,
        "max_absent": max_absent,
        "issues": issues,
        "hard_conflict": hard_conflict,
        "substitution_used": substitution_used,
        "affected_group_name": affected_group_name,
        "staffing_boost": staffing_boost,
        "group_staffing_boost": group_staffing_boost,
        "leadership_boost": leadership_boost,
    }


def _format_forecast_date(value):
    return value.strftime("%d.%m")


def _format_days_count(value):
    value = int(value or 0)
    last_two_digits = value % 100
    last_digit = value % 10
    if 11 <= last_two_digits <= 14:
        word = "дней"
    elif last_digit == 1:
        word = "день"
    elif 2 <= last_digit <= 4:
        word = "дня"
    else:
        word = "дней"
    return f"{value} {word}"


def _forecast_level_for_issue(issue):
    return _ISSUE_SEVERITY_TO_FORECAST_LEVEL.get(issue.get("severity"), "ok")


def _max_forecast_level(current_level, candidate_level):
    if _FORECAST_LEVEL_PRIORITY[candidate_level] > _FORECAST_LEVEL_PRIORITY[current_level]:
        return candidate_level
    return current_level


def _format_reserve_label(value):
    value = int(value or 0)
    if value < 0:
        return f"дефицит {format_staff_count(abs(value))}"
    if value == 0:
        return "нет резерва"
    return format_staff_count(value)


def _get_group_minimum_forecast_issues(staffing_context, absent_employee_ids):
    absent_employee_ids = set(absent_employee_ids or []) & staffing_context["staff_ids"]
    issues = []
    for coverage_rule in staffing_context["coverage_rules"]:
        if not coverage_rule.min_staff_required:
            continue

        group_staff_ids = staffing_context["staff_ids_by_group"].get(coverage_rule.production_group_id, set())
        if not group_staff_ids:
            continue

        remaining_group_staff = len(group_staff_ids - absent_employee_ids)
        if remaining_group_staff == coverage_rule.min_staff_required:
            issues.append(
                _staffing_issue(
                    "group_staff_minimum_reached",
                    "medium",
                    "Группа на минимуме",
                    (
                        f"Группа «{coverage_rule.production_group.name}» останется ровно на минимуме: "
                        f"{format_staff_count(coverage_rule.min_staff_required)}."
                    ),
                    affected_group=coverage_rule.production_group.name,
                    affected_employee_ids=set(group_staff_ids & absent_employee_ids),
                    remaining_staff=remaining_group_staff,
                    required_staff=coverage_rule.min_staff_required,
                )
            )
    return issues


def _forecast_reason_key(issue):
    return (
        issue.get("kind", ""),
        issue.get("affected_department", ""),
        issue.get("affected_group", ""),
        issue.get("required_staff"),
        issue.get("max_absent"),
    )


def _format_staffing_forecast_reason(first_date, issue, days_count):
    date_label = _format_forecast_date(first_date)
    suffix = f" ({_format_days_count(days_count)})" if days_count > 1 else ""
    issue_kind = issue.get("kind")

    if issue_kind == "department_staff_shortage":
        return (
            f"{date_label}: отдел ниже минимума, останется "
            f"{issue.get('remaining_staff', 0)} из {issue.get('required_staff', 0)}{suffix}"
        )
    if issue_kind == "department_staff_minimum_reached":
        return f"{date_label}: отдел на минимуме, резерв не остается{suffix}"
    if issue_kind == "department_absence_limit":
        return (
            f"{date_label}: много отсутствующих, "
            f"{issue.get('absent_staff', 0)} при лимите {issue.get('max_absent', 0)}{suffix}"
        )
    if issue_kind == "department_absence_limit_reached":
        return f"{date_label}: отдел выходит ровно на лимит отсутствующих{suffix}"
    if issue_kind == "department_absence_limit_near":
        return f"{date_label}: до лимита отсутствующих остается один человек{suffix}"
    if issue_kind == "group_shortage":
        return (
            f"{date_label}: группа «{issue.get('affected_group', '')}» ниже минимума, "
            f"{issue.get('remaining_staff', 0)} из {issue.get('required_staff', 0)}{suffix}"
        )
    if issue_kind == "group_staff_minimum_reached":
        return f"{date_label}: группа «{issue.get('affected_group', '')}» на минимуме{suffix}"
    if issue_kind == "group_absence_limit":
        return (
            f"{date_label}: группа «{issue.get('affected_group', '')}» выше лимита, "
            f"{issue.get('absent_staff', 0)} при лимите {issue.get('max_absent', 0)}{suffix}"
        )
    if issue_kind == "group_absence_limit_reached":
        return f"{date_label}: группа «{issue.get('affected_group', '')}» на лимите отсутствующих{suffix}"
    if issue_kind == "substitution_used":
        return f"{date_label}: группе «{issue.get('affected_group', '')}» нужен резерв{suffix}"
    if issue_kind == "department_leadership_pair":
        return f"{date_label}: руководитель отдела и заместитель отсутствуют одновременно{suffix}"

    return f"{date_label}: {issue.get('title', 'риск состава')}{suffix}"


def _format_staffing_forecast_compact_reason(first_date, issue):
    date_label = _format_forecast_date(first_date)
    issue_kind = issue.get("kind")
    group_name = issue.get("affected_group", "")

    if issue_kind == "department_staff_shortage":
        reason = "отдел ниже минимума"
    elif issue_kind == "department_staff_minimum_reached":
        reason = "отдел на минимуме"
    elif issue_kind == "department_absence_limit":
        reason = "много отсутствующих"
    elif issue_kind == "department_absence_limit_reached":
        reason = "отдел на лимите"
    elif issue_kind == "department_absence_limit_near":
        reason = "близко к лимиту"
    elif issue_kind == "group_shortage":
        reason = f"{group_name} ниже минимума" if group_name else "группа ниже минимума"
    elif issue_kind == "group_staff_minimum_reached":
        reason = f"{group_name} на минимуме" if group_name else "группа на минимуме"
    elif issue_kind == "group_absence_limit":
        reason = f"{group_name} выше лимита" if group_name else "группа выше лимита"
    elif issue_kind == "group_absence_limit_reached":
        reason = f"{group_name} на лимите" if group_name else "группа на лимите"
    elif issue_kind == "substitution_used":
        reason = f"{group_name}: нужен резерв" if group_name else "нужен резерв"
    elif issue_kind == "department_leadership_pair":
        reason = "нет пары управления"
    else:
        reason = issue.get("title", "риск состава")

    return f"{date_label} · {reason}"


def _empty_department_staffing_forecast(start_date, end_date, staffing_context=None):
    staff_count = staffing_context["staff_count"] if staffing_context else 0
    window_days = (end_date - start_date).days + 1
    return {
        "level": "ok",
        "label": STAFFING_FORECAST_LEVELS["ok"]["label"],
        "icon": STAFFING_FORECAST_LEVELS["ok"]["icon"],
        "summary": STAFFING_FORECAST_LEVELS["ok"]["summary"],
        "window_label": _format_days_count(window_days),
        "peak_absent_count": 0,
        "peak_absent_label": format_staff_count(0),
        "min_remaining_staff_count": staff_count,
        "min_remaining_label": format_staff_count(staff_count),
        "min_reserve_count": staff_count,
        "min_reserve_label": _format_reserve_label(staff_count),
        "conflict_days_count": 0,
        "reasons": [],
        "primary_reason": f"{_format_days_count(window_days)} · критичных рисков нет",
        "has_risk": False,
        "_reason_records": {},
        "_conflict_dates": set(),
    }


def _empty_group_staffing_forecast(start_date, end_date, group, staff_count, coverage_rule=None):
    window_days = (end_date - start_date).days + 1
    min_staff_required = int(coverage_rule.min_staff_required) if coverage_rule else 0
    max_absent = int(coverage_rule.max_absent) if coverage_rule else 0
    reserve_count = staff_count - min_staff_required if coverage_rule else staff_count
    return {
        "group": group,
        "has_rule": coverage_rule is not None,
        "level": "ok",
        "label": STAFFING_FORECAST_LEVELS["ok"]["label"],
        "icon": STAFFING_FORECAST_LEVELS["ok"]["icon"],
        "summary": STAFFING_FORECAST_LEVELS["ok"]["summary"],
        "window_label": _format_days_count(window_days),
        "employee_count": staff_count,
        "min_staff_required": min_staff_required,
        "max_absent": max_absent,
        "min_staff_label": format_staff_count(min_staff_required) if coverage_rule else "Правило не задано",
        "max_absent_label": format_staff_count(max_absent) if coverage_rule else "Правило не задано",
        "peak_absent_count": 0,
        "peak_absent_label": format_staff_count(0),
        "min_remaining_staff_count": staff_count,
        "min_remaining_label": format_staff_count(staff_count),
        "min_reserve_count": reserve_count,
        "min_reserve_label": _format_reserve_label(reserve_count),
        "conflict_days_count": 0,
        "reasons": [],
        "primary_reason": (
            f"{_format_days_count(window_days)} · критичных рисков нет"
            if coverage_rule
            else f"{_format_days_count(window_days)} · правило не задано"
        ),
        "has_risk": False,
        "_reason_records": {},
        "_conflict_dates": set(),
    }


def _add_staffing_forecast_reason(forecast, current_date, issue):
    issue_level = _forecast_level_for_issue(issue)
    if issue_level == "ok":
        return

    forecast["level"] = _max_forecast_level(forecast["level"], issue_level)
    reason_key = _forecast_reason_key(issue)
    reason_records = forecast["_reason_records"]
    if reason_key not in reason_records:
        reason_records[reason_key] = {
            "issue": issue,
            "first_date": current_date,
            "dates": set(),
            "priority": _FORECAST_LEVEL_PRIORITY[issue_level],
        }
    reason_records[reason_key]["first_date"] = min(reason_records[reason_key]["first_date"], current_date)
    reason_records[reason_key]["dates"].add(current_date)
    reason_records[reason_key]["priority"] = max(
        reason_records[reason_key]["priority"],
        _FORECAST_LEVEL_PRIORITY[issue_level],
    )


def _finalize_department_staffing_forecast(forecast):
    level = forecast["level"]
    meta = STAFFING_FORECAST_LEVELS[level]
    forecast["label"] = meta["label"]
    forecast["icon"] = meta["icon"]
    forecast["peak_absent_label"] = format_staff_count(forecast["peak_absent_count"])
    forecast["min_remaining_label"] = format_staff_count(forecast["min_remaining_staff_count"])
    forecast["min_reserve_label"] = _format_reserve_label(forecast["min_reserve_count"])
    forecast["conflict_days_count"] = len(forecast["_conflict_dates"])

    reason_records = sorted(
        forecast["_reason_records"].values(),
        key=lambda record: (-record["priority"], record["first_date"], record["issue"].get("kind", "")),
    )
    forecast["reasons"] = [
        _format_staffing_forecast_reason(record["first_date"], record["issue"], len(record["dates"]))
        for record in reason_records[:2]
    ]
    forecast["has_risk"] = level != "ok"
    if reason_records:
        primary_record = reason_records[0]
        forecast["primary_reason"] = _format_staffing_forecast_compact_reason(
            primary_record["first_date"],
            primary_record["issue"],
        )

    if level == "conflict" and forecast["conflict_days_count"]:
        forecast["summary"] = (
            f"{_format_days_count(forecast['conflict_days_count'])} с конфликтом, "
            f"пик отсутствующих — {forecast['peak_absent_label']}."
        )
    elif level in {"medium", "high"} and forecast["min_reserve_count"] <= 0:
        forecast["summary"] = (
            f"Минимальный резерв — {forecast['min_reserve_label']}, "
            f"пик отсутствующих — {forecast['peak_absent_label']}."
        )
    else:
        forecast["summary"] = meta["summary"]

    forecast.pop("_reason_records", None)
    forecast.pop("_conflict_dates", None)
    return forecast


def _evaluate_group_staffing_forecast_state(staffing_context, group, absent_employee_ids, coverage_rule):
    group_staff_ids = staffing_context["staff_ids_by_group"].get(group.id, set())
    absent_employee_ids = set(absent_employee_ids or []) & staffing_context["staff_ids"]
    absent_group_ids = group_staff_ids & absent_employee_ids
    primary_present_ids = group_staff_ids - absent_employee_ids
    primary_present_count = len(primary_present_ids)
    group_staff_count = len(group_staff_ids)
    absent_group_count = len(absent_group_ids)
    issues = []
    hard_conflict = False

    if coverage_rule is None:
        return {
            "absent_count": absent_group_count,
            "remaining_staff_count": primary_present_count,
            "min_staff_required": 0,
            "max_absent": 0,
            "reserve_count": primary_present_count,
            "issues": issues,
            "hard_conflict": hard_conflict,
        }

    min_staff_required = min(int(coverage_rule.min_staff_required or 0), group_staff_count) if group_staff_count else 0
    max_absent = int(coverage_rule.max_absent or 0)
    present_ids_by_group = {}
    substitute_capacity_remaining = {}
    for source_group_id, source_staff_ids in staffing_context["staff_ids_by_group"].items():
        present_ids = source_staff_ids - absent_employee_ids
        present_ids_by_group[source_group_id] = present_ids
        source_rule = staffing_context["coverage_rule_by_group"].get(source_group_id)
        source_minimum = source_rule.min_staff_required if source_rule is not None else 0
        substitute_capacity_remaining[source_group_id] = max(len(present_ids) - source_minimum, 0)

    shortage = max(min_staff_required - primary_present_count, 0)
    covered_by_substitution = 0
    substitution_names = []
    if shortage:
        for substitution_rule in staffing_context["substitution_rules_by_source"].get(group.id, []):
            substitute_group_id = substitution_rule.substitute_group_id
            available_capacity = substitute_capacity_remaining.get(substitute_group_id, 0)
            if available_capacity <= 0:
                continue

            covered_now = min(
                shortage - covered_by_substitution,
                available_capacity,
                substitution_rule.max_covered_absences,
            )
            if covered_now <= 0:
                continue

            covered_by_substitution += covered_now
            substitute_capacity_remaining[substitute_group_id] = available_capacity - covered_now
            substitution_names.append(f"{substitution_rule.substitute_group.name}: {format_staff_count(covered_now)}")
            if covered_by_substitution >= shortage:
                break

    effective_present_count = primary_present_count + covered_by_substitution
    affected_employee_ids = set(absent_group_ids or absent_employee_ids)

    if (
        min_staff_required
        and shortage
        and covered_by_substitution
        and primary_present_count < min_staff_required <= effective_present_count
    ):
        issues.append(
            _staffing_issue(
                "substitution_used",
                "high",
                "Нужно замещение",
                (
                    f"Группа «{group.name}» ниже минимума, но дефицит закрывается замещением "
                    f"({format_staff_count(covered_by_substitution)})."
                ),
                affected_group=group.name,
                affected_employee_ids=affected_employee_ids,
                substitution_used=True,
                covered_staff=covered_by_substitution,
                substitute_groups=", ".join(substitution_names),
            )
        )

    if min_staff_required and effective_present_count < min_staff_required:
        hard_conflict = True
        issues.append(
            _staffing_issue(
                "group_shortage",
                "conflict",
                "Недостаток группы",
                (
                    f"В группе «{group.name}» останется {format_staff_count(effective_present_count)} "
                    f"при минимуме {format_staff_count(min_staff_required)}."
                ),
                affected_group=group.name,
                affected_employee_ids=affected_employee_ids,
                remaining_staff=effective_present_count,
                required_staff=min_staff_required,
                missing_staff=max(min_staff_required - effective_present_count, 0),
            )
        )
    elif min_staff_required and effective_present_count == min_staff_required:
        issues.append(
            _staffing_issue(
                "group_staff_minimum_reached",
                "medium",
                "Группа на минимуме",
                f"Группа «{group.name}» останется ровно на минимуме: {format_staff_count(min_staff_required)}.",
                affected_group=group.name,
                affected_employee_ids=set(absent_group_ids),
                remaining_staff=effective_present_count,
                required_staff=min_staff_required,
            )
        )

    if max_absent and absent_group_count > max_absent:
        hard_conflict = True
        issues.append(
            _staffing_issue(
                "group_absence_limit",
                "conflict",
                "Лимит отсутствующих в группе",
                (
                    f"В группе «{group.name}» {format_staff_absence(absent_group_count, tense='future')}, "
                    f"лимит — {format_staff_count(max_absent)}."
                ),
                affected_group=group.name,
                affected_employee_ids=set(absent_group_ids),
                absent_staff=absent_group_count,
                max_absent=max_absent,
            )
        )
    elif max_absent and absent_group_count == max_absent:
        issues.append(
            _staffing_issue(
                "group_absence_limit_reached",
                "medium",
                "Группа на лимите",
                (
                    f"Группа «{group.name}» выйдет ровно на лимит отсутствующих: "
                    f"{format_staff_count(absent_group_count)}."
                ),
                affected_group=group.name,
                affected_employee_ids=set(absent_group_ids),
                absent_staff=absent_group_count,
                max_absent=max_absent,
            )
        )

    return {
        "absent_count": absent_group_count,
        "remaining_staff_count": effective_present_count,
        "min_staff_required": min_staff_required,
        "max_absent": max_absent,
        "reserve_count": effective_present_count - min_staff_required,
        "issues": issues,
        "hard_conflict": hard_conflict,
    }


def _finalize_group_staffing_forecast(forecast):
    finalized = _finalize_department_staffing_forecast(forecast)
    if not finalized.get("has_rule") and finalized["level"] == "ok":
        finalized["summary"] = "Правило минимального состава для группы не задано."
    return finalized


def build_department_group_staffing_forecast_map(
    department,
    *,
    groups=None,
    start_date=None,
    end_date=None,
    window_days=STAFFING_FORECAST_WINDOW_DAYS,
    staffing_context=None,
):
    if department is None:
        return {}

    start_date = start_date or timezone.localdate()
    if end_date is None:
        end_date = start_date + timedelta(days=max(int(window_days or 1), 1) - 1)

    groups = list(
        groups
        if groups is not None
        else ProductionGroup.objects.filter(department=department).order_by("name")
    )
    group_ids = {group.id for group in groups}
    if not group_ids:
        return {}

    if staffing_context is None:
        staffing_context = build_department_staffing_context(department, end_date)
    employee_ids = set(staffing_context["staff_ids"])
    absent_by_day = defaultdict(set)

    def add_absence_period(employee_id, absence_start, absence_end):
        if employee_id not in employee_ids:
            return

        clipped_period = clip_period_to_range(absence_start, absence_end, start_date, end_date)
        if clipped_period is None:
            return

        clipped_start, clipped_end = clipped_period
        for current_date in iterate_dates(clipped_start, clipped_end):
            absent_by_day[current_date].add(employee_id)

    if employee_ids:
        active_requests = VacationRequest.objects.filter(
            employee_id__in=employee_ids,
            status__in=ACTIVE_REQUEST_STATUSES,
            start_date__lte=end_date,
            end_date__gte=start_date,
        ).only("employee_id", "start_date", "end_date", "status", "vacation_type")
        active_requests = exclude_converted_paid_requests(
            active_requests,
            employee_ids=employee_ids,
            start_date=start_date,
            end_date=end_date,
        )
        for request_obj in active_requests:
            add_absence_period(request_obj.employee_id, request_obj.start_date, request_obj.end_date)

        active_schedule_items = VacationScheduleItem.objects.filter(
            employee_id__in=employee_ids,
            status__in=VacationScheduleItem.ACTIVE_STATUSES,
            start_date__lte=end_date,
            end_date__gte=start_date,
        ).only("employee_id", "start_date", "end_date", "status")
        for item in active_schedule_items:
            add_absence_period(item.employee_id, item.start_date, item.end_date)

        pending_change_requests = VacationScheduleChangeRequest.objects.filter(
            employee_id__in=employee_ids,
            status=VacationScheduleChangeRequest.STATUS_PENDING,
            new_start_date__lte=end_date,
            new_end_date__gte=start_date,
        ).only("employee_id", "new_start_date", "new_end_date", "status")
        for change_request in pending_change_requests:
            add_absence_period(change_request.employee_id, change_request.new_start_date, change_request.new_end_date)

    forecasts = {}
    for group in groups:
        group_staff_count = len(staffing_context["staff_ids_by_group"].get(group.id, set()))
        coverage_rule = staffing_context["coverage_rule_by_group"].get(group.id)
        forecasts[group.id] = _empty_group_staffing_forecast(
            start_date,
            end_date,
            group,
            group_staff_count,
            coverage_rule=coverage_rule,
        )

    for current_date in iterate_dates(start_date, end_date):
        absent_employee_ids = absent_by_day.get(current_date, set())
        for group in groups:
            forecast = forecasts[group.id]
            coverage_rule = staffing_context["coverage_rule_by_group"].get(group.id)
            staffing_evaluation = _evaluate_group_staffing_forecast_state(
                staffing_context,
                group,
                absent_employee_ids,
                coverage_rule,
            )
            forecast["peak_absent_count"] = max(
                forecast["peak_absent_count"],
                staffing_evaluation["absent_count"],
            )
            forecast["min_remaining_staff_count"] = min(
                forecast["min_remaining_staff_count"],
                staffing_evaluation["remaining_staff_count"],
            )
            forecast["min_reserve_count"] = min(
                forecast["min_reserve_count"],
                staffing_evaluation["reserve_count"],
            )
            if staffing_evaluation["hard_conflict"]:
                forecast["_conflict_dates"].add(current_date)

            for issue in staffing_evaluation["issues"]:
                _add_staffing_forecast_reason(forecast, current_date, issue)

    return {
        group_id: _finalize_group_staffing_forecast(forecast)
        for group_id, forecast in forecasts.items()
    }


def build_department_staffing_forecast_map(
    departments,
    *,
    start_date=None,
    end_date=None,
    window_days=STAFFING_FORECAST_WINDOW_DAYS,
    staffing_contexts=None,
):
    departments = list(departments)
    department_ids = [department.id for department in departments]
    if not department_ids:
        return {}

    start_date = start_date or timezone.localdate()
    if end_date is None:
        end_date = start_date + timedelta(days=max(int(window_days or 1), 1) - 1)

    if staffing_contexts is None:
        staffing_contexts = build_department_staffing_context_map(departments, end_date)
    employee_department_ids = {}
    for department_id, staffing_context in staffing_contexts.items():
        for employee_id in staffing_context["staff_ids"]:
            employee_department_ids[employee_id] = department_id

    employee_ids = set(employee_department_ids)
    absent_by_department_day = defaultdict(set)

    def add_absence_period(employee_id, absence_start, absence_end):
        department_id = employee_department_ids.get(employee_id)
        if department_id is None:
            return

        clipped_period = clip_period_to_range(absence_start, absence_end, start_date, end_date)
        if clipped_period is None:
            return

        clipped_start, clipped_end = clipped_period
        for current_date in iterate_dates(clipped_start, clipped_end):
            absent_by_department_day[(department_id, current_date)].add(employee_id)

    if employee_ids:
        active_requests = VacationRequest.objects.filter(
            employee_id__in=employee_ids,
            status__in=ACTIVE_REQUEST_STATUSES,
            start_date__lte=end_date,
            end_date__gte=start_date,
        ).only("employee_id", "start_date", "end_date", "status", "vacation_type")
        active_requests = exclude_converted_paid_requests(
            active_requests,
            employee_ids=employee_ids,
            start_date=start_date,
            end_date=end_date,
        )
        for request_obj in active_requests:
            add_absence_period(request_obj.employee_id, request_obj.start_date, request_obj.end_date)

        active_schedule_items = VacationScheduleItem.objects.filter(
            employee_id__in=employee_ids,
            status__in=VacationScheduleItem.ACTIVE_STATUSES,
            start_date__lte=end_date,
            end_date__gte=start_date,
        ).only("employee_id", "start_date", "end_date", "status")
        for item in active_schedule_items:
            add_absence_period(item.employee_id, item.start_date, item.end_date)

        pending_change_requests = VacationScheduleChangeRequest.objects.filter(
            employee_id__in=employee_ids,
            status=VacationScheduleChangeRequest.STATUS_PENDING,
            new_start_date__lte=end_date,
            new_end_date__gte=start_date,
        ).only("employee_id", "new_start_date", "new_end_date", "status")
        for change_request in pending_change_requests:
            add_absence_period(change_request.employee_id, change_request.new_start_date, change_request.new_end_date)

    staffing_rules = {
        department.id: get_department_staffing_rule(department)
        for department in departments
    }
    workloads = DepartmentWorkload.objects.filter(
        department_id__in=department_ids,
        year__gte=start_date.year,
        year__lte=end_date.year,
    )
    workload_by_department_month = {
        (workload.department_id, workload.year, workload.month): workload
        for workload in workloads
    }
    department_by_id = {department.id: department for department in departments}
    forecasts = {
        department_id: _empty_department_staffing_forecast(
            start_date,
            end_date,
            staffing_context=staffing_contexts[department_id],
        )
        for department_id in department_ids
    }

    for current_date in iterate_dates(start_date, end_date):
        for department_id in department_ids:
            department = department_by_id[department_id]
            staffing_context = staffing_contexts[department_id]
            absent_employee_ids = absent_by_department_day.get((department_id, current_date), set())
            min_staff_required, max_absent = get_staffing_limits_for_date(
                department,
                current_date,
                workload_by_department_month,
                staffing_rules,
            )
            staffing_evaluation = evaluate_department_staffing_state(
                staffing_context,
                absent_employee_ids,
                min_staff_required=min_staff_required,
                max_absent=max_absent,
                include_limit_warnings=True,
            )
            forecast = forecasts[department_id]
            forecast["peak_absent_count"] = max(
                forecast["peak_absent_count"],
                staffing_evaluation["absent_count"],
            )
            forecast["min_remaining_staff_count"] = min(
                forecast["min_remaining_staff_count"],
                staffing_evaluation["remaining_staff_count"],
            )
            reserve_count = staffing_evaluation["remaining_staff_count"] - staffing_evaluation["min_staff_required"]
            forecast["min_reserve_count"] = min(forecast["min_reserve_count"], reserve_count)
            if staffing_evaluation["hard_conflict"]:
                forecast["_conflict_dates"].add(current_date)

            forecast_issues = list(staffing_evaluation["issues"])
            forecast_issues.extend(_get_group_minimum_forecast_issues(staffing_context, absent_employee_ids))
            for issue in forecast_issues:
                _add_staffing_forecast_reason(forecast, current_date, issue)

    return {
        department_id: _finalize_department_staffing_forecast(forecast)
        for department_id, forecast in forecasts.items()
    }


def get_enterprise_leadership_employee_ids(as_of_date):
    enterprise_head_ids = set(
        Employees.objects.filter(
            role=Employees.ROLE_ENTERPRISE_HEAD,
            is_active_employee=True,
            date_joined__lte=as_of_date,
        )
        .exclude(role__in=Employees.SERVICE_ROLES)
        .values_list("id", flat=True)
    )
    enterprise_deputy_ids = set(
        Employees.objects.filter(
            is_enterprise_deputy=True,
            is_active_employee=True,
            date_joined__lte=as_of_date,
        )
        .exclude(role__in=Employees.SERVICE_ROLES)
        .values_list("id", flat=True)
    )
    return enterprise_head_ids, enterprise_deputy_ids


def evaluate_enterprise_leadership_state(
    absent_employee_ids,
    as_of_date,
    *,
    target_employee=None,
    enterprise_head_ids=None,
    enterprise_deputy_ids=None,
):
    absent_employee_ids = set(absent_employee_ids or [])
    if enterprise_head_ids is None or enterprise_deputy_ids is None:
        enterprise_head_ids, enterprise_deputy_ids = get_enterprise_leadership_employee_ids(as_of_date)
    else:
        enterprise_head_ids = set(enterprise_head_ids)
        enterprise_deputy_ids = set(enterprise_deputy_ids)
    absent_heads = enterprise_head_ids & absent_employee_ids
    absent_deputies = enterprise_deputy_ids & absent_employee_ids
    target_id = getattr(target_employee, "id", None)
    target_is_pair_member = target_id is None or target_id in enterprise_head_ids or target_id in enterprise_deputy_ids
    if not absent_heads or not absent_deputies or not target_is_pair_member:
        return {"issues": [], "hard_conflict": False, "leadership_boost": 0}

    return {
        "issues": [
            _staffing_issue(
                "enterprise_leadership_pair",
                "conflict",
                "Нет пары управления предприятием",
                "Руководитель предприятия и заместитель будут отсутствовать одновременно.",
                affected_employee_ids=absent_heads | absent_deputies,
            )
        ],
        "hard_conflict": True,
        "leadership_boost": 24,
    }
