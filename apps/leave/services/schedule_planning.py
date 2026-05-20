from collections import Counter
from decimal import Decimal
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
    VacationScheduleCandidatePackage,
    VacationScheduleDepartmentApproval,
    VacationScheduleEnterpriseApproval,
    VacationScheduleItem,
)

from .preferences import (
    build_calendar_preference_collection_context,
    build_preference_collection_summary,
)
from .planning_cycles import (
    available_planning_years,
    get_active_planning_year,
    get_next_planning_cycle_start_state,
)
from apps.leave.services.schedule_drafts.page_context import (
    build_schedule_draft_summary_context,
    has_department_schedule_hard_conflicts,
)
from apps.leave.services.schedule_drafts.utils import get_schedule_draft_status
from .schedule_auto_place_jobs import (
    get_active_schedule_auto_place_job,
    schedule_auto_place_job_page_payload,
)
from .neural_training_jobs import build_neural_training_context
from .schedule_approvals import (
    can_review_schedule_department_approval,
    can_review_schedule_enterprise_approval,
    get_schedule_department_review_start_state,
    get_schedule_enterprise_review_start_state,
)


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
    return get_active_planning_year(today)


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
                schedule__status=VacationSchedule.STATUS_DEPARTMENT_REVIEW,
            ).exists()
        )
    if is_authorized_person_employee(employee):
        return False
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


def _active_draft_auto_place_job_payload(*, year, schedule, employee):
    if schedule is None or schedule.status != VacationSchedule.STATUS_DRAFT or not is_hr_employee(employee):
        return None
    job = get_active_schedule_auto_place_job(year=year, schedule=schedule)
    if job is None:
        return None
    return schedule_auto_place_job_page_payload(job)


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


def _percent_label(value):
    if value is None:
        return ""
    value = Decimal(str(value)).quantize(Decimal("0.01"))
    if value == value.to_integral_value():
        return f"{int(value)}%"
    return f"{str(value).replace('.', ',')}%"


def _package_model_label(packages):
    models = Counter(package.model_version for package in packages if package.model_version)
    if not models:
        return "Модель не сохранена"
    if len(models) == 1:
        return models.most_common(1)[0][0]
    main_model, _ = models.most_common(1)[0]
    return f"{main_model} + ещё {len(models) - 1}"


def _schedule_package_ml_summary(schedule, *, department_id=None):
    if schedule is None:
        return {
            "has_data": False,
            "packages_count": 0,
            "employees_count": 0,
            "average_score_label": "",
            "high_risk_count": 0,
            "hard_conflicts": False,
            "model_label": "Модель не сохранена",
            "status_label": "Нет пакетной оценки",
            "status_tone": "muted",
            "attention_departments": [],
            "attention_departments_label": "Нет отделов с повышенным риском",
        }

    queryset = VacationScheduleCandidatePackage.objects.select_related("employee__department").filter(
        schedule=schedule,
        decision=VacationScheduleCandidatePackage.DECISION_SELECTED,
    )
    if department_id:
        queryset = queryset.filter(employee__department_id=department_id)
    packages = list(queryset)
    department_ids = {
        package.employee.department_id
        for package in packages
        if getattr(package.employee, "department_id", None)
    }
    if department_id:
        department_ids.add(department_id)
    else:
        department_ids.update(
            VacationScheduleItem.objects.filter(schedule=schedule, employee__department_id__isnull=False)
            .values_list("employee__department_id", flat=True)
            .distinct()
        )
    conflict_department_ids = {
        item_department_id
        for item_department_id in department_ids
        if has_department_schedule_hard_conflicts(schedule, item_department_id)
    }
    hard_conflicts = bool(conflict_department_ids)

    scored_packages = [package for package in packages if package.score is not None]
    average_score = (
        sum((Decimal(str(package.score)) for package in scored_packages), Decimal("0.00")) / Decimal(len(scored_packages))
        if scored_packages
        else None
    )
    high_risk_packages = [
        package
        for package in packages
        if package.risk_level == VacationScheduleItem.RISK_HIGH or int(package.risk_score or 0) >= 70
    ]
    attention_departments = sorted(
        {
            package.employee.department.name
            for package in high_risk_packages
            if getattr(package.employee, "department", None) is not None
        }
    )
    if hard_conflicts:
        conflict_departments = {
            package.employee.department.name
            for package in packages
            if getattr(package.employee, "department", None) is not None
            and package.employee.department_id in conflict_department_ids
        }
        conflict_departments.update(
            VacationScheduleItem.objects.select_related("employee__department")
            .filter(schedule=schedule, employee__department_id__in=conflict_department_ids)
            .values_list("employee__department__name", flat=True)
            .distinct()
        )
        attention_departments = sorted({*attention_departments, *conflict_departments})

    if hard_conflicts:
        status_label = "Есть hard-конфликты"
        status_tone = "danger"
    elif high_risk_packages:
        status_label = "Есть рисковые пакеты"
        status_tone = "warning"
    elif packages:
        status_label = "Пакеты безопасны"
        status_tone = "ok"
    else:
        status_label = "Нет пакетной оценки"
        status_tone = "muted"

    return {
        "has_data": bool(packages) or hard_conflicts,
        "packages_count": len(packages),
        "employees_count": len({package.employee_id for package in packages}),
        "average_score": average_score,
        "average_score_label": _percent_label(average_score),
        "high_risk_count": len(high_risk_packages),
        "hard_conflicts": hard_conflicts,
        "model_label": _package_model_label(packages),
        "status_label": status_label,
        "status_tone": status_tone,
        "attention_departments": attention_departments,
        "attention_departments_label": ", ".join(attention_departments) if attention_departments else "Нет отделов с повышенным риском",
    }


def _review_summary(schedule, employee=None):
    approvals = []
    counts = Counter()
    if schedule is not None:
        queryset = (
            VacationScheduleDepartmentApproval.objects.select_related(
                "department",
                "department_head",
            )
            .filter(schedule=schedule)
            .order_by("department__name")
        )
        if is_department_head_employee(employee):
            managed_department_id = get_managed_department_id(employee)
            queryset = queryset.filter(department_id=managed_department_id) if managed_department_id else queryset.none()
        approvals = list(queryset)
        counts.update(approval.status for approval in approvals)

    total = len(approvals)
    approved = counts[VacationScheduleDepartmentApproval.STATUS_APPROVED]
    pending = counts[VacationScheduleDepartmentApproval.STATUS_PENDING]
    rejected = counts[VacationScheduleDepartmentApproval.STATUS_REJECTED]

    if schedule is None or not approvals:
        status = _status("not_sent", "Не отправлено", "pending_actions", "warning")
    elif rejected:
        status = _status("returned", "Есть возвраты", "assignment_return", "danger")
    elif total and approved == total:
        status = _status("finished", "Отделы согласовали", "task_alt", "ok")
    else:
        status = _status("in_progress", "На проверке", "groups", "info")

    rows = []
    for approval in approvals:
        can_review = can_review_schedule_department_approval(employee, approval)
        can_rework = is_hr_employee(employee) and approval.status == VacationScheduleDepartmentApproval.STATUS_REJECTED
        rows.append(
            {
                "approval": approval,
                "approval_id": approval.id,
                "department": approval.department,
                "head_name": approval.department_head.full_name if approval.department_head else "Руководитель не назначен",
                "status": approval.status,
                "status_label": approval.get_status_display(),
                "comment": approval.comment,
                "approved_at": approval.approved_at,
                "can_review": can_review and approval.status == VacationScheduleDepartmentApproval.STATUS_PENDING,
                "can_rework": can_rework,
                "approve_url": reverse("schedule_department_review_approve", args=[schedule.year, approval.id]),
                "return_url": reverse("schedule_department_review_return", args=[schedule.year, approval.id]),
                "rework_url": reverse("schedule_department_review_rework", args=[schedule.year, approval.id]),
                "resubmit_url": reverse("schedule_department_review_resubmit", args=[schedule.year, approval.id]),
                "ml_summary": _schedule_package_ml_summary(schedule, department_id=approval.department_id),
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


def _single_approval_payload(approval, empty_label, employee=None):
    if approval is None:
        return {
            "status": "missing",
            "status_label": empty_label,
            "reviewer_name": "",
            "comment": "",
            "approval_id": None,
            "can_review": False,
            "approve_url": "",
            "return_url": "",
        }
    reviewer = getattr(approval, "enterprise_head", None)
    can_review = (
        can_review_schedule_enterprise_approval(employee, approval)
        and approval.status == VacationScheduleEnterpriseApproval.STATUS_PENDING
    )
    return {
        "approval_id": approval.id,
        "status": approval.status,
        "status_label": approval.get_status_display(),
        "reviewer_name": reviewer.full_name if reviewer else "",
        "comment": approval.comment,
        "approved_at": approval.approved_at,
        "can_review": can_review,
        "approve_url": reverse("schedule_final_review_approve", args=[approval.schedule.year, approval.id]),
        "return_url": reverse("schedule_final_review_return", args=[approval.schedule.year, approval.id]),
    }


def _final_summary(schedule, review_summary, employee=None):
    enterprise_approval = None
    if schedule is not None:
        enterprise_approval = schedule.enterprise_approvals.select_related("enterprise_head").first()

    if schedule is not None and schedule.status == VacationSchedule.STATUS_APPROVED:
        status = _status("approved", "График утверждён", "verified", "ok")
    elif enterprise_approval is not None and enterprise_approval.status == VacationScheduleEnterpriseApproval.STATUS_PENDING:
        status = _status("in_progress", "На финальном утверждении", "approval", "info")
    elif enterprise_approval is not None and enterprise_approval.status == VacationScheduleEnterpriseApproval.STATUS_REJECTED:
        status = _status("returned", "Возвращён HR", "assignment_return", "danger")
    elif review_summary["total"] and review_summary["approved"] == review_summary["total"]:
        status = _status("ready", "Готов к финалу", "published_with_changes", "info")
    else:
        status = _status("locked", "Недоступно", "lock", "muted")

    rework_departments = []
    if (
        schedule is not None
        and enterprise_approval is not None
        and enterprise_approval.status == VacationScheduleEnterpriseApproval.STATUS_REJECTED
    ):
        for approval in (
            VacationScheduleDepartmentApproval.objects.select_related("department", "department_head")
            .filter(schedule=schedule)
            .order_by("department__name")
        ):
            rework_departments.append(
                {
                    "approval": approval,
                    "department": approval.department,
                    "head_name": approval.department_head.full_name if approval.department_head else "Руководитель не назначен",
                    "status": approval.status,
                    "status_label": approval.get_status_display(),
                    "open_rework_url": reverse(
                        "schedule_final_review_open_department_rework",
                        args=[schedule.year, enterprise_approval.id, approval.department_id],
                    ),
                }
            )

    return {
        "status": status,
        "enterprise": _single_approval_payload(
            enterprise_approval,
            "Руководитель предприятия ещё не получил задачу",
            employee=employee,
        ),
        "rework_departments": rework_departments,
        "ml_summary": _schedule_package_ml_summary(schedule),
    }


def _overall_status(calendar_summary, collection_status, draft_status, review_summary, final_summary):
    if calendar_summary["status"]["key"] == "approved":
        return _status("approved", "График утверждён", "verified", "ok")
    if final_summary["status"]["key"] in {"ready", "in_progress", "returned"}:
        return final_summary["status"]
    if review_summary["status"]["key"] in {"in_progress", "returned", "finished"}:
        return review_summary["status"]
    if draft_status["exists"]:
        if draft_status.get("sent_to_review"):
            return _status("draft_sent", "Черновик отправлен", "fact_check", "ok")
        return _status("draft", "Черновик создан", "edit_calendar", "info")
    if collection_status["key"] == "finished":
        return _status("ready_for_draft", "Готово к черновику", "auto_awesome_motion", "info")
    return collection_status


def _default_stage(collection_status, draft_status, review_summary, final_summary):
    if final_summary["status"]["key"] in {"ready", "approved", "in_progress"}:
        return STAGE_FINAL
    if final_summary["status"]["key"] == "returned" and review_summary["status"]["key"] != "returned":
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
    active_year = get_active_planning_year()
    is_active_year = int(year) == active_year
    schedule = VacationSchedule.objects.filter(year=year).first()
    collection = VacationPreferenceCollection.objects.filter(year=year).first()
    collection_summary = build_preference_collection_summary(year)
    collection_status = _collection_status(collection)
    draft_status = get_schedule_draft_status(year)
    draft_context = build_schedule_draft_summary_context(year, actor=employee) if draft_status["exists"] else None
    draft_summary = draft_context["draft_summary"] if draft_context else build_schedule_draft_summary_context(year)["draft_summary"]
    calendar_summary = _calendar_summary(year, schedule)
    global_review_summary = _review_summary(schedule)
    review_summary = _review_summary(schedule, employee=employee)
    final_summary = _final_summary(schedule, global_review_summary, employee=employee)
    overall_status = _overall_status(
        calendar_summary,
        collection_status,
        draft_status,
        global_review_summary,
        final_summary,
    )

    selected_stage = params.get("stage") or _default_stage(collection_status, draft_status, global_review_summary, final_summary)
    if selected_stage not in STAGE_KEYS:
        selected_stage = _default_stage(collection_status, draft_status, global_review_summary, final_summary)
    stage_status_by_key = {
        STAGE_CALENDAR: calendar_summary["status"],
        STAGE_COLLECTION: collection_status,
        STAGE_DRAFT: _status(
            (
                "approved"
                if draft_status.get("approved")
                else ("sent" if draft_status.get("sent_to_review") else ("created" if draft_status["exists"] else "empty"))
            ),
            (
                "График утверждён"
                if draft_status.get("approved")
                else (
                    "Черновик отправлен"
                    if draft_status.get("sent_to_review")
                    else ("Черновик создан" if draft_status["exists"] else "Черновик не создан")
                )
            ),
            (
                "verified"
                if draft_status.get("approved")
                else ("fact_check" if draft_status.get("sent_to_review") else ("edit_calendar" if draft_status["exists"] else "pending_actions"))
            ),
            "ok" if draft_status.get("approved") or draft_status.get("sent_to_review") else ("info" if draft_status["exists"] else "warning"),
        ),
        STAGE_REVIEW: global_review_summary["status"],
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
    can_manage_collection = is_hr_employee(employee) and is_active_year
    can_manage_draft = is_hr_employee(employee) and is_active_year
    can_start_collection = (
        can_manage_collection
        and collection is None
    )
    draft_auto_place_job = _active_draft_auto_place_job_payload(
        year=year,
        schedule=schedule,
        employee=employee,
    )
    department_review_start_state = get_schedule_department_review_start_state(year, employee)
    enterprise_review_start_state = get_schedule_enterprise_review_start_state(year, employee)
    next_cycle_start_state = get_next_planning_cycle_start_state(year, employee)
    neural_training = build_neural_training_context(year, employee, schedule=schedule)
    planning_year_options = [
        {
            "year": option_year,
            "url": schedule_planning_url(option_year),
            "selected": option_year == year,
            "active": option_year == active_year,
        }
        for option_year in available_planning_years(year)
    ]

    return {
        "year": year,
        "active_planning_year": active_year,
        "is_active_planning_year": is_active_year,
        "planning_year_options": planning_year_options,
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
        "calendar_url": _with_planning_back(calendar_summary["calendar_url"], year, STAGE_CALENDAR),
        "readiness_url": _with_planning_back(collection_url, year, STAGE_COLLECTION),
        "draft_url": _with_planning_back(draft_url, year, STAGE_DRAFT),
        "draft_create_url": reverse("schedule_draft_create", args=[year]),
        "draft_create_next_url": schedule_planning_url(year, STAGE_DRAFT),
        "draft_auto_place_url": reverse("schedule_draft_auto_place", args=[year]),
        "draft_auto_place_next_url": schedule_planning_url(year, STAGE_DRAFT),
        "department_review_start_url": reverse("schedule_department_review_start", args=[year]),
        "department_review_start_next_url": schedule_planning_url(year, STAGE_REVIEW),
        "enterprise_review_start_url": reverse("schedule_final_review_submit", args=[year]),
        "enterprise_review_start_next_url": schedule_planning_url(year, STAGE_FINAL),
        "finish_url": reverse("preferences_collection_finish", args=[year]),
        "finish_next_url": schedule_planning_url(year, STAGE_COLLECTION),
        "can_manage_collection": can_manage_collection,
        "can_start_collection": can_start_collection,
        "calendar_preference_collection": build_calendar_preference_collection_context(
            employee,
            year,
            start_next_url=schedule_planning_url(year, STAGE_COLLECTION),
            collection=collection,
            summary=collection_summary,
            draft_status=draft_status,
        ),
        "can_manage_draft": can_manage_draft,
        "draft_is_editable": draft_status.get("is_editable", False),
        "draft_sent_to_review": draft_status.get("sent_to_review", False),
        "draft_approved": draft_status.get("approved", False),
        "can_create_draft": (
            can_manage_draft
            and collection is not None
            and collection.status == VacationPreferenceCollection.STATUS_FINISHED
            and not draft_status["exists"]
            and not draft_status["blocked_by_existing_schedule"]
        ),
        "draft_auto_place_job": draft_auto_place_job,
        "approval_blocked": bool(draft_context and draft_context["approval_blocked"]),
        "can_start_department_review": is_active_year and department_review_start_state.get("can_start", False),
        "department_review_start_block_reason": (
            department_review_start_state.get("reason", "")
            if is_active_year
            else "Действия доступны только для активного планового года."
        ),
        "can_submit_enterprise_review": is_active_year and enterprise_review_start_state.get("can_start", False),
        "enterprise_review_start_block_reason": (
            enterprise_review_start_state.get("reason", "")
            if is_active_year
            else "Действия доступны только для активного планового года."
        ),
        "can_start_next_planning_cycle": next_cycle_start_state.get("can_start", False),
        "next_planning_year": next_cycle_start_state.get("next_year", year + 1),
        "next_planning_cycle_start_block_reason": next_cycle_start_state.get("reason", ""),
        "next_planning_cycle_start_url": reverse("schedule_planning_start_next", args=[year]),
        "next_planning_cycle_start_next_url": schedule_planning_url(next_cycle_start_state.get("next_year", year + 1)),
        "neural_training": neural_training,
    }
