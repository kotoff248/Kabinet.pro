from datetime import date
from io import StringIO

from django.core.management import call_command
from django.db.models.functions import ExtractYear
from django.test import TestCase
from django.utils import timezone

from apps.employees.models import Employees
from apps.leave.models import (
    VacationEntitlementAllocation,
    VacationEntitlementPeriod,
    VacationPreference,
    VacationRequest,
    VacationScheduleAuthorizedApproval,
    VacationScheduleDepartmentApproval,
    VacationScheduleEnterpriseApproval,
    VacationScheduleItem,
)
from apps.leave.services.dates import add_months_safe, get_chargeable_leave_days
from apps.leave.services.ledger import get_employee_leave_summary
from apps.leave.services.querysets import exclude_converted_paid_requests


class SeedVacationDataCommandTests(TestCase):
    def test_command_generates_non_overlapping_active_vacations_and_metrics(self):
        call_command("seed_vacation_requests", seed_value=17, fast=True, confirm_reset=True, stdout=StringIO())

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
        historical_requests = list(
            VacationRequest.objects.select_related("employee__department", "employee__department__staffing_rule")
            .filter(start_date__year__lt=current_year)
            .exclude(employee__department=None)
        )
        scaled_historical_risk_count = 0
        for request_obj in historical_requests:
            department = request_obj.employee.department
            final_staff_count = Employees.objects.filter(
                department=department,
                is_active_employee=True,
            ).exclude(role__in=Employees.SERVICE_ROLES).count()
            period_staff_count = Employees.objects.filter(
                department=department,
                is_active_employee=True,
                date_joined__lte=request_obj.end_date,
            ).exclude(role__in=Employees.SERVICE_ROLES).count()

            with self.subTest(request=request_obj.id, employee=request_obj.employee.login):
                self.assertGreater(request_obj.risk_score, 0)
                self.assertLessEqual(request_obj.min_staff_required, department.staffing_rule.min_staff_required)
                if period_staff_count:
                    self.assertLessEqual(request_obj.min_staff_required, period_staff_count)
                if period_staff_count < final_staff_count and request_obj.min_staff_required < department.staffing_rule.min_staff_required:
                    scaled_historical_risk_count += 1
        self.assertGreater(scaled_historical_risk_count, 0)
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
        call_command("seed_vacation_requests", seed_value=23, fast=True, confirm_reset=True, stdout=StringIO())

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
        call_command("seed_vacation_requests", seed_value=31, fast=True, confirm_reset=True, stdout=StringIO())

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
