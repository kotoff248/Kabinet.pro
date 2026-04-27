import holidays
from django.db import migrations


def _russian_holiday_dates(start_date, end_date):
    holiday_dates = set()
    for year in range(start_date.year, end_date.year + 1):
        holiday_dates.update(holidays.country_holidays("RU", years=[year]).keys())
    return {holiday_date for holiday_date in holiday_dates if start_date <= holiday_date <= end_date}


def _requested_days(start_date, end_date):
    return (end_date - start_date).days + 1


def _chargeable_paid_days(start_date, end_date):
    return max(_requested_days(start_date, end_date) - len(_russian_holiday_dates(start_date, end_date)), 0)


def convert_approved_paid_requests(apps, schema_editor):
    VacationRequest = apps.get_model("leave", "VacationRequest")
    VacationSchedule = apps.get_model("leave", "VacationSchedule")
    VacationScheduleItem = apps.get_model("leave", "VacationScheduleItem")
    VacationEntitlementAllocation = apps.get_model("leave", "VacationEntitlementAllocation")

    affected_employee_ids = set()
    paid_requests = VacationRequest.objects.filter(vacation_type="paid", status="approved")
    for request_obj in paid_requests.iterator():
        existing_link = VacationScheduleItem.objects.filter(
            created_from_vacation_request_id=request_obj.id,
        ).first()
        if existing_link is not None:
            affected_employee_ids.add(request_obj.employee_id)
            continue

        exact_item = VacationScheduleItem.objects.filter(
            employee_id=request_obj.employee_id,
            vacation_type="paid",
            start_date=request_obj.start_date,
            end_date=request_obj.end_date,
            status__in=["draft", "planned", "approved"],
            created_from_vacation_request__isnull=True,
        ).first()
        if exact_item is not None:
            exact_item.created_from_vacation_request_id = request_obj.id
            exact_item.save(update_fields=["created_from_vacation_request"])
            affected_employee_ids.add(request_obj.employee_id)
            continue

        schedule = VacationSchedule.objects.filter(year=request_obj.start_date.year).first()
        if schedule is None:
            continue

        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee_id=request_obj.employee_id,
            start_date=request_obj.start_date,
            end_date=request_obj.end_date,
            vacation_type="paid",
            chargeable_days=_chargeable_paid_days(request_obj.start_date, request_obj.end_date),
            status="approved",
            source="manual",
            risk_score=request_obj.risk_score,
            risk_level=request_obj.risk_level,
            generated_by_ai=False,
            was_changed_by_manager=False,
            manager_comment="Создано из ранее одобренной оплачиваемой заявки.",
            created_from_vacation_request_id=request_obj.id,
        )
        affected_employee_ids.add(request_obj.employee_id)

    if affected_employee_ids:
        VacationEntitlementAllocation.objects.filter(employee_id__in=affected_employee_ids).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("leave", "0008_vacationscheduleitem_created_from_vacation_request"),
    ]

    operations = [
        migrations.RunPython(convert_approved_paid_requests, migrations.RunPython.noop),
    ]
