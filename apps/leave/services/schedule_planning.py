from collections import Counter
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from django.db.models import Count
from django.urls import reverse

from apps.accounts.services import (
    get_managed_department_id,
    is_authorized_person_employee,
    is_department_head_employee,
    is_enterprise_head_employee,
    is_hr_employee,
)
from apps.leave.models import (
    VacationPreferenceCollection,
    VacationSchedule,
    VacationScheduleAuthorizedApproval,
    VacationScheduleDepartmentApproval,
    VacationScheduleEnterpriseApproval,
    VacationScheduleItem,
)

from .preferences import build_preference_collection_summary, get_preference_planning_year
from .schedule_drafts import build_schedule_draft_page_context, get_schedule_draft_status


STAGE_CALENDAR = "calendar"
STAGE_COLLECTION = "collection"
STAGE_DRAFT = "draft"
STAGE_REVIEW = "review"
STAGE_FINAL = "final"

STAGES = (
    (STAGE_CALENDAR, "График", "event"),
    (STAGE_COLLECTION, "Сбор", "fact_check"),
    (STAGE_DRAFT, "Черновик", "edit_calendar"),
    (STAGE_REVIEW, "Проверка", "groups"),
    (STAGE_FINAL, "Финал", "verified"),
)
STAGE_KEYS = {stage[0] for stage in STAGES}


def get_schedule_planning_year(today=None):
    return get_preference_planning_year(today)


def schedule_planning_url(year, stage=None):
    url = reverse("schedule_planning", args=[year])
    if stage:
        return f"{url}?{urlencode({'stage': stage})}"
    return url


def can_access_schedule_planning(employee):
    if employee is None:
        return False
    if is_hr_employee(employee) or is_enterprise_head_employee(employee):
        return True
    if is_department_head_employee(employee):
        managed_department_id = get_managed_department_id(employee)
        return bool(
            managed_department_id
            and VacationScheduleDepartmentApproval.objects.filter(
                department_id=managed_department_id,
                status=VacationScheduleDepartmentApproval.STATUS_PENDING,
            ).exists()
        )
    if is_authorized_person_employee(employee):
        return VacationScheduleAuthorizedApproval.objects.filter(
            authorized_person=employee,
            status=VacationScheduleAuthorizedApproval.STATUS_PENDING,
        ).exists()
    return False


def _append_query(url, params):
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({key: value for key, value in params.items() if value})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _with_planning_back(url, year, stage):
    return _append_query(
        url,
        {
            "from": "schedule_planning",
            "back_url": schedule_planning_url(year, stage),
            "back_label": "К планированию",
        },
    )


def _status(key, label, icon, tone="neutral", hint=""):
    return {
        "key": key,
        "label": label,
        "icon": icon,
        "tone": tone,
        "hint": hint,
    }


def _collection_status(collection):
    if collection is None:
        return _status("not_started", "Не начат", "pending_actions", "warning")
    if collection.status == VacationPreferenceCollection.STATUS_OPEN:
        return _status("open", "Сбор идёт", "hourglass_top", "info")
    if collection.status == VacationPreferenceCollection.STATUS_FINISHED:
        return _status("finished", "Сбор завершён", "task_alt", "ok")
    return _status("unknown", collection.get_status_display(), "info", "neutral")


def _calendar_summary(year, schedule):
    item_counts = Counter()
    total_items = 0
    if schedule is not None:
        item_counts.update(
            {
                row["status"]: row["count"]
                for row in VacationScheduleItem.objects.filter(schedule=schedule)
                .values("status")
                .annotate(count=Count("id"))
            }
        )
        total_items = sum(item_counts.values())

    if schedule is None:
        status = _status("empty", "График не создан", "event_busy", "warning")
    elif schedule.status == VacationSchedule.STATUS_APPROVED:
        status = _status("approved", "График утверждён", "verified", "ok")
    elif schedule.status == VacationSchedule.STATUS_DEPARTMENT_REVIEW:
        status = _status("review", "На проверке", "groups", "info")
    elif schedule.status == VacationSchedule.STATUS_ARCHIVED:
        status = _status("archived", "Архив", "inventory_2", "neutral")
    else:
        status = _status("draft", "Есть черновик", "edit_calendar", "info")

    return {
        "status": status,
        "schedule": schedule,
        "total_items": total_items,
        "draft_items": item_counts[VacationScheduleItem.STATUS_DRAFT],
        "planned_items": item_counts[VacationScheduleItem.STATUS_PLANNED],
        "approved_items": item_counts[VacationScheduleItem.STATUS_APPROVED],
        "calendar_url": f"{reverse('calendar')}?{urlencode({'view': 'year', 'year': year})}",
    }


def _review_summary(schedule):
    approvals = []
    counts = Counter()
    if schedule is not None:
        approvals = list(
            VacationScheduleDepartmentApproval.objects.select_related(
                "department",
                "department_head",
            )
            .filter(schedule=schedule)
            .order_by("department__name")
        )
        counts.update(approval.status for approval in approvals)

    total = len(approvals)
    approved = counts[VacationScheduleDepartmentApproval.STATUS_APPROVED]
    pending = counts[VacationScheduleDepartmentApproval.STATUS_PENDING]
    rejected = counts[VacationScheduleDepartmentApproval.STATUS_REJECTED]

    if schedule is None or not approvals:
        status = _status("not_sent", "Не отправлено", "outgoing_mail", "warning")
    elif rejected:
        status = _status("returned", "Есть возвраты", "assignment_return", "danger")
    elif total and approved == total:
        status = _status("finished", "Отделы согласовали", "task_alt", "ok")
    else:
        status = _status("in_progress", "На проверке", "groups", "info")

    rows = []
    for approval in approvals:
        rows.append(
            {
                "department": approval.department,
                "head_name": approval.department_head.full_name if approval.department_head else "Руководитель не назначен",
                "status": approval.status,
                "status_label": approval.get_status_display(),
            }
        )

    return {
        "status": status,
        "total": total,
        "approved": approved,
        "pending": pending,
        "rejected": rejected,
        "rows": rows,
    }


def _single_approval_payload(approval, empty_label):
    if approval is None:
        return {
            "status": "missing",
            "status_label": empty_label,
            "reviewer_name": "",
            "comment": "",
        }
    reviewer = getattr(approval, "enterprise_head", None) or getattr(approval, "authorized_person", None)
    return {
        "status": approval.status,
        "status_label": approval.get_status_display(),
        "reviewer_name": reviewer.full_name if reviewer else "",
        "comment": approval.comment,
    }


def _final_summary(schedule, review_summary):
    enterprise_approval = None
    authorized_approval = None
    if schedule is not None:
        enterprise_approval = schedule.enterprise_approvals.select_related("enterprise_head").first()
        authorized_approval = schedule.authorized_approvals.select_related("authorized_person").first()

    if schedule is not None and schedule.status == VacationSchedule.STATUS_APPROVED:
        status = _status("approved", "График утверждён", "verified", "ok")
    elif review_summary["total"] and review_summary["approved"] == review_summary["total"]:
        status = _status("ready", "Готов к финалу", "published_with_changes", "info")
    else:
        status = _status("locked", "Недоступно", "lock", "muted")

    return {
        "status": status,
        "enterprise": _single_approval_payload(enterprise_approval, "Руководитель предприятия ещё не получил задачу"),
        "authorized": _single_approval_payload(authorized_approval, "Уполномоченное лицо ещё не получило задачу"),
    }


def _overall_status(calendar_summary, collection_status, draft_status, review_summary, final_summary):
    if calendar_summary["status"]["key"] == "approved":
        return _status("approved", "График утверждён", "verified", "ok")
    if final_summary["status"]["key"] == "ready":
        return _status("final_ready", "Готов к финалу", "published_with_changes", "info")
    if review_summary["status"]["key"] in {"in_progress", "returned", "finished"}:
        return review_summary["status"]
    if draft_status["exists"]:
        return _status("draft", "Черновик создан", "edit_calendar", "info")
    if collection_status["key"] == "finished":
        return _status("ready_for_draft", "Готово к черновику", "auto_awesome_motion", "info")
    return collection_status


def _default_stage(collection_status, draft_status, review_summary, final_summary):
    if final_summary["status"]["key"] in {"ready", "approved"}:
        return STAGE_FINAL
    if review_summary["status"]["key"] in {"in_progress", "returned", "finished"}:
        return STAGE_REVIEW
    if draft_status["exists"]:
        return STAGE_DRAFT
    if collection_status["key"] in {"open", "finished"}:
        return STAGE_COLLECTION
    return STAGE_CALENDAR


def build_schedule_planning_page_context(year, employee, params=None):
    params = params or {}
    schedule = VacationSchedule.objects.filter(year=year).first()
    collection = VacationPreferenceCollection.objects.filter(year=year).first()
    collection_summary = build_preference_collection_summary(year)
    collection_status = _collection_status(collection)
    draft_status = get_schedule_draft_status(year)
    draft_context = build_schedule_draft_page_context(year) if draft_status["exists"] else None
    draft_summary = draft_context["draft_summary"] if draft_context else {
        "placed": 0,
        "manual": 0,
        "blocking": 0,
        "open_required_days_label": "0 д.",
        "remaining_plan_days_label": "0 д.",
        "blocking_days_label": "0 д.",
        "high_risk": 0,
        "conflicts": 0,
        "total": 0,
    }
    calendar_summary = _calendar_summary(year, schedule)
    review_summary = _review_summary(schedule)
    final_summary = _final_summary(schedule, review_summary)
    overall_status = _overall_status(
        calendar_summary,
        collection_status,
        draft_status,
        review_summary,
        final_summary,
    )

    selected_stage = params.get("stage") or _default_stage(collection_status, draft_status, review_summary, final_summary)
    if selected_stage not in STAGE_KEYS:
        selected_stage = _default_stage(collection_status, draft_status, review_summary, final_summary)
    stage_status_by_key = {
        STAGE_CALENDAR: calendar_summary["status"],
        STAGE_COLLECTION: collection_status,
        STAGE_DRAFT: _status(
            "created" if draft_status["exists"] else "empty",
            "Черновик создан" if draft_status["exists"] else "Черновик не создан",
            "edit_calendar" if draft_status["exists"] else "pending_actions",
            "info" if draft_status["exists"] else "warning",
        ),
        STAGE_REVIEW: review_summary["status"],
        STAGE_FINAL: final_summary["status"],
    }
    stages = []
    for key, label, icon in STAGES:
        stages.append(
            {
                "key": key,
                "label": label,
                "icon": icon,
                "url": schedule_planning_url(year, key),
                "active": key == selected_stage,
                "status": stage_status_by_key[key],
            }
        )

    planning_stage_url = schedule_planning_url(year, selected_stage)
    collection_url = reverse("preference_collection_readiness", args=[year])
    draft_url = reverse("schedule_draft_detail", args=[year])

    return {
        "year": year,
        "schedule": schedule,
        "collection": collection,
        "selected_stage": selected_stage,
        "stages": stages,
        "overall_status": overall_status,
        "calendar_summary": calendar_summary,
        "collection_summary": collection_summary,
        "collection_status": collection_status,
        "draft_status": draft_status,
        "draft_summary": draft_summary,
        "review_summary": review_summary,
        "final_summary": final_summary,
        "planning_url": schedule_planning_url(year),
        "planning_stage_url": planning_stage_url,
        "calendar_url": calendar_summary["calendar_url"],
        "readiness_url": _with_planning_back(collection_url, year, STAGE_COLLECTION),
        "draft_url": _with_planning_back(draft_url, year, STAGE_DRAFT),
        "draft_create_url": reverse("schedule_draft_create", args=[year]),
        "draft_create_next_url": schedule_planning_url(year, STAGE_DRAFT),
        "draft_auto_place_url": reverse("schedule_draft_auto_place", args=[year]),
        "draft_auto_place_next_url": schedule_planning_url(year, STAGE_DRAFT),
        "finish_url": reverse("preferences_collection_finish", args=[year]),
        "finish_next_url": schedule_planning_url(year, STAGE_COLLECTION),
        "can_manage_collection": is_hr_employee(employee),
        "can_manage_draft": is_hr_employee(employee),
        "can_create_draft": (
            is_hr_employee(employee)
            and collection is not None
            and collection.status == VacationPreferenceCollection.STATUS_FINISHED
            and not draft_status["exists"]
            and not draft_status["blocked_by_existing_schedule"]
        ),
        "approval_blocked": bool(draft_context and draft_context["approval_blocked"]),
    }
