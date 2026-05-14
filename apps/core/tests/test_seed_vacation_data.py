from datetime import date
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.db.models import F
from django.db.models.functions import ExtractYear
from django.test import TestCase
from django.utils import timezone

from apps.core.models import DemoBaselineSnapshot, DemoDataResetJob
from apps.core.services.demo_baseline import INITIAL_DEMO_STATE_KEY
from apps.employees.models import Employees
from apps.leave.models import (
    VacationEntitlementAllocation,
    VacationEntitlementPeriod,
    VacationPreference,
    VacationRequest,
    VacationSchedule,
    VacationScheduleChangeRequest,
    VacationScheduleAuthorizedApproval,
    VacationScheduleCandidate,
    VacationScheduleCandidateFeedback,
    VacationScheduleCandidatePackage,
    VacationScheduleCandidatePackagePeriod,
    VacationScheduleDepartmentApproval,
    VacationScheduleEnterpriseApproval,
    VacationScheduleGenerationRun,
    VacationScheduleItem,
    VacationUrgentClosureRequest,
)
from apps.leave.services.dates import add_months_safe, get_chargeable_leave_days
from apps.leave.services.calendar import build_calendar_base_data, build_calendar_rows
from apps.leave.services.ledger import get_employee_leave_summary
from apps.leave.services.querysets import exclude_converted_paid_requests


class SeedVacationDataCommandTests(TestCase):
    def test_command_generates_non_overlapping_active_vacations_and_metrics(self):
        progress_job = DemoDataResetJob.objects.create(token="seed-progress-token", seed_value=17)
        call_command(
            "seed_vacation_requests",
            seed_value=17,
            fast=True,
            confirm_reset=True,
            progress_job_id=progress_job.id,
            stdout=StringIO(),
        )
        progress_job.refresh_from_db()
        self.assertEqual(progress_job.status, DemoDataResetJob.STATUS_SUCCEEDED)
        self.assertEqual(progress_job.progress_percent, 100)
        self.assertEqual(progress_job.stage_label, "Готово")

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
        current_pending_manager_transfers = VacationScheduleChangeRequest.objects.filter(
            status=VacationScheduleChangeRequest.STATUS_PENDING,
            requested_by__isnull=False,
        ).exclude(requested_by_id=F("employee_id"))
        self.assertGreaterEqual(current_pending_manager_transfers.count(), 3)
        self.assertTrue(
            current_pending_manager_transfers.filter(
                requested_by__role=Employees.ROLE_DEPARTMENT_HEAD,
                employee__role=Employees.ROLE_EMPLOYEE,
            ).exists()
        )
        self.assertTrue(
            current_pending_manager_transfers.filter(
                requested_by__role=Employees.ROLE_ENTERPRISE_HEAD,
                employee__role=Employees.ROLE_HR,
            ).exists()
        )
        self.assertTrue(
            current_pending_manager_transfers.filter(
                requested_by__role=Employees.ROLE_ENTERPRISE_HEAD,
                employee__role=Employees.ROLE_DEPARTMENT_HEAD,
            ).exists()
        )
        all_manager_initiated_transfers = VacationScheduleChangeRequest.objects.filter(
            requested_by__isnull=False,
        ).exclude(requested_by_id=F("employee_id"))
        historical_manager_transfers = all_manager_initiated_transfers.filter(
            schedule_item__schedule__year__lt=current_year,
        )
        self.assertGreaterEqual(historical_manager_transfers.count(), 2)
        self.assertTrue(historical_manager_transfers.filter(status=VacationScheduleChangeRequest.STATUS_APPROVED).exists())
        self.assertTrue(historical_manager_transfers.filter(status=VacationScheduleChangeRequest.STATUS_REJECTED).exists())
        self.assertFalse(all_manager_initiated_transfers.filter(requested_by__role=Employees.ROLE_HR).exists())
        active_urgent_closures = VacationUrgentClosureRequest.objects.filter(
            planning_year=current_year + 1,
            status__in=VacationUrgentClosureRequest.ACTIVE_STATUSES,
        ).select_related("employee", "created_by")
        self.assertGreaterEqual(active_urgent_closures.count(), 2)
        snapshot = DemoBaselineSnapshot.objects.get(key=INITIAL_DEMO_STATE_KEY)
        self.assertEqual(snapshot.planning_year, current_year + 1)
        self.assertIn("staffing", snapshot.payload)
        self.assertGreater(len(snapshot.payload["staffing"]["departments"]), 0)
        self.assertGreaterEqual(len(snapshot.payload["urgent_closures"]), 2)
        self.assertTrue(
            active_urgent_closures.filter(status=VacationUrgentClosureRequest.STATUS_EMPLOYEE_REVIEW).exists()
        )
        for closure_request in active_urgent_closures:
            with self.subTest(urgent_closure=closure_request.id):
                self.assertLess(closure_request.proposed_end_date, date(current_year + 1, 1, 1))
                self.assertLessEqual(closure_request.proposed_end_date, closure_request.deadline)
                self.assertIn(int(closure_request.required_days), {3, 4})
                self.assertEqual(closure_request.created_by.role, Employees.ROLE_HR)
        for transfer in all_manager_initiated_transfers.select_related("employee", "requested_by"):
            with self.subTest(manager_transfer=transfer.id, requested_by=transfer.requested_by.login):
                if transfer.requested_by.role == Employees.ROLE_DEPARTMENT_HEAD:
                    managed_department = getattr(transfer.requested_by, "managed_department", None) or transfer.requested_by.department
                    self.assertEqual(transfer.employee.role, Employees.ROLE_EMPLOYEE)
                    self.assertIsNotNone(managed_department)
                    self.assertEqual(transfer.employee.department_id, managed_department.id)
                elif transfer.requested_by.role == Employees.ROLE_ENTERPRISE_HEAD:
                    self.assertIn(transfer.employee.role, [Employees.ROLE_HR, Employees.ROLE_DEPARTMENT_HEAD])
                else:
                    self.fail("Manager-initiated transfer has an unsupported initiator role.")
        for transfer in historical_manager_transfers.exclude(
            status=VacationScheduleChangeRequest.STATUS_PENDING,
        ).select_related("employee", "reviewed_by"):
            with self.subTest(historical_manager_transfer=transfer.id):
                self.assertEqual(transfer.reviewed_by_id, transfer.employee_id)
                self.assertLess(transfer.created_at, transfer.reviewed_at)
                self.assertLess(transfer.reviewed_at.date(), transfer.old_start_date)
        approved_manager_transfer = historical_manager_transfers.filter(
            status=VacationScheduleChangeRequest.STATUS_APPROVED,
        ).select_related("schedule_item").first()
        self.assertIsNotNone(approved_manager_transfer)
        self.assertEqual(approved_manager_transfer.schedule_item.status, VacationScheduleItem.STATUS_TRANSFERRED)
        approved_replacements = list(approved_manager_transfer.created_schedule_items.all())
        self.assertEqual(len(approved_replacements), 1)
        self.assertEqual(approved_replacements[0].source, VacationScheduleItem.SOURCE_TRANSFER)
        self.assertEqual(approved_replacements[0].previous_item_id, approved_manager_transfer.schedule_item_id)
        rejected_manager_transfer = historical_manager_transfers.filter(
            status=VacationScheduleChangeRequest.STATUS_REJECTED,
        ).select_related("schedule_item").first()
        self.assertIsNotNone(rejected_manager_transfer)
        self.assertEqual(rejected_manager_transfer.schedule_item.status, VacationScheduleItem.STATUS_APPROVED)
        self.assertFalse(rejected_manager_transfer.schedule_item.was_changed_by_manager)
        self.assertFalse(rejected_manager_transfer.created_schedule_items.exists())
        self.assertTrue(
            VacationScheduleChangeRequest.objects.filter(
                schedule_item__schedule__year__lt=current_year,
            )
            .exclude(balance_after_change=0)
            .exists()
        )
        self.assertFalse(
            VacationScheduleItem.objects.filter(
                status__in=VacationScheduleItem.ACTIVE_STATUSES,
                was_changed_by_manager=True,
                change_requests__status=VacationScheduleChangeRequest.STATUS_REJECTED,
            ).exists()
        )
        self.assertFalse(
            VacationScheduleItem.objects.filter(
                status__in=VacationScheduleItem.ACTIVE_STATUSES,
                source=VacationScheduleItem.SOURCE_TRANSFER,
                created_from_change_request__status=VacationScheduleChangeRequest.STATUS_REJECTED,
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
        self.assertTrue(all(total >= 28 for total in current_schedule_totals))
        self.assertLessEqual(
            sum(1 for total in current_schedule_totals if total < 45),
            max(2, len(current_schedule_totals) // 20),
        )
        self.assertLessEqual(
            sum(1 for total in current_schedule_totals if total > 70),
            max(1, len(current_schedule_totals) // 12),
        )
        historical_years = VacationSchedule.objects.filter(year__lt=current_year).values_list("year", flat=True)
        historical_employees = Employees.objects.exclude(role__in=Employees.SERVICE_ROLES)
        for year in historical_years:
            year_start = date(year, 1, 1)
            year_end = date(year, 12, 31)
            for employee in historical_employees.filter(date_joined__lte=year_end):
                paid_days = sum(
                    item.chargeable_days
                    for item in employee.vacation_schedule_items.filter(
                        schedule__year=year,
                        vacation_type="paid",
                        status__in=VacationScheduleItem.BALANCE_STATUSES,
                    )
                )
                active_paid_items = list(
                    employee.vacation_schedule_items.filter(
                        schedule__year=year,
                        vacation_type="paid",
                        status__in=VacationScheduleItem.BALANCE_STATUSES,
                    )
                )
                short_paid_items = [
                    item
                    for item in active_paid_items
                    if (item.end_date - item.start_date).days + 1 < 14
                ]
                has_paid_anchor = any(
                    (item.end_date - item.start_date).days + 1 >= 14
                    for item in active_paid_items
                )
                eligibility_start = max(year_start, add_months_safe(employee.date_joined, 6))
                with self.subTest(employee=employee.login, historical_year=year):
                    if eligibility_start > year_end:
                        self.assertEqual(paid_days, 0)
                    elif eligibility_start <= year_start:
                        self.assertGreaterEqual(paid_days, 28)
                    elif eligibility_start <= date(year, 9, 30):
                        self.assertGreaterEqual(paid_days, 14)
                    else:
                        self.assertFalse(0 < paid_days < 14)
                    if short_paid_items:
                        self.assertTrue(has_paid_anchor)
                    self.assertLessEqual(paid_days, Decimal("70.00"))
            employees, employee_day_status, employee_entries = build_calendar_base_data(year)
            rows, _ = build_calendar_rows(
                employees,
                employee_day_status,
                employee_entries,
                year=year,
                month=1,
                view_mode="year",
                today=timezone.localdate(),
            )
            conflict_cells = sum(
                1
                for row in rows
                for cell in row.get("cells", [])
                if cell.get("has_conflict")
            )
            self.assertLessEqual(conflict_cells, max(10, len(rows) // 4))
        historical_items = VacationScheduleItem.objects.filter(
            schedule__year__lt=current_year,
            vacation_type="paid",
            status__in=VacationScheduleItem.BALANCE_STATUSES,
        )
        historical_high_risk_count = historical_items.filter(risk_level=VacationScheduleItem.RISK_HIGH).count()
        self.assertLessEqual(historical_high_risk_count, max(1, (historical_items.count() + 11) // 12))
        historical_ml_runs = VacationScheduleGenerationRun.objects.filter(
            year__lte=current_year,
            schedule__status__in=[VacationSchedule.STATUS_ARCHIVED, VacationSchedule.STATUS_APPROVED],
            status=VacationScheduleGenerationRun.STATUS_COMPLETED,
        )
        self.assertTrue(historical_ml_runs.filter(year__lt=current_year).exists())
        historical_trace_items = VacationScheduleItem.objects.filter(
            schedule__year__lte=current_year,
            schedule__status__in=[VacationSchedule.STATUS_ARCHIVED, VacationSchedule.STATUS_APPROVED],
            vacation_type="paid",
            status__in=[VacationScheduleItem.STATUS_APPROVED, VacationScheduleItem.STATUS_TRANSFERRED],
        ).exclude(employee__role__in=Employees.SERVICE_ROLES)
        self.assertTrue(historical_trace_items.exists())
        self.assertFalse(historical_trace_items.filter(selected_candidate__isnull=True).exists())
        self.assertFalse(historical_trace_items.filter(generation_run__isnull=True).exists())
        self.assertFalse(historical_trace_items.filter(ai_score__isnull=True).exists())
        candidate_decisions = set(
            VacationScheduleCandidate.objects.filter(schedule__year__lte=current_year).values_list("decision", flat=True)
        )
        self.assertTrue(
            {
                VacationScheduleCandidate.DECISION_SELECTED,
                VacationScheduleCandidate.DECISION_REJECTED,
                VacationScheduleCandidate.DECISION_BLOCKED,
            }.issubset(candidate_decisions)
        )
        selected_candidate = VacationScheduleCandidate.objects.filter(
            schedule__year__lte=current_year,
            decision=VacationScheduleCandidate.DECISION_SELECTED,
        ).first()
        self.assertIsNotNone(selected_candidate)
        self.assertEqual(selected_candidate.features["feature_schema_version"], 1)
        self.assertTrue(selected_candidate.features["candidate_passed_hard_rules"])
        self.assertIn("period_chargeable_days", selected_candidate.features)
        selected_staffing_candidates = list(
            VacationScheduleCandidate.objects.select_related("employee__department")
            .filter(
                schedule__year__lte=current_year,
                decision=VacationScheduleCandidate.DECISION_SELECTED,
                employee__department__isnull=False,
            )
            .order_by("id")[:80]
        )
        self.assertTrue(selected_staffing_candidates)
        self.assertTrue(
            any(
                candidate.features.get("risk_min_staff_required", 0) > 0
                and candidate.features.get("risk_remaining_staff_count", 0) > 0
                and "risk_staff_margin" in candidate.features
                for candidate in selected_staffing_candidates
            )
        )
        self.assertTrue(
            any(candidate.features.get("risk_department_load_level", 0) > 0 for candidate in selected_staffing_candidates)
        )
        self.assertIn("historical_assessment_can_place", selected_candidate.features)
        feedback_decisions = set(VacationScheduleCandidateFeedback.objects.values_list("decision", flat=True))
        self.assertTrue(
            {
                VacationScheduleCandidateFeedback.DECISION_AGREE,
                VacationScheduleCandidateFeedback.DECISION_NEEDS_CHANGE,
                VacationScheduleCandidateFeedback.DECISION_REJECT,
            }.issubset(feedback_decisions)
        )
        self.assertTrue(
            VacationScheduleCandidatePackage.objects.filter(
                schedule__year__lte=current_year,
                decision=VacationScheduleCandidatePackage.DECISION_SELECTED,
                periods_count__gte=2,
            ).exists()
        )
        self.assertTrue(
            VacationScheduleCandidatePackagePeriod.objects.filter(
                candidate_package__schedule__year__lte=current_year,
                candidate_package__decision=VacationScheduleCandidatePackage.DECISION_SELECTED,
                schedule_item__isnull=False,
            ).exists()
        )
        self.assertTrue(
            VacationScheduleCandidatePackage.objects.filter(
                schedule__year__lte=current_year,
                decision=VacationScheduleCandidatePackage.DECISION_REJECTED,
            ).exists()
        )
        self.assertTrue(
            VacationScheduleCandidatePackage.objects.filter(
                schedule__year__lte=current_year,
                decision=VacationScheduleCandidatePackage.DECISION_BLOCKED,
            ).exists()
        )
        planning_deadline = date(current_year + 1, 12, 31)
        generated_history_start_year = VacationSchedule.objects.order_by("year").values_list("year", flat=True).first()
        due_by_employee = {}
        for period in VacationEntitlementPeriod.objects.filter(
            employee__role=Employees.ROLE_EMPLOYEE,
            period_start__year__gte=generated_history_start_year,
            must_use_by__lte=planning_deadline,
        ).prefetch_related("allocations"):
            allocated_days = sum(Decimal(allocation.allocated_days) for allocation in period.allocations.all())
            remaining_days = max(Decimal(period.entitled_days) - allocated_days, Decimal("0.00"))
            due_by_employee[period.employee_id] = due_by_employee.get(period.employee_id, Decimal("0.00")) + remaining_days
        large_carryover_count = sum(1 for remaining_days in due_by_employee.values() if remaining_days > Decimal("18.00"))
        self.assertLessEqual(large_carryover_count, max(2, established_employees.count() // 10))
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
        filled_policies = set(
            VacationPreference.objects.filter(
                status=VacationPreference.STATUS_FILLED,
                priority=VacationPreference.PRIORITY_PRIMARY,
            ).values_list("remainder_policy", flat=True)
        )
        self.assertIn(VacationPreference.REMAINDER_DEFER, filled_policies)
        self.assertIn(VacationPreference.REMAINDER_APPROVAL, filled_policies)
        self.assertIn(VacationPreference.REMAINDER_AUTO, filled_policies)
        backup_preferences = list(
            VacationPreference.objects.filter(
                status=VacationPreference.STATUS_FILLED,
                priority=VacationPreference.PRIORITY_BACKUP,
                start_date__isnull=False,
                end_date__isnull=False,
            )
        )
        self.assertTrue(backup_preferences)
        for preference in backup_preferences:
            with self.subTest(employee=preference.employee_id, year=preference.year):
                self.assertGreaterEqual((preference.end_date - preference.start_date).days + 1, 14)
        self.assertFalse(VacationScheduleItem.objects.filter(manager_comment__contains="Рћ").exists())
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
