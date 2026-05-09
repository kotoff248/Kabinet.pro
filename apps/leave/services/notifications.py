from datetime import datetime, time, timedelta
from urllib.parse import urlencode

from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.accounts.services import can_approve_leave_for_employee
from apps.core.models import Notification
from apps.core.services.notifications import create_notification, mark_notifications_done_by_dedupe_prefix
from apps.employees.models import Employees
from apps.leave.models import (
    VacationRequest,
    VacationScheduleChangeRequest,
    VacationScheduleItem,
    VacationUrgentClosureRequest,
)

from .dates import format_period_label
from .urgent_closures import urgent_closure_detail_url


DEFAULT_UPCOMING_REMINDER_DAYS_BEFORE = 7
HISTORICAL_NOTIFICATION_HOUR = 9


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


def _schedule_change_detail_url(change_request):
    return reverse("schedule_change_detail", args=[change_request.id])


def _urgent_closure_action_prefix(closure_request):
    return f"urgent_closure:{closure_request.id}:"


def _is_manager_initiated_schedule_change(change_request):
    return (
        change_request.requested_by_id is not None
        and change_request.requested_by_id != change_request.employee_id
    )


def _build_url(name, query):
    return f"{reverse(name)}?{urlencode(query)}"


def _calendar_url_for_period(start_date, employee_id):
    return _build_url(
        "calendar",
        {
            "view": "month",
            "year": start_date.year,
            "month": start_date.month,
            "employee": employee_id,
        },
    )


def _notification_datetime(value, *, hour=HISTORICAL_NOTIFICATION_HOUR):
    if value is None:
        return timezone.now()
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value)
    return timezone.make_aware(datetime.combine(value, time(hour, 0)))


def _historical_status_for(event_at, as_of_date):
    if event_at is not None and event_at.date() < as_of_date:
        return Notification.STATUS_READ
    return Notification.STATUS_NEW


def _status_timestamps(status, event_at):
    if status == Notification.STATUS_DONE:
        return event_at, event_at
    if status == Notification.STATUS_READ:
        return event_at, None
    return None, None


def _update_notification_timestamps(notification, *, created_at=None, updated_at=None):
    updates = {}
    if created_at is not None:
        updates["created_at"] = created_at
    if updated_at is not None:
        updates["updated_at"] = updated_at
    if not updates:
        return False
    Notification.objects.filter(pk=notification.pk).update(**updates)
    for field_name, value in updates.items():
        setattr(notification, field_name, value)
    return True


def _sync_notification(
    *,
    recipient,
    event_type,
    title,
    message,
    actor=None,
    action_url="",
    priority=Notification.PRIORITY_NORMAL,
    requires_action=False,
    dedupe_key,
    status=Notification.STATUS_NEW,
    created_at=None,
    read_at=None,
    done_at=None,
    status_policy="force",
):
    if recipient is None:
        return None, False, False

    created_at = _notification_datetime(created_at)
    existing = Notification.objects.filter(dedupe_key=dedupe_key).first()
    if existing is None:
        notification = create_notification(
            recipient=recipient,
            actor=actor,
            event_type=event_type,
            title=title,
            message=message,
            action_url=action_url,
            priority=priority,
            requires_action=requires_action,
            dedupe_key=dedupe_key,
            status=status,
            created_at=created_at,
            read_at=read_at,
            done_at=done_at,
        )
        _update_notification_timestamps(notification, created_at=created_at, updated_at=done_at or read_at or created_at)
        return notification, True, False

    desired_status = status
    desired_read_at = read_at
    desired_done_at = done_at
    if status_policy == "preserve_active":
        if existing.status == Notification.STATUS_DONE:
            desired_status = Notification.STATUS_READ
            desired_read_at = existing.read_at or created_at
            desired_done_at = None
        else:
            desired_status = existing.status
            desired_read_at = existing.read_at
            desired_done_at = existing.done_at

    updates = {}
    desired_values = {
        "recipient_id": recipient.id,
        "actor_id": actor.id if actor is not None else None,
        "event_type": event_type,
        "title": title,
        "message": message,
        "action_url": action_url or "",
        "priority": priority,
        "requires_action": requires_action,
        "status": desired_status,
        "read_at": desired_read_at,
        "done_at": desired_done_at,
        "created_at": created_at,
    }
    for field_name, value in desired_values.items():
        if getattr(existing, field_name) != value:
            updates[field_name] = value

    if not updates:
        return existing, False, False

    updates["updated_at"] = desired_done_at or desired_read_at or timezone.now()
    Notification.objects.filter(pk=existing.pk).update(**updates)
    for field_name, value in updates.items():
        setattr(existing, field_name, value)
    return existing, False, True


def _record_sync_result(stats, category, created, updated):
    if stats is None:
        return
    if created:
        stats["notifications_created"] += 1
    if updated:
        stats["notifications_updated"] += 1
    if created or updated:
        stats[category] += 1


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
    if _is_manager_initiated_schedule_change(change_request):
        initiator_name = _employee_label(change_request.requested_by) if change_request.requested_by else "Руководитель"
        create_notification(
            recipient=change_request.employee,
            actor=change_request.requested_by,
            event_type=Notification.TYPE_SCHEDULE_CHANGE_CREATED,
            title="Предложение переноса отпуска",
            message=f"{initiator_name} предложил(а) перенести ваш отпуск на {period}.",
            action_url=_schedule_change_detail_url(change_request),
            priority=Notification.PRIORITY_HIGH,
            requires_action=True,
            dedupe_key=f"{_schedule_change_action_prefix(change_request)}{change_request.employee_id}",
        )
        return

    for approver in get_leave_approvers_for_employee(change_request.employee):
        create_notification(
            recipient=approver,
            actor=change_request.requested_by,
            event_type=Notification.TYPE_SCHEDULE_CHANGE_CREATED,
            title="Новый запрос переноса отпуска",
            message=f"{employee_name} запросил(а) перенос утверждённого отпуска на {period}.",
            action_url=_schedule_change_detail_url(change_request),
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
    if _is_manager_initiated_schedule_change(change_request):
        if not change_request.requested_by_id:
            return
        title = "Предложение переноса принято" if is_approved else "Предложение переноса отклонено"
        status_text = "принято" if is_approved else "отклонено"
        employee_name = _employee_label(change_request.employee)
        create_notification(
            recipient=change_request.requested_by,
            actor=change_request.reviewed_by,
            event_type=event_type,
            title=title,
            message=f"{employee_name}: предложение переноса на {period} {status_text}.",
            action_url=_schedule_change_detail_url(change_request),
            priority=Notification.PRIORITY_NORMAL,
            requires_action=False,
            dedupe_key=f"{event_type}:{change_request.id}:{change_request.requested_by_id}",
        )
        return

    create_notification(
        recipient=change_request.employee,
        actor=change_request.reviewed_by,
        event_type=event_type,
        title=title,
        message=f"{reviewer_name}: ваш запрос переноса на {period} {status_text}.",
        action_url=_schedule_change_detail_url(change_request),
        priority=Notification.PRIORITY_NORMAL,
        requires_action=False,
        dedupe_key=f"{event_type}:{change_request.id}:{change_request.employee_id}",
    )


def _urgent_closure_period(closure_request):
    return format_period_label(closure_request.proposed_start_date, closure_request.proposed_end_date)


def _urgent_closure_days_label(closure_request):
    days = closure_request.required_days
    if days == days.to_integral_value():
        return f"{int(days)} д."
    return f"{str(days).replace('.', ',')} д."


def _urgent_closure_approvers(closure_request):
    return get_leave_approvers_for_employee(closure_request.employee)


def _urgent_closure_hr_recipients(closure_request):
    candidates = []
    if closure_request.created_by_id and closure_request.created_by:
        candidates.append(closure_request.created_by)
    candidates.extend(
        Employees.objects.filter(role=Employees.ROLE_HR, is_active_employee=True)
    )
    return _unique_employees(candidates)


def notify_urgent_closure_created(closure_request):
    employee_name = _employee_label(closure_request.employee)
    period = _urgent_closure_period(closure_request)
    days = _urgent_closure_days_label(closure_request)
    mark_notifications_done_by_dedupe_prefix(_urgent_closure_action_prefix(closure_request))
    for approver in _urgent_closure_approvers(closure_request):
        create_notification(
            recipient=approver,
            actor=closure_request.created_by,
            event_type=Notification.TYPE_URGENT_CLOSURE_DEPARTMENT_REVIEW,
            title="Нужно согласовать закрытие остатка",
            message=(
                f"{employee_name}: нужно закрыть {days} до {closure_request.deadline:%d.%m.%Y}. "
                f"HR предложил(а) период {period}."
            ),
            action_url=urgent_closure_detail_url(closure_request),
            priority=Notification.PRIORITY_HIGH,
            requires_action=True,
            dedupe_key=f"{_urgent_closure_action_prefix(closure_request)}department:{approver.id}",
        )


def notify_urgent_closure_employee_review(closure_request):
    employee_name = _employee_label(closure_request.employee)
    period = _urgent_closure_period(closure_request)
    days = _urgent_closure_days_label(closure_request)
    reviewer_name = _employee_label(closure_request.department_reviewer) if closure_request.department_reviewer else "Руководитель"
    mark_notifications_done_by_dedupe_prefix(_urgent_closure_action_prefix(closure_request))
    create_notification(
        recipient=closure_request.employee,
        actor=closure_request.department_reviewer,
        event_type=Notification.TYPE_URGENT_CLOSURE_EMPLOYEE_REVIEW,
        title="Согласуйте период срочного отпуска",
        message=(
            f"{reviewer_name} подтвердил(а) период {period}, чтобы закрыть {days} "
            f"до {closure_request.deadline:%d.%m.%Y}."
        ),
        action_url=urgent_closure_detail_url(closure_request),
        priority=Notification.PRIORITY_HIGH,
        requires_action=True,
        dedupe_key=f"{_urgent_closure_action_prefix(closure_request)}employee:{closure_request.employee_id}",
    )
    for recipient in _urgent_closure_hr_recipients(closure_request):
        if recipient.id == closure_request.employee_id:
            continue
        create_notification(
            recipient=recipient,
            actor=closure_request.department_reviewer,
            event_type=Notification.TYPE_URGENT_CLOSURE_STATUS,
            title="Период отправлен сотруднику",
            message=f"{employee_name}: период {period} отправлен сотруднику на согласие.",
            action_url=urgent_closure_detail_url(closure_request),
            priority=Notification.PRIORITY_NORMAL,
            requires_action=False,
            dedupe_key=f"{_urgent_closure_action_prefix(closure_request)}status:employee_review:{recipient.id}",
        )


def notify_urgent_closure_period_changed_by_employee(closure_request):
    employee_name = _employee_label(closure_request.employee)
    period = _urgent_closure_period(closure_request)
    mark_notifications_done_by_dedupe_prefix(_urgent_closure_action_prefix(closure_request))
    for approver in _urgent_closure_approvers(closure_request):
        create_notification(
            recipient=approver,
            actor=closure_request.employee,
            event_type=Notification.TYPE_URGENT_CLOSURE_DEPARTMENT_REVIEW,
            title="Сотрудник предложил другой период",
            message=f"{employee_name} предложил(а) закрыть срочный остаток периодом {period}.",
            action_url=urgent_closure_detail_url(closure_request),
            priority=Notification.PRIORITY_HIGH,
            requires_action=True,
            dedupe_key=f"{_urgent_closure_action_prefix(closure_request)}department:{approver.id}",
        )
    for recipient in _urgent_closure_hr_recipients(closure_request):
        create_notification(
            recipient=recipient,
            actor=closure_request.employee,
            event_type=Notification.TYPE_URGENT_CLOSURE_STATUS,
            title="Сотрудник предложил другой период",
            message=f"{employee_name}: новый вариант {period} снова отправлен руководителю.",
            action_url=urgent_closure_detail_url(closure_request),
            priority=Notification.PRIORITY_NORMAL,
            requires_action=False,
            dedupe_key=f"{_urgent_closure_action_prefix(closure_request)}status:period_changed:{recipient.id}",
        )


def notify_urgent_closure_hr_finalization(closure_request):
    employee_name = _employee_label(closure_request.employee)
    period = _urgent_closure_period(closure_request)
    mark_notifications_done_by_dedupe_prefix(_urgent_closure_action_prefix(closure_request))
    for recipient in _urgent_closure_hr_recipients(closure_request):
        create_notification(
            recipient=recipient,
            actor=closure_request.employee,
            event_type=Notification.TYPE_URGENT_CLOSURE_HR_FINALIZATION,
            title="Финализируйте закрытие остатка",
            message=f"{employee_name} согласовал(а) период {period}. Нужно внести корректировку в график.",
            action_url=urgent_closure_detail_url(closure_request),
            priority=Notification.PRIORITY_HIGH,
            requires_action=True,
            dedupe_key=f"{_urgent_closure_action_prefix(closure_request)}hr:{recipient.id}",
        )


def notify_urgent_closure_rejected(closure_request):
    employee_name = _employee_label(closure_request.employee)
    actor_name = _employee_label(closure_request.rejected_by) if closure_request.rejected_by else "Участник согласования"
    mark_notifications_done_by_dedupe_prefix(_urgent_closure_action_prefix(closure_request))
    recipients = _unique_employees(
        [
            closure_request.created_by,
            closure_request.employee,
            closure_request.department_reviewer,
        ]
    )
    for recipient in recipients:
        if closure_request.rejected_by_id and recipient.id == closure_request.rejected_by_id:
            continue
        create_notification(
            recipient=recipient,
            actor=closure_request.rejected_by,
            event_type=Notification.TYPE_URGENT_CLOSURE_STATUS,
            title="Закрытие остатка отклонено",
            message=f"{actor_name}: согласование срочного остатка сотрудника {employee_name} отклонено.",
            action_url=urgent_closure_detail_url(closure_request),
            priority=Notification.PRIORITY_NORMAL,
            requires_action=False,
            dedupe_key=f"{_urgent_closure_action_prefix(closure_request)}rejected:{recipient.id}",
        )


def notify_urgent_closure_completed(closure_request):
    employee_name = _employee_label(closure_request.employee)
    period = _urgent_closure_period(closure_request)
    mark_notifications_done_by_dedupe_prefix(_urgent_closure_action_prefix(closure_request))
    recipients = _unique_employees(
        [
            closure_request.created_by,
            closure_request.employee,
            closure_request.department_reviewer,
        ]
    )
    for recipient in recipients:
        create_notification(
            recipient=recipient,
            actor=closure_request.finalized_by,
            event_type=Notification.TYPE_URGENT_CLOSURE_STATUS,
            title="Срочный остаток закрыт",
            message=f"{employee_name}: отпуск {period} внесён в график {closure_request.closure_year} года.",
            action_url=urgent_closure_detail_url(closure_request),
            priority=Notification.PRIORITY_NORMAL,
            requires_action=False,
            dedupe_key=f"{_urgent_closure_action_prefix(closure_request)}completed:{recipient.id}",
        )


def notify_preferences_collection_started(year, recipients, actor=None):
    for employee in _unique_employees(recipients):
        _sync_notification(
            recipient=employee,
            actor=actor,
            event_type=Notification.TYPE_PREFERENCES_COLLECTION_STARTED,
            title="Открыт сбор пожеланий по отпуску",
            message=f"Заполните пожелания по отпуску на {year} год для формирования годового графика.",
            action_url=reverse("vacation_preferences", args=[year]),
            priority=Notification.PRIORITY_HIGH,
            requires_action=True,
            status=Notification.STATUS_NEW,
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


def notify_schedule_item_changed_by_manager(schedule_item, actor=None):
    if not _is_manager_changed_item_notification_candidate(schedule_item):
        return None

    period = format_period_label(schedule_item.start_date, schedule_item.end_date)
    actor_name = _employee_label(actor) if actor is not None else "Руководитель"
    return create_notification(
        recipient=schedule_item.employee,
        actor=actor,
        event_type=Notification.TYPE_SCHEDULE_ITEM_CHANGED_BY_MANAGER,
        title="График отпуска изменён",
        message=f"{actor_name} изменил(а) период вашего отпуска: {period}.",
        action_url=_calendar_url_for_period(schedule_item.start_date, schedule_item.employee_id),
        priority=Notification.PRIORITY_NORMAL,
        requires_action=False,
        dedupe_key=_schedule_item_changed_by_manager_dedupe_key(schedule_item),
    )


def _schedule_item_changed_by_manager_dedupe_key(schedule_item):
    return f"{Notification.TYPE_SCHEDULE_ITEM_CHANGED_BY_MANAGER}:{schedule_item.id}:{schedule_item.employee_id}"


def _is_manager_changed_item_notification_candidate(schedule_item):
    if not (
        schedule_item.id is not None
        and schedule_item.was_changed_by_manager
        and schedule_item.status in VacationScheduleItem.ACTIVE_STATUSES
        and schedule_item.source != VacationScheduleItem.SOURCE_TRANSFER
        and schedule_item.created_from_change_request_id is None
        and schedule_item.created_from_vacation_request_id is None
    ):
        return False

    return not VacationScheduleChangeRequest.objects.filter(schedule_item_id=schedule_item.id).exists()


def _sync_schedule_item_changed_by_manager_notification(schedule_item, *, stats, as_of_date):
    if not _is_manager_changed_item_notification_candidate(schedule_item):
        deleted, _ = Notification.objects.filter(
            dedupe_key=_schedule_item_changed_by_manager_dedupe_key(schedule_item),
        ).delete()
        if deleted:
            stats["notifications_updated"] += deleted
            stats["schedule_item_changes"] += 1
        return

    event_at = _notification_datetime(schedule_item.created_at)
    status = _historical_status_for(event_at, as_of_date)
    read_at, done_at = _status_timestamps(status, event_at)
    period = format_period_label(schedule_item.start_date, schedule_item.end_date)
    _, created, updated = _sync_notification(
        recipient=schedule_item.employee,
        actor=None,
        event_type=Notification.TYPE_SCHEDULE_ITEM_CHANGED_BY_MANAGER,
        title="График отпуска изменён",
        message=f"Руководитель изменил(а) период вашего отпуска: {period}.",
        action_url=_calendar_url_for_period(schedule_item.start_date, schedule_item.employee_id),
        priority=Notification.PRIORITY_NORMAL,
        requires_action=False,
        dedupe_key=_schedule_item_changed_by_manager_dedupe_key(schedule_item),
        status=status,
        created_at=event_at,
        read_at=read_at,
        done_at=done_at,
    )
    _record_sync_result(stats, "schedule_item_changes", created, updated)


def _sync_vacation_request_notifications(vacation, *, stats, as_of_date):
    period = format_period_label(vacation.start_date, vacation.end_date)
    employee_name = _employee_label(vacation.employee)
    action_url = reverse("vacation_detail", args=[vacation.id])

    if vacation.status == VacationRequest.STATUS_PENDING:
        for approver in get_leave_approvers_for_employee(vacation.employee):
            _, created, updated = _sync_notification(
                recipient=approver,
                actor=vacation.employee,
                event_type=Notification.TYPE_VACATION_REQUEST_CREATED,
                title="Новая заявка на отпуск",
                message=f"{employee_name} отправил(а) заявку на отпуск: {period}.",
                action_url=action_url,
                priority=Notification.PRIORITY_HIGH,
                requires_action=True,
                dedupe_key=f"{_vacation_request_action_prefix(vacation)}{approver.id}",
                status=Notification.STATUS_NEW,
                created_at=vacation.created_at,
                status_policy="preserve_active",
            )
            _record_sync_result(stats, "vacation_requests", created, updated)
        return

    reviewers = [vacation.reviewed_by] if vacation.reviewed_by_id else get_leave_approvers_for_employee(vacation.employee)
    review_event_at = _notification_datetime(vacation.reviewed_at or vacation.created_at)
    for reviewer in _unique_employees(reviewers):
        _, created, updated = _sync_notification(
            recipient=reviewer,
            actor=vacation.employee,
            event_type=Notification.TYPE_VACATION_REQUEST_CREATED,
            title="Новая заявка на отпуск",
            message=f"{employee_name} отправил(а) заявку на отпуск: {period}.",
            action_url=action_url,
            priority=Notification.PRIORITY_HIGH,
            requires_action=True,
            dedupe_key=f"{_vacation_request_action_prefix(vacation)}{reviewer.id}",
            status=Notification.STATUS_DONE,
            created_at=vacation.created_at,
            read_at=review_event_at,
            done_at=review_event_at,
        )
        _record_sync_result(stats, "vacation_requests", created, updated)

    is_approved = vacation.status == VacationRequest.STATUS_APPROVED
    event_type = (
        Notification.TYPE_VACATION_REQUEST_APPROVED
        if is_approved
        else Notification.TYPE_VACATION_REQUEST_REJECTED
    )
    title = "Заявка на отпуск одобрена" if is_approved else "Заявка на отпуск отклонена"
    status_text = "одобрена" if is_approved else "отклонена"
    reviewer_name = _employee_label(vacation.reviewed_by) if vacation.reviewed_by else "Согласующий"
    notification_status = _historical_status_for(review_event_at, as_of_date)
    read_at, done_at = _status_timestamps(notification_status, review_event_at)
    _, created, updated = _sync_notification(
        recipient=vacation.employee,
        actor=vacation.reviewed_by,
        event_type=event_type,
        title=title,
        message=f"{reviewer_name}: ваша заявка на отпуск {period} {status_text}.",
        action_url=action_url,
        priority=Notification.PRIORITY_NORMAL,
        requires_action=False,
        dedupe_key=f"{event_type}:{vacation.id}:{vacation.employee_id}",
        status=notification_status,
        created_at=review_event_at,
        read_at=read_at,
        done_at=done_at,
    )
    _record_sync_result(stats, "vacation_requests", created, updated)


def _sync_schedule_change_notifications(change_request, *, stats, as_of_date):
    employee_name = _employee_label(change_request.employee)
    period = format_period_label(change_request.new_start_date, change_request.new_end_date)
    action_url = _schedule_change_detail_url(change_request)
    is_manager_initiated = _is_manager_initiated_schedule_change(change_request)

    if change_request.status == VacationScheduleChangeRequest.STATUS_PENDING:
        if is_manager_initiated:
            initiator_name = _employee_label(change_request.requested_by) if change_request.requested_by else "Руководитель"
            _, created, updated = _sync_notification(
                recipient=change_request.employee,
                actor=change_request.requested_by,
                event_type=Notification.TYPE_SCHEDULE_CHANGE_CREATED,
                title="Предложение переноса отпуска",
                message=f"{initiator_name} предложил(а) перенести ваш отпуск на {period}.",
                action_url=action_url,
                priority=Notification.PRIORITY_HIGH,
                requires_action=True,
                dedupe_key=f"{_schedule_change_action_prefix(change_request)}{change_request.employee_id}",
                status=Notification.STATUS_NEW,
                created_at=change_request.created_at,
                status_policy="preserve_active",
            )
            _record_sync_result(stats, "schedule_changes", created, updated)
            return

        for approver in get_leave_approvers_for_employee(change_request.employee):
            _, created, updated = _sync_notification(
                recipient=approver,
                actor=change_request.requested_by,
                event_type=Notification.TYPE_SCHEDULE_CHANGE_CREATED,
                title="Новый запрос переноса отпуска",
                message=f"{employee_name} запросил(а) перенос утверждённого отпуска на {period}.",
                action_url=action_url,
                priority=Notification.PRIORITY_HIGH,
                requires_action=True,
                dedupe_key=f"{_schedule_change_action_prefix(change_request)}{approver.id}",
                status=Notification.STATUS_NEW,
                created_at=change_request.created_at,
                status_policy="preserve_active",
            )
            _record_sync_result(stats, "schedule_changes", created, updated)
        return

    review_event_at = _notification_datetime(change_request.reviewed_at or change_request.created_at)
    if is_manager_initiated:
        initiator_name = _employee_label(change_request.requested_by) if change_request.requested_by else "Руководитель"
        _, created, updated = _sync_notification(
            recipient=change_request.employee,
            actor=change_request.requested_by,
            event_type=Notification.TYPE_SCHEDULE_CHANGE_CREATED,
            title="Предложение переноса отпуска",
            message=f"{initiator_name} предложил(а) перенести ваш отпуск на {period}.",
            action_url=action_url,
            priority=Notification.PRIORITY_HIGH,
            requires_action=True,
            dedupe_key=f"{_schedule_change_action_prefix(change_request)}{change_request.employee_id}",
            status=Notification.STATUS_DONE,
            created_at=change_request.created_at,
            read_at=review_event_at,
            done_at=review_event_at,
        )
        _record_sync_result(stats, "schedule_changes", created, updated)
    else:
        reviewers = (
            [change_request.reviewed_by]
            if change_request.reviewed_by_id
            else get_leave_approvers_for_employee(change_request.employee)
        )
        for reviewer in _unique_employees(reviewers):
            _, created, updated = _sync_notification(
                recipient=reviewer,
                actor=change_request.requested_by,
                event_type=Notification.TYPE_SCHEDULE_CHANGE_CREATED,
                title="Новый запрос переноса отпуска",
                message=f"{employee_name} запросил(а) перенос утверждённого отпуска на {period}.",
                action_url=action_url,
                priority=Notification.PRIORITY_HIGH,
                requires_action=True,
                dedupe_key=f"{_schedule_change_action_prefix(change_request)}{reviewer.id}",
                status=Notification.STATUS_DONE,
                created_at=change_request.created_at,
                read_at=review_event_at,
                done_at=review_event_at,
            )
            _record_sync_result(stats, "schedule_changes", created, updated)

    if not is_manager_initiated:
        result_recipient = change_request.employee
        result_dedupe_recipient_id = change_request.employee_id
        result_message_actor = _employee_label(change_request.reviewed_by) if change_request.reviewed_by else "Согласующий"
    else:
        if not change_request.requested_by_id:
            return
        result_recipient = change_request.requested_by
        result_dedupe_recipient_id = change_request.requested_by_id
        result_message_actor = employee_name

    is_approved = change_request.status == VacationScheduleChangeRequest.STATUS_APPROVED
    event_type = (
        Notification.TYPE_SCHEDULE_CHANGE_APPROVED
        if is_approved
        else Notification.TYPE_SCHEDULE_CHANGE_REJECTED
    )
    if is_manager_initiated:
        title = "Предложение переноса принято" if is_approved else "Предложение переноса отклонено"
        status_text = "принято" if is_approved else "отклонено"
        message = f"{result_message_actor}: предложение переноса на {period} {status_text}."
    else:
        title = "Перенос отпуска одобрен" if is_approved else "Перенос отпуска отклонён"
        status_text = "одобрен" if is_approved else "отклонён"
        message = f"{result_message_actor}: ваш запрос переноса на {period} {status_text}."

    notification_status = _historical_status_for(review_event_at, as_of_date)
    read_at, done_at = _status_timestamps(notification_status, review_event_at)
    _, created, updated = _sync_notification(
        recipient=result_recipient,
        actor=change_request.reviewed_by,
        event_type=event_type,
        title=title,
        message=message,
        action_url=action_url,
        priority=Notification.PRIORITY_NORMAL,
        requires_action=False,
        dedupe_key=f"{event_type}:{change_request.id}:{result_dedupe_recipient_id}",
        status=notification_status,
        created_at=review_event_at,
        read_at=read_at,
        done_at=done_at,
    )
    _record_sync_result(stats, "schedule_changes", created, updated)
    return


def _sync_upcoming_schedule_item_reminder(schedule_item, *, stats, days_before, as_of_date, status=None):
    reminder_at = _notification_datetime(schedule_item.start_date - timedelta(days=days_before))
    notification_status = status or _historical_status_for(reminder_at, as_of_date)
    read_at, done_at = _status_timestamps(notification_status, reminder_at)
    period = format_period_label(schedule_item.start_date, schedule_item.end_date)
    _, created, updated = _sync_notification(
        recipient=schedule_item.employee,
        actor=None,
        event_type=Notification.TYPE_UPCOMING_VACATION_REMINDER,
        title="Скоро отпуск",
        message=f"Ваш отпуск по графику начнётся через {days_before} дней: {period}.",
        action_url=_calendar_url_for_period(schedule_item.start_date, schedule_item.employee_id),
        priority=Notification.PRIORITY_NORMAL,
        requires_action=False,
        dedupe_key=(
            f"{Notification.TYPE_UPCOMING_VACATION_REMINDER}:"
            f"schedule_item:{schedule_item.id}:{schedule_item.employee_id}:{days_before}"
        ),
        status=notification_status,
        created_at=reminder_at,
        read_at=read_at,
        done_at=done_at,
    )
    _record_sync_result(stats, "upcoming_reminders", created, updated)


def _sync_upcoming_vacation_request_reminder(vacation, *, stats, days_before, as_of_date, status=None):
    reminder_at = _notification_datetime(vacation.start_date - timedelta(days=days_before))
    notification_status = status or _historical_status_for(reminder_at, as_of_date)
    read_at, done_at = _status_timestamps(notification_status, reminder_at)
    period = format_period_label(vacation.start_date, vacation.end_date)
    _, created, updated = _sync_notification(
        recipient=vacation.employee,
        actor=None,
        event_type=Notification.TYPE_UPCOMING_VACATION_REMINDER,
        title="Скоро отпуск",
        message=f"Ваш отпуск начнётся через {days_before} дней: {period}.",
        action_url=reverse("vacation_detail", args=[vacation.id]),
        priority=Notification.PRIORITY_NORMAL,
        requires_action=False,
        dedupe_key=(
            f"{Notification.TYPE_UPCOMING_VACATION_REMINDER}:"
            f"request:{vacation.id}:{vacation.employee_id}:{days_before}"
        ),
        status=notification_status,
        created_at=reminder_at,
        read_at=read_at,
        done_at=done_at,
    )
    _record_sync_result(stats, "upcoming_reminders", created, updated)


def _base_backfill_stats():
    stats = {
        "vacation_requests": 0,
        "schedule_changes": 0,
        "upcoming_reminders": 0,
        "schedule_item_changes": 0,
        "notifications_created": 0,
        "notifications_updated": 0,
    }
    return stats


def send_upcoming_vacation_reminders(*, days_before=DEFAULT_UPCOMING_REMINDER_DAYS_BEFORE, as_of_date=None):
    as_of_date = as_of_date or timezone.localdate()
    target_date = as_of_date + timedelta(days=days_before)
    stats = _base_backfill_stats()

    schedule_items = VacationScheduleItem.objects.select_related("employee").filter(
        status__in=VacationScheduleItem.ACTIVE_STATUSES,
        start_date=target_date,
        employee__is_active_employee=True,
    )
    for schedule_item in schedule_items:
        _sync_upcoming_schedule_item_reminder(
            schedule_item,
            stats=stats,
            days_before=days_before,
            as_of_date=as_of_date,
            status=Notification.STATUS_NEW,
        )

    vacation_requests = (
        VacationRequest.objects.select_related("employee")
        .filter(
            status=VacationRequest.STATUS_APPROVED,
            start_date=target_date,
            employee__is_active_employee=True,
        )
        .exclude(vacation_type="paid", created_schedule_items__isnull=False)
        .distinct()
    )
    for vacation in vacation_requests:
        _sync_upcoming_vacation_request_reminder(
            vacation,
            stats=stats,
            days_before=days_before,
            as_of_date=as_of_date,
            status=Notification.STATUS_NEW,
        )

    return stats


def backfill_notifications_from_history(*, days_before=DEFAULT_UPCOMING_REMINDER_DAYS_BEFORE, as_of_date=None):
    as_of_date = as_of_date or timezone.localdate()
    stats = _base_backfill_stats()

    vacation_requests = VacationRequest.objects.select_related(
        "employee",
        "employee__department",
        "employee__department__head",
        "reviewed_by",
    ).all()

    for vacation in vacation_requests:
        _sync_vacation_request_notifications(vacation, stats=stats, as_of_date=as_of_date)

    change_requests = VacationScheduleChangeRequest.objects.select_related(
        "employee",
        "employee__department",
        "employee__department__head",
        "requested_by",
        "reviewed_by",
        "schedule_item",
    ).all()

    for change_request in change_requests:
        _sync_schedule_change_notifications(change_request, stats=stats, as_of_date=as_of_date)

    reminder_cutoff = as_of_date + timedelta(days=days_before)
    schedule_items = VacationScheduleItem.objects.select_related("employee").filter(
        status__in=VacationScheduleItem.ACTIVE_STATUSES,
        start_date__lte=reminder_cutoff,
        employee__is_active_employee=True,
    )
    for schedule_item in schedule_items:
        _sync_upcoming_schedule_item_reminder(
            schedule_item,
            stats=stats,
            days_before=days_before,
            as_of_date=as_of_date,
        )

    vacation_reminders = (
        VacationRequest.objects.select_related("employee")
        .filter(
            status=VacationRequest.STATUS_APPROVED,
            start_date__lte=reminder_cutoff,
            employee__is_active_employee=True,
        )
        .exclude(vacation_type="paid", created_schedule_items__isnull=False)
        .distinct()
    )
    for vacation in vacation_reminders:
        _sync_upcoming_vacation_request_reminder(
            vacation,
            stats=stats,
            days_before=days_before,
            as_of_date=as_of_date,
        )

    manager_changed_items = VacationScheduleItem.objects.select_related("employee").filter(
        was_changed_by_manager=True,
        employee__is_active_employee=True,
    )
    for schedule_item in manager_changed_items:
        _sync_schedule_item_changed_by_manager_notification(schedule_item, stats=stats, as_of_date=as_of_date)

    return stats


def backfill_pending_approval_notifications():
    return backfill_notifications_from_history()
