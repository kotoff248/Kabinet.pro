from datetime import date, timedelta
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts.services import sync_employee_user
from apps.employees.models import Employees
from apps.leave.models import (
    DepartmentWorkload,
    VacationEntitlementAllocation,
    VacationRequest,
    VacationRequestHistory,
    VacationSchedule,
    VacationScheduleItem,
)
from apps.leave.services.dates import get_chargeable_leave_days
from apps.leave.services.ledger import get_employee_leave_summary, rebuild_employee_leave_ledger
from apps.leave.services.metrics import set_vacation_metric_sync_enabled
from apps.leave.services.approval_routes import get_expected_vacation_approver
from apps.leave.services.requests import (
    approve_vacation_request,
    create_vacation_request,
    delete_pending_vacation_request,
    reject_vacation_request,
)
from apps.leave.services.risk import calculate_vacation_request_risk
from apps.leave.services.validation import get_paid_request_eligibility_for_year

from .base import LeaveTestCase


class VacationRequestTests(LeaveTestCase):
    def test_department_load_is_weighted_by_days_across_months(self):
        Employees.objects.bulk_create(
            [
                Employees(
                    last_name=f"Нагрузка{index}",
                    first_name="Тест",
                    middle_name="Сотрудник",
                    login=f"risk-weight-staff-{index}",
                    position="Инженер",
                    department=self.engineering,
                    date_joined=date(2025, 1, 1),
                    annual_paid_leave_days=52,
                    role=Employees.ROLE_EMPLOYEE,
                )
                for index in range(25)
            ]
        )
        DepartmentWorkload.objects.create(
            department=self.engineering,
            year=2026,
            month=4,
            load_level=2,
            min_staff_required=10,
            max_absent=3,
        )
        DepartmentWorkload.objects.create(
            department=self.engineering,
            year=2026,
            month=5,
            load_level=4,
            min_staff_required=20,
            max_absent=9,
        )

        mostly_may = calculate_vacation_request_risk(
            self.employee,
            date(2026, 4, 29),
            date(2026, 5, 9),
            "unpaid",
        )
        mostly_april = calculate_vacation_request_risk(
            self.employee,
            date(2026, 4, 20),
            date(2026, 5, 2),
            "unpaid",
        )

        self.assertEqual(mostly_may["department_load_level"], 4)
        self.assertEqual(mostly_may["min_staff_required"], 18)
        self.assertEqual(mostly_april["department_load_level"], 2)
        self.assertEqual(mostly_april["min_staff_required"], 12)

    def test_rejected_request_does_not_block_new_request(self):
        VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-08-11",
            end_date="2026-08-15",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_REJECTED,
        )

        self.client.force_login(self.employee.user)
        response = self.client.post(
            reverse("calendar"),
            {
                "type_vacation": "unpaid",
                "start_date": "2026-08-11",
                "end_date": "2026-08-15",
                "next_view_mode": "month",
                "next_year": "2026",
                "next_month": "8",
            },
        )

        self.assertRedirects(response, f'{reverse("calendar")}?view=month&year=2026&month=8')
        self.assertTrue(
            VacationRequest.objects.filter(
                employee=self.employee,
                start_date="2026-08-11",
                end_date="2026-08-15",
                status=VacationRequest.STATUS_PENDING,
            ).exists()
        )

    def test_paid_request_uses_free_balance_even_when_employee_has_schedule(self):
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        allowed, _ = get_paid_request_eligibility_for_year(self.employee, 2026, as_of_date=date(2026, 9, 1))

        self.client.force_login(self.employee.user)
        response = self.client.post(
            reverse("calendar"),
            {
                "type_vacation": "paid",
                "start_date": "2026-09-01",
                "end_date": "2026-09-07",
                "next_view_mode": "month",
                "next_year": "2026",
                "next_month": "9",
            },
        )

        self.assertTrue(allowed)
        self.assertRedirects(response, f'{reverse("calendar")}?view=month&year=2026&month=9')
        self.assertTrue(
            VacationRequest.objects.filter(
                employee=self.employee,
                vacation_type="paid",
                start_date="2026-09-01",
                status=VacationRequest.STATUS_PENDING,
            ).exists()
        )

    def test_paid_request_is_blocked_when_balance_is_fully_reserved(self):
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        limited_employee = Employees.objects.create(
            last_name="Лимитов",
            first_name="Петр",
            middle_name="Сергеевич",
            login="reserved-balance-employee",
            position="Специалист",
            department=self.engineering,
            date_joined=date(2026, 1, 1),
            annual_paid_leave_days=5,
            role=Employees.ROLE_EMPLOYEE,
        )
        sync_employee_user(limited_employee, raw_password="reserved-pass")
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=limited_employee,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 5),
            vacation_type="paid",
            chargeable_days=5,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        allowed, _ = get_paid_request_eligibility_for_year(limited_employee, 2026, as_of_date=date(2026, 9, 1))

        self.assertFalse(allowed)
        with self.assertRaises(ValidationError):
            create_vacation_request(
                employee=limited_employee,
                start_date=date(2026, 9, 1),
                end_date=date(2026, 9, 2),
                vacation_type="paid",
            )

    def test_new_hire_without_schedule_can_create_paid_exception_after_six_months(self):
        VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        newcomer = Employees.objects.create(
            last_name="Новиков",
            first_name="Артем",
            middle_name="Сергеевич",
            login="new-hire-paid-exception",
            position="Инженер",
            department=self.engineering,
            date_joined=date(2026, 1, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        allowed, _ = get_paid_request_eligibility_for_year(newcomer, 2026, as_of_date=date(2026, 9, 1))

        with self.assertRaises(ValidationError):
            create_vacation_request(
                employee=newcomer,
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 7),
                vacation_type="paid",
            )
        request_obj = create_vacation_request(
            employee=newcomer,
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 7),
            vacation_type="paid",
            reason="Отпуск после шести месяцев работы.",
        )

        self.assertTrue(allowed)
        self.assertEqual(request_obj.status, VacationRequest.STATUS_PENDING)
        self.assertEqual(request_obj.vacation_type, "paid")
        self.assertEqual(
            list(request_obj.history_entries.order_by("created_at", "id").values_list("action", flat=True)),
            [
                VacationRequestHistory.ACTION_CREATED,
                VacationRequestHistory.ACTION_SUBMITTED,
            ],
        )

    def test_approved_paid_request_creates_manual_schedule_item_without_double_balance(self):
        VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        pending_request = VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 11, 10),
            end_date=date(2026, 11, 16),
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
        )

        approved_request = approve_vacation_request(pending_request.id, reviewer=self.department_head)
        approved_request.refresh_from_db()
        schedule_item = approved_request.created_schedule_items.get()
        chargeable_days = get_chargeable_leave_days(
            approved_request.start_date,
            approved_request.end_date,
            approved_request.vacation_type,
        )
        summary = get_employee_leave_summary(self.employee, as_of_date=date(2026, 12, 31))
        rebuild_employee_leave_ledger(self.employee)

        self.assertEqual(approved_request.status, VacationRequest.STATUS_APPROVED)
        self.assertEqual(schedule_item.source, VacationScheduleItem.SOURCE_MANUAL)
        self.assertEqual(schedule_item.status, VacationScheduleItem.STATUS_APPROVED)
        self.assertFalse(schedule_item.generated_by_ai)
        self.assertEqual(schedule_item.chargeable_days, chargeable_days)
        self.assertFalse(VacationEntitlementAllocation.objects.filter(vacation_request=approved_request).exists())
        self.assertTrue(VacationEntitlementAllocation.objects.filter(schedule_item=schedule_item).exists())
        self.assertEqual(summary["used"], chargeable_days)
        self.assertTrue(
            approved_request.history_entries.filter(action=VacationRequestHistory.ACTION_APPROVED).exists()
        )

    def test_rejected_request_records_history(self):
        pending_request = VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 12, 1),
            end_date=date(2026, 12, 3),
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )

        reject_vacation_request(pending_request.id, reviewer=self.department_head)

        self.assertTrue(
            pending_request.history_entries.filter(action=VacationRequestHistory.ACTION_REJECTED).exists()
        )

    def test_deleted_request_keeps_history_audit_entry(self):
        pending_request = create_vacation_request(
            employee=self.employee,
            start_date=date(2026, 12, 5),
            end_date=date(2026, 12, 6),
            vacation_type="unpaid",
        )
        request_id = pending_request.id

        delete_pending_vacation_request(request_id, actor=self.employee)

        self.assertFalse(VacationRequest.objects.filter(id=request_id).exists())
        self.assertTrue(
            VacationRequestHistory.objects.filter(
                vacation_request__isnull=True,
                employee=self.employee,
                action=VacationRequestHistory.ACTION_DELETED,
            ).exists()
        )

    def test_unpaid_and_study_approvals_do_not_create_schedule_items(self):
        unpaid_request = VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 3),
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        study_request = VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 5),
            vacation_type="study",
            status=VacationRequest.STATUS_PENDING,
        )

        approve_vacation_request(unpaid_request.id, reviewer=self.department_head)
        approve_vacation_request(study_request.id, reviewer=self.department_head)

        unpaid_request.refresh_from_db()
        study_request.refresh_from_db()
        self.assertEqual(unpaid_request.status, VacationRequest.STATUS_APPROVED)
        self.assertEqual(study_request.status, VacationRequest.STATUS_APPROVED)
        self.assertFalse(unpaid_request.created_schedule_items.exists())
        self.assertFalse(study_request.created_schedule_items.exists())

    def test_approve_fails_when_balance_insufficient(self):
        limited_employee = Employees.objects.create(
            last_name="Лимитов",
            first_name="Петр",
            middle_name="Сергеевич",
            login="limited-balance",
            position="Специалист",
            department=self.engineering,
            date_joined=date(2026, 1, 1),
            annual_paid_leave_days=5,
            role=Employees.ROLE_EMPLOYEE,
        )
        VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        previous_sync_state = set_vacation_metric_sync_enabled(False)
        try:
            pending_request = VacationRequest.objects.create(
                employee=limited_employee,
                start_date="2026-09-01",
                end_date="2026-09-10",
                vacation_type="paid",
                status=VacationRequest.STATUS_PENDING,
            )
        finally:
            set_vacation_metric_sync_enabled(previous_sync_state)

        with self.assertRaises(ValidationError):
            approve_vacation_request(pending_request.id, reviewer=self.department_head)

    def test_approve_fails_when_dates_conflict(self):
        VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-09-10",
            end_date="2026-09-12",
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            VacationRequest.objects.create(
                employee=self.employee,
                start_date="2026-09-11",
                end_date="2026-09-13",
                vacation_type="paid",
                status=VacationRequest.STATUS_PENDING,
            )

    def test_approve_requires_valid_reviewer(self):
        pending_request = VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 9, 20),
            end_date=date(2026, 9, 24),
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
        )

        invalid_reviewers = [None, self.employee, self.foreign_department_head, self.hr_employee]
        for reviewer in invalid_reviewers:
            with self.subTest(reviewer=reviewer):
                with self.assertRaises(ValidationError):
                    approve_vacation_request(pending_request.id, reviewer=reviewer)

        pending_request.refresh_from_db()
        self.assertEqual(pending_request.status, VacationRequest.STATUS_PENDING)

    def test_reject_requires_valid_reviewer(self):
        pending_request = VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 10, 20),
            end_date=date(2026, 10, 24),
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )

        with self.assertRaises(ValidationError):
            reject_vacation_request(pending_request.id, reviewer=self.employee)
        with self.assertRaises(ValidationError):
            reject_vacation_request(pending_request.id, reviewer=None)

        pending_request.refresh_from_db()
        self.assertEqual(pending_request.status, VacationRequest.STATUS_PENDING)

    def test_valid_approval_chains(self):
        employee_request = VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 3),
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        hr_request = VacationRequest.objects.create(
            employee=self.hr_employee,
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 12),
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        department_head_request = VacationRequest.objects.create(
            employee=self.department_head,
            start_date=date(2026, 8, 20),
            end_date=date(2026, 8, 22),
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        enterprise_head_request = VacationRequest.objects.create(
            employee=self.enterprise_head,
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 3),
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )

        approve_vacation_request(employee_request.id, reviewer=self.department_head)
        approve_vacation_request(hr_request.id, reviewer=self.enterprise_head)
        approve_vacation_request(department_head_request.id, reviewer=self.enterprise_head)
        approve_vacation_request(enterprise_head_request.id, reviewer=self.authorized_person)

        for request_obj in (employee_request, hr_request, department_head_request, enterprise_head_request):
            request_obj.refresh_from_db()
            self.assertEqual(request_obj.status, VacationRequest.STATUS_APPROVED)

    def test_expected_vacation_approver_uses_real_department_head(self):
        route = get_expected_vacation_approver(self.employee)

        self.assertEqual(route.role_label, "Руководитель отдела")
        self.assertEqual(route.employee, self.department_head)

    def test_repair_command_rebuilds_reviewer_and_chronological_history(self):
        created_at = timezone.now()
        reviewed_at = created_at - timedelta(days=30)
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2027, 3, 1),
            end_date=date(2027, 3, 3),
            vacation_type="unpaid",
            status=VacationRequest.STATUS_APPROVED,
            reviewed_by=self.hr_employee,
            reviewed_at=reviewed_at,
        )
        VacationRequest.objects.filter(pk=request_obj.pk).update(created_at=created_at)

        call_command("repair_vacation_request_history", "--apply", stdout=StringIO())

        request_obj.refresh_from_db()
        self.assertEqual(request_obj.reviewed_by, self.department_head)
        self.assertLess(request_obj.created_at, request_obj.reviewed_at)

        entries = list(request_obj.history_entries.order_by("created_at", "id"))
        self.assertEqual(
            [entry.action for entry in entries],
            [
                VacationRequestHistory.ACTION_CREATED,
                VacationRequestHistory.ACTION_SUBMITTED,
                VacationRequestHistory.ACTION_APPROVED,
            ],
        )
        self.assertEqual([entry.status_snapshot for entry in entries], ["pending", "pending", "approved"])
        self.assertLessEqual(entries[0].created_at, entries[1].created_at)
        self.assertLessEqual(entries[1].created_at, entries[2].created_at)
        self.assertEqual(entries[2].actor, self.department_head)

    def test_delete_pending_request_requires_valid_actor(self):
        pending_request = VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 12, 1),
            end_date=date(2026, 12, 3),
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )

        for actor in (None, self.foreign_department_head, self.hr_employee):
            with self.subTest(actor=actor):
                with self.assertRaises(ValidationError):
                    delete_pending_vacation_request(pending_request.id, actor=actor)

        deleted_employee = delete_pending_vacation_request(pending_request.id, actor=self.employee)

        self.assertEqual(deleted_employee, self.employee)
        self.assertFalse(VacationRequest.objects.filter(id=pending_request.id).exists())
