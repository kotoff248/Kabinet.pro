from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.db.models import Count
from django.urls import reverse
from django.utils import timezone

from apps.employees.models import Employees
from apps.leave.models import (
    DepartmentWorkload,
    VacationPreference,
    VacationRequest,
    VacationUrgentClosureRequest,
    VacationScheduleChangeRequest,
    VacationScheduleItem,
)

from .calendar import build_calendar_base_data, build_calendar_rows
from .constants import RUSSIAN_MONTH_NAMES, RUSSIAN_MONTH_SHORT_NAMES
from .dates import get_month_end, get_month_range, get_overlap_days
from .ledger import get_employee_list_leave_summaries
from .querysets import exclude_converted_paid_requests


MODULE_RECOMMENDATION_LABELS = {
    "prefer": "спокойный месяц",
    "normal": "под контролем",
    "avoid": "лучше проверить",
    "blocked": "есть ограничения",
}

MODULE_RECOMMENDATION_VARIANTS = {
    "prefer": "planned",
    "normal": "info",
    "avoid": "risk",
    "blocked": "conflict",
}

MODULE_SOURCE_LABELS = {
    "schedule": "График",
    "request": "Заявка",
    "transfer": "Перенос",
    "urgent_closure": "Срочное закрытие",
}

MODULE_MONTH_BASE_SCORE = Decimal("96.00")
MODULE_MONTH_MIN_CALM_SCORE = Decimal("82.00")
MODULE_MONTH_MIN_LOAD_SCORE = Decimal("55.00")
MODULE_MONTH_RISK_CAP = Decimal("59.00")
MODULE_MONTH_CONFLICT_CAP = Decimal("34.00")


def _percent(part, total):
    return round((part / total) * 100) if total else 0


def _format_decimal(value):
    value = Decimal(value or 0).quantize(Decimal("0.01"))
    return int(value) if value == value.to_integral_value() else value


def _module_score(value):
    if value is None:
        return None
    try:
        score = Decimal(str(value))
    except Exception:
        return None
    return max(Decimal("0.00"), min(Decimal("100.00"), score)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _module_score_label(value):
    score = _module_score(value)
    if score is None:
        return ""
    text = f"{score:.2f}".replace(".", ",")
    return f"{text}%"


def _module_ratio(part, total):
    if not total:
        return Decimal("0.00")
    return (Decimal(part or 0) / Decimal(total)) * Decimal("100.00")


def _module_recommendation_for_score(score):
    score = _module_score(score)
    if score is None:
        return "normal"
    if score < Decimal("35.00"):
        return "blocked"
    if score < Decimal("60.00"):
        return "avoid"
    if score < Decimal("80.00"):
        return "normal"
    return "prefer"


def _module_snapshot_payload(*, department_id, start_date, end_date, score, recommendation="", source_kind=""):
    score = _module_score(score)
    if not department_id or start_date is None or end_date is None or score is None:
        return None
    if end_date < start_date:
        return None
    return {
        "department_id": department_id,
        "start_date": start_date,
        "end_date": end_date,
        "score": score,
        "recommendation": recommendation or _module_recommendation_for_score(score),
        "source_kind": source_kind,
    }


def _normalize_employee_ids(employee_ids):
    if employee_ids is not None:
        return list(employee_ids)

    return list(
        Employees.objects.filter(is_active_employee=True)
        .exclude(role__in=Employees.SERVICE_ROLES)
        .values_list("id", flat=True)
    )


def _overlaps_year_queryset(queryset, year, start_field="start_date", end_field="end_date"):
    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)
    return queryset.filter(**{f"{start_field}__lte": year_end, f"{end_field}__gte": year_start})


def _calendar_url(year, department_id=None, issue="all"):
    params = {"view": "year", "year": year, "from": "analytics"}
    if department_id:
        params["department"] = department_id
    if issue and issue != "all":
        params["issue"] = issue
    return f"{reverse('calendar')}?{urlencode(params)}"


def _analytics_profile_url(employee_id):
    return f"{reverse('employee_profile', args=[employee_id])}?{urlencode({'from': 'analytics'})}"


def _profile_url_from_analytics(profile_url):
    if not profile_url:
        return profile_url
    parsed = urlsplit(profile_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["from"] = "analytics"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _get_absent_employee_ids(employee_ids, current_date):
    employee_ids = set(employee_ids)
    if not employee_ids:
        return set()

    approved_absence_requests = VacationRequest.objects.filter(
        employee_id__in=employee_ids,
        status=VacationRequest.STATUS_APPROVED,
        start_date__lte=current_date,
        end_date__gte=current_date,
    )
    approved_absence_requests = exclude_converted_paid_requests(
        approved_absence_requests,
        employee_ids=employee_ids,
        start_date=current_date,
        end_date=current_date,
    )
    absent_employee_ids = set(approved_absence_requests.values_list("employee_id", flat=True))
    absent_employee_ids.update(
        VacationScheduleItem.objects.filter(
            employee_id__in=employee_ids,
            status__in=VacationScheduleItem.ACTIVE_STATUSES,
            start_date__lte=current_date,
            end_date__gte=current_date,
        ).values_list("employee_id", flat=True)
    )
    return absent_employee_ids


def _empty_month_row(month_number):
    return {
        "month_number": month_number,
        "month_short": RUSSIAN_MONTH_SHORT_NAMES[month_number - 1],
        "month_name": RUSSIAN_MONTH_NAMES[month_number - 1],
        "vacation_count": 0,
        "duration_total": 0,
        "average_duration": 0,
        "planned_days": 0,
        "schedule_days": 0,
        "request_days": 0,
        "changed_days": 0,
        "pending_days": 0,
        "rejected_days": 0,
        "medium_risk_count": 0,
        "high_risk_count": 0,
        "conflict_count": 0,
        "employee_ids": set(),
    }


def _entry_is_active(entry):
    if entry.get("source_kind") == "schedule":
        return entry.get("schedule_status") in VacationScheduleItem.ACTIVE_STATUSES
    if entry.get("source_kind") == "request":
        return entry.get("status") in VacationRequest.ACTIVE_STATUSES
    return False


def _build_monthly_metrics(employee_entries, calendar_rows, year):
    months = [_empty_month_row(month_number) for month_number in range(1, 13)]
    planned_employee_ids = set()
    active_absence_employee_ids = set()

    for entries in employee_entries.values():
        for entry in entries:
            if _entry_is_active(entry):
                active_absence_employee_ids.add(entry["employee_id"])
            if (
                entry.get("source_kind") == "schedule"
                and entry.get("schedule_status") in VacationScheduleItem.ACTIVE_STATUSES
            ):
                planned_employee_ids.add(entry["employee_id"])

            for month_start in get_month_range(entry["start_date"], entry["end_date"]):
                if month_start.year != year:
                    continue

                month_index = month_start.month - 1
                month = months[month_index]
                overlap_days = get_overlap_days(
                    entry["start_date"],
                    entry["end_date"],
                    month_start,
                    get_month_end(month_start),
                )
                month["vacation_count"] += 1
                month["duration_total"] += overlap_days
                month["planned_days"] += overlap_days
                month["employee_ids"].add(entry["employee_id"])

                if entry.get("source_kind") == "schedule":
                    if entry.get("schedule_status") in VacationScheduleItem.ACTIVE_STATUSES:
                        month["schedule_days"] += overlap_days
                    elif entry.get("schedule_status") in {
                        VacationScheduleItem.STATUS_TRANSFERRED,
                        VacationScheduleItem.STATUS_CANCELLED,
                    }:
                        month["changed_days"] += overlap_days
                else:
                    if entry.get("status") == VacationRequest.STATUS_APPROVED:
                        month["request_days"] += overlap_days
                    elif entry.get("status") == VacationRequest.STATUS_PENDING:
                        month["pending_days"] += overlap_days
                    elif entry.get("status") == VacationRequest.STATUS_REJECTED:
                        month["rejected_days"] += overlap_days

                if entry.get("risk_level") == VacationRequest.RISK_HIGH:
                    month["high_risk_count"] += 1
                elif entry.get("risk_level") == VacationRequest.RISK_MEDIUM:
                    month["medium_risk_count"] += 1

    for row in calendar_rows:
        for cell in row.get("cells") or []:
            month_index = cell.get("month_number", 0) - 1
            if 0 <= month_index < 12 and cell.get("has_conflict"):
                months[month_index]["conflict_count"] += 1

    for month in months:
        if month["vacation_count"]:
            month["average_duration"] = round(month["duration_total"] / month["vacation_count"], 2)
        month["employee_count"] = len(month["employee_ids"])
        month["issue_count"] = month["high_risk_count"] + month["conflict_count"]
        month["employee_ids"] = sorted(month["employee_ids"])

    return months, planned_employee_ids, active_absence_employee_ids


def _status_for_department_month(month):
    if month["conflict_count"]:
        return "conflict"
    if month["risk_count"]:
        return "risk"
    if (
        month["absent_count"]
        or month["load_level"] >= 4
        or month["near_limit"]
        or month["breaks_min_staff"]
        or month["exceeds_absent_limit"]
    ):
        return "watch"
    return "stable"


def _build_department_heatmap(employees, calendar_rows, year, employee_entries=None):
    employee_by_id = {employee.id: employee for employee in employees}
    employee_entries = employee_entries or {}
    departments_by_id = {
        employee.department_id: employee.department
        for employee in employees
        if employee.department_id and employee.department
    }
    missing_department_id = 0
    if any(employee.department_id is None for employee in employees):
        departments_by_id[missing_department_id] = None

    employee_ids_by_department = defaultdict(set)
    for employee in employees:
        department_id = employee.department_id or missing_department_id
        employee_ids_by_department[department_id].add(employee.id)

    department_ids = [department_id for department_id in departments_by_id if department_id]
    workload_by_department_month = {
        (workload.department_id, workload.month): workload
        for workload in DepartmentWorkload.objects.filter(department_id__in=department_ids, year=year)
    }

    stats = {}
    for department_id, department in departments_by_id.items():
        total_employees = len(employee_ids_by_department[department_id])
        staffing_rule = getattr(department, "staffing_rule", None) if department else None
        months = []
        for month_number in range(1, 13):
            workload = workload_by_department_month.get((department_id, month_number))
            min_staff_required = (
                workload.min_staff_required
                if workload is not None
                else (staffing_rule.min_staff_required if staffing_rule else 0)
            )
            max_absent = (
                workload.max_absent
                if workload is not None
                else (staffing_rule.max_absent if staffing_rule else 1)
            )
            months.append(
                {
                    "month_number": month_number,
                    "month_short": RUSSIAN_MONTH_SHORT_NAMES[month_number - 1],
                    "month_name": RUSSIAN_MONTH_NAMES[month_number - 1],
                    "load_level": workload.load_level if workload else 1,
                    "min_staff_required": min_staff_required,
                    "max_absent": max_absent,
                    "absent_employee_ids": set(),
                    "busy_days": 0,
                    "medium_risk_count": 0,
                    "risk_count": 0,
                    "conflict_count": 0,
                    "remaining_staff": total_employees,
                    "status": "stable",
                    "intensity": 0,
                    "near_limit": False,
                    "breaks_min_staff": False,
                    "exceeds_absent_limit": False,
                }
            )

        stats[department_id] = {
            "department_id": department_id,
            "department_name": department.name if department else "Без отдела",
            "employees_count": total_employees,
            "months": months,
        }

    for row in calendar_rows:
        employee = employee_by_id.get(row["employee_id"])
        if employee is None:
            continue
        department_id = employee.department_id or missing_department_id
        department_stats = stats.get(department_id)
        if department_stats is None:
            continue

        for cell in row.get("cells") or []:
            month_index = cell.get("month_number", 0) - 1
            if not 0 <= month_index < 12:
                continue
            month = department_stats["months"][month_index]
            busy_days = int(cell.get("busy_days") or 0)
            if busy_days:
                month["absent_employee_ids"].add(row["employee_id"])
                month["busy_days"] += busy_days
            if cell.get("has_high_risk"):
                month["risk_count"] += 1
            if cell.get("has_conflict"):
                month["conflict_count"] += 1

    for employee_id, entries in employee_entries.items():
        employee = employee_by_id.get(employee_id)
        if employee is None:
            continue
        department_id = employee.department_id or missing_department_id
        department_stats = stats.get(department_id)
        if department_stats is None:
            continue

        for entry in entries:
            if entry.get("risk_level") != VacationRequest.RISK_MEDIUM or not _entry_is_active(entry):
                continue
            for month_start in get_month_range(entry["start_date"], entry["end_date"]):
                if month_start.year != year:
                    continue
                department_stats["months"][month_start.month - 1]["medium_risk_count"] += 1

    for department in stats.values():
        for month in department["months"]:
            absent_count = len(month["absent_employee_ids"])
            month["absent_count"] = absent_count
            month["remaining_staff"] = max(department["employees_count"] - absent_count, 0)
            month["breaks_min_staff"] = bool(
                month["min_staff_required"] and month["remaining_staff"] < month["min_staff_required"]
            )
            month["exceeds_absent_limit"] = bool(month["max_absent"] and absent_count > month["max_absent"])
            month["near_limit"] = bool(
                absent_count
                and (
                    (month["max_absent"] and absent_count >= month["max_absent"])
                    or (month["min_staff_required"] and month["remaining_staff"] <= month["min_staff_required"])
                )
            )
            month["status"] = _status_for_department_month(month)
            month["intensity"] = min(
                100,
                (month["load_level"] * 10)
                + (month["medium_risk_count"] * 5)
                + (month["risk_count"] * 12)
                + (month["conflict_count"] * 18)
                + (_percent(absent_count, department["employees_count"]) // 2 if department["employees_count"] else 0),
            )
            month["absent_employee_ids"] = sorted(month["absent_employee_ids"])

        peak_month = max(department["months"], key=lambda item: (item["absent_count"], item["busy_days"]))
        department["busy_days"] = sum(month["busy_days"] for month in department["months"])
        department["medium_risk_count"] = sum(month["medium_risk_count"] for month in department["months"])
        department["risk_count"] = sum(month["risk_count"] for month in department["months"])
        department["conflict_count"] = sum(month["conflict_count"] for month in department["months"])
        department["planned_people_count"] = len(
            {
                employee_id
                for month in department["months"]
                for employee_id in month["absent_employee_ids"]
            }
        )
        department["peak_month_label"] = peak_month["month_short"]
        department["peak_absent_count"] = peak_month["absent_count"]
        department["average_load"] = round(
            sum(month["load_level"] for month in department["months"]) / 12,
            1,
        )
        department["status"] = (
            "conflict"
            if department["conflict_count"]
            else ("risk" if department["risk_count"] else ("watch" if department["busy_days"] else "stable"))
        )
        department["calendar_url"] = _calendar_url(year, department["department_id"] or None, "conflict" if department["conflict_count"] else "all")

    return sorted(
        stats.values(),
        key=lambda item: (-item["conflict_count"], -item["risk_count"], item["department_name"]),
    )


def _effective_request_ai_snapshot(request_obj):
    if request_obj.status == VacationRequest.STATUS_PENDING:
        return request_obj.ai_score, request_obj.ai_recommendation
    if request_obj.decision_ai_score is not None:
        return request_obj.decision_ai_score, request_obj.decision_ai_recommendation
    return request_obj.ai_score, request_obj.ai_recommendation


def _effective_schedule_change_ai_snapshot(change_request):
    if change_request.status == VacationScheduleChangeRequest.STATUS_PENDING:
        return change_request.ai_score, change_request.ai_recommendation
    if change_request.decision_ai_score is not None:
        return change_request.decision_ai_score, change_request.decision_ai_recommendation
    return change_request.ai_score, change_request.ai_recommendation


def _effective_urgent_closure_ai_snapshot(closure_request):
    if closure_request.status in VacationUrgentClosureRequest.ACTIVE_STATUSES:
        return closure_request.ai_score, closure_request.ai_recommendation
    if closure_request.decision_ai_score is not None:
        return closure_request.decision_ai_score, closure_request.decision_ai_recommendation
    return closure_request.ai_score, closure_request.ai_recommendation


def _add_snapshot_to_month_index(snapshot_index, snapshot, year):
    if snapshot is None:
        return
    for month_start in get_month_range(snapshot["start_date"], snapshot["end_date"]):
        if month_start.year != year:
            continue
        snapshot_index[(snapshot["department_id"], month_start.month)].append(snapshot)


def _build_module_snapshot_index(employee_ids, year):
    employee_ids = list(employee_ids)
    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)
    snapshot_index = defaultdict(list)

    request_records = VacationRequest.objects.select_related("employee").filter(
        employee_id__in=employee_ids,
        start_date__lte=year_end,
        end_date__gte=year_start,
    )
    request_records = exclude_converted_paid_requests(
        request_records,
        employee_ids=employee_ids,
        start_date=year_start,
        end_date=year_end,
    )
    for request_obj in request_records:
        score, recommendation = _effective_request_ai_snapshot(request_obj)
        _add_snapshot_to_month_index(
            snapshot_index,
            _module_snapshot_payload(
                department_id=request_obj.employee.department_id,
                start_date=request_obj.start_date,
                end_date=request_obj.end_date,
                score=score,
                recommendation=recommendation,
                source_kind="request",
            ),
            year,
        )

    schedule_items = VacationScheduleItem.objects.select_related("employee").filter(
        employee_id__in=employee_ids,
        start_date__lte=year_end,
        end_date__gte=year_start,
        status__in=(VacationScheduleItem.STATUS_DRAFT, *VacationScheduleItem.ACTIVE_STATUSES),
        ai_score__isnull=False,
    )
    for item in schedule_items:
        _add_snapshot_to_month_index(
            snapshot_index,
            _module_snapshot_payload(
                department_id=item.employee.department_id,
                start_date=item.start_date,
                end_date=item.end_date,
                score=item.ai_score,
                source_kind="schedule",
            ),
            year,
        )

    change_requests = VacationScheduleChangeRequest.objects.select_related("employee").filter(
        employee_id__in=employee_ids,
        new_start_date__lte=year_end,
        new_end_date__gte=year_start,
    )
    for change_request in change_requests:
        score, recommendation = _effective_schedule_change_ai_snapshot(change_request)
        _add_snapshot_to_month_index(
            snapshot_index,
            _module_snapshot_payload(
                department_id=change_request.employee.department_id,
                start_date=change_request.new_start_date,
                end_date=change_request.new_end_date,
                score=score,
                recommendation=recommendation,
                source_kind="transfer",
            ),
            year,
        )

    urgent_closures = VacationUrgentClosureRequest.objects.select_related("employee").filter(
        employee_id__in=employee_ids,
        proposed_start_date__lte=year_end,
        proposed_end_date__gte=year_start,
    )
    for closure_request in urgent_closures:
        score, recommendation = _effective_urgent_closure_ai_snapshot(closure_request)
        _add_snapshot_to_month_index(
            snapshot_index,
            _module_snapshot_payload(
                department_id=closure_request.employee.department_id,
                start_date=closure_request.proposed_start_date,
                end_date=closure_request.proposed_end_date,
                score=score,
                recommendation=recommendation,
                source_kind="urgent_closure",
            ),
            year,
        )

    return snapshot_index


def _module_snapshot_source_label(snapshots):
    counts = defaultdict(int)
    for snapshot in snapshots:
        counts[snapshot["source_kind"]] += 1
    parts = [
        f"{MODULE_SOURCE_LABELS.get(source_kind, 'Источник')} {count}"
        for source_kind, count in sorted(counts.items(), key=lambda item: MODULE_SOURCE_LABELS.get(item[0], item[0]))
    ]
    return ", ".join(parts)


def _module_status_for_month(month):
    has_absence_pressure = bool(
        month["absent_count"]
        or month["busy_days"]
        or month.get("medium_risk_count", 0)
        or month["risk_count"]
        or month["conflict_count"]
    )
    if month["conflict_count"]:
        return "conflict"
    if month["risk_count"]:
        return "risk"
    if (
        has_absence_pressure
        and (
            month["near_limit"]
            or month["breaks_min_staff"]
            or month["exceeds_absent_limit"]
            or month["load_level"] >= 5
        )
    ):
        return "watch"
    return "stable"


def _module_reason_for_month(month, snapshots, score, module_status, actionable_snapshots=None):
    actionable_snapshots = actionable_snapshots or []
    if module_status == "conflict":
        return f'Конфликтов состава: {month["conflict_count"]}.'

    if module_status == "risk":
        reasons = []
        if month["risk_count"]:
            reasons.append(f'высоких рисков: {month["risk_count"]}')
        if month["load_level"] >= 5:
            reasons.append("нагрузка 5/5")
        if month["near_limit"]:
            reasons.append("месяц близко к лимиту состава")
        if month.get("medium_risk_count", 0):
            reasons.append(f'средних предупреждений: {month["medium_risk_count"]}')
        return "Стоит проверить: " + ", ".join(reasons or ["есть напряжение по графику"]) + "."

    if actionable_snapshots and score < Decimal("60.00"):
        source_label = _module_snapshot_source_label(actionable_snapshots)
        return f"Сохраненные ML-снимки ниже комфортного уровня: {source_label}."

    if module_status == "watch":
        if month["exceeds_absent_limit"]:
            return (
                f'Отпускная активность выше ориентира отдела: отсутствуют {month["absent_count"]}, '
                f'ориентир {month["max_absent"]}.'
            )
        if month["breaks_min_staff"]:
            return (
                f'Отпускная активность выше ориентира отдела: останется {month["remaining_staff"]}, '
                f'ориентир по составу {month["min_staff_required"]}.'
            )
        if not month["absent_count"] and not month["busy_days"]:
            return f'Высокая рабочая нагрузка отдела: {month["load_level"]}/5, отпускных конфликтов не видно.'
        if month.get("medium_risk_count", 0) >= 3:
            return (
                f'Есть отпускная активность и средние предупреждения: '
                f'{month["medium_risk_count"]}, отсутствуют {month["absent_count"]}.'
            )
        return f'Есть отпускная активность: отсутствуют {month["absent_count"]}, нагрузка {month["load_level"]}/5.'

    if snapshots:
        return f"Учтены сохраненные ML-снимки: {_module_snapshot_source_label(snapshots)}."
    return "Жестких рисков по графику не видно."


def _module_department_size(department, month):
    employees_count = department.get("employees_count")
    if employees_count:
        return employees_count
    return (month.get("remaining_staff") or 0) + (month.get("absent_count") or 0)


def _module_month_score(department, month, snapshots, module_status, year):
    employees_count = _module_department_size(department, month)
    month_days = get_month_end(date(year, month["month_number"], 1)).day
    capacity_days = employees_count * month_days
    absent_ratio = _module_ratio(month["absent_count"], employees_count)
    busy_ratio = _module_ratio(month["busy_days"], capacity_days)

    score = MODULE_MONTH_BASE_SCORE
    score -= min(absent_ratio * Decimal("0.27"), Decimal("14.00"))
    score -= min(busy_ratio * Decimal("0.22"), Decimal("8.00"))
    score -= Decimal(max((month.get("load_level") or 1) - 1, 0)) * Decimal("1.35")

    if month["max_absent"] and month["absent_count"]:
        limit_ratio = Decimal(month["absent_count"]) / Decimal(month["max_absent"])
        if limit_ratio > 1:
            score -= min((limit_ratio - 1) * Decimal("8.00"), Decimal("6.50"))
        elif limit_ratio >= Decimal("0.85"):
            score -= Decimal("1.50")
    if month["breaks_min_staff"]:
        staff_gap = max((month["min_staff_required"] or 0) - (month["remaining_staff"] or 0), 1)
        score -= min(Decimal(staff_gap) * Decimal("1.50"), Decimal("6.00"))

    score -= min(Decimal(month["risk_count"]) * Decimal("15.00"), Decimal("36.00"))
    score -= min(Decimal(month.get("medium_risk_count", 0)) * Decimal("2.20"), Decimal("13.00"))
    score -= min(Decimal(month["conflict_count"]) * Decimal("28.00"), Decimal("56.00"))

    actionable_snapshots = [snapshot for snapshot in snapshots if snapshot["source_kind"] != "schedule"]
    if module_status in {"risk", "conflict"} and snapshots:
        snapshot_score = sum(snapshot["score"] for snapshot in snapshots) / Decimal(len(snapshots))
        score = min(score, snapshot_score)
    elif len(actionable_snapshots) >= 2:
        snapshot_score = sum(snapshot["score"] for snapshot in actionable_snapshots) / Decimal(len(actionable_snapshots))
        if snapshot_score < Decimal("60.00"):
            score -= min((Decimal("60.00") - snapshot_score) / Decimal("4.00"), Decimal("6.00"))

    if month["conflict_count"]:
        score = min(score, MODULE_MONTH_CONFLICT_CAP)
    elif month["risk_count"]:
        score = min(score, MODULE_MONTH_RISK_CAP)
    elif module_status == "watch":
        score = max(score, MODULE_MONTH_MIN_LOAD_SCORE)
    else:
        score = max(score, MODULE_MONTH_MIN_CALM_SCORE)

    return _module_score(score)


def _module_payload_for_department_month(department, month, snapshots, year):
    module_status = _module_status_for_month(month)
    actionable_snapshots = [snapshot for snapshot in snapshots if snapshot["source_kind"] != "schedule"]
    score = _module_month_score(department, month, snapshots, module_status, year)
    recommendation = _module_recommendation_for_score(score)
    variant = MODULE_RECOMMENDATION_VARIANTS.get(recommendation, "info")
    issue = "conflict" if module_status == "conflict" else ("risk" if module_status == "risk" else "all")
    attention_score = (
        (Decimal("100.00") - score)
        + Decimal(month["conflict_count"] * 28)
        + Decimal(month["risk_count"] * 12)
        + Decimal(month.get("medium_risk_count", 0) * 4)
        + Decimal(max(month["load_level"] - 1, 0) * 3)
        + Decimal(month["absent_count"] * 2)
    )
    has_attention = (
        variant in {"risk", "conflict"}
        or module_status in {"risk", "conflict"}
    )
    reason = _module_reason_for_month(month, snapshots, score, module_status, actionable_snapshots)
    return {
        "department_id": department["department_id"],
        "department_name": department["department_name"],
        "month_number": month["month_number"],
        "month_short": month["month_short"],
        "month_name": month["month_name"],
        "title": f'{month["month_name"]} · {department["department_name"]}',
        "score": score,
        "score_label": _module_score_label(score),
        "recommendation": recommendation,
        "recommendation_label": MODULE_RECOMMENDATION_LABELS.get(recommendation, "под контролем"),
        "variant": variant,
        "module_status": module_status,
        "reason": reason,
        "calendar_url": _calendar_url(year, department["department_id"] or None, issue),
        "snapshot_count": len(snapshots),
        "snapshot_source_label": _module_snapshot_source_label(snapshots) if snapshots else "",
        "attention_score": attention_score,
        "has_attention": has_attention,
        "absent_count": month["absent_count"],
        "load_level": month["load_level"],
        "medium_risk_count": month.get("medium_risk_count", 0),
        "risk_count": month["risk_count"],
        "conflict_count": month["conflict_count"],
    }


def _build_module_monthly_rollup(cells, year):
    rollup = []
    cells_by_month = defaultdict(list)
    for cell in cells:
        cells_by_month[cell["month_number"]].append(cell)

    for month_number in range(1, 13):
        month_cells = cells_by_month.get(month_number) or []
        attention_cells = [cell for cell in month_cells if cell["has_attention"]]
        if attention_cells:
            worst_cell = min(attention_cells, key=lambda item: item["score"])
        elif month_cells:
            visible_cells = [cell for cell in month_cells if cell["department_id"]] or month_cells
            worst_cell = min(
                visible_cells,
                key=lambda item: (item["score"], -item["absent_count"], item["department_name"]),
            )
        else:
            worst_cell = None

        if worst_cell:
            rollup.append(
                {
                    "month_number": month_number,
                    "month_short": RUSSIAN_MONTH_SHORT_NAMES[month_number - 1],
                    "score_label": worst_cell["score_label"],
                    "recommendation_label": worst_cell["recommendation_label"],
                    "variant": worst_cell["variant"],
                    "department_name": worst_cell["department_name"],
                    "calendar_url": worst_cell["calendar_url"],
                    "has_attention": worst_cell["has_attention"],
                }
            )
        else:
            rollup.append(
                {
                    "month_number": month_number,
                    "month_short": RUSSIAN_MONTH_SHORT_NAMES[month_number - 1],
                    "score_label": "96,00%",
                    "recommendation_label": "спокойный месяц",
                    "variant": "planned",
                    "department_name": "",
                    "calendar_url": _calendar_url(year),
                    "has_attention": False,
                }
            )
    return rollup


def _build_module_summary(department_heatmap, employee_ids, year):
    snapshot_index = _build_module_snapshot_index(employee_ids, year)
    cells = []

    for department in department_heatmap:
        department_id = department["department_id"]
        for month in department["months"]:
            snapshots = snapshot_index.get((department_id, month["month_number"]), [])
            module_cell = _module_payload_for_department_month(department, month, snapshots, year)
            month["module_score"] = module_cell["score"]
            month["module_score_label"] = module_cell["score_label"]
            month["module_recommendation_label"] = module_cell["recommendation_label"]
            month["module_variant"] = module_cell["variant"]
            month["module_reason"] = module_cell["reason"]
            month["module_snapshot_count"] = module_cell["snapshot_count"]
            cells.append(module_cell)

    top_cells = sorted(
        (cell for cell in cells if cell["has_attention"]),
        key=lambda item: (-item["attention_score"], item["score"], item["department_name"], item["month_number"]),
    )[:5]
    headline = top_cells[0] if top_cells else {
        "title": "Критичных месяцев модуль не видит",
        "department_name": "",
        "month_name": "",
        "score_label": "",
        "recommendation_label": "под контролем",
        "variant": "planned",
        "reason": "По выбранному году нет месяцев с конфликтом, низкими ML-оценками или перегрузкой отдела.",
        "calendar_url": _calendar_url(year),
    }

    return {
        "headline": headline,
        "top_cells": top_cells,
        "monthly_rollup": _build_module_monthly_rollup(cells, year),
        "has_attention": bool(top_cells),
    }


def _build_balance_summary(employees, year):
    as_of_date = date(year, 12, 31)
    summaries = get_employee_list_leave_summaries(employees, as_of_date=as_of_date)
    totals = {
        "available": Decimal("0.00"),
        "reserved": Decimal("0.00"),
        "used": Decimal("0.00"),
        "accrued": Decimal("0.00"),
        "advance_available": Decimal("0.00"),
    }
    employee_rows = []

    for employee in employees:
        summary = summaries.get(employee.id, {})
        for key in totals:
            totals[key] += Decimal(summary.get(key, 0) or 0)
        available = Decimal(summary.get("available", 0) or 0)
        reserved = Decimal(summary.get("reserved", 0) or 0)
        used = Decimal(summary.get("used", 0) or 0)
        employee_rows.append(
            {
                "employee_id": employee.id,
                "employee_name": employee.full_name,
                "department_name": employee.department.name if employee.department else "Без отдела",
                "available": _format_decimal(available),
                "reserved": _format_decimal(reserved),
                "used": _format_decimal(used),
                "is_low": available <= Decimal("7.00"),
                "profile_url": _analytics_profile_url(employee.id),
            }
        )

    low_balance_rows = sorted(
        (row for row in employee_rows if row["is_low"]),
        key=lambda row: (Decimal(row["available"]), row["employee_name"]),
    )[:6]

    return {
        "totals": {key: _format_decimal(value) for key, value in totals.items()},
        "low_balance_count": sum(1 for row in employee_rows if row["is_low"]),
        "low_balance_rows": low_balance_rows,
        "as_of_label": f"на 31 декабря {year}",
    }


def _build_preference_summary(employee_ids, total_employees, year):
    preferences = VacationPreference.objects.filter(employee_id__in=employee_ids, year=year)
    status_counts = {
        row["status"]: row["count"]
        for row in preferences.values("status").annotate(count=Count("id"))
    }
    employee_ids_with_preference = set(preferences.values_list("employee_id", flat=True).distinct())
    filled_employee_ids = set(
        preferences.filter(status=VacationPreference.STATUS_FILLED).values_list("employee_id", flat=True).distinct()
    )
    skipped_employee_ids = set(
        preferences.filter(status=VacationPreference.STATUS_SKIPPED).values_list("employee_id", flat=True).distinct()
    )
    pending_employee_ids = set(
        preferences.filter(status=VacationPreference.STATUS_PENDING).values_list("employee_id", flat=True).distinct()
    )
    missing_count = max(total_employees - len(employee_ids_with_preference), 0)
    ready_count = len(filled_employee_ids | skipped_employee_ids)
    attention_count = missing_count + len(pending_employee_ids)
    return {
        "total_preferences": preferences.count(),
        "ready_count": ready_count,
        "skipped_count": len(skipped_employee_ids),
        "pending_count": len(pending_employee_ids),
        "missing_count": missing_count,
        "attention_count": attention_count,
        "ready_percentage": _percent(ready_count, total_employees),
        "status_counts": status_counts,
    }


def _build_approval_summary(employee_ids, year):
    employee_ids = list(employee_ids)
    requests = _overlaps_year_queryset(VacationRequest.objects.filter(employee_id__in=employee_ids), year)
    changes = _overlaps_year_queryset(
        VacationScheduleChangeRequest.objects.filter(employee_id__in=employee_ids),
        year,
        "new_start_date",
        "new_end_date",
    )
    return {
        "pending_requests": requests.filter(status=VacationRequest.STATUS_PENDING).count(),
        "approved_requests": requests.filter(status=VacationRequest.STATUS_APPROVED).count(),
        "rejected_requests": requests.filter(status=VacationRequest.STATUS_REJECTED).count(),
        "pending_changes": changes.filter(status=VacationScheduleChangeRequest.STATUS_PENDING).count(),
        "approved_changes": changes.filter(status=VacationScheduleChangeRequest.STATUS_APPROVED).count(),
        "rejected_changes": changes.filter(status=VacationScheduleChangeRequest.STATUS_REJECTED).count(),
        "total_pending": requests.filter(status=VacationRequest.STATUS_PENDING).count()
        + changes.filter(status=VacationScheduleChangeRequest.STATUS_PENDING).count(),
    }


def _build_attention_items(department_heatmap, balance_summary, preference_summary, approval_summary, rows, year):
    items = []

    for department in department_heatmap:
        conflict_month = next((month for month in department["months"] if month["status"] == "conflict"), None)
        if conflict_month:
            items.append(
                {
                    "tone": "danger",
                    "icon": "warning",
                    "title": f'{department["department_name"]}: конфликт в {conflict_month["month_name"].lower()}',
                    "text": (
                        f'Отсутствуют {conflict_month["absent_count"]}, '
                        f'останется {conflict_month["remaining_staff"]}, '
                        f'минимум {conflict_month["min_staff_required"]}.'
                    ),
                    "url": _calendar_url(year, department["department_id"] or None, "conflict"),
                    "action_label": "Открыть график",
                    "priority": 0,
                }
            )
            continue

        risk_month = next((month for month in department["months"] if month["status"] == "risk"), None)
        if risk_month:
            items.append(
                {
                    "tone": "warning",
                    "icon": "bolt",
                    "title": f'{department["department_name"]}: риск в {risk_month["month_name"].lower()}',
                    "text": (
                        f'Нагрузка {risk_month["load_level"]}/5, '
                        f'рисковых записей: {risk_month["risk_count"]}.'
                    ),
                    "url": _calendar_url(year, department["department_id"] or None, "risk"),
                    "action_label": "Проверить",
                    "priority": 1,
                }
            )

    for row in rows:
        if not row.get("has_conflict") and not row.get("has_high_risk"):
            continue
        items.append(
            {
                "tone": "danger" if row.get("has_conflict") else "warning",
                "icon": row.get("role_icon") or "person",
                "icon_type": row.get("role_icon_type") or "material",
                "icon_role_variant": row.get("role_variant") or "employee",
                "title": row["employee_name"],
                "text": row.get("issue_description") or "В годовом графике есть риск.",
                "url": _profile_url_from_analytics(row["profile_url"]),
                "action_label": "Профиль",
                "priority": 2 if row.get("has_conflict") else 3,
            }
        )

    if approval_summary["total_pending"]:
        items.append(
            {
                "tone": "warning",
                "icon": "pending_actions",
                "title": "Есть решения на согласовании",
                "text": (
                    f'{approval_summary["pending_requests"]} заявок и '
                    f'{approval_summary["pending_changes"]} переносов ждут решения.'
                ),
                "url": reverse("applications"),
                "action_label": "К заявкам",
                "priority": 2,
            }
        )

    if preference_summary["attention_count"]:
        items.append(
            {
                "tone": "info",
                "icon": "fact_check",
                "title": "Предпочтения заполнены не полностью",
                "text": (
                    f'{preference_summary["ready_percentage"]}% сотрудников дали основной вариант, '
                    f'требуют внимания: {preference_summary["attention_count"]}.'
                ),
                "url": _calendar_url(year),
                "action_label": "Сверить",
                "priority": 4,
            }
        )

    if balance_summary["low_balance_count"]:
        items.append(
            {
                "tone": "info",
                "icon": "account_balance_wallet",
                "title": "Низкий отпускной баланс",
                "text": f'{balance_summary["low_balance_count"]} сотрудников имеют 7 дней или меньше к концу года.',
                "url": reverse("employees"),
                "action_label": "Сотрудники",
                "priority": 5,
            }
        )

    return sorted(items, key=lambda item: (item["priority"], item["title"]))[:8]


def _build_planning_kpis(
    total_employees,
    employees_not_on_vacation_count,
    planned_employee_count,
    monthly_metrics,
    department_heatmap,
    approval_summary,
    preference_summary,
):
    peak_month = max(monthly_metrics, key=lambda item: (item["employee_count"], item["planned_days"]))
    conflict_departments = sum(1 for department in department_heatmap if department["status"] == "conflict")
    risk_months = sum(1 for month in monthly_metrics if month["issue_count"])
    schedule_ready_percentage = _percent(planned_employee_count, total_employees)

    return [
        {
            "tone": "primary",
            "icon": "event_available",
            "label": "Готовность графика",
            "value": f"{schedule_ready_percentage}%",
            "detail": f"{planned_employee_count} из {total_employees} сотрудников включены в годовой график",
        },
        {
            "tone": "danger" if conflict_departments else "success",
            "icon": "crisis_alert",
            "label": "Конфликты состава",
            "value": conflict_departments,
            "detail": "отделов требуют корректировки" if conflict_departments else "критичных провалов не найдено",
        },
        {
            "tone": "warning" if risk_months else "success",
            "icon": "bolt",
            "label": "Рисковые месяцы",
            "value": risk_months,
            "detail": "месяцев с высоким риском или конфликтом",
        },
        {
            "tone": "primary",
            "icon": "calendar_month",
            "label": "Пик отсутствий",
            "value": peak_month["month_short"],
            "detail": f'{peak_month["employee_count"]} сотрудников, {peak_month["planned_days"]} дней',
        },
        {
            "tone": "warning" if approval_summary["total_pending"] or preference_summary["attention_count"] else "success",
            "icon": "task_alt",
            "label": "Долг по действиям",
            "value": approval_summary["total_pending"] + preference_summary["attention_count"],
            "detail": "заявки, переносы и предпочтения, которые мешают закрыть план",
        },
        {
            "tone": "success",
            "icon": "groups",
            "label": "Работают сегодня",
            "value": f"{employees_not_on_vacation_count} из {total_employees}",
            "detail": f"{_percent(employees_not_on_vacation_count, total_employees)}% сотрудников не в отпуске",
        },
    ]


def _build_chart_payload(monthly_metrics, balance_summary):
    return {
        "labels": RUSSIAN_MONTH_SHORT_NAMES,
        "sources": {
            "schedule": [month["schedule_days"] for month in monthly_metrics],
            "requests": [month["request_days"] + month["pending_days"] for month in monthly_metrics],
            "changes": [month["changed_days"] for month in monthly_metrics],
        },
        "risks": {
            "medium": [month["medium_risk_count"] for month in monthly_metrics],
            "high": [month["high_risk_count"] for month in monthly_metrics],
            "conflicts": [month["conflict_count"] for month in monthly_metrics],
        },
        "balance": {
            "available": float(balance_summary["totals"]["available"]),
            "reserved": float(balance_summary["totals"]["reserved"]),
            "used": float(balance_summary["totals"]["used"]),
        },
    }


def build_analytics_payload(employee_ids=None, year=None):
    today = timezone.localdate()
    year = int(year or today.year)
    employee_ids = _normalize_employee_ids(employee_ids)
    employees, employee_day_status, employee_entries = build_calendar_base_data(year, employee_ids=employee_ids)
    rows, _ = build_calendar_rows(
        employees,
        employee_day_status,
        employee_entries,
        year=year,
        month=today.month if today.year == year else 1,
        view_mode="year",
        today=today,
    )

    monthly_metrics, planned_employee_ids, active_absence_employee_ids = _build_monthly_metrics(
        employee_entries,
        rows,
        year,
    )
    department_heatmap = _build_department_heatmap(employees, rows, year, employee_entries)
    employee_id_set = {employee.id for employee in employees}
    module_summary = _build_module_summary(department_heatmap, employee_id_set, year)
    total_employees = len(employees)
    absent_today_ids = _get_absent_employee_ids(employee_id_set, today)
    employees_not_on_vacation_count = total_employees - len(absent_today_ids)

    total_applications_queryset = VacationRequest.objects.filter(employee_id__in=employee_id_set)
    total_applications_count = total_applications_queryset.count()
    canceled_count = total_applications_queryset.filter(status=VacationRequest.STATUS_REJECTED).count()
    rejection_percentage = _percent(canceled_count, total_applications_count)
    avg_vacation_days = round(
        sum(employee.annual_paid_leave_days for employee in employees) / total_employees,
        2,
    ) if total_employees else 0

    balance_summary = _build_balance_summary(employees, year)
    preference_summary = _build_preference_summary(employee_id_set, total_employees, year)
    approval_summary = _build_approval_summary(employee_id_set, year)
    planning_kpis = _build_planning_kpis(
        total_employees,
        employees_not_on_vacation_count,
        len(planned_employee_ids),
        monthly_metrics,
        department_heatmap,
        approval_summary,
        preference_summary,
    )
    attention_items = _build_attention_items(
        department_heatmap,
        balance_summary,
        preference_summary,
        approval_summary,
        rows,
        year,
    )

    vacation_counts = [month["vacation_count"] for month in monthly_metrics]
    average_duration_days = [month["average_duration"] for month in monthly_metrics]
    planned_days = [month["planned_days"] for month in monthly_metrics]

    return {
        "labels": RUSSIAN_MONTH_SHORT_NAMES,
        "values1": vacation_counts,
        "values2": average_duration_days,
        "values3": planned_days,
        "rows": rows,
        "analytics_year": year,
        "monthly_metrics": monthly_metrics,
        "department_heatmap": department_heatmap,
        "module_summary": module_summary,
        "planning_kpis": planning_kpis,
        "attention_items": attention_items,
        "balance_summary": balance_summary,
        "preference_summary": preference_summary,
        "approval_summary": approval_summary,
        "analytics_chart_payload": _build_chart_payload(monthly_metrics, balance_summary),
        "total_employees": total_employees,
        "employees_not_on_vacation_count": employees_not_on_vacation_count,
        "working_employees": _percent(employees_not_on_vacation_count, total_employees),
        "employees_with_active_absence_count": len(active_absence_employee_ids),
        "planned_employee_count": len(planned_employee_ids),
        "planned_employee_percentage": _percent(len(planned_employee_ids), total_employees),
        "total_applications_count": total_applications_count,
        "canceled_count": canceled_count,
        "rejection_percentage": rejection_percentage,
        "avg_vacation_days": avg_vacation_days,
    }
