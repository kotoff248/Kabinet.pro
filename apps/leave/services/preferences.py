import random
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.core.models import Notification
from apps.core.services.notifications import mark_notifications_done_by_dedupe_prefix
from apps.employees.models import Employees
from apps.leave.models import VacationPreference, VacationPreferenceCollection, VacationSchedule, VacationScheduleItem

from .constants import LEAVE_ADVANCE_MONTHS
from .dates import add_months_safe, get_chargeable_leave_days, quantize_leave_days
from .employee_presentation import get_employee_identity_presentation
from .ledger import get_employee_available_balance, get_employee_entitlement_rows
from .notifications import notify_preferences_collection_started
from .validation import MIN_CONTINUOUS_PAID_LEAVE_DAYS


DEMO_FILL_MIN_PERCENT = 72
DEMO_FILL_MAX_PERCENT = 85
DEMO_SKIP_PERCENT = 7


def preference_response_url(year):
    return reverse("vacation_preferences", args=[year])


def preference_readiness_url(year):
    return reverse("preference_collection_readiness", args=[year])


def preference_draft_url(year):
    return reverse("schedule_draft_detail", args=[year])


def preference_notification_dedupe_key(year, employee_id):
    return f"{Notification.TYPE_PREFERENCES_COLLECTION_STARTED}:{year}:{employee_id}"


def preference_notification_dedupe_prefix(year):
    return f"{Notification.TYPE_PREFERENCES_COLLECTION_STARTED}:{year}:"


def _format_days(value):
    value = quantize_leave_days(value or Decimal("0"))
    if value == value.to_integral_value():
        return str(int(value))
    return str(value).replace(".", ",").rstrip("0").rstrip(",")


def _days_label(value):
    return f"{_format_days(value)} д."


def preference_remainder_policy_label(policy):
    return dict(VacationPreference.REMAINDER_POLICY_CHOICES).get(
        policy or VacationPreference.REMAINDER_AUTO,
        "Можно распределить автоматически",
    )


def get_preference_planning_year(today=None):
    today = today or timezone.localdate()
    return today.year + 1


def get_paid_leave_available_from(employee):
    return add_months_safe(employee.date_joined, LEAVE_ADVANCE_MONTHS)


def employee_can_join_preference_collection(employee, year):
    if employee is None:
        return False
    if not getattr(employee, "is_active_employee", True):
        return False
    if employee.role in Employees.SERVICE_ROLES:
        return False
    year_end = date(year, 12, 31)
    return employee.date_joined <= year_end and get_paid_leave_available_from(employee) <= year_end


def get_eligible_preference_employees(year):
    year_end = date(year, 12, 31)
    candidates = (
        Employees.objects.select_related("department", "employee_position")
        .select_related("employee_position__production_group")
        .filter(is_active_employee=True, date_joined__lte=year_end)
        .exclude(role__in=Employees.SERVICE_ROLES)
        .order_by("last_name", "first_name", "middle_name", "id")
    )
    return [employee for employee in candidates if employee_can_join_preference_collection(employee, year)]


def _first_preference(employee, year, priority):
    return (
        VacationPreference.objects.filter(employee=employee, year=year, priority=priority)
        .order_by("created_at", "id")
        .first()
    )


def get_employee_preference_pair(employee, year):
    return {
        VacationPreference.PRIORITY_PRIMARY: _first_preference(employee, year, VacationPreference.PRIORITY_PRIMARY),
        VacationPreference.PRIORITY_BACKUP: _first_preference(employee, year, VacationPreference.PRIORITY_BACKUP),
    }


def get_employee_preference_pair_map(employee_ids, year):
    employee_ids = [int(employee_id) for employee_id in dict.fromkeys(employee_ids or []) if employee_id]
    pair_by_employee = {
        employee_id: {
            VacationPreference.PRIORITY_PRIMARY: None,
            VacationPreference.PRIORITY_BACKUP: None,
        }
        for employee_id in employee_ids
    }
    if not employee_ids:
        return pair_by_employee

    preferences = VacationPreference.objects.filter(employee_id__in=employee_ids, year=year).order_by(
        "employee_id",
        "priority",
        "created_at",
        "id",
    )
    for preference in preferences:
        if preference.priority not in {
            VacationPreference.PRIORITY_PRIMARY,
            VacationPreference.PRIORITY_BACKUP,
        }:
            continue
        pair = pair_by_employee.setdefault(
            preference.employee_id,
            {
                VacationPreference.PRIORITY_PRIMARY: None,
                VacationPreference.PRIORITY_BACKUP: None,
            },
        )
        if pair[preference.priority] is None:
            pair[preference.priority] = preference

    return pair_by_employee


def get_employee_preference_state(employee, year):
    preferences = list(VacationPreference.objects.filter(employee=employee, year=year))
    if not preferences:
        return "missing"
    if any(preference.status == VacationPreference.STATUS_SKIPPED for preference in preferences):
        return VacationPreference.STATUS_SKIPPED

    pair = get_employee_preference_pair(employee, year)
    primary = pair[VacationPreference.PRIORITY_PRIMARY]
    backup = pair[VacationPreference.PRIORITY_BACKUP]
    if (
        primary is not None
        and backup is not None
        and primary.status == VacationPreference.STATUS_FILLED
        and backup.status == VacationPreference.STATUS_FILLED
    ):
        return VacationPreference.STATUS_FILLED
    return VacationPreference.STATUS_PENDING


def get_employee_preference_state_map(employee_ids, year):
    employee_ids = [int(employee_id) for employee_id in dict.fromkeys(employee_ids or []) if employee_id]
    if not employee_ids:
        return {}

    preferences_by_employee = defaultdict(list)
    preferences = VacationPreference.objects.filter(employee_id__in=employee_ids, year=year).order_by(
        "employee_id",
        "created_at",
        "id",
    )
    for preference in preferences:
        preferences_by_employee[preference.employee_id].append(preference)

    state_by_employee = {}
    for employee_id in employee_ids:
        employee_preferences = preferences_by_employee.get(employee_id, [])
        if not employee_preferences:
            state_by_employee[employee_id] = "missing"
            continue
        if any(preference.status == VacationPreference.STATUS_SKIPPED for preference in employee_preferences):
            state_by_employee[employee_id] = VacationPreference.STATUS_SKIPPED
            continue

        first_by_priority = {}
        for preference in employee_preferences:
            first_by_priority.setdefault(preference.priority, preference)
        primary = first_by_priority.get(VacationPreference.PRIORITY_PRIMARY)
        backup = first_by_priority.get(VacationPreference.PRIORITY_BACKUP)
        if (
            primary is not None
            and backup is not None
            and primary.status == VacationPreference.STATUS_FILLED
            and backup.status == VacationPreference.STATUS_FILLED
        ):
            state_by_employee[employee_id] = VacationPreference.STATUS_FILLED
        else:
            state_by_employee[employee_id] = VacationPreference.STATUS_PENDING

    return state_by_employee


def employee_needs_preference_response(employee, year):
    return get_employee_preference_state(employee, year) in {"missing", VacationPreference.STATUS_PENDING}


def ensure_employee_pending_preferences(employee, year):
    if not employee_can_join_preference_collection(employee, year):
        return False
    state = get_employee_preference_state(employee, year)
    if state in {VacationPreference.STATUS_FILLED, VacationPreference.STATUS_SKIPPED}:
        return False

    created = False
    for priority in (VacationPreference.PRIORITY_PRIMARY, VacationPreference.PRIORITY_BACKUP):
        if _first_preference(employee, year, priority) is None:
            VacationPreference.objects.create(
                employee=employee,
                year=year,
                priority=priority,
                status=VacationPreference.STATUS_PENDING,
                created_automatically=True,
            )
            created = True
    return created


def _replace_employee_preferences(employee, year, rows, *, created_automatically):
    VacationPreference.objects.filter(employee=employee, year=year).delete()
    return [
        VacationPreference.objects.create(
            employee=employee,
            year=year,
            priority=row["priority"],
            start_date=row.get("start_date"),
            end_date=row.get("end_date"),
            status=row["status"],
            remainder_policy=row.get("remainder_policy", VacationPreference.REMAINDER_AUTO),
            comment=row.get("comment", ""),
            created_automatically=created_automatically,
        )
        for row in rows
    ]


def mark_preference_notification_done(employee, year):
    now = timezone.now()
    Notification.objects.filter(
        recipient=employee,
        dedupe_key=preference_notification_dedupe_key(year, employee.id),
    ).exclude(status=Notification.STATUS_DONE).update(
        status=Notification.STATUS_DONE,
        read_at=now,
        done_at=now,
        updated_at=now,
    )


def mark_preference_notification_active(employee, year, actor=None):
    notify_preferences_collection_started(year, [employee], actor=actor)


def _mark_answered_preference_notifications_done(employees, year):
    for employee in employees:
        if get_employee_preference_state(employee, year) in {
            VacationPreference.STATUS_FILLED,
            VacationPreference.STATUS_SKIPPED,
        }:
            mark_preference_notification_done(employee, year)


@transaction.atomic
def submit_employee_preferences(
    *,
    collection,
    employee,
    primary_start=None,
    primary_end=None,
    backup_start=None,
    backup_end=None,
    comment="",
    no_preferences=False,
    remainder_policy=VacationPreference.REMAINDER_AUTO,
):
    today = timezone.localdate()
    if (
        collection.status != VacationPreferenceCollection.STATUS_OPEN
        or today > collection.deadline
        or collection.year != get_preference_planning_year(today)
    ):
        raise ValidationError("Сбор пожеланий закрыт, изменить ответ уже нельзя.")

    if no_preferences:
        rows = [
            {
                "priority": VacationPreference.PRIORITY_PRIMARY,
                "status": VacationPreference.STATUS_SKIPPED,
                "remainder_policy": VacationPreference.REMAINDER_AUTO,
                "comment": comment,
            },
            {
                "priority": VacationPreference.PRIORITY_BACKUP,
                "status": VacationPreference.STATUS_SKIPPED,
                "remainder_policy": VacationPreference.REMAINDER_AUTO,
                "comment": comment,
            },
        ]
    else:
        if remainder_policy not in dict(VacationPreference.REMAINDER_POLICY_CHOICES):
            remainder_policy = VacationPreference.REMAINDER_AUTO
        rows = [
            {
                "priority": VacationPreference.PRIORITY_PRIMARY,
                "start_date": primary_start,
                "end_date": primary_end,
                "status": VacationPreference.STATUS_FILLED,
                "remainder_policy": remainder_policy,
                "comment": comment,
            },
            {
                "priority": VacationPreference.PRIORITY_BACKUP,
                "start_date": backup_start,
                "end_date": backup_end,
                "status": VacationPreference.STATUS_FILLED,
                "remainder_policy": remainder_policy,
                "comment": comment,
            },
        ]
    preferences = _replace_employee_preferences(
        employee,
        collection.year,
        rows,
        created_automatically=False,
    )
    mark_preference_notification_done(employee, collection.year)
    return preferences


def _dates_overlap(left_start, left_end, right_start, right_end):
    return left_start <= right_end and right_start <= left_end


def _random_period(year, earliest_start, duration, rng, excluded_periods=None):
    excluded_periods = excluded_periods or []
    year_end = date(year, 12, 31)
    duration = max(1, min(duration, (year_end - earliest_start).days + 1))
    latest_start = year_end - timedelta(days=duration - 1)
    month_pool = [2, 3, 4, 6, 7, 8, 9, 10, 11]

    for _ in range(40):
        month = rng.choice(month_pool)
        day = rng.randint(1, 12)
        candidate_start = max(date(year, month, min(day, 28)), earliest_start)
        if candidate_start > latest_start:
            continue
        candidate_end = candidate_start + timedelta(days=duration - 1)
        if any(_dates_overlap(candidate_start, candidate_end, start, end) for start, end in excluded_periods):
            continue
        return candidate_start, candidate_end

    fallback_start = earliest_start
    for start, end in excluded_periods:
        fallback_start = max(fallback_start, end + timedelta(days=7))
    if fallback_start > latest_start:
        fallback_start = earliest_start
    return fallback_start, fallback_start + timedelta(days=duration - 1)


def _demo_comment(rng):
    comments = [
        "Хочется совместить отпуск с семейной поездкой.",
        "Желательно поставить отпуск на спокойный период отдела.",
        "Готов рассмотреть перенос, если будет высокая нагрузка.",
        "Основной вариант связан с личными планами.",
        "Запасной период подойдёт, если летние даты будут заняты.",
        "Важно не ставить отпуск на период квартальной отчётности.",
    ]
    return rng.choice(comments)


def _build_demo_preference_rows(employee, year, rng):
    earliest_start = max(date(year, 1, 1), get_paid_leave_available_from(employee))
    year_end = date(year, 12, 31)
    if earliest_start > year_end:
        return []

    available_days = int(min(quantize_leave_days(get_employee_available_balance(employee, year_end)), Decimal("70.00")))
    duration_pool = [days for days in [14, 21, 28, 35, 42, 52, 60, 70] if days <= max(available_days, 14)]
    primary_duration = rng.choice(duration_pool or [14])
    backup_duration = rng.choice([days for days in [14, 21, 28, 35, 42, 52] if days <= primary_duration] or [14])
    primary_start, primary_end = _random_period(year, earliest_start, primary_duration, rng)
    backup_start, backup_end = _random_period(
        year,
        earliest_start,
        backup_duration,
        rng,
        excluded_periods=[(primary_start, primary_end)],
    )
    comment = _demo_comment(rng)
    return [
        {
            "priority": VacationPreference.PRIORITY_PRIMARY,
            "start_date": primary_start,
            "end_date": primary_end,
            "status": VacationPreference.STATUS_FILLED,
            "remainder_policy": VacationPreference.REMAINDER_AUTO,
            "comment": comment,
        },
        {
            "priority": VacationPreference.PRIORITY_BACKUP,
            "start_date": backup_start,
            "end_date": backup_end,
            "status": VacationPreference.STATUS_FILLED,
            "remainder_policy": VacationPreference.REMAINDER_AUTO,
            "comment": comment,
        },
    ]


def _demo_fill_preferences(collection, employees):
    pending_employees = [
        employee
        for employee in employees
        if get_employee_preference_state(employee, collection.year) == VacationPreference.STATUS_PENDING
    ]
    if not pending_employees:
        return {"filled": 0, "skipped": 0}

    mandatory_pending = next((employee for employee in pending_employees if employee.login == "employ_1"), None)
    if mandatory_pending is not None:
        pending_employees = [employee for employee in pending_employees if employee.id != mandatory_pending.id]

    rng = random.Random(timezone.now().timestamp())
    rng.shuffle(pending_employees)
    fill_percent = rng.randint(DEMO_FILL_MIN_PERCENT, DEMO_FILL_MAX_PERCENT)
    fill_count = max(1, round(len(pending_employees) * fill_percent / 100))
    skipped_count = round(len(pending_employees) * DEMO_SKIP_PERCENT / 100)
    filled_employees = pending_employees[:fill_count]
    skipped_employees = pending_employees[fill_count : fill_count + skipped_count]

    filled = 0
    for employee in filled_employees:
        rows = _build_demo_preference_rows(employee, collection.year, rng)
        if rows:
            _replace_employee_preferences(employee, collection.year, rows, created_automatically=True)
            filled += 1

    skipped = 0
    for employee in skipped_employees:
        _replace_employee_preferences(
            employee,
            collection.year,
            [
                {
                    "priority": VacationPreference.PRIORITY_PRIMARY,
                    "status": VacationPreference.STATUS_SKIPPED,
                    "remainder_policy": VacationPreference.REMAINDER_AUTO,
                    "comment": "Пожелания не указаны.",
                },
                {
                    "priority": VacationPreference.PRIORITY_BACKUP,
                    "status": VacationPreference.STATUS_SKIPPED,
                    "remainder_policy": VacationPreference.REMAINDER_AUTO,
                    "comment": "Пожелания не указаны.",
                },
            ],
            created_automatically=True,
        )
        skipped += 1
    return {"filled": filled, "skipped": skipped}


@transaction.atomic
def start_preference_collection(*, year, deadline, actor, demo_autofill=False):
    now = timezone.now()
    collection, created = VacationPreferenceCollection.objects.select_for_update().get_or_create(
        year=year,
        defaults={
            "deadline": deadline,
            "status": VacationPreferenceCollection.STATUS_OPEN,
            "started_by": actor,
            "started_at": now,
            "demo_autofill_enabled": demo_autofill,
        },
    )
    if not created:
        collection.status = VacationPreferenceCollection.STATUS_OPEN
        collection.deadline = deadline
        collection.started_by = actor
        collection.started_at = now
        collection.finished_by = None
        collection.finished_at = None
        collection.demo_autofill_enabled = demo_autofill
        collection.save(
            update_fields=[
                "status",
                "deadline",
                "started_by",
                "started_at",
                "finished_by",
                "finished_at",
                "demo_autofill_enabled",
            ]
        )

    VacationPreference.objects.filter(year=year).delete()

    eligible_employees = get_eligible_preference_employees(year)
    for employee in eligible_employees:
        ensure_employee_pending_preferences(employee, year)

    demo_stats = {"filled": 0, "skipped": 0}
    if demo_autofill:
        demo_stats = _demo_fill_preferences(collection, eligible_employees)

    notify_preferences_collection_started(year, eligible_employees, actor=actor)
    _mark_answered_preference_notifications_done(eligible_employees, year)

    pending_count = sum(
        1
        for employee in eligible_employees
        if get_employee_preference_state(employee, year) == VacationPreference.STATUS_PENDING
    )
    return {
        "collection": collection,
        "eligible_count": len(eligible_employees),
        "notified_count": pending_count,
        "demo_filled_count": demo_stats["filled"],
        "demo_skipped_count": demo_stats["skipped"],
    }


@transaction.atomic
def finish_preference_collection(*, year, actor):
    collection = VacationPreferenceCollection.objects.select_for_update().get(year=year)
    collection.status = VacationPreferenceCollection.STATUS_FINISHED
    collection.finished_by = actor
    collection.finished_at = timezone.now()
    collection.save(update_fields=["status", "finished_by", "finished_at"])
    mark_notifications_done_by_dedupe_prefix(preference_notification_dedupe_prefix(year))
    return collection


def attach_employee_to_open_preference_collections(employee, *, actor=None):
    attached_years = []
    if employee.role in Employees.SERVICE_ROLES or not employee.is_active_employee:
        return attached_years

    collections = VacationPreferenceCollection.objects.filter(status=VacationPreferenceCollection.STATUS_OPEN)
    for collection in collections:
        if timezone.localdate() > collection.deadline:
            continue
        if not employee_can_join_preference_collection(employee, collection.year):
            continue
        ensure_employee_pending_preferences(employee, collection.year)
        if employee_needs_preference_response(employee, collection.year):
            mark_preference_notification_active(employee, collection.year, actor=actor)
            attached_years.append(collection.year)
    return attached_years


def build_preference_collection_summary(year):
    eligible_employees = get_eligible_preference_employees(year)
    total = len(eligible_employees)
    counts = {
        VacationPreference.STATUS_FILLED: 0,
        VacationPreference.STATUS_SKIPPED: 0,
        VacationPreference.STATUS_PENDING: 0,
        "missing": 0,
    }
    preference_state_by_employee = get_employee_preference_state_map(
        [employee.id for employee in eligible_employees],
        year,
    )
    for employee in eligible_employees:
        counts[preference_state_by_employee.get(employee.id, "missing")] += 1

    filled = counts[VacationPreference.STATUS_FILLED]
    skipped = counts[VacationPreference.STATUS_SKIPPED]
    pending = counts[VacationPreference.STATUS_PENDING]
    missing = counts["missing"]
    ready = filled + skipped
    attention = pending + missing
    ready_percentage = round(ready * 100 / total) if total else 0
    return {
        "total": total,
        "ready": ready,
        "answered": ready,
        "filled": filled,
        "pending": pending,
        "skipped": skipped,
        "no_preferences": skipped,
        "missing": missing,
        "not_answered": attention,
        "attention": attention,
        "ready_percentage": ready_percentage,
    }


def _preference_period_payload(preference):
    if preference is None:
        return {
            "start_date": None,
            "end_date": None,
            "status": VacationPreference.STATUS_PENDING,
        }
    return {
        "start_date": preference.start_date,
        "end_date": preference.end_date,
        "status": preference.status,
    }


def _readiness_filter_url(year, status, query):
    params = {"status": status}
    if query:
        params["q"] = query
    return f"{preference_readiness_url(year)}?{urlencode(params)}"


def _readiness_status_from_state(state):
    if state == VacationPreference.STATUS_FILLED:
        return {
            "key": VacationPreference.STATUS_FILLED,
            "label": "Заполнено",
            "icon": "task_alt",
        }
    if state == VacationPreference.STATUS_SKIPPED:
        return {
            "key": VacationPreference.STATUS_SKIPPED,
            "label": "Без пожеланий",
            "icon": "event_busy",
        }
    return {
        "key": VacationPreference.STATUS_PENDING,
        "label": "Не ответил",
        "icon": "schedule",
    }


def build_preference_collection_readiness_context(year, params=None):
    params = params or {}
    selected_status = params.get("status") or "all"
    if selected_status not in {"all", VacationPreference.STATUS_FILLED, VacationPreference.STATUS_SKIPPED, VacationPreference.STATUS_PENDING}:
        selected_status = "all"
    query = (params.get("q") or "").strip()
    normalized_query = query.casefold()

    collection = VacationPreferenceCollection.objects.filter(year=year).first()
    summary = build_preference_collection_summary(year)
    employees = get_eligible_preference_employees(year)
    preference_state_by_employee = get_employee_preference_state_map(
        [employee.id for employee in employees],
        year,
    )
    rows = []
    for employee in employees:
        state = preference_state_by_employee.get(employee.id, "missing")
        status = _readiness_status_from_state(state)
        if selected_status != "all" and status["key"] != selected_status:
            continue

        position = employee.employee_position
        group = position.production_group if position and position.production_group_id else None
        department_name = employee.department.name if employee.department_id else "Без отдела"
        group_name = group.name if group else "Без группы"
        search_text = " ".join(
            [
                employee.full_name,
                employee.login,
                employee.position,
                department_name,
                group_name,
            ]
        ).casefold()
        if normalized_query and normalized_query not in search_text:
            continue

        pair = get_employee_preference_pair(employee, year)
        primary = pair[VacationPreference.PRIORITY_PRIMARY]
        backup = pair[VacationPreference.PRIORITY_BACKUP]
        remainder_policy = getattr(primary, "remainder_policy", VacationPreference.REMAINDER_AUTO)
        identity = get_employee_identity_presentation(employee)
        rows.append(
            {
                "employee": employee,
                "department_name": department_name,
                "group_name": group_name,
                "position": employee.position,
                "role_icon": identity["employee_role_icon"],
                "role_icon_type": identity["employee_role_icon_type"],
                "role_variant": identity["employee_role_variant"],
                "role_label": identity["employee_role_label"],
                "status": status,
                "primary": _preference_period_payload(primary),
                "backup": _preference_period_payload(backup),
                "remainder_policy": remainder_policy,
                "remainder_policy_label": preference_remainder_policy_label(remainder_policy),
                "comment": (primary.comment if primary and primary.comment else backup.comment if backup else ""),
            }
        )

    status_filters = [
        {
            "key": "all",
            "label": "Все",
            "count": summary["total"],
            "url": _readiness_filter_url(year, "all", query),
            "active": selected_status == "all",
        },
        {
            "key": VacationPreference.STATUS_FILLED,
            "label": "Заполнено",
            "count": summary["filled"],
            "url": _readiness_filter_url(year, VacationPreference.STATUS_FILLED, query),
            "active": selected_status == VacationPreference.STATUS_FILLED,
        },
        {
            "key": VacationPreference.STATUS_SKIPPED,
            "label": "Без пожеланий",
            "count": summary["no_preferences"],
            "url": _readiness_filter_url(year, VacationPreference.STATUS_SKIPPED, query),
            "active": selected_status == VacationPreference.STATUS_SKIPPED,
        },
        {
            "key": VacationPreference.STATUS_PENDING,
            "label": "Не ответили",
            "count": summary["not_answered"],
            "url": _readiness_filter_url(year, VacationPreference.STATUS_PENDING, query),
            "active": selected_status == VacationPreference.STATUS_PENDING,
        },
    ]
    return {
        "collection": collection,
        "year": year,
        "summary": summary,
        "rows": rows,
        "result_count": len(rows),
        "query": query,
        "selected_status": selected_status,
        "selected_status_index": {
            "all": 0,
            VacationPreference.STATUS_FILLED: 1,
            VacationPreference.STATUS_SKIPPED: 2,
            VacationPreference.STATUS_PENDING: 3,
        }[selected_status],
        "status_filters": status_filters,
        "readiness_url": preference_readiness_url(year),
        "finish_url": reverse("preferences_collection_finish", args=[year]),
        "is_open": collection is not None and collection.status == VacationPreferenceCollection.STATUS_OPEN,
        "is_finished": collection is not None and collection.status == VacationPreferenceCollection.STATUS_FINISHED,
    }


def build_calendar_preference_collection_context(current_employee, calendar_year):
    today = timezone.localdate()
    year = get_preference_planning_year(today)
    collection = VacationPreferenceCollection.objects.filter(year=year).first()
    summary = build_preference_collection_summary(year)
    is_open = collection is not None and collection.status == VacationPreferenceCollection.STATUS_OPEN
    is_finished = collection is not None and collection.status == VacationPreferenceCollection.STATUS_FINISHED
    deadline_passed = is_open and today > collection.deadline
    can_manage = current_employee is not None and current_employee.role == Employees.ROLE_HR
    can_view = can_manage or (
        current_employee is not None and current_employee.role == Employees.ROLE_ENTERPRISE_HEAD
    )
    draft_schedule = VacationSchedule.objects.filter(year=year, status=VacationSchedule.STATUS_DRAFT).first()
    draft_items_count = 0
    if draft_schedule is not None:
        draft_items_count = draft_schedule.items.filter(status=VacationScheduleItem.STATUS_DRAFT).count()
    draft_exists = draft_schedule is not None
    if draft_exists:
        status_key = "draft_created"
        status_label = "Черновик создан"
        status_hint = f"Рабочий черновик уже создан: {draft_items_count} размещено. Нажмите, чтобы открыть его."
    elif is_finished:
        status_key = "ready"
        status_label = "Готово к черновику"
        status_hint = "Сбор завершён. Неответившие сотрудники пойдут в черновик как ручное размещение."
    elif is_open:
        status_key = "open"
        status_label = "Сбор идет"
        status_hint = "HR может завершить сбор в любой момент, даже если ответили не все сотрудники."
    else:
        status_key = "not_started"
        status_label = "Не начат"
        status_hint = "HR запускает сбор пожеланий перед формированием годового черновика."
    return {
        "collection": collection,
        "can_view": can_view,
        "can_manage": can_manage,
        "year": year,
        "is_open": is_open,
        "is_finished": is_finished,
        "deadline_passed": deadline_passed,
        "status_label": collection.get_status_display() if collection else "Не начат",
        "readiness_status_key": status_key,
        "readiness_status_label": status_label,
        "readiness_status_hint": status_hint,
        "draft_ready": is_finished,
        "draft_exists": draft_exists,
        "draft_url": preference_draft_url(year),
        "draft_items_count": draft_items_count,
        "primary_url": preference_draft_url(year) if draft_exists else preference_readiness_url(year),
        "primary_label": "Черновик графика" if draft_exists else "Сбор пожеланий",
        "deadline": collection.deadline if collection else None,
        "default_deadline": today + timedelta(days=14),
        "start_url": reverse("preferences_collection_start"),
        "finish_url": reverse("preferences_collection_finish", args=[year]),
        "response_url": preference_response_url(year),
        "readiness_url": preference_readiness_url(year),
        "summary": summary,
    }


def get_employee_preference_page_context(employee, collection):
    pair = get_employee_preference_pair(employee, collection.year)
    state = get_employee_preference_state(employee, collection.year)
    today = timezone.localdate()
    planning_year = get_preference_planning_year(today)
    paid_leave_available_from = get_paid_leave_available_from(employee)
    planning_end = date(collection.year, 12, 31)
    available_balance = quantize_leave_days(get_employee_available_balance(employee, planning_end))
    entitlement_rows = get_employee_entitlement_rows(employee, as_of_date=planning_end, limit=100)
    mandatory_days = quantize_leave_days(
        sum(
            (
                Decimal(row["remaining_days"])
                for row in entitlement_rows
                if row["remaining_days"] > 0 and row["must_use_by"] <= planning_end
            ),
            Decimal("0.00"),
        )
    )
    primary = pair[VacationPreference.PRIORITY_PRIMARY]
    backup = pair[VacationPreference.PRIORITY_BACKUP]
    remainder_policy = getattr(primary, "remainder_policy", VacationPreference.REMAINDER_AUTO)
    primary_days = (
        quantize_leave_days(get_chargeable_leave_days(primary.start_date, primary.end_date, "paid"))
        if primary and primary.start_date and primary.end_date and primary.status == VacationPreference.STATUS_FILLED
        else Decimal("0.00")
    )
    backup_days = (
        quantize_leave_days(get_chargeable_leave_days(backup.start_date, backup.end_date, "paid"))
        if backup and backup.start_date and backup.end_date and backup.status == VacationPreference.STATUS_FILLED
        else Decimal("0.00")
    )
    editable = (
        collection.status == VacationPreferenceCollection.STATUS_OPEN
        and today <= collection.deadline
        and collection.year == planning_year
    )
    return {
        "collection": collection,
        "preference_state": state,
        "primary_preference": primary,
        "backup_preference": backup,
        "editable": editable,
        "planning_year": planning_year,
        "is_planning_year": collection.year == planning_year,
        "paid_leave_available_from": paid_leave_available_from,
        "show_paid_leave_available_hint": today < paid_leave_available_from,
        "available_balance": available_balance,
        "available_balance_label": _days_label(available_balance),
        "min_continuous_paid_leave_days": MIN_CONTINUOUS_PAID_LEAVE_DAYS,
        "mandatory_days": mandatory_days,
        "mandatory_days_label": _days_label(mandatory_days),
        "annual_paid_leave_days_label": _days_label(employee.annual_paid_leave_days),
        "primary_preference_days_label": _days_label(primary_days),
        "backup_preference_days_label": _days_label(backup_days),
        "remainder_policy": remainder_policy,
        "remainder_policy_label": preference_remainder_policy_label(remainder_policy),
    }
