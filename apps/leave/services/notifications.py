from urllib.parse import urlencode

from django.db.models import Q
from django.urls import reverse

from apps.accounts.services import can_approve_leave_for_employee
from apps.core.models import Notification
from apps.core.services.notifications import create_notification, mark_notifications_done_by_dedupe_prefix
from apps.employees.models import Employees
from apps.leave.models import VacationRequest, VacationScheduleChangeRequest

from .dates import format_period_label


def _employee_label(employee):
    return employee.full_name or employee.login


def _unique_employees(employees):
    unique = []
    seen_ids = set()
    for employee in employees:
        if employee is None or employee.id in seen_ids:
            continue
        unique.append(employee)
        seen_ids.add(employee.id)
    return unique


def get_leave_approvers_for_employee(employee):
    if employee is None or employee.role in Employees.SERVICE_ROLES:
        return []

    if employee.role == Employees.ROLE_EMPLOYEE:
        department = employee.department
        candidates = []
        if department is not None:
            candidates.append(getattr(department, "head", None))
            candidates.extend(
                Employees.objects.filter(
                    role=Employees.ROLE_DEPARTMENT_HEAD,
                    department=department,
                    is_active_employee=True,
                )
            )
        return [
            approver
            for approver in _unique_employees(candidates)
            if can_approve_leave_for_employee(approver, employee)
        ]

    if employee.role in {Employees.ROLE_HR, Employees.ROLE_DEPARTMENT_HEAD}:
        return list(
            Employees.objects.filter(
                role=Employees.ROLE_ENTERPRISE_HEAD,
                is_active_employee=True,
            ).exclude(id=employee.id)
        )

    if employee.role == Employees.ROLE_ENTERPRISE_HEAD:
        return list(
            Employees.objects.filter(
                role=Employees.ROLE_AUTHORIZED_PERSON,
                is_active_employee=True,
            )
        )

    return []


def _vacation_request_action_prefix(vacation):
    return f"{Notification.TYPE_VACATION_REQUEST_CREATED}:{vacation.id}:"


def delete_vacation_request_notifications(vacation):
    detail_url = reverse("vacation_detail", args=[vacation.id])
    return Notification.objects.filter(
        Q(action_url=detail_url)
        | Q(dedupe_key__startswith=_vacation_request_action_prefix(vacation))
        | Q(dedupe_key__startswith=f"{Notification.TYPE_VACATION_REQUEST_APPROVED}:{vacation.id}:")
        | Q(dedupe_key__startswith=f"{Notification.TYPE_VACATION_REQUEST_REJECTED}:{vacation.id}:")
    ).delete()[0]


def _schedule_change_action_prefix(change_request):
    return f"{Notification.TYPE_SCHEDULE_CHANGE_CREATED}:{change_request.id}:"


def _build_url(name, query):
    return f"{reverse(name)}?{urlencode(query)}"


def notify_vacation_request_created(vacation):
    period = format_period_label(vacation.start_date, vacation.end_date)
    employee_name = _employee_label(vacation.employee)
    for approver in get_leave_approvers_for_employee(vacation.employee):
        create_notification(
            recipient=approver,
            actor=vacation.employee,
            event_type=Notification.TYPE_VACATION_REQUEST_CREATED,
            title="Новая заявка на отпуск",
            message=f"{employee_name} отправил(а) заявку на отпуск: {period}.",
            action_url=reverse("vacation_detail", args=[vacation.id]),
            priority=Notification.PRIORITY_HIGH,
            requires_action=True,
            dedupe_key=f"{_vacation_request_action_prefix(vacation)}{approver.id}",
        )


def notify_vacation_request_reviewed(vacation):
    mark_notifications_done_by_dedupe_prefix(_vacation_request_action_prefix(vacation))
    is_approved = vacation.status == vacation.STATUS_APPROVED
    event_type = (
        Notification.TYPE_VACATION_REQUEST_APPROVED
        if is_approved
        else Notification.TYPE_VACATION_REQUEST_REJECTED
    )
    title = "Заявка на отпуск одобрена" if is_approved else "Заявка на отпуск отклонена"
    status_text = "одобрена" if is_approved else "отклонена"
    period = format_period_label(vacation.start_date, vacation.end_date)
    reviewer_name = _employee_label(vacation.reviewed_by) if vacation.reviewed_by else "Согласующий"
    create_notification(
        recipient=vacation.employee,
        actor=vacation.reviewed_by,
        event_type=event_type,
        title=title,
        message=f"{reviewer_name}: ваша заявка на отпуск {period} {status_text}.",
        action_url=reverse("vacation_detail", args=[vacation.id]),
        priority=Notification.PRIORITY_NORMAL,
        requires_action=False,
        dedupe_key=f"{event_type}:{vacation.id}:{vacation.employee_id}",
    )


def notify_schedule_change_created(change_request):
    period = format_period_label(change_request.new_start_date, change_request.new_end_date)
    employee_name = _employee_label(change_request.employee)
    for approver in get_leave_approvers_for_employee(change_request.employee):
        create_notification(
            recipient=approver,
            actor=change_request.requested_by,
            event_type=Notification.TYPE_SCHEDULE_CHANGE_CREATED,
            title="Новый запрос переноса отпуска",
            message=f"{employee_name} запросил(а) перенос утверждённого отпуска на {period}.",
            action_url=_build_url(
                "applications",
                {
                    "status": VacationScheduleChangeRequest.STATUS_PENDING,
                    "search": employee_name,
                },
            ),
            priority=Notification.PRIORITY_HIGH,
            requires_action=True,
            dedupe_key=f"{_schedule_change_action_prefix(change_request)}{approver.id}",
        )


def notify_schedule_change_reviewed(change_request):
    mark_notifications_done_by_dedupe_prefix(_schedule_change_action_prefix(change_request))
    is_approved = change_request.status == change_request.STATUS_APPROVED
    event_type = (
        Notification.TYPE_SCHEDULE_CHANGE_APPROVED
        if is_approved
        else Notification.TYPE_SCHEDULE_CHANGE_REJECTED
    )
    title = "Перенос отпуска одобрен" if is_approved else "Перенос отпуска отклонён"
    status_text = "одобрен" if is_approved else "отклонён"
    period = format_period_label(change_request.new_start_date, change_request.new_end_date)
    reviewer_name = _employee_label(change_request.reviewed_by) if change_request.reviewed_by else "Согласующий"
    create_notification(
        recipient=change_request.employee,
        actor=change_request.reviewed_by,
        event_type=event_type,
        title=title,
        message=f"{reviewer_name}: ваш запрос переноса на {period} {status_text}.",
        action_url=_build_url(
            "calendar",
            {
                "view": "month",
                "year": change_request.new_start_date.year,
                "month": change_request.new_start_date.month,
                "employee": change_request.employee_id,
            },
        ),
        priority=Notification.PRIORITY_NORMAL,
        requires_action=False,
        dedupe_key=f"{event_type}:{change_request.id}:{change_request.employee_id}",
    )


def notify_preferences_collection_started(year, recipients, actor=None):
    for employee in _unique_employees(recipients):
        create_notification(
            recipient=employee,
            actor=actor,
            event_type=Notification.TYPE_PREFERENCES_COLLECTION_STARTED,
            title="Открыт сбор пожеланий по отпуску",
            message=f"Заполните пожелания по отпуску на {year} год для формирования годового графика.",
            action_url=f'{reverse("calendar")}?view=year&year={year}',
            priority=Notification.PRIORITY_HIGH,
            requires_action=True,
            dedupe_key=f"{Notification.TYPE_PREFERENCES_COLLECTION_STARTED}:{year}:{employee.id}",
        )


def notify_schedule_department_review(schedule, department_approval, actor=None):
    department = department_approval.department
    recipient = department_approval.department_head or getattr(department, "head", None)
    create_notification(
        recipient=recipient,
        actor=actor,
        event_type=Notification.TYPE_SCHEDULE_REVIEW_REQUESTED,
        title="График отпусков ожидает согласования отдела",
        message=f"Проверьте график отпусков на {schedule.year} год по отделу «{department.name}».",
        action_url=f'{reverse("calendar")}?view=year&year={schedule.year}',
        priority=Notification.PRIORITY_HIGH,
        requires_action=True,
        dedupe_key=f"{Notification.TYPE_SCHEDULE_REVIEW_REQUESTED}:department:{schedule.id}:{department.id}",
    )


def notify_schedule_enterprise_review(schedule, enterprise_approval=None, actor=None):
    recipients = []
    if enterprise_approval is not None and enterprise_approval.enterprise_head_id:
        recipients.append(enterprise_approval.enterprise_head)
    else:
        recipients.extend(Employees.objects.filter(role=Employees.ROLE_ENTERPRISE_HEAD, is_active_employee=True))

    for recipient in _unique_employees(recipients):
        create_notification(
            recipient=recipient,
            actor=actor,
            event_type=Notification.TYPE_SCHEDULE_REVIEW_REQUESTED,
            title="Годовой график готов к согласованию",
            message=f"Проверьте сводный график отпусков на {schedule.year} год.",
            action_url=f'{reverse("calendar")}?view=year&year={schedule.year}',
            priority=Notification.PRIORITY_HIGH,
            requires_action=True,
            dedupe_key=f"{Notification.TYPE_SCHEDULE_REVIEW_REQUESTED}:enterprise:{schedule.id}:{recipient.id}",
        )


def notify_schedule_authorized_review(schedule, authorized_approval=None, actor=None):
    recipients = []
    if authorized_approval is not None and authorized_approval.authorized_person_id:
        recipients.append(authorized_approval.authorized_person)
    else:
        recipients.extend(Employees.objects.filter(role=Employees.ROLE_AUTHORIZED_PERSON, is_active_employee=True))

    for recipient in _unique_employees(recipients):
        create_notification(
            recipient=recipient,
            actor=actor,
            event_type=Notification.TYPE_SCHEDULE_REVIEW_REQUESTED,
            title="Годовой график ожидает финального утверждения",
            message=f"Утвердите график отпусков на {schedule.year} год после согласования руководителем предприятия.",
            action_url=f'{reverse("calendar")}?view=year&year={schedule.year}',
            priority=Notification.PRIORITY_HIGH,
            requires_action=True,
            dedupe_key=f"{Notification.TYPE_SCHEDULE_REVIEW_REQUESTED}:authorized:{schedule.id}:{recipient.id}",
        )


def _count_created_notifications(dedupe_prefix, callback):
    before_keys = set(
        Notification.objects.filter(dedupe_key__startswith=dedupe_prefix).values_list(
            "dedupe_key",
            flat=True,
        )
    )
    callback()
    after_keys = set(
        Notification.objects.filter(dedupe_key__startswith=dedupe_prefix).values_list(
            "dedupe_key",
            flat=True,
        )
    )
    return len(after_keys - before_keys)


def backfill_pending_approval_notifications():
    stats = {
        "vacation_requests": 0,
        "schedule_changes": 0,
        "notifications_created": 0,
    }

    vacation_requests = VacationRequest.objects.select_related(
        "employee",
        "employee__department",
        "employee__department__head",
    ).filter(status=VacationRequest.STATUS_PENDING)

    for vacation in vacation_requests:
        created_count = _count_created_notifications(
            _vacation_request_action_prefix(vacation),
            lambda vacation=vacation: notify_vacation_request_created(vacation),
        )
        if created_count:
            stats["vacation_requests"] += 1
            stats["notifications_created"] += created_count

    change_requests = VacationScheduleChangeRequest.objects.select_related(
        "employee",
        "employee__department",
        "employee__department__head",
        "requested_by",
        "schedule_item",
    ).filter(status=VacationScheduleChangeRequest.STATUS_PENDING)

    for change_request in change_requests:
        created_count = _count_created_notifications(
            _schedule_change_action_prefix(change_request),
            lambda change_request=change_request: notify_schedule_change_created(change_request),
        )
        if created_count:
            stats["schedule_changes"] += 1
            stats["notifications_created"] += created_count

    return stats
