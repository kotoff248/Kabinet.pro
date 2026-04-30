from datetime import datetime, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.employees.models import Departments


DEMO_DEPARTMENT_FORMATION = {
    "Производство": (1, 11, 9, 0),
    "Техническое обслуживание": (2, 8, 9, 30),
    "Промышленная безопасность": (3, 15, 10, 0),
    "Логистика": (4, 12, 9, 15),
    "Финансы и закупки": (5, 17, 10, 30),
}


def _aware(value):
    if timezone.is_naive(value):
        return timezone.make_aware(value, timezone.get_current_timezone())
    return value


class Command(BaseCommand):
    help = "Repairs demo department formation dates and department head start dates."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Apply changes. Without it the command only prints a dry run.")

    def handle(self, *args, **options):
        should_apply = options["apply"]
        formation_year = timezone.localdate().year - 5
        departments = Departments.objects.select_related("head").order_by("id")
        changed = 0

        for department in departments:
            formation = DEMO_DEPARTMENT_FORMATION.get(department.name)
            if formation is None:
                continue

            target = datetime(formation_year, *formation)
            target = _aware(target)
            target_date = target.date()
            department_changed = timezone.localtime(department.date_added).date() != target_date
            head_changed = department.head is not None and department.head.date_joined != target_date
            employee_scope = department.employees.all()
            if department.head_id:
                employee_scope = employee_scope.exclude(pk=department.head_id)
            early_employees = employee_scope.filter(date_joined__lt=target_date + timedelta(days=1))
            early_count = early_employees.count()

            if not department_changed and not head_changed and early_count == 0:
                continue

            changed += 1
            self.stdout.write(f"{department.name}: formation -> {target:%Y-%m-%d %H:%M}, early employees={early_count}")

            if should_apply:
                if department_changed:
                    department.date_added = target
                    department.save(update_fields=["date_added"])
                if head_changed:
                    department.head.date_joined = target_date
                    department.head.save(update_fields=["date_joined"])
                if early_count:
                    early_employees.update(date_joined=target_date + timedelta(days=1))

        mode = "updated" if should_apply else "would update"
        self.stdout.write(self.style.SUCCESS(f"{mode}: {changed} department(s)"))
