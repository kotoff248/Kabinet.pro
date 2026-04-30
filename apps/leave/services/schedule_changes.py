from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as date_format

from apps.accounts.services import can_approve_leave_for_employee
from apps.leave.models import VacationScheduleChangeRequest, VacationScheduleItem

from .constants import REQUEST_STATUS_UI
from .dates import format_period_label
from .notifications import notify_schedule_change_created, notify_schedule_change_reviewed
from .risk import calculate_schedule_change_risk
from .validation import validate_schedule_change_request

def _validate_reviewer_can_approve_change(reviewer, employee):
    if not can_approve_leave_for_employee(reviewer, employee):
        raise ValidationError("У вас нет прав для согласования этого переноса.")

def get_schedule_change_requests_queryset():
    return VacationScheduleChangeRequest.objects.select_related(
        "employee",
        "employee__department",
        "schedule_item",
        "schedule_item__schedule",
        "requested_by",
        "reviewed_by",
    )

def _change_request_status_meta(change_request):
    return REQUEST_STATUS_UI[change_request.status]

def enrich_schedule_change_request(change_request):
    status_meta = _change_request_status_meta(change_request)
    change_request.status_label = status_meta["label"]
    change_request.status_icon = status_meta["icon"]
    change_request.status_css_class = status_meta["css_class"]
    change_request.risk_label = change_request.get_risk_level_display()
    change_request.old_period_label = format_period_label(change_request.old_start_date, change_request.old_end_date)
    change_request.new_period_label = format_period_label(change_request.new_start_date, change_request.new_end_date)
    change_request.created_at_formatted = date_format(change_request.created_at, "j E Y")
    return change_request

def serialize_schedule_change_request_row(change_request):
    enrich_schedule_change_request(change_request)
    return {
        "id": change_request.id,
        "employee_name": change_request.employee.full_name,
        "employee_department": change_request.employee.department.name if change_request.employee.department else "Не указан",
        "profile_url": reverse("employee_profile", args=[change_request.employee_id]),
        "old_period_label": change_request.old_period_label,
        "new_period_label": change_request.new_period_label,
        "status": change_request.status,
        "status_label": change_request.status_label,
        "status_icon": change_request.status_icon,
        "status_css_class": change_request.status_css_class,
        "risk_score": change_request.risk_score,
        "risk_label": change_request.risk_label,
        "can_approve": getattr(change_request, "can_approve", False),
        "decision_locked": getattr(change_request, "decision_locked", False),
        "approve_url": reverse("schedule_change_approve", args=[change_request.id]),
        "reject_url": reverse("schedule_change_reject", args=[change_request.id]),
    }

@transaction.atomic
def create_schedule_change_request(schedule_item_id, requested_by, new_start_date, new_end_date, reason=""):
    schedule_item = VacationScheduleItem.objects.select_related("employee", "schedule").select_for_update().get(
        pk=schedule_item_id
    )
    if requested_by is None or requested_by.id != schedule_item.employee_id:
        raise ValidationError("Запросить перенос может только сотрудник, которому принадлежит отпуск.")

    validate_schedule_change_request(schedule_item, new_start_date, new_end_date)
    risk_payload = calculate_schedule_change_risk(schedule_item, new_start_date, new_end_date)
    change_request = VacationScheduleChangeRequest.objects.create(
        schedule_item=schedule_item,
        employee=schedule_item.employee,
        old_start_date=schedule_item.start_date,
        old_end_date=schedule_item.end_date,
        new_start_date=new_start_date,
        new_end_date=new_end_date,
        reason=reason,
        requested_by=requested_by,
        **risk_payload,
    )
    notify_schedule_change_created(change_request)
    return change_request

@transaction.atomic
def approve_schedule_change_request(change_request_id, *, reviewer, review_comment=""):
    change_request = get_schedule_change_requests_queryset().select_for_update(of=("self",)).get(pk=change_request_id)
    if change_request.status != VacationScheduleChangeRequest.STATUS_PENDING:
        raise ValidationError("Одобрить можно только запрос переноса в ожидании.")
    _validate_reviewer_can_approve_change(reviewer, change_request.employee)

    schedule_item = VacationScheduleItem.objects.select_related("employee", "schedule").select_for_update().get(
        pk=change_request.schedule_item_id
    )
    new_chargeable_days = validate_schedule_change_request(
        schedule_item,
        change_request.new_start_date,
        change_request.new_end_date,
        exclude_change_request_id=change_request.id,
    )
    risk_payload = calculate_schedule_change_risk(
        schedule_item,
        change_request.new_start_date,
        change_request.new_end_date,
    )

    schedule_item.status = VacationScheduleItem.STATUS_TRANSFERRED
    schedule_item.was_changed_by_manager = True
    schedule_item.manager_comment = "Перенесено по согласованному запросу сотрудника."
    schedule_item.save(update_fields=["status", "was_changed_by_manager", "manager_comment"])

    replacement_item = VacationScheduleItem.objects.create(
        schedule=schedule_item.schedule,
        employee=schedule_item.employee,
        start_date=change_request.new_start_date,
        end_date=change_request.new_end_date,
        vacation_type=schedule_item.vacation_type,
        chargeable_days=new_chargeable_days,
        status=VacationScheduleItem.STATUS_APPROVED,
        source=VacationScheduleItem.SOURCE_TRANSFER,
        risk_score=risk_payload["risk_score"],
        risk_level=risk_payload["risk_level"],
        generated_by_ai=False,
        was_changed_by_manager=True,
        manager_comment="Создано после согласования переноса.",
        previous_item=schedule_item,
        created_from_change_request=change_request,
    )

    change_request.status = VacationScheduleChangeRequest.STATUS_APPROVED
    change_request.reviewed_by = reviewer
    change_request.reviewed_at = timezone.now()
    change_request.review_comment = review_comment
    for field_name, value in risk_payload.items():
        setattr(change_request, field_name, value)
    change_request.save(
        update_fields=[
            "status",
            "reviewed_by",
            "reviewed_at",
            "review_comment",
            "risk_score",
            "risk_level",
            "department_load_level",
            "overlapping_absences_count",
            "remaining_staff_count",
            "min_staff_required",
            "balance_after_change",
        ]
    )
    notify_schedule_change_reviewed(change_request)
    return replacement_item

@transaction.atomic
def reject_schedule_change_request(change_request_id, *, reviewer, review_comment=""):
    change_request = get_schedule_change_requests_queryset().select_for_update(of=("self",)).get(pk=change_request_id)
    if change_request.status != VacationScheduleChangeRequest.STATUS_PENDING:
        raise ValidationError("Отклонить можно только запрос переноса в ожидании.")
    _validate_reviewer_can_approve_change(reviewer, change_request.employee)

    risk_payload = calculate_schedule_change_risk(
        change_request.schedule_item,
        change_request.new_start_date,
        change_request.new_end_date,
    )
    change_request.status = VacationScheduleChangeRequest.STATUS_REJECTED
    change_request.reviewed_by = reviewer
    change_request.reviewed_at = timezone.now()
    change_request.review_comment = review_comment
    for field_name, value in risk_payload.items():
        setattr(change_request, field_name, value)
    change_request.save(
        update_fields=[
            "status",
            "reviewed_by",
            "reviewed_at",
            "review_comment",
            "risk_score",
            "risk_level",
            "department_load_level",
            "overlapping_absences_count",
            "remaining_staff_count",
            "min_staff_required",
            "balance_after_change",
        ]
    )
    notify_schedule_change_reviewed(change_request)
    return change_request
