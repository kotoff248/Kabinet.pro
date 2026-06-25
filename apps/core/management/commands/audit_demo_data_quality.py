from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.core.models import DemoDataResetJob
from apps.employees.models import Departments, Employees
from apps.leave.models import VacationRequest, VacationSchedule, VacationScheduleItem
from apps.leave.services.ledger import get_employee_entitlement_rows
from apps.leave.services.querysets import exclude_converted_paid_requests


class Command(BaseCommand):
    help = "Checks generated demo data for old leave leftovers and leadership-pair absence conflicts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--legacy-cutoff-year",
            type=int,
            default=None,
            help="Flag remaining entitlement rows with must_use_by up to this year. Defaults to current year - 3.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=10,
            help="Maximum number of problem rows to print per section.",
        )

    def handle(self, *args, **options):
        as_of = timezone.localdate()
        cutoff_year = options["legacy_cutoff_year"] or (as_of.year - 3)
        limit = max(int(options["limit"] or 10), 0)

        schedules = list(VacationSchedule.objects.order_by("year").values_list("year", "status"))
        employees_qs = Employees.objects.filter(is_active_employee=True).exclude(role__in=Employees.SERVICE_ROLES)
        active_employee_count = employees_qs.count()
        self.stdout.write(f"Дата проверки: {as_of}")
        self.stdout.write(f"Активные сотрудники: {active_employee_count}")
        self.stdout.write(f"Графики: {schedules}")

        latest_job = (
            DemoDataResetJob.objects.order_by("-id")
            .values_list("id", "status", "preset", "employee_count", "history_years", "progress_percent")
            .first()
        )
        if latest_job:
            self.stdout.write(f"Последний UI reset-job: {latest_job}")

        missing_assignments = self._missing_leadership_assignments()
        legacy_leftovers = self._legacy_overdue_rows(employees_qs, cutoff_year)
        leadership_conflicts = self._leadership_pair_conflicts()

        self.stdout.write("")
        self.stdout.write(f"Старые остатки до {cutoff_year}: {len(legacy_leftovers)}")
        for row in legacy_leftovers[:limit]:
            self.stdout.write(f"  - {row}")

        self.stdout.write(f"Конфликты руководитель+заместитель: {len(leadership_conflicts)}")
        for row in leadership_conflicts[:limit]:
            self.stdout.write(f"  - {row}")

        self.stdout.write(f"Отделы без руководителя/заместителя: {len(missing_assignments)}")
        for row in missing_assignments[:limit]:
            self.stdout.write(f"  - {row}")

        errors = []
        if legacy_leftovers:
            errors.append(f"старые остатки до {cutoff_year}: {len(legacy_leftovers)}")
        if leadership_conflicts:
            errors.append(f"конфликты руководитель+заместитель: {len(leadership_conflicts)}")
        if missing_assignments:
            errors.append(f"неполные назначения руководства отдела: {len(missing_assignments)}")

        if errors:
            raise CommandError("Демо-база требует проверки: " + "; ".join(errors))

        self.stdout.write(self.style.SUCCESS("Демо-база прошла быстрый аудит качества."))

    def _legacy_overdue_rows(self, employees_qs, cutoff_year):
        cutoff_date = date(cutoff_year, 12, 31)
        rows = []
        for employee in employees_qs.order_by("id"):
            total = Decimal("0.00")
            details = []
            for row in get_employee_entitlement_rows(employee, as_of_date=timezone.localdate(), limit=100):
                remaining = Decimal(row["remaining_days"])
                if remaining <= 0 or row["must_use_by"] > cutoff_date:
                    continue
                total += remaining
                details.append(
                    f"{row['period_start']}..{row['period_end']} "
                    f"остаток={remaining} использовать_до={row['must_use_by']}"
                )
            if total > 0:
                rows.append((employee.id, employee.full_name, total, "; ".join(details[:3])))
        return sorted(rows, key=lambda item: item[2], reverse=True)

    def _leadership_pair_conflicts(self):
        intervals_by_employee = defaultdict(list)
        for item in VacationScheduleItem.objects.select_related("schedule").filter(
            status__in=VacationScheduleItem.ACTIVE_STATUSES,
        ):
            intervals_by_employee[item.employee_id].append(
                (
                    "график",
                    f"{item.schedule.year}/{item.status}/{item.vacation_type}/{item.source}",
                    item.id,
                    item.start_date,
                    item.end_date,
                )
            )

        active_requests = VacationRequest.objects.filter(status__in=VacationRequest.ACTIVE_STATUSES)
        active_requests = exclude_converted_paid_requests(active_requests)
        for request_obj in active_requests:
            intervals_by_employee[request_obj.employee_id].append(
                (
                    "заявка",
                    f"{request_obj.status}/{request_obj.vacation_type}",
                    request_obj.id,
                    request_obj.start_date,
                    request_obj.end_date,
                )
            )

        conflicts = []
        for department in Departments.objects.select_related("head", "deputy").order_by("id"):
            if not department.head_id or not department.deputy_id:
                continue
            for head_interval in intervals_by_employee.get(department.head_id, []):
                for deputy_interval in intervals_by_employee.get(department.deputy_id, []):
                    overlap_start = max(head_interval[3], deputy_interval[3])
                    overlap_end = min(head_interval[4], deputy_interval[4])
                    if overlap_start <= overlap_end:
                        conflicts.append(
                            (
                                department.name,
                                department.head.full_name,
                                department.deputy.full_name,
                                overlap_start,
                                overlap_end,
                                head_interval[:3],
                                deputy_interval[:3],
                            )
                        )
        return conflicts

    def _missing_leadership_assignments(self):
        rows = []
        for department in Departments.objects.select_related("head", "deputy").order_by("id"):
            missing = []
            if not department.head_id:
                missing.append("руководитель")
            if not department.deputy_id:
                missing.append("заместитель")
            if missing:
                rows.append((department.name, ", ".join(missing)))
        return rows
