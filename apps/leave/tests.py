from datetime import date, timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.services import sync_employee_user
from apps.employees.models import Departments, Employees
from apps.leave.models import (
    VacationEntitlementAllocation,
    VacationEntitlementPeriod,
    VacationRequest,
    VacationSchedule,
    VacationScheduleChangeRequest,
    VacationScheduleItem,
)
from apps.leave.services import (
    approve_schedule_change_request,
    approve_vacation_request,
    build_analytics_payload,
    build_calendar_base_data,
    build_calendar_rows,
    create_schedule_change_request,
    create_vacation_request,
    get_chargeable_leave_days,
    get_employee_accrued_leave,
    get_employee_entitlement_rows,
    get_employee_list_leave_summaries,
    get_employee_leave_summaries,
    get_employee_leave_summary,
    get_employee_requestable_leave,
    get_paid_request_eligibility_for_year,
    reject_schedule_change_request,
    set_vacation_metric_sync_enabled,
    sync_employee_vacation_metrics,
)


class VacationRulesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.today = timezone.localdate()
        cls.engineering = Departments.objects.create(name="Engineering")
        cls.hr_department = Departments.objects.create(name="HR")

        cls.employee = Employees.objects.create(
            last_name="Календарев",
            first_name="Иван",
            middle_name="Петрович",
            login="calendar-user",
            position="Специалист",
            department=cls.engineering,
            date_joined=cls.today - timedelta(days=420),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        sync_employee_user(cls.employee, raw_password="employee-pass")

        cls.department_head = Employees.objects.create(
            last_name="Планова",
            first_name="Мария",
            middle_name="Игоревна",
            login="calendar-dept-head",
            position="Руководитель отдела",
            department=cls.engineering,
            date_joined=cls.today - timedelta(days=800),
            annual_paid_leave_days=52,
            role=Employees.ROLE_DEPARTMENT_HEAD,
        )
        sync_employee_user(cls.department_head, raw_password="dept-head-pass")

        cls.enterprise_head = Employees.objects.create(
            last_name="Директоров",
            first_name="Олег",
            middle_name="Игоревич",
            login="calendar-enterprise-head",
            position="Директор",
            department=cls.hr_department,
            date_joined=cls.today - timedelta(days=900),
            annual_paid_leave_days=52,
            role=Employees.ROLE_ENTERPRISE_HEAD,
        )
        sync_employee_user(cls.enterprise_head, raw_password="enterprise-pass")

        cls.hr_employee = Employees.objects.create(
            last_name="Кадрова",
            first_name="Анна",
            middle_name="Сергеевна",
            login="calendar-hr",
            position="HR",
            department=cls.hr_department,
            date_joined=cls.today - timedelta(days=700),
            annual_paid_leave_days=52,
            role=Employees.ROLE_HR,
        )
        sync_employee_user(cls.hr_employee, raw_password="hr-pass")

        cls.authorized_person = Employees.objects.create(
            last_name="Админова",
            first_name="Инна",
            middle_name="Олеговна",
            login="authorized-person",
            position="Уполномоченное лицо",
            date_joined=cls.today - timedelta(days=1000),
            annual_paid_leave_days=52,
            role=Employees.ROLE_AUTHORIZED_PERSON,
        )
        sync_employee_user(cls.authorized_person, raw_password="authorized-pass")

        cls.outsider = Employees.objects.create(
            last_name="Чужой",
            first_name="Петр",
            middle_name="Сергеевич",
            login="other-department-user",
            position="Аналитик",
            department=cls.hr_department,
            date_joined=cls.today - timedelta(days=300),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        sync_employee_user(cls.outsider, raw_password="outsider-pass")

        cls.foreign_department_head = Employees.objects.create(
            last_name="Другой",
            first_name="Роман",
            middle_name="Олегович",
            login="foreign-department-head",
            position="Руководитель отдела",
            department=cls.hr_department,
            date_joined=cls.today - timedelta(days=850),
            annual_paid_leave_days=52,
            role=Employees.ROLE_DEPARTMENT_HEAD,
        )
        sync_employee_user(cls.foreign_department_head, raw_password="foreign-head-pass")

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

    def test_schedule_change_request_approves_by_replacing_old_item(self):
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.employee,
            new_start_date=date(2026, 8, 1),
            new_end_date=date(2026, 8, 14),
            reason="Нужно перенести по семейным обстоятельствам.",
        )
        schedule_item.refresh_from_db()

        self.assertEqual(change_request.status, VacationScheduleChangeRequest.STATUS_PENDING)
        self.assertEqual(schedule_item.status, VacationScheduleItem.STATUS_APPROVED)

        replacement = approve_schedule_change_request(change_request.id, reviewer=self.department_head)
        schedule_item.refresh_from_db()
        change_request.refresh_from_db()

        self.assertEqual(schedule_item.status, VacationScheduleItem.STATUS_TRANSFERRED)
        self.assertEqual(change_request.status, VacationScheduleChangeRequest.STATUS_APPROVED)
        self.assertEqual(replacement.previous_item_id, schedule_item.id)
        self.assertEqual(replacement.created_from_change_request_id, change_request.id)
        self.assertEqual(replacement.source, VacationScheduleItem.SOURCE_TRANSFER)
        self.assertEqual(replacement.chargeable_days, 14)

    def test_rejected_schedule_change_does_not_modify_schedule_item(self):
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.employee,
            new_start_date=date(2026, 8, 1),
            new_end_date=date(2026, 8, 14),
            reason="Нужно перенести.",
        )

        reject_schedule_change_request(change_request.id, reviewer=self.department_head)
        schedule_item.refresh_from_db()
        change_request.refresh_from_db()

        self.assertEqual(schedule_item.status, VacationScheduleItem.STATUS_APPROVED)
        self.assertEqual(change_request.status, VacationScheduleChangeRequest.STATUS_REJECTED)
        self.assertFalse(VacationScheduleItem.objects.filter(previous_item=schedule_item).exists())

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

        self.assertEqual(approved_request.status, VacationRequest.STATUS_APPROVED)
        self.assertEqual(schedule_item.source, VacationScheduleItem.SOURCE_MANUAL)
        self.assertEqual(schedule_item.status, VacationScheduleItem.STATUS_APPROVED)
        self.assertFalse(schedule_item.generated_by_ai)
        self.assertEqual(schedule_item.chargeable_days, chargeable_days)
        self.assertFalse(VacationEntitlementAllocation.objects.filter(vacation_request=approved_request).exists())
        self.assertTrue(VacationEntitlementAllocation.objects.filter(schedule_item=schedule_item).exists())
        self.assertEqual(summary["used"], chargeable_days)

    def test_calendar_shows_converted_paid_request_only_as_schedule_item(self):
        VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        pending_request = VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 10, 6),
            end_date=date(2026, 10, 12),
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
        )
        approved_request = approve_vacation_request(pending_request.id, reviewer=self.department_head)

        _, _, employee_entries = build_calendar_base_data(2026, employee_ids=[self.employee.id])
        entries = employee_entries[self.employee.id]

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source_kind"], "schedule")
        self.assertEqual(entries[0]["source_label"], "Дополнение к графику")
        self.assertEqual(entries[0]["source_id"], approved_request.created_schedule_items.get().id)

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
            approve_vacation_request(pending_request.id)

    def test_approve_fails_when_dates_conflict(self):
        VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-09-10",
            end_date="2026-09-12",
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )
        pending_request = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-09-11",
            end_date="2026-09-13",
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
        )

        with self.assertRaises(ValidationError):
            approve_vacation_request(pending_request.id)

    def test_unpaid_vacation_does_not_reduce_balance(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-10-01",
            end_date="2026-10-05",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_APPROVED,
        )
        sync_employee_vacation_metrics(self.employee)

        self.assertEqual(request_obj.vacation_type, "unpaid")
        self.assertEqual(get_employee_leave_summary(self.employee)["used"], 0)

    def test_holiday_days_do_not_reduce_paid_balance(self):
        self.assertEqual(get_chargeable_leave_days(date(2026, 1, 1), date(2026, 1, 8), "paid"), 0)

        VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-01-01",
            end_date="2026-01-08",
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )
        sync_employee_vacation_metrics(self.employee)

        self.assertEqual(get_employee_leave_summary(self.employee)["used"], 0)

    def test_less_than_six_months_requestable_balance_matches_accrued(self):
        newcomer = Employees.objects.create(
            last_name="Новичков",
            first_name="Олег",
            middle_name="Сергеевич",
            login="newcomer",
            position="Стажер",
            department=self.engineering,
            date_joined=self.today - timedelta(days=45),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )

        accrued = get_employee_accrued_leave(newcomer, self.today)
        requestable = get_employee_requestable_leave(newcomer, self.today)

        self.assertEqual(accrued, requestable)
        self.assertLess(requestable, 52)

    def test_after_six_months_employee_can_use_advance(self):
        six_month_employee = Employees.objects.create(
            last_name="Северов",
            first_name="Павел",
            middle_name="Андреевич",
            login="north-employee",
            position="Инженер",
            department=self.engineering,
            date_joined=self.today - timedelta(days=190),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )

        accrued = get_employee_accrued_leave(six_month_employee, self.today)
        requestable = get_employee_requestable_leave(six_month_employee, self.today)

        self.assertLess(accrued, 52)
        self.assertEqual(requestable, 52)

    def test_second_working_year_does_not_require_waiting_another_six_months(self):
        experienced_employee = Employees.objects.create(
            last_name="Опытный",
            first_name="Алексей",
            middle_name="Игоревич",
            login="experienced-employee",
            position="Инженер",
            department=self.engineering,
            date_joined=self.today - timedelta(days=420),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )

        accrued = get_employee_accrued_leave(experienced_employee, self.today)
        requestable = get_employee_requestable_leave(experienced_employee, self.today)

        self.assertLess(accrued, 104)
        self.assertEqual(requestable, 104)

    def test_available_balance_uses_requestable_for_subsequent_working_years(self):
        experienced_employee = Employees.objects.create(
            last_name="Балансов",
            first_name="Павел",
            middle_name="Сергеевич",
            login="experienced-balance",
            position="Ведущий инженер",
            department=self.engineering,
            date_joined=self.today - timedelta(days=420),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        VacationRequest.objects.create(
            employee=experienced_employee,
            start_date=self.today - timedelta(days=300),
            end_date=self.today - timedelta(days=287),
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )

        summary = get_employee_leave_summary(experienced_employee, self.today)

        self.assertEqual(summary["requestable"], 104)
        self.assertEqual(summary["used"], 14)
        self.assertEqual(summary["available"], 90)
        self.assertEqual(summary["accrued_balance"], summary["accrued"] - 14)
        self.assertEqual(summary["advance_available"], summary["available"] - summary["accrued_balance"])

    def test_leave_summary_counts_approved_schedule_items_as_used_days(self):
        schedule_year = self.today.year - 1
        schedule = VacationSchedule.objects.create(
            year=schedule_year,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        start_date = date(schedule_year, 7, 1)
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=start_date,
            end_date=start_date + timedelta(days=13),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        summary = get_employee_leave_summary(self.employee, self.today)
        list_summary = get_employee_list_leave_summaries([self.employee], self.today)[self.employee.id]

        self.assertEqual(summary["used"], 14)
        self.assertEqual(list_summary["used"], summary["used"])

    def test_leave_ledger_allocates_paid_days_to_working_years(self):
        first_period_start = self.employee.date_joined + timedelta(days=200)
        schedule = VacationSchedule.objects.create(
            year=first_period_start.year,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        first_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=first_period_start,
            end_date=first_period_start + timedelta(days=13),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        summary = get_employee_leave_summary(self.employee, self.today)
        first_allocation = VacationEntitlementAllocation.objects.get(schedule_item=first_item)
        first_period = VacationEntitlementPeriod.objects.get(employee=self.employee, working_year_number=1)

        self.assertEqual(first_allocation.entitlement_period, first_period)
        self.assertEqual(first_allocation.allocated_days, 14)
        self.assertEqual(summary["used"], 14)

    def test_entitlement_rows_expose_working_year_balances(self):
        rows = get_employee_entitlement_rows(self.employee, self.today)

        self.assertGreaterEqual(len(rows), 2)
        self.assertIn("period_label", rows[0])
        self.assertIn("remaining_days", rows[0])
        self.assertIn("status_label", rows[0])

    def test_leave_summary_exposes_advance_breakdown(self):
        six_month_employee = Employees.objects.create(
            last_name="Авансов",
            first_name="Иван",
            middle_name="Петрович",
            login="advance-breakdown",
            position="Инженер",
            department=self.engineering,
            date_joined=self.today - timedelta(days=190),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        VacationRequest.objects.create(
            employee=six_month_employee,
            start_date=self.today - timedelta(days=20),
            end_date=self.today - timedelta(days=6),
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )

        summary = get_employee_leave_summary(six_month_employee, self.today)
        positive_accrued_balance = max(summary["accrued_balance"], 0)

        self.assertLess(summary["accrued"], summary["requestable"])
        self.assertEqual(summary["accrued_balance"], summary["accrued"] - summary["used"] - summary["reserved"])
        self.assertEqual(summary["advance_available"], summary["available"] - positive_accrued_balance)

    def test_bulk_leave_summary_matches_single_employee_calculation(self):
        teammate = Employees.objects.create(
            last_name="Командный",
            first_name="Игорь",
            middle_name="Сергеевич",
            login="teammate-bulk",
            position="Инженер",
            department=self.engineering,
            date_joined=self.today - timedelta(days=650),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )

        VacationRequest.objects.create(
            employee=self.employee,
            start_date=self.today - timedelta(days=60),
            end_date=self.today - timedelta(days=46),
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )
        VacationRequest.objects.create(
            employee=self.employee,
            start_date=self.today + timedelta(days=30),
            end_date=self.today + timedelta(days=36),
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
        )
        VacationRequest.objects.create(
            employee=teammate,
            start_date=self.today - timedelta(days=45),
            end_date=self.today - timedelta(days=36),
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )

        bulk_summaries = get_employee_leave_summaries([self.employee, teammate], as_of_date=self.today)

        self.assertEqual(bulk_summaries[self.employee.id], get_employee_leave_summary(self.employee, self.today))
        self.assertEqual(bulk_summaries[teammate.id], get_employee_leave_summary(teammate, self.today))

    def test_employee_list_leave_summary_matches_full_summary_for_display_fields(self):
        teammate = Employees.objects.create(
            last_name="Списков",
            first_name="Андрей",
            middle_name="Игоревич",
            login="list-summary-employee",
            position="Инженер",
            department=self.engineering,
            date_joined=self.today - timedelta(days=510),
            annual_paid_leave_days=52,
            manual_leave_adjustment_days=3,
            role=Employees.ROLE_EMPLOYEE,
        )

        VacationRequest.objects.create(
            employee=self.employee,
            start_date=self.today - timedelta(days=30),
            end_date=self.today - timedelta(days=21),
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )
        VacationRequest.objects.create(
            employee=self.employee,
            start_date=self.today + timedelta(days=15),
            end_date=self.today + timedelta(days=19),
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
        )
        VacationRequest.objects.create(
            employee=teammate,
            start_date=self.today - timedelta(days=80),
            end_date=self.today - timedelta(days=68),
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )
        VacationRequest.objects.create(
            employee=teammate,
            start_date=self.today + timedelta(days=40),
            end_date=self.today + timedelta(days=46),
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
        )

        self.employee.refresh_from_db()
        teammate.refresh_from_db()
        list_summaries = get_employee_list_leave_summaries([self.employee, teammate], as_of_date=self.today)

        for employee in (self.employee, teammate):
            full_summary = get_employee_leave_summary(employee, self.today)
            list_summary = list_summaries[employee.id]
            self.assertEqual(list_summary["requestable"], full_summary["requestable"])
            self.assertEqual(list_summary["used"], full_summary["used"])
            self.assertEqual(list_summary["reserved"], full_summary["reserved"])
            self.assertEqual(list_summary["available"], full_summary["available"])

    def test_applications_ajax_returns_only_department_scope_for_department_head(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-11-01",
            end_date="2026-11-03",
            vacation_type="study",
            status=VacationRequest.STATUS_PENDING,
        )
        VacationRequest.objects.create(
            employee=self.outsider,
            start_date="2026-11-05",
            end_date="2026-11-07",
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
        )
        self.client.force_login(self.department_head.user)

        response = self.client.get(
            reverse("applications"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["vacations"]), 1)
        self.assertEqual(payload["vacations"][0]["employee_name"], self.employee.full_name)
        self.assertEqual(payload["vacations"][0]["employee_department"], self.employee.department.name)
        self.assertEqual(payload["vacations"][0]["detail_url"], reverse("vacation_detail", args=[request_obj.id]))
        self.assertIn("period_label", payload["vacations"][0])

    def test_applications_search_filters_requests_and_transfers_by_employee_name(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-11-01",
            end_date="2026-11-03",
            vacation_type="study",
            status=VacationRequest.STATUS_PENDING,
        )
        VacationRequest.objects.create(
            employee=self.outsider,
            start_date="2026-11-05",
            end_date="2026-11-07",
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
        )
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.employee,
            new_start_date=date(2026, 8, 1),
            new_end_date=date(2026, 8, 14),
            reason="Search test",
        )
        self.client.force_login(self.department_head.user)

        response = self.client.get(
            reverse("applications"),
            {
                "status": VacationRequest.STATUS_PENDING,
                "search": self.employee.first_name,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item["id"] for item in payload["vacations"]], [request_obj.id])
        self.assertEqual([item["id"] for item in payload["change_requests"]], [change_request.id])

    def test_applications_search_respects_department_head_scope(self):
        VacationRequest.objects.create(
            employee=self.outsider,
            start_date="2026-11-05",
            end_date="2026-11-07",
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
        )
        self.client.force_login(self.department_head.user)

        response = self.client.get(
            reverse("applications"),
            {"search": self.outsider.first_name},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["vacations"], [])
        self.assertEqual(payload["change_requests"], [])

    def test_applications_page_uses_sectioned_cards_and_custom_department_select(self):
        self.client.force_login(self.department_head.user)

        response = self.client.get(reverse("applications"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-applications-page")
        self.assertContains(response, "applications-board--transfers")
        self.assertContains(response, "applications-board--requests")
        self.assertContains(response, "data-applications-transfer-scroll")
        self.assertContains(response, "data-applications-request-scroll")
        self.assertContains(response, 'class="employee-select__native"')
        self.assertNotContains(response, 'id="lineCustom"')
        self.assertNotContains(response, 'id="vacationsTableBody"')
        self.assertNotContains(response, 'id="changeRequestsTableBody"')

        content = response.content.decode(response.charset or "utf-8")
        self.assertLess(
            content.index("applications-board--transfers"),
            content.index("applications-board--requests"),
        )

    def test_applications_pending_filter_applies_to_requests_and_transfers(self):
        pending_request = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-10-01",
            end_date="2026-10-03",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-10-10",
            end_date="2026-10-12",
            vacation_type="study",
            status=VacationRequest.STATUS_APPROVED,
        )
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.employee,
            new_start_date=date(2026, 8, 1),
            new_end_date=date(2026, 8, 14),
            reason="Нужно перенести отпуск.",
        )

        self.client.force_login(self.department_head.user)
        response = self.client.get(
            reverse("applications"),
            {"status": VacationRequest.STATUS_PENDING},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item["id"] for item in payload["vacations"]], [pending_request.id])
        self.assertEqual([item["id"] for item in payload["change_requests"]], [change_request.id])
        self.assertEqual(payload["change_requests"][0]["employee_department"], self.employee.department.name)
        self.assertIn("approve_url", payload["change_requests"][0])
        self.assertIn("reject_url", payload["change_requests"][0])

    def test_calendar_ajax_returns_partial_results_for_view_switch(self):
        VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-07-10",
            end_date="2026-07-14",
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )
        self.client.force_login(self.employee.user)

        response = self.client.get(
            reverse("calendar"),
            {"view": "year", "year": 2026},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("board_html", payload)
        self.assertIn("period_label", payload)
        self.assertIn("period_description", payload)
        self.assertIn("calendar_details", payload)
        self.assertIn("year-board", payload["board_html"])
        self.assertNotIn("calendar-board-card", payload["board_html"])
        self.assertNotIn('id="calendar-filters-form"', payload["board_html"])
        self.assertNotIn("calendar-summary-grid", payload["board_html"])
        self.assertIn(str(self.employee.id), payload["calendar_details"])
        self.assertEqual(payload["period_label"], "График отпусков на 2026 год")

        month_response = self.client.get(
            reverse("calendar"),
            {"view": "month", "year": 2026, "month": 7},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(month_response.status_code, 200)
        self.assertEqual(month_response.json()["period_label"], "График отпусков на июль 2026")

    def test_employee_cannot_open_management_sections(self):
        self.client.force_login(self.employee.user)

        applications_response = self.client.get(reverse("applications"))
        analytics_response = self.client.get(reverse("analytics"))

        self.assertRedirects(applications_response, reverse("main"))
        self.assertRedirects(analytics_response, reverse("main"))

    def test_hr_can_view_all_applications_but_cannot_approve(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-12-01",
            end_date="2026-12-02",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        self.client.force_login(self.hr_employee.user)

        applications_response = self.client.get(reverse("applications"))
        approve_response = self.client.post(reverse("approve_vacation", args=[request_obj.id]))
        request_obj.refresh_from_db()

        self.assertEqual(applications_response.status_code, 200)
        self.assertContains(applications_response, self.employee.full_name)
        self.assertRedirects(approve_response, reverse("vacation_detail", args=[request_obj.id]))
        self.assertEqual(request_obj.status, VacationRequest.STATUS_PENDING)

    def test_enterprise_head_can_view_all_applications_but_approve_only_management(self):
        department_head_request = VacationRequest.objects.create(
            employee=self.department_head,
            start_date="2026-12-01",
            end_date="2026-12-02",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-12-05",
            end_date="2026-12-06",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        self.client.force_login(self.enterprise_head.user)

        applications_response = self.client.get(reverse("applications"))
        approve_department_head_response = self.client.post(reverse("approve_vacation", args=[department_head_request.id]))
        approve_regular_employee_response = self.client.post(reverse("approve_vacation", args=[request_obj.id]))

        department_head_request.refresh_from_db()
        request_obj.refresh_from_db()

        self.assertEqual(applications_response.status_code, 200)
        self.assertContains(applications_response, self.department_head.full_name)
        self.assertContains(applications_response, self.employee.full_name)
        self.assertEqual(applications_response.context["pending_requests_count"], 2)
        self.assertRedirects(approve_department_head_response, reverse("applications"))
        self.assertEqual(department_head_request.status, VacationRequest.STATUS_APPROVED)
        self.assertRedirects(approve_regular_employee_response, reverse("vacation_detail", args=[request_obj.id]))
        self.assertEqual(request_obj.status, VacationRequest.STATUS_PENDING)

    def test_enterprise_head_can_view_regular_employee_transfer_requests_without_approving(self):
        future_year = timezone.localdate().year + 1
        schedule = VacationSchedule.objects.create(
            year=future_year,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(future_year, 7, 1),
            end_date=date(future_year, 7, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.employee,
            new_start_date=date(future_year, 8, 1),
            new_end_date=date(future_year, 8, 14),
            reason="Семейные обстоятельства.",
        )
        self.client.force_login(self.enterprise_head.user)

        response = self.client.get(reverse("applications"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.employee.full_name)
        self.assertEqual(response.context["pending_requests_count"], 1)
        change_request.refresh_from_db()
        self.assertFalse(response.context["change_requests"][0].can_approve)
        self.assertEqual(change_request.status, VacationScheduleChangeRequest.STATUS_PENDING)

    def test_department_head_can_approve_only_own_department_requests(self):
        own_request = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-12-10",
            end_date="2026-12-12",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        foreign_request = VacationRequest.objects.create(
            employee=self.outsider,
            start_date="2026-12-15",
            end_date="2026-12-17",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        foreign_head_request = VacationRequest.objects.create(
            employee=self.foreign_department_head,
            start_date="2026-12-20",
            end_date="2026-12-21",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        self.client.force_login(self.department_head.user)

        approve_own_response = self.client.post(reverse("approve_vacation", args=[own_request.id]))
        approve_foreign_response = self.client.post(reverse("approve_vacation", args=[foreign_request.id]))
        approve_foreign_head_response = self.client.post(reverse("approve_vacation", args=[foreign_head_request.id]))

        own_request.refresh_from_db()
        foreign_request.refresh_from_db()
        foreign_head_request.refresh_from_db()

        self.assertRedirects(approve_own_response, reverse("applications"))
        self.assertEqual(own_request.status, VacationRequest.STATUS_APPROVED)
        self.assertEqual(approve_foreign_response.status_code, 302)
        self.assertEqual(approve_foreign_response.url, reverse("vacation_detail", args=[foreign_request.id]))
        self.assertEqual(approve_foreign_head_response.status_code, 302)
        self.assertEqual(approve_foreign_head_response.url, reverse("vacation_detail", args=[foreign_head_request.id]))
        self.assertEqual(foreign_request.status, VacationRequest.STATUS_PENDING)
        self.assertEqual(foreign_head_request.status, VacationRequest.STATUS_PENDING)

    def test_authorized_person_can_approve_only_enterprise_head_requests(self):
        enterprise_request = VacationRequest.objects.create(
            employee=self.enterprise_head,
            start_date="2026-12-10",
            end_date="2026-12-12",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        department_head_request = VacationRequest.objects.create(
            employee=self.department_head,
            start_date="2026-12-15",
            end_date="2026-12-17",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        self.client.force_login(self.authorized_person.user)

        applications_response = self.client.get(reverse("applications"))
        approve_enterprise_response = self.client.post(reverse("approve_vacation", args=[enterprise_request.id]))
        approve_department_head_response = self.client.post(reverse("approve_vacation", args=[department_head_request.id]))

        enterprise_request.refresh_from_db()
        department_head_request.refresh_from_db()

        self.assertEqual(applications_response.status_code, 200)
        self.assertContains(applications_response, self.enterprise_head.full_name)
        self.assertNotContains(applications_response, self.department_head.full_name)
        self.assertRedirects(approve_enterprise_response, reverse("applications"))
        self.assertEqual(enterprise_request.status, VacationRequest.STATUS_APPROVED)
        self.assertEqual(approve_department_head_response.status_code, 302)
        self.assertEqual(
            approve_department_head_response.url,
            reverse("vacation_detail", args=[department_head_request.id]),
        )
        self.assertEqual(department_head_request.status, VacationRequest.STATUS_PENDING)

    def test_enterprise_head_cannot_approve_own_request(self):
        own_request = VacationRequest.objects.create(
            employee=self.enterprise_head,
            start_date="2026-12-22",
            end_date="2026-12-23",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        self.client.force_login(self.enterprise_head.user)

        response = self.client.post(reverse("approve_vacation", args=[own_request.id]))
        own_request.refresh_from_db()

        self.assertRedirects(response, reverse("vacation_detail", args=[own_request.id]))
        self.assertEqual(own_request.status, VacationRequest.STATUS_PENDING)

    def test_department_head_analytics_are_limited_to_own_department(self):
        VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 1, 30),
            end_date=date(2026, 2, 2),
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )
        VacationRequest.objects.create(
            employee=self.outsider,
            start_date=date(2026, 1, 10),
            end_date=date(2026, 1, 12),
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )
        self.client.force_login(self.department_head.user)

        response = self.client.get(reverse("analytics"))

        self.assertEqual(response.status_code, 200)
        row_employee_ids = {row["employee_id"] for row in response.context["rows"]}
        self.assertIn(self.employee.id, row_employee_ids)
        self.assertNotIn(self.outsider.id, row_employee_ids)

    def test_analytics_split_duration_by_month_overlap(self):
        VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 1, 30),
            end_date=date(2026, 2, 2),
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )

        payload = build_analytics_payload()

        self.assertEqual(payload["values1"][0], 1)
        self.assertEqual(payload["values1"][1], 1)
        self.assertEqual(payload["values2"][0], 2)
        self.assertEqual(payload["values2"][1], 2)
        self.assertEqual(payload["values3"][0], 2)
        self.assertEqual(payload["values3"][1], 2)

    def test_vacation_detail_renders_role_based_action_forms(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-12-15",
            end_date="2026-12-17",
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
        )

        self.client.force_login(self.department_head.user)
        manager_response = self.client.get(reverse("vacation_detail", args=[request_obj.id]))

        self.client.force_login(self.enterprise_head.user)
        enterprise_response = self.client.get(reverse("vacation_detail", args=[request_obj.id]))

        self.assertEqual(manager_response.status_code, 200)
        self.assertContains(manager_response, reverse("approve_vacation", args=[request_obj.id]))
        self.assertContains(manager_response, reverse("reject_vacation", args=[request_obj.id]))
        self.assertContains(manager_response, reverse("delete_vacation", args=[request_obj.id]))
        self.assertContains(manager_response, "Можно запланировать сейчас")
        self.assertContains(manager_response, "Начислено по стажу")

        self.assertEqual(enterprise_response.status_code, 200)
        self.assertNotContains(enterprise_response, reverse("approve_vacation", args=[request_obj.id]))
        self.assertNotContains(enterprise_response, reverse("reject_vacation", args=[request_obj.id]))

    def test_authorized_person_sees_action_forms_for_enterprise_head_request(self):
        request_obj = VacationRequest.objects.create(
            employee=self.enterprise_head,
            start_date="2026-12-20",
            end_date="2026-12-22",
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
        )
        self.client.force_login(self.authorized_person.user)

        response = self.client.get(reverse("vacation_detail", args=[request_obj.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("approve_vacation", args=[request_obj.id]))
        self.assertContains(response, reverse("reject_vacation", args=[request_obj.id]))

    def test_authorized_person_has_only_applications_access(self):
        self.client.force_login(self.authorized_person.user)

        main_response = self.client.get(reverse("main"))
        applications_response = self.client.get(reverse("applications"))
        employees_response = self.client.get(reverse("employees"))
        calendar_response = self.client.get(reverse("calendar"))
        profile_response = self.client.get(reverse("employee_profile", args=[self.authorized_person.id]))

        self.assertRedirects(main_response, reverse("applications"))
        self.assertEqual(applications_response.status_code, 200)
        self.assertNotContains(applications_response, "Профиль")
        self.assertContains(applications_response, "Служебный доступ")
        self.assertRedirects(employees_response, reverse("applications"))
        self.assertRedirects(calendar_response, reverse("applications"))
        self.assertRedirects(profile_response, reverse("applications"))

    def test_calendar_page_uses_shared_vacation_modal_hooks(self):
        self.client.force_login(self.employee.user)

        response = self.client.get(reverse("calendar"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-modal-open="vacation-modal"')
        self.assertContains(response, 'id="vacation-modal"')
        self.assertContains(response, 'id="chargeable_days"')
        self.assertContains(response, 'id="calendar-charge-preview"')
        self.assertContains(response, 'name="reason"')
        self.assertContains(response, 'data-modal-close')
        self.assertContains(response, 'data-date-field')
        self.assertContains(response, 'id="calendar-filters-form"', count=1)
        self.assertNotContains(response, "calendar-summary-grid")

    def test_calendar_page_renders_only_visible_employee_rows_for_regular_employee(self):
        VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-11-01",
            end_date="2026-11-03",
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )
        VacationRequest.objects.create(
            employee=self.outsider,
            start_date="2026-11-05",
            end_date="2026-11-07",
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )
        self.client.force_login(self.employee.user)

        response = self.client.get(reverse("calendar"), {"view": "year", "year": 2026})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.employee.full_name)
        self.assertNotContains(response, self.outsider.full_name)

    def test_calendar_year_filter_includes_years_with_schedule_items(self):
        old_employee = Employees.objects.create(
            last_name="Исторический",
            first_name="Иван",
            middle_name="Петрович",
            login="historical-calendar-user",
            position="Специалист",
            department=self.engineering,
            date_joined=date(2014, 1, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        schedule = VacationSchedule.objects.create(
            year=2015,
            status=VacationSchedule.STATUS_ARCHIVED,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=old_employee,
            start_date=date(2015, 7, 1),
            end_date=date(2015, 7, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("calendar"), {"view": "year", "year": 2015})

        self.assertEqual(response.status_code, 200)
        self.assertIn(2015, response.context["calendar_filters"]["available_years"])
        self.assertEqual(response.context["calendar_filters"]["selected_year"], 2015)

    def test_calendar_rows_include_schedule_items(self):
        old_employee = Employees.objects.create(
            last_name="Архивный",
            first_name="Петр",
            middle_name="Иванович",
            login="archive-calendar-user",
            position="Специалист",
            department=self.engineering,
            date_joined=date(2014, 1, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        schedule = VacationSchedule.objects.create(
            year=2015,
            status=VacationSchedule.STATUS_ARCHIVED,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=old_employee,
            start_date=date(2015, 7, 1),
            end_date=date(2015, 7, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        employees, employee_day_status, employee_entries = build_calendar_base_data(2015)
        rows, details = build_calendar_rows(
            employees,
            employee_day_status,
            employee_entries,
            year=2015,
            month=7,
            view_mode="month",
            today=date(2015, 7, 1),
        )

        row = next(row for row in rows if row["employee_id"] == old_employee.id)
        self.assertEqual(row["selected_approved_days"], 14)
        self.assertEqual(row["selected_schedule_days"], 14)
        self.assertEqual(row["status"], "schedule-approved")
        self.assertEqual(details[str(old_employee.id)]["selected_entries"][0]["status_label"], "График утвержден")
        self.assertEqual(details[str(old_employee.id)]["selected_entries"][0]["source_label"], "Годовой график")

    def test_calendar_hides_employees_not_hired_by_selected_year_end(self):
        employees, _, _ = build_calendar_base_data(2015)

        self.assertNotIn(self.employee.id, [employee.id for employee in employees])

    def test_calendar_rows_include_rejected_requests_in_month_and_year_views(self):
        VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-05-10",
            end_date="2026-05-12",
            vacation_type="paid",
            status=VacationRequest.STATUS_REJECTED,
        )

        employees, employee_day_status, employee_entries = build_calendar_base_data(2026)
        month_rows, month_details = build_calendar_rows(
            employees,
            employee_day_status,
            employee_entries,
            year=2026,
            month=5,
            view_mode="month",
            today=date(2026, 5, 1),
        )
        year_rows, _ = build_calendar_rows(
            employees,
            employee_day_status,
            employee_entries,
            year=2026,
            month=5,
            view_mode="year",
            today=date(2026, 5, 1),
        )

        month_row = next(row for row in month_rows if row["employee_id"] == self.employee.id)
        year_row = next(row for row in year_rows if row["employee_id"] == self.employee.id)
        may_cell = year_row["cells"][4]

        self.assertEqual(month_row["selected_rejected_days"], 3)
        self.assertEqual(month_row["status"], "request-rejected")
        self.assertEqual(year_row["year_rejected_days"], 3)
        self.assertEqual(may_cell["rejected_days"], 3)
        self.assertEqual(may_cell["status"], "request-rejected")
        self.assertEqual(month_details[str(self.employee.id)]["selected_rejected_days"], 3)

    def test_year_view_segments_follow_real_dates_across_month_boundary(self):
        VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-03-19",
            end_date="2026-04-01",
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
        )

        employees, employee_day_status, employee_entries = build_calendar_base_data(2026)
        rows, _ = build_calendar_rows(
            employees,
            employee_day_status,
            employee_entries,
            year=2026,
            month=3,
            view_mode="year",
            today=date(2026, 3, 1),
        )

        row = next(row for row in rows if row["employee_id"] == self.employee.id)
        march_cell = row["cells"][2]
        april_cell = row["cells"][3]

        self.assertEqual(march_cell["pending_days"], 13)
        self.assertEqual(len(march_cell["segments"]), 1)
        self.assertEqual(march_cell["segments"][0]["offset_percent"], 58.1)
        self.assertEqual(march_cell["segments"][0]["width_percent"], 41.9)
        self.assertEqual(april_cell["pending_days"], 1)
        self.assertEqual(len(april_cell["segments"]), 1)
        self.assertEqual(april_cell["segments"][0]["offset_percent"], 0.0)
        self.assertEqual(april_cell["segments"][0]["width_percent"], 3.3)
