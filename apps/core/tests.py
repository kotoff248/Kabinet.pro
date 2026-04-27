from datetime import date, timedelta
from io import StringIO

from django.core.management import call_command
from django.db.models.functions import ExtractYear
from django.test import TestCase
from django.utils import timezone

from apps.employees.models import Departments, Employees
from apps.leave.models import (
    DepartmentStaffingRule,
    DepartmentWorkload,
    VacationEntitlementAllocation,
    VacationEntitlementPeriod,
    VacationPreference,
    VacationRequest,
    VacationSchedule,
    VacationScheduleAuthorizedApproval,
    VacationScheduleDepartmentApproval,
    VacationScheduleEnterpriseApproval,
    VacationScheduleItem,
)
from apps.leave.services import (
    add_months_safe,
    add_years_safe,
    exclude_converted_paid_requests,
    get_chargeable_leave_days,
    get_employee_leave_summary,
)


class SeedVacationRequestsCommandTests(TestCase):
    def test_command_rebuilds_enterprise_structure_and_credentials(self):
        stale_department = Departments.objects.create(name="Старый отдел")
        Employees.objects.create(
            login="stale_user",
            last_name="Старый",
            first_name="Сотрудник",
            middle_name="Тестович",
            position="Тестировщик",
            department=stale_department,
            role=Employees.ROLE_EMPLOYEE,
        )

        stdout = StringIO()
        call_command("seed_vacation_requests", seed_value=7, stdout=stdout)

        self.assertEqual(Departments.objects.count(), 5)
        self.assertEqual(Employees.objects.filter(role=Employees.ROLE_EMPLOYEE).count(), 100)
        self.assertEqual(Employees.objects.filter(role=Employees.ROLE_HR).count(), 2)
        self.assertEqual(Employees.objects.filter(role=Employees.ROLE_DEPARTMENT_HEAD).count(), 5)
        self.assertEqual(Employees.objects.filter(role=Employees.ROLE_ENTERPRISE_HEAD).count(), 1)
        self.assertEqual(Employees.objects.filter(role=Employees.ROLE_AUTHORIZED_PERSON).count(), 1)
        self.assertFalse(Employees.objects.filter(login="stale_user").exists())

        self.assertTrue(Employees.objects.filter(login="director_1", role=Employees.ROLE_ENTERPRISE_HEAD).exists())
        self.assertTrue(Employees.objects.filter(login="admin_1", role=Employees.ROLE_AUTHORIZED_PERSON).exists())
        self.assertTrue(Employees.objects.filter(login="hr_1", role=Employees.ROLE_HR).exists())
        self.assertTrue(Employees.objects.filter(login="manager_5", role=Employees.ROLE_DEPARTMENT_HEAD).exists())
        self.assertTrue(Employees.objects.filter(login="employ_100", role=Employees.ROLE_EMPLOYEE).exists())

        expected_department_counts = {
            "Производство": 30,
            "Техническое обслуживание": 24,
            "Промышленная безопасность": 12,
            "Логистика": 18,
            "Финансы и закупки": 16,
        }
        expected_rules = {
            "Производство": (23, 8, 5, "production-core"),
            "Техническое обслуживание": (18, 7, 5, "maintenance-critical"),
            "Промышленная безопасность": (10, 3, 5, "safety-control"),
            "Логистика": (14, 5, 4, "logistics-shifts"),
            "Финансы и закупки": (12, 5, 4, "finance-procurement"),
        }

        authorized_person = Employees.objects.get(login="admin_1")
        self.assertEqual(authorized_person.full_name, "")
        self.assertEqual(authorized_person.position, "")
        self.assertIsNone(authorized_person.department)
        self.assertEqual(authorized_person.user.first_name, "")
        self.assertTrue(authorized_person.user.check_password("1234"))
        self.assertTrue(Employees.objects.get(login="hr_1").user.check_password("1234"))
        self.assertTrue(Employees.objects.get(login="manager_1").user.check_password("1234"))
        self.assertTrue(Employees.objects.get(login="employ_1").user.check_password("1234"))
        current_year = timezone.localdate().year
        expected_schedule_years = list(range(current_year - 5, current_year + 1))
        self.assertEqual(
            list(VacationSchedule.objects.order_by("year").values_list("year", flat=True)),
            expected_schedule_years,
        )
        self.assertFalse(VacationSchedule.objects.filter(year__lt=current_year - 5).exists())
        self.assertEqual(VacationSchedule.objects.get(year=current_year).status, VacationSchedule.STATUS_APPROVED)
        self.assertFalse(VacationSchedule.objects.filter(year__lt=current_year).exclude(status=VacationSchedule.STATUS_ARCHIVED).exists())
        self.assertEqual(DepartmentStaffingRule.objects.count(), 5)
        self.assertEqual(DepartmentWorkload.objects.count(), 5 * len(expected_schedule_years) * 12)

        employee_last_names = list(Employees.objects.filter(role=Employees.ROLE_EMPLOYEE).values_list("last_name", flat=True))
        self.assertGreaterEqual(len(set(employee_last_names)), 90)
        most_common_last_name_count = max(employee_last_names.count(last_name) for last_name in set(employee_last_names))
        self.assertLessEqual(most_common_last_name_count, 2)

        for department in Departments.objects.all():
            with self.subTest(department=department.name):
                self.assertIsNotNone(department.head)
                self.assertEqual(
                    department.employees.filter(role=Employees.ROLE_EMPLOYEE).count(),
                    expected_department_counts[department.name],
                )
                rule = department.staffing_rule
                self.assertEqual(
                    (
                        rule.min_staff_required,
                        rule.max_absent,
                        rule.criticality_level,
                        rule.substitution_group,
                    ),
                    expected_rules[department.name],
                )

    def test_command_generates_non_overlapping_active_vacations_and_metrics(self):
        call_command("seed_vacation_requests", seed_value=17, fast=True, stdout=StringIO())

        self.assertGreater(VacationScheduleItem.objects.filter(status=VacationScheduleItem.STATUS_APPROVED).count(), 0)
        paid_requests = VacationRequest.objects.filter(vacation_type="paid")
        self.assertGreater(paid_requests.count(), 0)
        current_year = timezone.localdate().year
        schedule_cutoff = date(current_year - 1, 12, 31)
        new_hires = Employees.objects.filter(role=Employees.ROLE_EMPLOYEE, date_joined__gt=schedule_cutoff)
        self.assertGreater(new_hires.count(), 0)
        self.assertFalse(
            VacationScheduleItem.objects.filter(
                employee__in=new_hires,
                schedule__year=current_year,
                source=VacationScheduleItem.SOURCE_GENERATED,
            ).exists()
        )
        established_employees = Employees.objects.filter(role=Employees.ROLE_EMPLOYEE, date_joined__lte=schedule_cutoff)
        current_schedule_totals = [
            sum(
                item.chargeable_days
                for item in employee.vacation_schedule_items.filter(
                    schedule__year=current_year,
                    vacation_type="paid",
                    status__in=VacationScheduleItem.BALANCE_STATUSES,
                )
            )
            for employee in established_employees
        ]
        self.assertTrue(current_schedule_totals)
        self.assertTrue(all(total >= 45 for total in current_schedule_totals))
        for request_obj in paid_requests:
            with self.subTest(request=request_obj.id):
                self.assertGreaterEqual(request_obj.start_date, add_months_safe(request_obj.employee.date_joined, 6))
                overlapping_items = request_obj.employee.vacation_schedule_items.filter(
                    status__in=VacationScheduleItem.ACTIVE_STATUSES,
                    start_date__lte=request_obj.end_date,
                    end_date__gte=request_obj.start_date,
                )
                if request_obj.status == VacationRequest.STATUS_APPROVED:
                    linked_items = request_obj.created_schedule_items.filter(
                        status__in=VacationScheduleItem.ACTIVE_STATUSES,
                    )
                    self.assertEqual(linked_items.count(), 1)
                    self.assertEqual(linked_items.get().source, VacationScheduleItem.SOURCE_MANUAL)
                    self.assertFalse(overlapping_items.exclude(id__in=linked_items.values_list("id", flat=True)).exists())
                else:
                    self.assertFalse(overlapping_items.exists())
        approved_paid_requests = VacationRequest.objects.filter(
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )
        self.assertFalse(approved_paid_requests.filter(created_schedule_items__isnull=True).exists())
        self.assertFalse(
            VacationRequest.objects.filter(
                vacation_type__in=["unpaid", "study"],
                created_schedule_items__isnull=False,
            ).exists()
        )
        self.assertGreater(
            VacationRequest.objects.filter(status=VacationRequest.STATUS_REJECTED).count(),
            0,
        )
        special_requests = VacationRequest.objects.filter(vacation_type__in=["unpaid", "study"])
        historical_special_requests = special_requests.filter(start_date__year__lt=current_year)
        rejected_years = set(
            special_requests.filter(status=VacationRequest.STATUS_REJECTED)
            .annotate(year=ExtractYear("start_date"))
            .values_list("year", flat=True)
        )
        historical_years_with_special_requests = set(
            historical_special_requests.annotate(year=ExtractYear("start_date")).values_list("year", flat=True)
        )

        self.assertGreaterEqual(VacationRequest.objects.filter(vacation_type="unpaid").count(), 3)
        self.assertGreaterEqual(len(rejected_years - {current_year}), 1)
        self.assertGreaterEqual(len(historical_years_with_special_requests), 1)
        self.assertTrue(historical_special_requests.filter(vacation_type="unpaid").exists())
        self.assertFalse(VacationRequest.objects.filter(reason="").exists())
        self.assertFalse(VacationRequest.objects.filter(risk_score=0).exists())
        self.assertFalse(
            VacationRequest.objects.filter(
                status__in=[VacationRequest.STATUS_APPROVED, VacationRequest.STATUS_REJECTED],
                reviewed_by__isnull=True,
            ).exists()
        )
        self.assertFalse(
            VacationRequest.objects.filter(
                status__in=[VacationRequest.STATUS_APPROVED, VacationRequest.STATUS_REJECTED],
                reviewed_at__isnull=True,
            ).exists()
        )

        for employee in Employees.objects.all():
            active_requests = employee.vacation_requests.filter(status__in=VacationRequest.ACTIVE_STATUSES)
            active_requests = exclude_converted_paid_requests(active_requests, employee_ids=[employee.id])
            active_periods = list(
                active_requests.values_list("start_date", "end_date")
            )
            active_periods.extend(
                employee.vacation_schedule_items.filter(
                    status__in=VacationScheduleItem.ACTIVE_STATUSES,
                ).values_list("start_date", "end_date")
            )
            active_periods = sorted(active_periods)
            for previous, current in zip(active_periods, active_periods[1:]):
                with self.subTest(employee=employee.login, previous=previous, current=current):
                    self.assertLess(previous[1], current[0])

            self.assertGreaterEqual(get_employee_leave_summary(employee)["available"], 0)

    def test_command_generates_realistic_available_balances(self):
        call_command("seed_vacation_requests", seed_value=23, fast=True, stdout=StringIO())

        employees = list(Employees.objects.filter(role=Employees.ROLE_EMPLOYEE))
        available_days = [float(get_employee_leave_summary(employee)["available"]) for employee in employees]

        self.assertTrue(all(balance <= 104 for balance in available_days))
        self.assertTrue(all(balance >= 0 for balance in available_days))
        self.assertGreaterEqual(sum(balance <= 30 for balance in available_days), 1)
        self.assertGreaterEqual(sum(balance >= 31 for balance in available_days), 1)

        self.assertGreater(VacationEntitlementPeriod.objects.count(), 0)
        self.assertGreater(VacationEntitlementAllocation.objects.count(), 0)
        active_schedule_days = sum(
            item.chargeable_days
            for item in VacationScheduleItem.objects.filter(
                vacation_type="paid",
                status__in=VacationScheduleItem.BALANCE_STATUSES,
            )
        )
        active_paid_requests = VacationRequest.objects.filter(
            vacation_type="paid",
            status__in=VacationRequest.ACTIVE_STATUSES,
        )
        active_paid_requests = exclude_converted_paid_requests(active_paid_requests)
        active_paid_request_days = sum(
            get_chargeable_leave_days(request_obj.start_date, request_obj.end_date, request_obj.vacation_type)
            for request_obj in active_paid_requests
        )
        allocated_days = sum(allocation.allocated_days for allocation in VacationEntitlementAllocation.objects.all())
        self.assertEqual(allocated_days, active_schedule_days + active_paid_request_days)

    def test_command_generates_realistic_leave_patterns_and_types(self):
        call_command("seed_vacation_requests", seed_value=31, fast=True, stdout=StringIO())

        self.assertTrue(VacationRequest.objects.filter(vacation_type="unpaid").exists())
        self.assertTrue(VacationRequest.objects.filter(vacation_type__in=["unpaid", "study"]).exists())
        self.assertTrue(VacationPreference.objects.filter(status=VacationPreference.STATUS_FILLED).exists())
        self.assertTrue(VacationScheduleDepartmentApproval.objects.filter(status=VacationScheduleDepartmentApproval.STATUS_APPROVED).exists())
        self.assertTrue(VacationScheduleEnterpriseApproval.objects.filter(status=VacationScheduleEnterpriseApproval.STATUS_APPROVED).exists())
        self.assertTrue(VacationScheduleAuthorizedApproval.objects.filter(status=VacationScheduleAuthorizedApproval.STATUS_APPROVED).exists())

        today = timezone.localdate()
        current_year = today.year
        employees = Employees.objects.filter(role=Employees.ROLE_EMPLOYEE)

        for employee in employees:
            approved_paid_items = list(
                employee.vacation_schedule_items.filter(
                    vacation_type="paid",
                    status__in=VacationScheduleItem.BALANCE_STATUSES,
                ).order_by("start_date", "end_date")
            )

            if (today - employee.date_joined).days >= 365:
                completed_periods = employee.vacation_entitlement_periods.filter(period_end__lt=today)
                for period in completed_periods:
                    allocations = list(period.allocations.select_related("schedule_item", "vacation_request"))
                    if not allocations:
                        continue
                    with self.subTest(employee=employee.login, year_start=period.period_start):
                        self.assertTrue(
                            any(
                                (
                                    (
                                        allocation.schedule_item.end_date - allocation.schedule_item.start_date
                                    ).days
                                    + 1
                                )
                                >= 14
                                if allocation.schedule_item_id
                                else (
                                    (
                                        allocation.vacation_request.end_date - allocation.vacation_request.start_date
                                    ).days
                                    + 1
                                )
                                >= 14
                                for allocation in allocations
                            )
                        )

            for previous, current in zip(approved_paid_items, approved_paid_items[1:]):
                with self.subTest(employee=employee.login, previous=previous.id, current=current.id):
                    self.assertLess(previous.end_date, current.start_date)
