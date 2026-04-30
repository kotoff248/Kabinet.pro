from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from apps.leave.models import VacationRequest, VacationScheduleItem
from apps.leave.services.metrics import is_vacation_metric_sync_enabled, sync_employee_vacation_metrics


def _sync_metrics_on_commit(employee):
    if not is_vacation_metric_sync_enabled():
        return
    transaction.on_commit(lambda: sync_employee_vacation_metrics(employee))


@receiver(post_save, sender=VacationRequest)
def update_employee_status(sender, instance, **kwargs):
    _sync_metrics_on_commit(instance.employee)


@receiver(post_delete, sender=VacationRequest)
def reset_employee_status(sender, instance, **kwargs):
    _sync_metrics_on_commit(instance.employee)


@receiver(post_save, sender=VacationScheduleItem)
def update_employee_status_from_schedule_item(sender, instance, **kwargs):
    _sync_metrics_on_commit(instance.employee)


@receiver(post_delete, sender=VacationScheduleItem)
def reset_employee_status_from_schedule_item(sender, instance, **kwargs):
    _sync_metrics_on_commit(instance.employee)

