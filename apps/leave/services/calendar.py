import calendar
from collections import defaultdict
from datetime import date, timedelta
from urllib.parse import urlencode

from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme

from apps.employees.models import Employees
from apps.leave.models import DepartmentWorkload, VacationRequest, VacationScheduleChangeRequest, VacationScheduleItem

from .constants import (
    CALENDAR_VISIBLE_STATUSES,
    DISPLAY_FREE,
    DISPLAY_MIXED,
    DISPLAY_REQUEST_APPROVED,
    DISPLAY_REQUEST_PENDING,
    DISPLAY_REQUEST_REJECTED,
    DISPLAY_SCHEDULE_APPROVED,
    DISPLAY_SCHEDULE_CANCELLED,
    DISPLAY_SCHEDULE_PLANNED,
    DISPLAY_SCHEDULE_TRANSFERRED,
    DISPLAY_STATUS_PRIORITY,
    DISPLAY_STATUS_UI,
    REQUEST_STATUS_TO_DISPLAY_STATUS,
    REQUEST_STATUS_UI,
    RUSSIAN_MONTH_NAMES,
    RUSSIAN_MONTH_SHORT_NAMES,
    SCHEDULE_STATUS_TO_CALENDAR_STATUS,
    SCHEDULE_STATUS_TO_DISPLAY_STATUS,
    VACATION_STATUS_META,
    WEEKDAY_SHORT_NAMES,
)
from .dates import clip_period_to_range, format_period_label, get_month_end, get_requested_days, iterate_dates
from .employee_presentation import get_employee_identity_presentation
from .querysets import exclude_converted_paid_requests, get_vacation_requests_queryset
from .schedule_changes import build_schedule_change_transfer_action
from .schedule_items import get_schedule_item_detail_reference
from .staffing import (
    build_department_staffing_context_map,
    evaluate_department_staffing_state,
    evaluate_enterprise_leadership_state,
    format_staff_absence,
    format_staff_count,
    get_department_staffing_rule,
    get_enterprise_leadership_employee_ids,
    get_staffing_limits_for_date,
)

RUSSIAN_MONTH_GENITIVE_NAMES = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

EMPLOYEE_SCHEDULE_STATUS_META = {
    "conflict": {
        "label": "Есть конфликт",
        "short_label": "Конфликт",
        "variant": "conflict",
        "icon": "⚔",
        "icon_type": "symbol",
        "issue": "conflict",
    },
    "risk": {
        "label": "Есть риск",
        "short_label": "Риск",
        "variant": "risk",
        "icon": "bolt",
        "icon_type": "material",
        "issue": "risk",
    },
    "planned": {
        "label": "График есть",
        "short_label": "График",
        "variant": "planned",
        "icon": "event_available",
        "icon_type": "material",
        "issue": "all",
    },
    "empty": {
        "label": "Нет отпуска",
        "short_label": "Нет отпуска",
        "variant": "empty",
        "icon": "event_busy",
        "icon_type": "material",
        "issue": "all",
    },
}

CALENDAR_ISSUE_SCHEDULE_STATUSES = (
    VacationScheduleItem.STATUS_DRAFT,
    *VacationScheduleItem.ACTIVE_STATUSES,
)

def get_calendar_redirect_url(request):
    next_url = request.POST.get("next_url") or request.GET.get("next_url")
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return next_url

    next_view = request.POST.get("next_view_mode", request.GET.get("view", "month"))
    next_year = request.POST.get("next_year", request.GET.get("year", timezone.localdate().year))
    next_month = request.POST.get("next_month", request.GET.get("month", timezone.localdate().month))
    query = urlencode({"view": next_view, "year": next_year, "month": next_month})
    return f"{reverse('calendar')}?{query}"

def _schedule_item_source_label(item):
    if item.status == VacationScheduleItem.STATUS_TRANSFERRED:
        return "Перенос"
    if item.source == VacationScheduleItem.SOURCE_MANUAL:
        return "Дополнение к графику"
    if item.source == VacationScheduleItem.SOURCE_TRANSFER:
        return "Перенос"
    return "Годовой график"

def _entry_overlaps_period(entry, period_start, period_end):
    return clip_period_to_range(entry["start_date"], entry["end_date"], period_start, period_end) is not None

def _is_conflict_relevant_entry(entry):
    if entry.get("source_kind") == "request":
        return entry.get("status") in VacationRequest.ACTIVE_STATUSES
    if entry.get("source_kind") == "schedule":
        return entry.get("schedule_status") in CALENDAR_ISSUE_SCHEDULE_STATUSES
    return False

def _issue_tense_for_period(start_date, end_date, today=None):
    today = today or timezone.localdate()
    if end_date < today:
        return "past"
    if start_date > today:
        return "future"
    return "present"

def _issue_tense_for_date(current_date, today=None):
    return _issue_tense_for_period(current_date, current_date, today)

def _format_remaining_staff(value, tense="present"):
    value = int(value or 0)
    if tense == "past":
        verb = "остался" if format_staff_count(value).endswith(" сотрудник") else "осталось"
    elif tense == "future":
        verb = "останется"
    else:
        verb = "остается"
    return f"{verb} {format_staff_count(value)}"

def _format_substitution_text(scope, tense="present"):
    if tense == "past":
        return f"{scope}: дефицит закрывался замещением"
    if tense == "future":
        return f"{scope}: дефицит будет закрываться замещением"
    return f"{scope}: дефицит закрывается замещением"

def _format_leadership_pair_text(issue_kind, tense="present"):
    subject = "Руководитель предприятия и заместитель"
    if issue_kind == "department_leadership_pair":
        subject = "Руководитель отдела и заместитель"
    if tense == "past":
        return f"{subject} отсутствовали одновременно."
    if tense == "future":
        return f"{subject} будут отсутствовать одновременно."
    return f"{subject} отсутствуют одновременно."

def _format_conflict_reason(current_date, absent_count, max_absent, remaining_staff_count, min_staff_required, today=None):
    reason_parts = []
    tense = _issue_tense_for_date(current_date, today)
    if max_absent and absent_count > max_absent:
        reason_parts.append(f"{format_staff_absence(absent_count, tense=tense)}, лимит {max_absent}")
    if min_staff_required and remaining_staff_count < min_staff_required:
        reason_parts.append(f"{_format_remaining_staff(remaining_staff_count, tense)}, минимум {min_staff_required}")
    return f'{current_date.strftime("%d.%m.%Y")}: {"; ".join(reason_parts)}'

def _format_group_conflict_reason(current_date, group_name, available_count, min_staff_required, absent_count, max_absent, today=None):
    reason_parts = []
    tense = _issue_tense_for_date(current_date, today)
    if min_staff_required and available_count < min_staff_required:
        reason_parts.append(f"не хватает: {group_name} ({available_count}/{min_staff_required})")
    if max_absent is not None and absent_count > max_absent:
        reason_parts.append(f"{group_name}: {format_staff_absence(absent_count, tense=tense)}, лимит {max_absent}")
    return f'{current_date.strftime("%d.%m.%Y")}: {"; ".join(reason_parts)}'

def _format_substitution_risk_reason(current_date, group_name, covered_count):
    return f'{current_date.strftime("%d.%m.%Y")}: {group_name} закрыта замещением ({covered_count})'


def _format_staffing_issue_reason(current_date, issue, today=None):
    issue_kind = issue.get("kind")
    if issue_kind == "substitution_used":
        return _format_substitution_risk_reason(
            current_date,
            issue.get("affected_group", ""),
            issue.get("covered_staff", 0),
        )
    if issue_kind in {"group_shortage", "group_absence_limit"}:
        return _format_group_conflict_reason(
            current_date,
            issue.get("affected_group", ""),
            issue.get("remaining_staff", 0),
            issue.get("required_staff", 0),
            issue.get("absent_staff", 0),
            issue.get("max_absent", 0),
            today=today,
        )
    if issue_kind == "department_leadership_pair":
        tense = _issue_tense_for_date(current_date, today)
        return f'{current_date.strftime("%d.%m.%Y")}: {_format_leadership_pair_text(issue_kind, tense).rstrip(".").lower()}'
    if issue_kind == "enterprise_leadership_pair":
        tense = _issue_tense_for_date(current_date, today)
        return f'{current_date.strftime("%d.%m.%Y")}: {_format_leadership_pair_text(issue_kind, tense).rstrip(".").lower()}'
    return _format_conflict_reason(
        current_date,
        issue.get("absent_staff", 0),
        issue.get("max_absent", 0),
        issue.get("remaining_staff", 0),
        issue.get("required_staff", 0),
        today=today,
    )


def _build_staffing_issue_event(current_date, issue, fallback_employee_ids):
    affected_employee_ids = issue.get("affected_employee_ids") or fallback_employee_ids
    return {
        "date": current_date,
        "kind": issue.get("kind", ""),
        "severity": issue.get("severity", ""),
        "title": issue.get("title", ""),
        "text": issue.get("text", ""),
        "affected_department": issue.get("affected_department", ""),
        "affected_group": issue.get("affected_group", ""),
        "affected_employee_ids": tuple(sorted(affected_employee_ids or [])),
        "remaining_staff": issue.get("remaining_staff"),
        "required_staff": issue.get("required_staff"),
        "missing_staff": issue.get("missing_staff"),
        "absent_staff": issue.get("absent_staff"),
        "max_absent": issue.get("max_absent"),
        "covered_staff": issue.get("covered_staff"),
        "substitute_groups": issue.get("substitute_groups", ""),
    }


def _staffing_issue_event_key(event):
    return (
        event.get("kind", ""),
        event.get("severity", ""),
        event.get("affected_department", ""),
        event.get("affected_group", ""),
        event.get("remaining_staff"),
        event.get("required_staff"),
        event.get("missing_staff"),
        event.get("absent_staff"),
        event.get("max_absent"),
        event.get("covered_staff"),
        event.get("substitute_groups", ""),
        tuple(event.get("affected_employee_ids", ())),
    )


def _append_staffing_issue_event(meta, employee_ids, event):
    event_key = (_staffing_issue_event_key(event), event["date"])
    for employee_id in employee_ids:
        meta[employee_id]["dates"].add(event["date"].isoformat())
        if event_key in meta[employee_id]["event_keys"]:
            continue
        meta[employee_id]["event_keys"].add(event_key)
        meta[employee_id]["events"].append(event)


def _get_staffing_issue_meta(employees, employee_entries, period_start, period_end, today=None):
    department_by_id = {employee.department_id: employee.department for employee in employees if employee.department_id}
    if not department_by_id:
        return {"conflicts": {}, "substitution_risks": {}}

    department_ids = set(department_by_id)
    staffing_rules = {
        department_id: get_department_staffing_rule(department)
        for department_id, department in department_by_id.items()
    }
    workloads = DepartmentWorkload.objects.filter(
        department_id__in=department_ids,
        year__gte=period_start.year,
        year__lte=period_end.year,
    )
    workload_by_department_month = {
        (workload.department_id, workload.year, workload.month): workload
        for workload in workloads
    }
    staffing_contexts = build_department_staffing_context_map(department_by_id.values(), period_end)

    absent_by_department_day = defaultdict(set)
    absent_by_day = defaultdict(set)
    for entries in employee_entries.values():
        for entry in entries:
            if not _is_conflict_relevant_entry(entry):
                continue

            clipped_period = clip_period_to_range(entry["start_date"], entry["end_date"], period_start, period_end)
            if clipped_period is None:
                continue

            clipped_start, clipped_end = clipped_period
            department_id = entry.get("department_id")
            for current_date in iterate_dates(clipped_start, clipped_end):
                absent_by_day[current_date].add(entry["employee_id"])
                if department_id in department_ids:
                    absent_by_department_day[(department_id, current_date)].add(entry["employee_id"])

    conflict_meta = defaultdict(lambda: {"summaries": [], "dates": set(), "events": [], "event_keys": set()})
    substitution_risk_meta = defaultdict(lambda: {"summaries": [], "dates": set(), "events": [], "event_keys": set()})
    department_staffing_evaluation_cache = {}
    enterprise_leadership_evaluation_cache = {}

    def add_conflict(employee_ids, current_date, reason, event):
        for employee_id in employee_ids:
            if reason not in conflict_meta[employee_id]["summaries"]:
                conflict_meta[employee_id]["summaries"].append(reason)
        _append_staffing_issue_event(conflict_meta, employee_ids, event)

    def add_substitution_risk(employee_ids, current_date, reason, event):
        for employee_id in employee_ids:
            if reason not in substitution_risk_meta[employee_id]["summaries"]:
                substitution_risk_meta[employee_id]["summaries"].append(reason)
        _append_staffing_issue_event(substitution_risk_meta, employee_ids, event)

    for (department_id, current_date), absent_employee_ids in absent_by_department_day.items():
        department = department_by_id[department_id]
        min_staff_required, max_absent = get_staffing_limits_for_date(
            department,
            current_date,
            workload_by_department_month,
            staffing_rules,
        )
        evaluation_key = (
            department_id,
            tuple(sorted(absent_employee_ids)),
            int(min_staff_required or 0),
            int(max_absent or 0),
        )
        staffing_evaluation = department_staffing_evaluation_cache.get(evaluation_key)
        if staffing_evaluation is None:
            staffing_evaluation = evaluate_department_staffing_state(
                staffing_contexts[department_id],
                absent_employee_ids,
                min_staff_required=min_staff_required,
                max_absent=max_absent,
                include_limit_warnings=False,
            )
            department_staffing_evaluation_cache[evaluation_key] = staffing_evaluation
        for issue in staffing_evaluation["issues"]:
            affected_employee_ids = issue.get("affected_employee_ids") or absent_employee_ids
            reason = _format_staffing_issue_reason(current_date, issue, today=today)
            event = _build_staffing_issue_event(current_date, issue, absent_employee_ids)
            if issue.get("kind") == "substitution_used":
                add_substitution_risk(affected_employee_ids, current_date, reason, event)
            elif issue.get("severity") == "conflict":
                add_conflict(affected_employee_ids, current_date, reason, event)

    enterprise_head_ids, enterprise_deputy_ids = get_enterprise_leadership_employee_ids(period_end)
    for current_date, absent_employee_ids in absent_by_day.items():
        evaluation_key = tuple(sorted(absent_employee_ids))
        enterprise_evaluation = enterprise_leadership_evaluation_cache.get(evaluation_key)
        if enterprise_evaluation is None:
            enterprise_evaluation = evaluate_enterprise_leadership_state(
                absent_employee_ids,
                period_end,
                enterprise_head_ids=enterprise_head_ids,
                enterprise_deputy_ids=enterprise_deputy_ids,
            )
            enterprise_leadership_evaluation_cache[evaluation_key] = enterprise_evaluation
        for issue in enterprise_evaluation["issues"]:
            affected_employee_ids = issue.get("affected_employee_ids") or absent_employee_ids
            add_conflict(
                affected_employee_ids,
                current_date,
                _format_staffing_issue_reason(current_date, issue, today=today),
                _build_staffing_issue_event(current_date, issue, absent_employee_ids),
            )

    return {
        "conflicts": {
            employee_id: {
                "dates": meta["dates"],
                "summary": "; ".join(meta["summaries"][:2]),
                "events": meta["events"],
            }
            for employee_id, meta in conflict_meta.items()
        },
        "substitution_risks": {
            employee_id: {
                "dates": meta["dates"],
                "summary": "; ".join(meta["summaries"][:2]),
                "events": meta["events"],
            }
            for employee_id, meta in substitution_risk_meta.items()
        },
    }

def _get_conflict_meta(employees, employee_entries, period_start, period_end):
    return _get_staffing_issue_meta(employees, employee_entries, period_start, period_end)["conflicts"]

def _get_conflicting_employee_ids(employees, employee_entries, period_start, period_end):
    return set(_get_conflict_meta(employees, employee_entries, period_start, period_end))

def _risk_label_for_level(risk_level):
    return dict(VacationRequest.RISK_CHOICES).get(risk_level, "Низкий")

def _build_entry_anchor(entry):
    return {
        "employee_id": entry["employee_id"],
        "source_kind": entry.get("source_kind", ""),
        "source_id": entry.get("source_id"),
        "start_date": entry["start_date"].isoformat(),
        "end_date": entry["end_date"].isoformat(),
    }

def _shorten_conflict_summary(summary):
    if not summary:
        return ""

    first_reason = summary.split("; ", 1)[0].strip()
    if ": " in first_reason:
        first_reason = first_reason.split(": ", 1)[1].strip()

    if len(first_reason) <= 34:
        return first_reason

    return first_reason[:31].rstrip() + "..."

def _build_row_issue_chips(employee_issue_meta, issue_filter):
    chips = []
    if employee_issue_meta["has_high_risk"]:
        chips.append(
            {
                "kind": "risk",
                "label": employee_issue_meta["risk_summary"] if issue_filter == "risk" else "Высокий риск",
                "icon": "bolt",
                "icon_type": "material",
            }
        )
    if employee_issue_meta["has_conflict"]:
        conflict_label = "Конфликт"
        if issue_filter == "conflict":
            conflict_reason = _shorten_conflict_summary(employee_issue_meta["conflict_summary"])
            conflict_label = f"Конфликт: {conflict_reason}" if conflict_reason else "Конфликт"
        chips.append({"kind": "conflict", "label": conflict_label, "icon": "⚔", "icon_type": "symbol"})
    return chips

def _entry_identity(entry):
    return (entry.get("source_kind", ""), entry.get("source_id"), entry["start_date"], entry["end_date"])

def _starts_with_issue_date(value):
    return (
        len(value) >= 11
        and value[2] == "."
        and value[5] == "."
        and value[10] == ":"
        and value[:2].isdigit()
        and value[3:5].isdigit()
        and value[6:10].isdigit()
    )

def _split_issue_summary(summary):
    summary = (summary or "").strip()
    if not summary:
        return []

    reasons = []
    for fragment in (part.strip() for part in summary.split("; ") if part.strip()):
        if _starts_with_issue_date(fragment) or not reasons:
            reasons.append(fragment)
        else:
            reasons[-1] = f"{reasons[-1]}; {fragment}"
    return reasons

def _split_issue_date(reason):
    if _starts_with_issue_date(reason):
        return reason[:10], reason[12:].strip()
    return "", reason

def _format_problem_period_label(start_date, end_date):
    if start_date == end_date:
        return f"{start_date.day} {RUSSIAN_MONTH_GENITIVE_NAMES[start_date.month - 1]}"
    if start_date.month == end_date.month and start_date.year == end_date.year:
        return f"{start_date.day}-{end_date.day} {RUSSIAN_MONTH_GENITIVE_NAMES[start_date.month - 1]}"
    return (
        f"{start_date.day} {RUSSIAN_MONTH_GENITIVE_NAMES[start_date.month - 1]} - "
        f"{end_date.day} {RUSSIAN_MONTH_GENITIVE_NAMES[end_date.month - 1]}"
    )

def _short_employee_name(full_name):
    parts = str(full_name or "").split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[1]}"
    return str(full_name or "").strip()

def _employee_profile_url(employee_id):
    return f"{reverse('employee_profile', args=[employee_id])}?from=calendar"

def _format_staff_object_count(value):
    value = int(value or 0)
    object_forms = {
        1: "одного сотрудника",
        2: "двух сотрудников",
        3: "трёх сотрудников",
        4: "четырёх сотрудников",
    }
    if value in object_forms:
        return object_forms[value]
    count_label = format_staff_count(value)
    if count_label.endswith(" сотрудник"):
        return f"{value} сотрудника"
    return count_label

def _build_affected_employees(employee_ids, employee_names_by_id, limit=4):
    employees = sorted(
        (
            {
                "id": employee_id,
                "name": _short_employee_name(employee_names_by_id.get(employee_id, "")),
                "profile_url": _employee_profile_url(employee_id),
            }
            for employee_id in employee_ids
            if employee_names_by_id.get(employee_id)
        ),
        key=lambda item: item["name"],
    )
    visible_employees = employees[:limit]
    return visible_employees, max(len(employees) - len(visible_employees), 0)

def _build_affected_employee_ids(employee_ids, employee_names_by_id):
    normalized_ids = []
    seen_ids = set()
    for employee_id in employee_ids or ():
        try:
            normalized_id = int(employee_id)
        except (TypeError, ValueError):
            continue
        if normalized_id in seen_ids or not employee_names_by_id.get(normalized_id):
            continue
        seen_ids.add(normalized_id)
        normalized_ids.append(normalized_id)

    return sorted(
        normalized_ids,
        key=lambda employee_id: _short_employee_name(employee_names_by_id.get(employee_id, "")),
    )

def _build_affected_names(employee_ids, employee_names_by_id, limit=4):
    affected_employees, extra_affected_count = _build_affected_employees(
        employee_ids,
        employee_names_by_id,
        limit=limit,
    )
    return [employee["name"] for employee in affected_employees], extra_affected_count

def _group_staffing_issue_events(events):
    buckets = defaultdict(list)
    seen = set()
    for event in events or []:
        event_key = (_staffing_issue_event_key(event), event.get("date"))
        if event_key in seen:
            continue
        seen.add(event_key)
        buckets[_staffing_issue_event_key(event)].append(event)

    groups = []
    for key, bucket_events in buckets.items():
        bucket_events.sort(key=lambda item: item["date"])
        current_group = None
        for event in bucket_events:
            if current_group and event["date"] == current_group["end_date"] + timedelta(days=1):
                current_group["end_date"] = event["date"]
                current_group["dates"].append(event["date"])
                continue

            if current_group:
                groups.append(current_group)
            current_group = {
                "key": key,
                "event": event,
                "start_date": event["date"],
                "end_date": event["date"],
                "dates": [event["date"]],
            }
        if current_group:
            groups.append(current_group)

    return sorted(groups, key=lambda item: (item["start_date"], item["event"].get("kind", "")))

def _build_group_staffing_combined_event(absence_event, shortage_event):
    missing_staff = shortage_event.get("missing_staff")
    if missing_staff is None:
        missing_staff = max(
            int(shortage_event.get("required_staff") or 0) - int(shortage_event.get("remaining_staff") or 0),
            0,
        )

    affected_employee_ids = tuple(
        sorted(set(absence_event.get("affected_employee_ids", ())) & set(shortage_event.get("affected_employee_ids", ())))
    )
    return {
        "date": absence_event["date"],
        "kind": "group_staffing_combined",
        "severity": "conflict",
        "title": "Группа не проходит по составу",
        "text": "",
        "affected_department": absence_event.get("affected_department") or shortage_event.get("affected_department", ""),
        "affected_group": absence_event.get("affected_group") or shortage_event.get("affected_group", ""),
        "affected_employee_ids": affected_employee_ids,
        "remaining_staff": shortage_event.get("remaining_staff"),
        "required_staff": shortage_event.get("required_staff"),
        "missing_staff": missing_staff,
        "absent_staff": absence_event.get("absent_staff"),
        "max_absent": absence_event.get("max_absent"),
        "covered_staff": shortage_event.get("covered_staff") or absence_event.get("covered_staff"),
        "substitute_groups": shortage_event.get("substitute_groups", "") or absence_event.get("substitute_groups", ""),
    }

def _combine_group_staffing_events(events):
    events = list(events or [])
    grouped_events = defaultdict(list)
    passthrough_indexes = set()

    for index, event in enumerate(events):
        if event.get("kind") not in {"group_absence_limit", "group_shortage"}:
            passthrough_indexes.add(index)
            continue
        if not event.get("affected_group"):
            passthrough_indexes.add(index)
            continue

        grouped_events[
            (
                event.get("date"),
                event.get("affected_group"),
                tuple(event.get("affected_employee_ids", ())),
            )
        ].append((index, event))

    combined_indexes = set()
    combined_items = []
    for indexed_events in grouped_events.values():
        absence_items = [
            (index, event)
            for index, event in indexed_events
            if event.get("kind") == "group_absence_limit"
        ]
        shortage_items = [
            (index, event)
            for index, event in indexed_events
            if event.get("kind") == "group_shortage"
        ]
        if not absence_items or not shortage_items:
            continue

        for (absence_index, absence_event), (shortage_index, shortage_event) in zip(absence_items, shortage_items):
            combined_indexes.update({absence_index, shortage_index})
            combined_items.append(
                (
                    min(absence_index, shortage_index),
                    _build_group_staffing_combined_event(absence_event, shortage_event),
                )
            )

    merged_items = [
        (index, event)
        for index, event in enumerate(events)
        if index in passthrough_indexes or index not in combined_indexes
    ]
    merged_items.extend(combined_items)
    return [event for _, event in sorted(merged_items, key=lambda item: item[0])]

def _problem_title_for_event(event):
    issue_kind = event.get("kind")
    if issue_kind == "group_staffing_combined":
        return "Группа не проходит по составу"
    if issue_kind in {"group_absence_limit", "department_absence_limit"}:
        return "Превышен лимит отсутствующих"
    if issue_kind == "group_shortage":
        return "Группа ниже минимума"
    if issue_kind == "department_staff_shortage":
        return "Отдел ниже минимума"
    if issue_kind == "substitution_used":
        return "Нужно замещение"
    if issue_kind == "department_leadership_pair":
        return "Нет пары управления отделом"
    if issue_kind == "enterprise_leadership_pair":
        return "Нет пары управления предприятием"
    if issue_kind == "stored_high_risk":
        return "Высокий риск записи"
    return event.get("title") or "Риск состава"

def _event_scope_label(event):
    return event.get("affected_group") or event.get("affected_department") or "Состав"

def _problem_text_for_event(event, tense="present"):
    issue_kind = event.get("kind")
    scope = _event_scope_label(event)
    if issue_kind == "group_staffing_combined":
        return (
            f"{scope}: {format_staff_absence(event.get('absent_staff', 0), tense=tense)}, "
            f"{_format_remaining_staff(event.get('remaining_staff', 0), tense)} "
            f"при минимуме {format_staff_count(event.get('required_staff', 0))}"
        )
    if issue_kind in {"group_absence_limit", "department_absence_limit"}:
        return (
            f"{scope}: {format_staff_absence(event.get('absent_staff', 0), tense=tense)} "
            f"при лимите {format_staff_count(event.get('max_absent', 0))}"
        )
    if issue_kind in {"group_shortage", "department_staff_shortage"}:
        return (
            f"{scope}: {_format_remaining_staff(event.get('remaining_staff', 0), tense)} "
            f"при минимуме {format_staff_count(event.get('required_staff', 0))}"
        )
    if issue_kind == "substitution_used":
        return _format_substitution_text(scope, tense)
    if issue_kind in {"department_leadership_pair", "enterprise_leadership_pair"}:
        return _format_leadership_pair_text(issue_kind, tense)
    return event.get("text") or "Есть риск для состава в выбранном периоде."

def _impact_label_for_event(event):
    issue_kind = event.get("kind")
    if issue_kind == "group_staffing_combined":
        missing = event.get("missing_staff")
        if missing is None:
            missing = max(int(event.get("required_staff") or 0) - int(event.get("remaining_staff") or 0), 0)
        if missing:
            return f"Не хватает: {_format_staff_object_count(missing)}"
        excess = max(int(event.get("absent_staff") or 0) - int(event.get("max_absent") or 0), 0)
        return f"Превышение: {format_staff_count(excess)}" if excess else ""
    if issue_kind in {"group_absence_limit", "department_absence_limit"}:
        excess = max(int(event.get("absent_staff") or 0) - int(event.get("max_absent") or 0), 0)
        return f"Превышение: {format_staff_count(excess)}" if excess else ""
    if issue_kind in {"group_shortage", "department_staff_shortage"}:
        missing = event.get("missing_staff")
        if missing is None:
            missing = max(int(event.get("required_staff") or 0) - int(event.get("remaining_staff") or 0), 0)
        return f"Не хватает: {_format_staff_object_count(missing)}" if missing else ""
    if issue_kind == "substitution_used":
        covered = int(event.get("covered_staff") or 0)
        return f"Замещение покрывает {_format_staff_object_count(covered)}" if covered else ""
    return ""

def _group_dates_overlap(left_group, right_group):
    return bool(set(left_group["dates"]) & set(right_group["dates"]))

def _groups_share_scope(left_group, right_group):
    left_event = left_group["event"]
    right_event = right_group["event"]
    return (
        left_event.get("affected_group")
        and left_event.get("affected_group") == right_event.get("affected_group")
    )

def _substitution_label_for_problem(event, substitution_groups):
    if not substitution_groups:
        return ""

    covered_staff = max(int(group["event"].get("covered_staff") or 0) for group in substitution_groups)
    substitute_labels = sorted(
        {
            group["event"].get("substitute_groups", "")
            for group in substitution_groups
            if group["event"].get("substitute_groups")
        }
    )
    label = f"Замещение покрывает {_format_staff_object_count(covered_staff)}"
    if substitute_labels:
        label = f"{label}: {'; '.join(substitute_labels)}"

    issue_kind = event.get("kind")
    if issue_kind == "group_staffing_combined":
        missing = event.get("missing_staff")
        if missing is None:
            missing = max(int(event.get("required_staff") or 0) - int(event.get("remaining_staff") or 0), 0)
        if missing:
            return f"{label}, но всё равно не хватает {_format_staff_object_count(missing)}."
        excess = max(int(event.get("absent_staff") or 0) - int(event.get("max_absent") or 0), 0)
        if excess:
            return f"{label}, но лимит всё равно превышен на {_format_staff_object_count(excess)}."
    if issue_kind in {"group_absence_limit", "department_absence_limit"}:
        excess = max(int(event.get("absent_staff") or 0) - int(event.get("max_absent") or 0), 0)
        if excess:
            return f"{label}, но лимит всё равно превышен на {_format_staff_object_count(excess)}."
    if issue_kind in {"group_shortage", "department_staff_shortage"}:
        missing = event.get("missing_staff")
        if missing is None:
            missing = max(int(event.get("required_staff") or 0) - int(event.get("remaining_staff") or 0), 0)
        if missing:
            return f"{label}, но всё равно не хватает {_format_staff_object_count(missing)}."
    return f"{label}."

def _build_problem_from_group(group, employee_names_by_id, substitution_groups=None, today=None):
    event = group["event"]
    tense = _issue_tense_for_period(group["start_date"], group["end_date"], today)
    affected_employee_ids = _build_affected_employee_ids(
        event.get("affected_employee_ids", ()),
        employee_names_by_id,
    )
    affected_employees, extra_affected_count = _build_affected_employees(
        affected_employee_ids,
        employee_names_by_id,
    )
    affected_names = [employee["name"] for employee in affected_employees]
    return {
        "kind": event.get("kind", ""),
        "severity": event.get("severity", ""),
        "start_date": group["start_date"].isoformat(),
        "end_date": group["end_date"].isoformat(),
        "period_label": _format_problem_period_label(group["start_date"], group["end_date"]),
        "title": _problem_title_for_event(event),
        "text": _problem_text_for_event(event, tense),
        "impact_label": _impact_label_for_event(event),
        "affected_employee_ids": affected_employee_ids,
        "affected_employees": affected_employees,
        "affected_names": affected_names,
        "extra_affected_count": extra_affected_count,
        "substitution_label": _substitution_label_for_problem(event, substitution_groups or []),
    }

def _build_fallback_risk_problem(summary, employee_id, employee_names_by_id):
    affected_employee_ids = _build_affected_employee_ids([employee_id], employee_names_by_id)
    affected_employees, extra_affected_count = _build_affected_employees(affected_employee_ids, employee_names_by_id)
    affected_names = [employee["name"] for employee in affected_employees]
    return {
        "kind": "stored_high_risk",
        "severity": "high",
        "start_date": "",
        "end_date": "",
        "period_label": "",
        "title": "Высокий риск записи",
        "text": summary,
        "impact_label": "",
        "affected_employee_ids": affected_employee_ids,
        "affected_employees": affected_employees,
        "affected_names": affected_names,
        "extra_affected_count": extra_affected_count,
        "substitution_label": "",
    }

def _build_calendar_risk_details(employee_id, employee_issue_meta, issue_label, issue_description, employee_names_by_id, today=None):
    has_conflict = employee_issue_meta["has_conflict"]
    has_high_risk = employee_issue_meta["has_high_risk"]
    status = "conflict" if has_conflict else ("risk" if has_high_risk else "clear")
    if has_conflict:
        summary = "В выбранном периоде есть конфликт состава. Причины показаны ниже."
    elif has_high_risk:
        summary = "Критического конфликта нет, но есть фактор высокого риска."
    else:
        summary = "В выбранном периоде критичных проблем не найдено."

    conflict_events = _combine_group_staffing_events(employee_issue_meta.get("conflict_events", []))
    conflict_groups = _group_staffing_issue_events(conflict_events)
    risk_groups = _group_staffing_issue_events(employee_issue_meta.get("risk_events", []))
    used_risk_group_indexes = set()
    problems = []
    for conflict_group in conflict_groups:
        substitution_groups = []
        for risk_group_index, risk_group in enumerate(risk_groups):
            if risk_group_index in used_risk_group_indexes:
                continue
            if risk_group["event"].get("kind") != "substitution_used":
                continue
            if not _groups_share_scope(conflict_group, risk_group):
                continue
            if not _group_dates_overlap(conflict_group, risk_group):
                continue
            used_risk_group_indexes.add(risk_group_index)
            substitution_groups.append(risk_group)
        problems.append(_build_problem_from_group(conflict_group, employee_names_by_id, substitution_groups, today=today))

    for risk_group_index, risk_group in enumerate(risk_groups):
        if risk_group_index in used_risk_group_indexes:
            continue
        problems.append(_build_problem_from_group(risk_group, employee_names_by_id, today=today))

    if has_high_risk and employee_issue_meta["risk_summary"] and not risk_groups:
        problems.append(_build_fallback_risk_problem(employee_issue_meta["risk_summary"], employee_id, employee_names_by_id))

    return {
        "status": status,
        "label": issue_label,
        "summary": summary,
        "problems": problems,
        "reasons": problems,
    }

def _build_calendar_issue_meta(employees, employee_entries, issue_employee_entries, period_start, period_end, today=None):
    issue_meta = {
        employee.id: {
            "has_high_risk": False,
            "has_conflict": False,
            "risk_summary": "",
            "conflict_summary": "",
            "risk_dates": set(),
            "conflict_dates": set(),
            "risk_events": [],
            "conflict_events": [],
        }
        for employee in employees
    }
    for employee in employees:
        for entry in employee_entries.get(employee.id, []):
            if _entry_overlaps_period(entry, period_start, period_end) and entry.get("risk_level") == VacationRequest.RISK_HIGH:
                issue_meta[employee.id]["has_high_risk"] = True
                if not issue_meta[employee.id]["risk_summary"]:
                    issue_meta[employee.id]["risk_summary"] = f'Высокий риск: {entry.get("risk_score", 0)}%'
                clipped_period = clip_period_to_range(entry["start_date"], entry["end_date"], period_start, period_end)
                if clipped_period is not None:
                    clipped_start, clipped_end = clipped_period
                    issue_meta[employee.id]["risk_dates"].update(
                        current_date.isoformat()
                        for current_date in iterate_dates(clipped_start, clipped_end)
                    )

    staffing_issue_meta = _get_staffing_issue_meta(
        employees,
        issue_employee_entries or employee_entries,
        period_start,
        period_end,
        today=today,
    )
    substitution_risk_meta = staffing_issue_meta["substitution_risks"]
    for employee_id, meta in substitution_risk_meta.items():
        if employee_id in issue_meta:
            issue_meta[employee_id]["has_high_risk"] = True
            if not issue_meta[employee_id]["risk_summary"]:
                issue_meta[employee_id]["risk_summary"] = meta["summary"]
            issue_meta[employee_id]["risk_dates"].update(meta["dates"])
            issue_meta[employee_id]["risk_events"].extend(meta.get("events", []))

    conflict_meta = staffing_issue_meta["conflicts"]
    for employee_id, meta in conflict_meta.items():
        if employee_id in issue_meta:
            issue_meta[employee_id]["has_conflict"] = True
            issue_meta[employee_id]["conflict_summary"] = meta["summary"]
            issue_meta[employee_id]["conflict_dates"] = meta["dates"]
            issue_meta[employee_id]["conflict_events"].extend(meta.get("events", []))

    return issue_meta

def build_calendar_base_data(year, employee_ids=None):
    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)
    employees_queryset = Employees.objects.select_related(
        "department",
        "department__staffing_rule",
        "employee_position",
        "employee_position__production_group",
    ).filter(
        is_active_employee=True,
        date_joined__lte=year_end,
    ).order_by(
        "last_name",
        "first_name",
        "middle_name",
    )
    if employee_ids is not None:
        employees_queryset = employees_queryset.filter(id__in=employee_ids)

    employees = list(employees_queryset)
    employee_day_status = {employee.id: {} for employee in employees}
    employee_entries = {employee.id: [] for employee in employees}

    records = get_vacation_requests_queryset().filter(
        start_date__lte=year_end,
        end_date__gte=year_start,
        status__in=CALENDAR_VISIBLE_STATUSES,
    )
    if employee_ids is not None:
        records = records.filter(employee_id__in=employee_ids)
    records = exclude_converted_paid_requests(
        records,
        employee_ids=employee_ids,
        start_date=year_start,
        end_date=year_end,
    )

    for record in records:
        clipped_period = clip_period_to_range(record.start_date, record.end_date, year_start, year_end)
        if clipped_period is None:
            continue

        clipped_start, clipped_end = clipped_period
        employee = record.employee
        production_group = (
            employee.employee_position.production_group
            if employee.employee_position_id and employee.employee_position
            else None
        )
        display_status = REQUEST_STATUS_TO_DISPLAY_STATUS[record.status]
        display_meta = DISPLAY_STATUS_UI[display_status]
        entry = {
            "employee_id": employee.id,
            "department_id": employee.department_id,
            "source_kind": "request",
            "source_id": record.id,
            "detail_url": reverse("vacation_detail", args=[record.id]),
            "detail_label": "Открыть заявку",
            "employee_name": employee.full_name,
            "employee_position": employee.position,
            "production_group_id": production_group.id if production_group else None,
            "production_group_name": production_group.name if production_group else "Не указана",
            "department_name": employee.department.name if employee.department else "Не указан",
            "status": record.status,
            "risk_score": record.risk_score,
            "risk_level": record.risk_level,
            "risk_label": _risk_label_for_level(record.risk_level),
            "is_active_absence": record.status in VacationRequest.ACTIVE_STATUSES,
            "display_status": display_status,
            "display_type": display_meta["display_type"],
            "display_label": display_meta["label"],
            "status_label": display_meta["label"],
            "source_label": display_meta["source_label"],
            "css_class": display_meta["css_class"],
            "status_icon": REQUEST_STATUS_UI[record.status]["icon"],
            "vacation_type_label": record.get_vacation_type_display(),
            "start_date": clipped_start,
            "end_date": clipped_end,
            "days": get_requested_days(clipped_start, clipped_end),
            "period_label": format_period_label(clipped_start, clipped_end),
            "sort_key": clipped_start.toordinal(),
        }
        employee_entries[employee.id].append(entry)

        for current_date in iterate_dates(clipped_start, clipped_end):
            current_status = employee_day_status[employee.id].get(current_date, DISPLAY_FREE)
            if DISPLAY_STATUS_PRIORITY[display_status] >= DISPLAY_STATUS_PRIORITY[current_status]:
                employee_day_status[employee.id][current_date] = display_status

    schedule_items = VacationScheduleItem.objects.select_related(
        "employee",
        "employee__department",
        "employee__employee_position",
        "employee__employee_position__production_group",
        "schedule",
        "created_from_vacation_request",
        "created_from_change_request",
    ).prefetch_related("change_requests").filter(
        start_date__lte=year_end,
        end_date__gte=year_start,
        status__in=SCHEDULE_STATUS_TO_DISPLAY_STATUS.keys(),
    )
    if employee_ids is not None:
        schedule_items = schedule_items.filter(employee_id__in=employee_ids)

    for item in schedule_items:
        if item.employee_id not in employee_entries:
            continue

        clipped_period = clip_period_to_range(item.start_date, item.end_date, year_start, year_end)
        if clipped_period is None:
            continue

        clipped_start, clipped_end = clipped_period
        employee = item.employee
        production_group = (
            employee.employee_position.production_group
            if employee.employee_position_id and employee.employee_position
            else None
        )
        calendar_status = SCHEDULE_STATUS_TO_CALENDAR_STATUS[item.status]
        display_status = SCHEDULE_STATUS_TO_DISPLAY_STATUS[item.status]
        display_meta = DISPLAY_STATUS_UI[display_status]
        detail_reference = get_schedule_item_detail_reference(item)
        prefetched_change_requests = getattr(item, "_prefetched_objects_cache", {}).get("change_requests")
        has_pending_change_request = (
            any(
                change_request.status == VacationScheduleChangeRequest.STATUS_PENDING
                for change_request in prefetched_change_requests
            )
            if prefetched_change_requests is not None
            else VacationScheduleChangeRequest.objects.filter(
                schedule_item_id=item.id,
                status=VacationScheduleChangeRequest.STATUS_PENDING,
            ).exists()
        )
        entry = {
            "employee_id": employee.id,
            "department_id": employee.department_id,
            "source_kind": "schedule",
            "source_id": item.id,
            "detail_url": detail_reference["detail_url"],
            "detail_label": detail_reference["detail_label"],
            "employee_name": employee.full_name,
            "employee_position": employee.position,
            "production_group_id": production_group.id if production_group else None,
            "production_group_name": production_group.name if production_group else "Не указана",
            "department_name": employee.department.name if employee.department else "Не указан",
            "status": calendar_status,
            "schedule_status": item.status,
            "risk_score": item.risk_score,
            "risk_level": item.risk_level,
            "risk_label": _risk_label_for_level(item.risk_level),
            "is_active_absence": item.status in VacationScheduleItem.ACTIVE_STATUSES,
            "display_status": display_status,
            "display_type": display_meta["display_type"],
            "display_label": display_meta["label"],
            "status_label": display_meta["label"],
            "source_label": _schedule_item_source_label(item),
            "css_class": display_meta["css_class"],
            "status_icon": REQUEST_STATUS_UI[calendar_status]["icon"],
            "vacation_type_label": item.get_vacation_type_display(),
            "start_date": clipped_start,
            "end_date": clipped_end,
            "days": get_requested_days(clipped_start, clipped_end),
            "period_label": format_period_label(clipped_start, clipped_end),
            "sort_key": clipped_start.toordinal(),
            "has_pending_change_request": has_pending_change_request,
        }
        employee_entries[employee.id].append(entry)

        for current_date in iterate_dates(clipped_start, clipped_end):
            current_status = employee_day_status[employee.id].get(current_date, DISPLAY_FREE)
            if DISPLAY_STATUS_PRIORITY[display_status] >= DISPLAY_STATUS_PRIORITY[current_status]:
                employee_day_status[employee.id][current_date] = display_status

    for employee_id, entries in employee_entries.items():
        entries.sort(key=lambda item: (item["sort_key"], -DISPLAY_STATUS_PRIORITY[item["display_status"]]))

    return employees, employee_day_status, employee_entries

def _build_employee_schedule_status_url(employee_id, year, status_key):
    status_meta = EMPLOYEE_SCHEDULE_STATUS_META[status_key]
    query = urlencode({
        "view": "year",
        "year": year,
        "issue": status_meta["issue"],
        "employee": employee_id,
        "calendar_modal": "employee_detail",
        "calendar_employee": employee_id,
    })
    return f"{reverse('calendar')}?{query}"

def _serialize_employee_schedule_status(employee_id, year, status_key):
    status_meta = EMPLOYEE_SCHEDULE_STATUS_META[status_key]
    tooltip_text_by_status = {
        "conflict": f"В графике на {year} год есть конфликт состава. Нажмите, чтобы открыть график сотрудника.",
        "risk": f"Конфликтов нет, но в графике на {year} год есть высокий риск. Нажмите, чтобы открыть график сотрудника.",
        "planned": f"На {year} год есть утвержденный график или одобренная заявка. Нажмите, чтобы открыть график сотрудника.",
        "empty": f"На {year} год нет запланированного отпуска. Нажмите, чтобы открыть график сотрудника.",
    }
    return {
        "key": status_key,
        "label": status_meta["label"],
        "short_label": status_meta["short_label"],
        "variant": status_meta["variant"],
        "icon": status_meta["icon"],
        "icon_type": status_meta["icon_type"],
        "tooltip_title": status_meta["label"],
        "tooltip_text": tooltip_text_by_status[status_key],
        "calendar_url": _build_employee_schedule_status_url(employee_id, year, status_key),
    }


def _get_schedule_status_issue_scope_employee_ids(target_employee_ids, year_end):
    target_employee_ids = {int(employee_id) for employee_id in target_employee_ids or [] if employee_id}
    if not target_employee_ids:
        return set()

    target_employees = list(
        Employees.objects.filter(
            id__in=target_employee_ids,
            is_active_employee=True,
            date_joined__lte=year_end,
        ).only("id", "department_id", "role", "is_enterprise_deputy", "is_active_employee", "date_joined")
    )
    department_ids = {employee.department_id for employee in target_employees if employee.department_id}
    scope_employee_ids = set(target_employee_ids)
    if department_ids:
        scope_employee_ids.update(
            Employees.objects.filter(
                department_id__in=department_ids,
                is_active_employee=True,
                date_joined__lte=year_end,
            )
            .exclude(role__in=Employees.SERVICE_ROLES)
            .values_list("id", flat=True)
        )

    if any(
        employee.role == Employees.ROLE_ENTERPRISE_HEAD or employee.is_enterprise_deputy
        for employee in target_employees
    ):
        enterprise_head_ids, enterprise_deputy_ids = get_enterprise_leadership_employee_ids(year_end)
        scope_employee_ids.update(enterprise_head_ids | enterprise_deputy_ids)

    return scope_employee_ids


def build_employee_schedule_status_map(employee_ids, year=None):
    target_employee_ids = [int(employee_id) for employee_id in dict.fromkeys(employee_ids or []) if employee_id]
    if not target_employee_ids:
        return {}

    year = int(year or timezone.localdate().year)
    year_start = date(year, 1, 1)
    year_end = date(year, 12, 31)
    status_key_by_employee = {employee_id: "empty" for employee_id in target_employee_ids}
    issue_relevant_absence_employee_ids = set()

    request_records = exclude_converted_paid_requests(
        VacationRequest.objects.filter(
            employee_id__in=target_employee_ids,
            start_date__lte=year_end,
            end_date__gte=year_start,
            status__in=CALENDAR_VISIBLE_STATUSES,
        ),
        employee_ids=target_employee_ids,
        start_date=year_start,
        end_date=year_end,
    ).values_list("employee_id", "status", "risk_level")
    for employee_id, request_status, risk_level in request_records:
        if request_status == VacationRequest.STATUS_APPROVED:
            status_key_by_employee[employee_id] = "planned"
        if risk_level == VacationRequest.RISK_HIGH:
            status_key_by_employee[employee_id] = "risk"
        if request_status in VacationRequest.ACTIVE_STATUSES:
            issue_relevant_absence_employee_ids.add(employee_id)

    schedule_records = VacationScheduleItem.objects.filter(
        employee_id__in=target_employee_ids,
        start_date__lte=year_end,
        end_date__gte=year_start,
        status__in=SCHEDULE_STATUS_TO_DISPLAY_STATUS.keys(),
    ).values_list("employee_id", "status", "risk_level")
    for employee_id, schedule_status, risk_level in schedule_records:
        if schedule_status in VacationScheduleItem.ACTIVE_STATUSES and status_key_by_employee[employee_id] != "risk":
            status_key_by_employee[employee_id] = "planned"
        if risk_level == VacationRequest.RISK_HIGH:
            status_key_by_employee[employee_id] = "risk"
        if schedule_status in CALENDAR_ISSUE_SCHEDULE_STATUSES:
            issue_relevant_absence_employee_ids.add(employee_id)

    issue_meta = {}
    if issue_relevant_absence_employee_ids:
        issue_scope_employee_ids = _get_schedule_status_issue_scope_employee_ids(
            issue_relevant_absence_employee_ids,
            year_end,
        )
        employees, _, employee_entries = build_calendar_base_data(year, employee_ids=issue_scope_employee_ids)
        issue_meta = _build_calendar_issue_meta(
            employees,
            employee_entries,
            None,
            year_start,
            year_end,
            today=timezone.localdate(),
        )

    status_map = {}
    for employee_id in target_employee_ids:
        employee_issue_meta = issue_meta.get(employee_id, {})
        if employee_issue_meta.get("has_conflict"):
            status_key = "conflict"
        elif employee_issue_meta.get("has_high_risk"):
            status_key = "risk"
        else:
            status_key = status_key_by_employee.get(employee_id, "empty")
        status_map[employee_id] = _serialize_employee_schedule_status(employee_id, year, status_key)

    return status_map

def _empty_calendar_display_counts():
    counts = {
        "schedule_days": 0,
        "request_days": 0,
        "changed_days": 0,
        "total_days": 0,
        "display_statuses": set(),
    }
    for status in (
        VacationRequest.STATUS_APPROVED,
        VacationRequest.STATUS_PENDING,
        VacationRequest.STATUS_REJECTED,
    ):
        counts[status] = 0
    return counts

def _add_entry_to_display_counts(counts, entry, days):
    if days <= 0:
        return

    counts["total_days"] += days
    counts["display_statuses"].add(entry["display_status"])
    if entry["status"] in (
        VacationRequest.STATUS_APPROVED,
        VacationRequest.STATUS_PENDING,
        VacationRequest.STATUS_REJECTED,
    ):
        counts[entry["status"]] += days

    if entry["display_type"] == "request":
        counts["request_days"] += days
    elif entry["display_status"] in (DISPLAY_SCHEDULE_TRANSFERRED, DISPLAY_SCHEDULE_CANCELLED):
        counts["changed_days"] += days
    else:
        counts["schedule_days"] += days

def _get_display_status_from_counts(counts):
    statuses = counts["display_statuses"]
    if len(statuses) > 1:
        return DISPLAY_MIXED
    if statuses:
        return next(iter(statuses))
    return DISPLAY_FREE

def _calendar_entry_stage_meta(start_date, end_date, today=None):
    today = today or timezone.localdate()
    if end_date < today:
        return {
            "stage": "past",
            "stage_label": "Прошел",
            "stage_icon": "task_alt",
        }
    if start_date <= today <= end_date:
        return {
            "stage": "current",
            "stage_label": "Идет сейчас",
            "stage_icon": "beach_access",
        }
    return {
        "stage": "upcoming",
        "stage_label": "Предстоит",
        "stage_icon": "event",
    }

def _calendar_cell_stage(start_date, end_date, status, today=None):
    if status == DISPLAY_FREE:
        return ""
    return _calendar_entry_stage_meta(start_date, end_date, today=today)["stage"]

def _serialize_calendar_entry(
    entry,
    current_employee=None,
    employee=None,
    today=None,
    conflict_dates=None,
    conflict_summary="",
    risk_summary="",
):
    today = today or timezone.localdate()
    conflict_dates = conflict_dates or set()
    entry_dates = {
        current_date.isoformat()
        for current_date in iterate_dates(entry["start_date"], entry["end_date"])
    }
    has_conflict = bool(entry_dates & conflict_dates)
    transfer_action = {}
    if entry.get("source_kind") == "schedule" and employee is not None:
        transfer_action = build_schedule_change_transfer_action(
            actor=current_employee,
            employee=employee,
            schedule_item_id=entry["source_id"],
            start_date=entry["start_date"],
            end_date=entry["end_date"],
            vacation_type_label=entry["vacation_type_label"],
            schedule_status=entry.get("schedule_status"),
            today=today,
            pending_change_exists=entry.get("has_pending_change_request"),
        )
    stage_meta = _calendar_entry_stage_meta(entry["start_date"], entry["end_date"], today=today)
    payload = {
        "source_kind": entry.get("source_kind", ""),
        "source_id": entry.get("source_id"),
        "detail_url": entry.get("detail_url", ""),
        "detail_label": entry.get("detail_label", ""),
        "period_label": entry["period_label"],
        "status_label": entry["status_label"],
        "display_label": entry["display_label"],
        "source_label": entry["source_label"],
        "display_type": entry["display_type"],
        "status": entry["display_status"],
        "css_class": entry["css_class"],
        "risk_score": entry.get("risk_score", 0),
        "risk_level": entry.get("risk_level", VacationRequest.RISK_LOW),
        "risk_label": entry.get("risk_label") or _risk_label_for_level(entry.get("risk_level", VacationRequest.RISK_LOW)),
        "has_high_risk": entry.get("risk_level") == VacationRequest.RISK_HIGH,
        "has_conflict": has_conflict,
        "conflict_summary": conflict_summary if has_conflict else "",
        "risk_short_reason": "" if has_conflict else (risk_summary if entry.get("risk_level") == VacationRequest.RISK_HIGH else ""),
        "anchor": _build_entry_anchor(entry),
        "vacation_type_label": entry["vacation_type_label"],
        "days": entry["days"],
        "stage": stage_meta["stage"],
        "stage_label": stage_meta["stage_label"],
        "stage_icon": stage_meta["stage_icon"],
        "can_request_transfer": False,
        "transfer_url": "",
        "transfer_preview_url": "",
        "transfer_title": "",
        "transfer_employee_id": "",
        "transfer_exclude_schedule_item_id": "",
        "transfer_year": "",
        "transfer_action_label": "",
        "transfer_submit_label": "",
        "transfer_hint": "",
        "transfer_modal_title": "",
        "transfer_modal_subtitle": "",
    }
    payload.update(transfer_action)
    return payload

def build_month_timeline_cells(day_map, year, month, today, conflict_dates=None, risk_dates=None):
    conflict_dates = conflict_dates or set()
    risk_dates = risk_dates or set()
    days_in_month = calendar.monthrange(year, month)[1]
    cells = []
    for day in range(1, days_in_month + 1):
        current_date = date(year, month, day)
        current_date_iso = current_date.isoformat()
        status = day_map.get(current_date, DISPLAY_FREE)
        previous_status = day_map.get(current_date - timedelta(days=1), DISPLAY_FREE) if day > 1 else DISPLAY_FREE
        next_status = day_map.get(current_date + timedelta(days=1), DISPLAY_FREE) if day < days_in_month else DISPLAY_FREE
        is_start = status != DISPLAY_FREE and previous_status != status
        is_end = status != DISPLAY_FREE and next_status != status
        has_conflict = current_date_iso in conflict_dates
        has_high_risk = not has_conflict and current_date_iso in risk_dates
        issue_tooltip = ""
        if has_conflict:
            issue_tooltip = " • Есть конфликт состава"
        elif has_high_risk:
            issue_tooltip = " • Есть высокий риск"
        cells.append(
            {
                "day": day,
                "date_iso": current_date_iso,
                "status": status,
                "display_status": status,
                "css_class": DISPLAY_STATUS_UI[status]["css_class"],
                "stage": _calendar_cell_stage(current_date, current_date, status, today=today),
                "has_high_risk": has_high_risk,
                "has_conflict": has_conflict,
                "is_weekend": current_date.weekday() >= 5,
                "is_today": current_date == today,
                "is_start": is_start,
                "is_end": is_end,
                "is_single": is_start and is_end,
                "tooltip": f'{day:02d}.{month:02d}.{year} • {VACATION_STATUS_META[status]["label"]}{issue_tooltip}',
            }
        )
    return cells

def build_year_month_cells(entries, year, conflict_dates=None, risk_dates=None, today=None):
    conflict_dates = conflict_dates or set()
    risk_dates = risk_dates or set()
    today = today or timezone.localdate()
    month_cells = []
    for month_number in range(1, 13):
        month_start = date(year, month_number, 1)
        month_end = get_month_end(month_start)
        days_in_month = calendar.monthrange(year, month_number)[1]
        counts = _empty_calendar_display_counts()
        segments = []
        segment_stages = []
        has_high_risk = False
        has_conflict = False
        for entry in entries:
            overlap = clip_period_to_range(entry["start_date"], entry["end_date"], month_start, month_end)
            if overlap is None:
                continue

            overlap_start, overlap_end = overlap
            overlap_days = get_requested_days(overlap_start, overlap_end)
            _add_entry_to_display_counts(counts, entry, overlap_days)
            overlap_date_iso = {
                current_date.isoformat()
                for current_date in iterate_dates(overlap_start, overlap_end)
            }
            if entry.get("risk_level") == VacationRequest.RISK_HIGH or bool(overlap_date_iso & risk_dates):
                has_high_risk = True
            if bool(overlap_date_iso & conflict_dates):
                has_conflict = True

            segment_stage = _calendar_cell_stage(
                overlap_start,
                overlap_end,
                entry["display_status"],
                today=today,
            )
            if segment_stage:
                segment_stages.append(segment_stage)

            segments.append(
                {
                    "status": entry["display_status"],
                    "display_status": entry["display_status"],
                    "css_class": entry["css_class"],
                    "stage": segment_stage,
                    "days": overlap_days,
                    "offset_percent": round(((overlap_start.day - 1) / days_in_month) * 100, 1),
                    "width_percent": round((overlap_days / days_in_month) * 100, 1),
                }
            )

        busy_days = counts["total_days"]
        status_key = _get_display_status_from_counts(counts)

        segments.sort(
            key=lambda segment: (
                segment["offset_percent"],
                -DISPLAY_STATUS_PRIORITY.get(segment["status"], 0),
            )
        )
        issue_tooltip = ""
        if has_conflict:
            issue_tooltip = " • Есть конфликт состава"
        elif has_high_risk:
            issue_tooltip = " • Есть высокий риск"

        stage_key = ""
        if "current" in segment_stages:
            stage_key = "current"
        elif "upcoming" in segment_stages:
            stage_key = "upcoming"
        elif segment_stages and all(stage == "past" for stage in segment_stages):
            stage_key = "past"

        month_cells.append(
            {
                "month_name": RUSSIAN_MONTH_NAMES[month_number - 1],
                "month_short": RUSSIAN_MONTH_SHORT_NAMES[month_number - 1],
                "month_number": month_number,
                "busy_days": busy_days,
                "status": status_key,
                "display_status": status_key,
                "stage": stage_key,
                "schedule_days": counts["schedule_days"],
                "request_days": counts["request_days"],
                "changed_days": counts["changed_days"],
                "has_high_risk": has_high_risk,
                "has_conflict": has_conflict,
                "issue_tooltip": issue_tooltip,
                "segments": segments,
                "approved_days": counts[VacationRequest.STATUS_APPROVED],
                "pending_days": counts[VacationRequest.STATUS_PENDING],
                "rejected_days": counts[VacationRequest.STATUS_REJECTED],
            }
        )

    return month_cells

def build_calendar_rows(
    employees,
    employee_day_status,
    employee_entries,
    year,
    month,
    view_mode,
    today,
    current_employee=None,
    issue_employee_entries=None,
    issue_filter="all",
    detail_employee_ids=None,
):
    detail_employee_id_set = None
    if detail_employee_ids is not None:
        detail_employee_id_set = {
            int(employee_id)
            for employee_id in detail_employee_ids
            if str(employee_id).isdigit()
        }
    period_start = date(year, 1, 1) if view_mode == "year" else date(year, month, 1)
    period_end = date(year, 12, 31) if view_mode == "year" else date(year, month, calendar.monthrange(year, month)[1])
    rows = []
    details = {}
    issue_meta = _build_calendar_issue_meta(
        employees,
        employee_entries,
        issue_employee_entries,
        period_start,
        period_end,
        today=today,
    )
    employee_names_by_id = {employee.id: employee.full_name for employee in employees}
    for entries_by_employee in (employee_entries, issue_employee_entries or {}):
        for entries in entries_by_employee.values():
            for entry in entries:
                if entry.get("employee_name"):
                    employee_names_by_id.setdefault(entry["employee_id"], entry["employee_name"])

    for employee in employees:
        day_map = employee_day_status.get(employee.id, {})
        entries = employee_entries.get(employee.id, [])
        identity = get_employee_identity_presentation(employee)
        production_group = (
            employee.employee_position.production_group
            if employee.employee_position_id and employee.employee_position
            else None
        )
        profile_url = _employee_profile_url(employee.id)

        selected_entries = [
            entry for entry in entries if clip_period_to_range(entry["start_date"], entry["end_date"], period_start, period_end)
        ]
        upcoming_entry = next((entry for entry in entries if entry["end_date"] >= today), None)
        period_counts = _empty_calendar_display_counts()
        year_counts = _empty_calendar_display_counts()
        year_start = date(year, 1, 1)
        year_end = date(year, 12, 31)
        for entry in entries:
            year_overlap = clip_period_to_range(entry["start_date"], entry["end_date"], year_start, year_end)
            if year_overlap is not None:
                _add_entry_to_display_counts(year_counts, entry, get_requested_days(*year_overlap))
            period_overlap = clip_period_to_range(entry["start_date"], entry["end_date"], period_start, period_end)
            if period_overlap is not None:
                _add_entry_to_display_counts(period_counts, entry, get_requested_days(*period_overlap))

        row_status = _get_display_status_from_counts(period_counts)
        employee_issue_meta = issue_meta.get(
            employee.id,
            {
                "has_high_risk": False,
                "has_conflict": False,
                "risk_summary": "",
                "conflict_summary": "",
                "risk_dates": set(),
                "conflict_dates": set(),
                "risk_events": [],
                "conflict_events": [],
            },
        )
        if issue_filter == "risk" and not employee_issue_meta["has_high_risk"]:
            continue
        if issue_filter == "conflict" and not employee_issue_meta["has_conflict"]:
            continue
        issue_label = "Конфликт" if employee_issue_meta["has_conflict"] else (
            "Высокий риск" if employee_issue_meta["has_high_risk"] else "Проблем нет"
        )
        issue_description = employee_issue_meta["conflict_summary"] or employee_issue_meta["risk_summary"] or "В выбранном периоде критичных проблем не найдено."
        issue_chips = _build_row_issue_chips(employee_issue_meta, issue_filter)
        is_year_view = view_mode == "year"

        rows.append(
            {
                "employee_id": employee.id,
                "employee_name": employee.full_name,
                "profile_url": profile_url,
                "role_icon": identity["employee_role_icon"],
                "role_icon_type": identity["employee_role_icon_type"],
                "role_variant": identity["employee_role_variant"],
                "role_label": identity["employee_role_label"],
                "has_high_risk": employee_issue_meta["has_high_risk"],
                "has_conflict": employee_issue_meta["has_conflict"],
                "issue_label": issue_label,
                "issue_description": issue_description,
                "issue_chips": issue_chips,
                "position": employee.position,
                "production_group": identity["employee_production_group_label"],
                "department": identity["employee_department_label"],
                "department_id": employee.department_id or 0,
                "department_name": identity["employee_department_label"],
                "production_group_id": production_group.id if production_group else 0,
                "production_group_name": identity["employee_production_group_label"],
                "employee_department_label": identity["employee_department_label"],
                "employee_production_group_label": identity["employee_production_group_label"],
                "employee_management_badges": identity["employee_management_badges"],
                "employee_new_hire_badge": identity["employee_new_hire_badge"],
                "status": row_status,
                "display_status": row_status,
                "selected_schedule_days": period_counts["schedule_days"],
                "selected_request_days": period_counts["request_days"],
                "selected_changed_days": period_counts["changed_days"],
                "selected_total_days": period_counts["total_days"],
                "selected_approved_days": period_counts[VacationRequest.STATUS_APPROVED],
                "selected_pending_days": period_counts[VacationRequest.STATUS_PENDING],
                "selected_rejected_days": period_counts[VacationRequest.STATUS_REJECTED],
                "year_schedule_days": year_counts["schedule_days"],
                "year_request_days": year_counts["request_days"],
                "year_changed_days": year_counts["changed_days"],
                "year_total_days": year_counts["total_days"],
                "year_approved_days": year_counts[VacationRequest.STATUS_APPROVED],
                "year_pending_days": year_counts[VacationRequest.STATUS_PENDING],
                "year_rejected_days": year_counts[VacationRequest.STATUS_REJECTED],
                "cells": build_year_month_cells(
                    entries,
                    year,
                    employee_issue_meta["conflict_dates"],
                    employee_issue_meta["risk_dates"],
                    today=today,
                )
                if view_mode == "year"
                else build_month_timeline_cells(
                    day_map,
                    year,
                    month,
                    today,
                    employee_issue_meta["conflict_dates"],
                    employee_issue_meta["risk_dates"],
                ),
            }
        )

        should_build_detail = detail_employee_id_set is None or employee.id in detail_employee_id_set
        if should_build_detail:
            risk_details = _build_calendar_risk_details(
                employee.id,
                employee_issue_meta,
                issue_label,
                issue_description,
                employee_names_by_id,
                today=today,
            )
            selected_entry_identities = {_entry_identity(entry) for entry in selected_entries}
            secondary_entries = [] if is_year_view else [
                entry for entry in entries if _entry_identity(entry) not in selected_entry_identities
            ]
            serialized_selected_entries = [
                _serialize_calendar_entry(
                    entry,
                    current_employee,
                    employee,
                    today,
                    employee_issue_meta["conflict_dates"],
                    employee_issue_meta["conflict_summary"],
                    employee_issue_meta["risk_summary"],
                )
                for entry in selected_entries
            ]
            serialized_year_entries = [
                _serialize_calendar_entry(
                    entry,
                    current_employee,
                    employee,
                    today,
                    employee_issue_meta["conflict_dates"],
                    employee_issue_meta["conflict_summary"],
                    employee_issue_meta["risk_summary"],
                )
                for entry in entries
            ]
            serialized_primary_entries = serialized_year_entries if is_year_view else serialized_selected_entries
            serialized_secondary_entries = [
                _serialize_calendar_entry(
                    entry,
                    current_employee,
                    employee,
                    today,
                    employee_issue_meta["conflict_dates"],
                    employee_issue_meta["conflict_summary"],
                    employee_issue_meta["risk_summary"],
                )
                for entry in secondary_entries
            ]
            details[str(employee.id)] = {
                "employee_name": employee.full_name,
                "position": employee.position,
                "production_group": identity["employee_production_group_label"],
                "department": identity["employee_department_label"],
                "profile_url": profile_url,
                "role_icon": identity["employee_role_icon"],
                "role_icon_type": identity["employee_role_icon_type"],
                "role_variant": identity["employee_role_variant"],
                "role_label": identity["employee_role_label"],
                "employee_management_badges": identity["employee_management_badges"],
                "employee_new_hire_badge": identity["employee_new_hire_badge"],
                "has_high_risk": employee_issue_meta["has_high_risk"],
                "has_conflict": employee_issue_meta["has_conflict"],
                "issue_label": issue_label,
                "issue_description": issue_description,
                "risk_summary": employee_issue_meta["risk_summary"],
                "conflict_summary": employee_issue_meta["conflict_summary"],
                "risk_details": risk_details,
                "view_mode": view_mode,
                "is_year_view": is_year_view,
                "selected_period_label": f"{RUSSIAN_MONTH_NAMES[month - 1]} {year}" if view_mode == "month" else f"Годовой обзор {year}",
                "selected_schedule_days": period_counts["schedule_days"],
                "selected_request_days": period_counts["request_days"],
                "selected_changed_days": period_counts["changed_days"],
                "selected_total_days": period_counts["total_days"],
                "selected_approved_days": period_counts[VacationRequest.STATUS_APPROVED],
                "selected_pending_days": period_counts[VacationRequest.STATUS_PENDING],
                "selected_rejected_days": period_counts[VacationRequest.STATUS_REJECTED],
                "year_schedule_days": year_counts["schedule_days"],
                "year_request_days": year_counts["request_days"],
                "year_changed_days": year_counts["changed_days"],
                "year_total_days": year_counts["total_days"],
                "year_approved_days": year_counts[VacationRequest.STATUS_APPROVED],
                "year_pending_days": year_counts[VacationRequest.STATUS_PENDING],
                "year_rejected_days": year_counts[VacationRequest.STATUS_REJECTED],
                "upcoming_label": upcoming_entry["period_label"] if upcoming_entry else "Ближайший отпуск не запланирован",
                "upcoming_status": upcoming_entry["status_label"] if upcoming_entry else "",
                "upcoming_anchor": _build_entry_anchor(upcoming_entry) if upcoming_entry else None,
                "primary_entries_title": "Записи за год" if is_year_view else "Отпуска в выбранном месяце",
                "primary_entries_empty": "За этот год записей пока нет." if is_year_view else "В выбранном месяце отпусков нет.",
                "primary_entries": serialized_primary_entries,
                "secondary_entries_title": "Остальные записи за год",
                "secondary_entries_empty": "Других записей за год нет.",
                "secondary_entries": serialized_secondary_entries,
                "selected_entries": serialized_selected_entries,
                "year_entries": serialized_year_entries,
            }

    return rows, details

def build_calendar_month_totals(calendar_rows):
    totals = []
    for month_number, month_short in enumerate(RUSSIAN_MONTH_SHORT_NAMES, start=1):
        employee_count = 0
        busy_days = 0
        risk_count = 0
        conflict_count = 0

        for row in calendar_rows:
            cells = row.get("cells") or []
            if len(cells) < month_number:
                continue

            cell = cells[month_number - 1]
            cell_busy_days = cell.get("busy_days", 0)
            if cell_busy_days:
                employee_count += 1
                busy_days += cell_busy_days
            if cell.get("has_high_risk"):
                risk_count += 1
            if cell.get("has_conflict"):
                conflict_count += 1

        totals.append(
            {
                "month_number": month_number,
                "month_short": month_short,
                "employee_count": employee_count,
                "busy_days": busy_days,
                "risk_count": risk_count,
                "risk_count_label": _format_calendar_entry_count(risk_count),
                "conflict_count": conflict_count,
                "conflict_count_label": _format_calendar_entry_count(conflict_count),
            }
        )

    return totals


def _format_calendar_entry_count(value):
    value = int(value or 0)
    value_mod_100 = value % 100
    value_mod_10 = value % 10
    if value_mod_100 not in range(11, 15) and value_mod_10 == 1:
        word = "запись"
    elif value_mod_100 not in range(11, 15) and 2 <= value_mod_10 <= 4:
        word = "записи"
    else:
        word = "записей"
    return f"{value} {word}"


def _parse_calendar_iso_date(value):
    try:
        return date.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None

def _get_serialized_entry_period(entry):
    anchor = entry.get("anchor") or {}
    start_date = _parse_calendar_iso_date(anchor.get("start_date"))
    end_date = _parse_calendar_iso_date(anchor.get("end_date"))
    if start_date is None or end_date is None:
        return None
    return start_date, end_date

def _iter_month_detail_entries(detail, month_start, month_end):
    seen = set()
    for entry in detail.get("year_entries") or detail.get("primary_entries") or []:
        anchor = entry.get("anchor") or {}
        entry_key = (
            anchor.get("employee_id"),
            anchor.get("source_kind"),
            anchor.get("source_id"),
            anchor.get("start_date"),
            anchor.get("end_date"),
        )
        if entry_key in seen:
            continue
        seen.add(entry_key)

        entry_period = _get_serialized_entry_period(entry)
        if entry_period is None:
            continue

        overlap = clip_period_to_range(entry_period[0], entry_period[1], month_start, month_end)
        if overlap is None:
            continue

        yield entry, overlap[0], overlap[1]

def _clip_problem_to_month(problem, month_start, month_end):
    start_date = _parse_calendar_iso_date(problem.get("start_date"))
    end_date = _parse_calendar_iso_date(problem.get("end_date"))
    if start_date is None or end_date is None:
        return None

    overlap = clip_period_to_range(start_date, end_date, month_start, month_end)
    if overlap is None:
        return None

    overlap_start, overlap_end = overlap
    clipped_problem = dict(problem)
    clipped_problem["start_date"] = overlap_start.isoformat()
    clipped_problem["end_date"] = overlap_end.isoformat()
    clipped_problem["period_label"] = _format_problem_period_label(overlap_start, overlap_end)
    return clipped_problem

def _problem_identity(problem):
    return (
        problem.get("kind", ""),
        problem.get("start_date", ""),
        problem.get("end_date", ""),
        problem.get("title", ""),
        problem.get("text", ""),
        problem.get("impact_label", ""),
        problem.get("substitution_label", ""),
        tuple(problem.get("affected_employee_ids") or []),
        tuple(problem.get("affected_names") or []),
        problem.get("extra_affected_count", 0),
    )

def _build_month_detail_day_map(year, month_number):
    return {
        day: {
            "day": day,
            "date_iso": date(year, month_number, day).isoformat(),
            "weekday": WEEKDAY_SHORT_NAMES[date(year, month_number, day).weekday()],
            "is_weekend": date(year, month_number, day).weekday() >= 5,
            "employee_ids": set(),
            "employee_count": 0,
            "has_high_risk": False,
            "has_conflict": False,
            "status": "free",
        }
        for day in range(1, calendar.monthrange(year, month_number)[1] + 1)
    }

def _month_detail_status(day):
    if day["has_conflict"]:
        return "conflict"
    if day["has_high_risk"]:
        return "risk"
    if day["employee_count"]:
        return "busy"
    return "free"

def _build_month_detail_groups(absence_groups):
    groups = []
    for group in absence_groups.values():
        employees = sorted(group["employees"].values(), key=lambda item: item["employee_name"])
        groups.append(
            {
                "department": group["department"],
                "production_group": group["production_group"],
                "employee_count": len(employees),
                "days": sum(employee["days"] for employee in employees),
                "employees": employees,
            }
        )

    return sorted(groups, key=lambda item: (item["department"], item["production_group"]))

def build_calendar_month_details(calendar_rows, calendar_details, year, month_numbers=None):
    selected_month_numbers = None
    if month_numbers is not None:
        selected_month_numbers = {
            int(month_number)
            for month_number in month_numbers
            if str(month_number).isdigit() and 1 <= int(month_number) <= 12
        }

    details = {}
    for month_number, month_name in enumerate(RUSSIAN_MONTH_NAMES, start=1):
        if selected_month_numbers is not None and month_number not in selected_month_numbers:
            continue

        month_start = date(year, month_number, 1)
        month_end = get_month_end(month_start)
        day_map = _build_month_detail_day_map(year, month_number)
        absence_groups = {}
        problem_map = {}
        employee_count = 0
        busy_days = 0
        schedule_days = 0
        request_days = 0
        changed_days = 0
        risk_count = 0
        conflict_count = 0

        for row in calendar_rows:
            cells = row.get("cells") or []
            if len(cells) < month_number:
                continue

            cell = cells[month_number - 1]
            cell_busy_days = int(cell.get("busy_days") or 0)
            if cell_busy_days:
                employee_count += 1
                busy_days += cell_busy_days
                schedule_days += int(cell.get("schedule_days") or 0)
                request_days += int(cell.get("request_days") or 0)
                changed_days += int(cell.get("changed_days") or 0)
            if cell.get("has_high_risk"):
                risk_count += 1
            if cell.get("has_conflict"):
                conflict_count += 1

            employee_id = row.get("employee_id")
            detail = calendar_details.get(str(employee_id)) or {}
            employee_month_dates = set()
            employee_entries = []
            for entry, overlap_start, overlap_end in _iter_month_detail_entries(detail, month_start, month_end):
                period_dates = set(iterate_dates(overlap_start, overlap_end))
                employee_month_dates.update(period_dates)
                for current_date in period_dates:
                    day = day_map[current_date.day]
                    day["employee_ids"].add(employee_id)
                    if entry.get("has_high_risk"):
                        day["has_high_risk"] = True

                employee_entries.append(
                    {
                        "period_label": format_period_label(overlap_start, overlap_end),
                        "status": entry.get("status", ""),
                        "status_label": entry.get("status_label", ""),
                        "source_label": entry.get("source_label", ""),
                        "vacation_type_label": entry.get("vacation_type_label", ""),
                        "days": get_requested_days(overlap_start, overlap_end),
                        "has_high_risk": bool(entry.get("has_high_risk")),
                        "has_conflict": bool(entry.get("has_conflict")),
                        "anchor": entry.get("anchor"),
                    }
                )

            if employee_entries:
                group_key = (
                    detail.get("department") or row.get("employee_department_label") or "Не указан",
                    detail.get("production_group") or row.get("employee_production_group_label") or "Не указана",
                )
                group = absence_groups.setdefault(
                    group_key,
                    {
                        "department": group_key[0],
                        "production_group": group_key[1],
                        "employees": {},
                    },
                )
                group["employees"][employee_id] = {
                    "employee_id": employee_id,
                    "employee_name": detail.get("employee_name") or row.get("employee_name") or "",
                    "profile_url": detail.get("profile_url") or row.get("profile_url") or "",
                    "days": len(employee_month_dates),
                    "entries": employee_entries,
                }

            for problem in (detail.get("risk_details") or {}).get("problems") or []:
                clipped_problem = _clip_problem_to_month(problem, month_start, month_end)
                if clipped_problem is None:
                    continue
                problem_map[_problem_identity(clipped_problem)] = clipped_problem
                problem_is_conflict = clipped_problem.get("severity") == "conflict"
                for current_date in iterate_dates(
                    _parse_calendar_iso_date(clipped_problem["start_date"]),
                    _parse_calendar_iso_date(clipped_problem["end_date"]),
                ):
                    day = day_map[current_date.day]
                    if problem_is_conflict:
                        day["has_conflict"] = True
                    else:
                        day["has_high_risk"] = True

        days = []
        for day in day_map.values():
            day["employee_count"] = len(day.pop("employee_ids"))
            day["status"] = _month_detail_status(day)
            days.append(day)

        problems = sorted(
            problem_map.values(),
            key=lambda item: (item.get("start_date", ""), item.get("severity") != "conflict", item.get("title", "")),
        )

        details[str(month_number)] = {
            "month_number": month_number,
            "month_name": month_name,
            "month_short": RUSSIAN_MONTH_SHORT_NAMES[month_number - 1],
            "year": year,
            "title": f"{month_name} {year}",
            "employee_count": employee_count,
            "busy_days": busy_days,
            "schedule_days": schedule_days,
            "request_days": request_days,
            "changed_days": changed_days,
            "risk_count": risk_count,
            "conflict_count": conflict_count,
            "days": days,
            "problems": problems,
            "absence_groups": _build_month_detail_groups(absence_groups),
        }

    return details

def build_calendar_summary(employee_entries, year, month, view_mode):
    period_start = date(year, 1, 1) if view_mode == "year" else date(year, month, 1)
    period_end = date(year, 12, 31) if view_mode == "year" else date(year, month, calendar.monthrange(year, month)[1])
    employees_in_period = set()
    counts = _empty_calendar_display_counts()

    for entries in employee_entries.values():
        for entry in entries:
            overlap = clip_period_to_range(entry["start_date"], entry["end_date"], period_start, period_end)
            if overlap is None:
                continue

            overlap_start, overlap_end = overlap
            overlap_days = get_requested_days(overlap_start, overlap_end)
            employees_in_period.add(entry["employee_id"])
            _add_entry_to_display_counts(counts, entry, overlap_days)

    return [
        {
            "icon": "groups",
            "label": "Сотрудников в периоде",
            "value": len(employees_in_period),
            "hint": "У кого есть отпуск или заявка в выбранном диапазоне.",
        },
        {
            "icon": "event_available",
            "label": "По годовому графику",
            "value": counts["schedule_days"],
            "hint": "Дни из утвержденного или запланированного графика отпусков.",
        },
        {
            "icon": "watch_later",
            "label": "Заявки и изменения",
            "value": counts["request_days"] + counts["changed_days"],
            "hint": "Внеплановые заявки, переносы и отмененные пункты графика.",
        },
    ]
