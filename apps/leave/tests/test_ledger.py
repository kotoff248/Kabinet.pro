from datetime import date, timedelta

from apps.employees.models import Employees
from apps.leave.models import (
    VacationEntitlementAllocation,
    VacationEntitlementPeriod,
    VacationRequest,
    VacationSchedule,
    VacationScheduleItem,
)
from apps.leave.services.dates import get_chargeable_leave_days
from apps.leave.services.ledger import (
    get_employee_accrued_leave,
    get_employee_entitlement_rows,
    get_employee_entitlement_source_preview,
    get_employee_leave_summaries,
    get_employee_leave_summary,
    get_employee_list_leave_summaries,
    get_employee_requestable_leave,
    rebuild_employee_leave_ledger,
)
from apps.leave.services.metrics import sync_employee_vacation_metrics

from .base import LeaveTestCase


class LeaveLedgerTests(LeaveTestCase):
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
        rebuild_employee_leave_ledger(self.employee)
        first_allocation = VacationEntitlementAllocation.objects.get(schedule_item=first_item)
        first_period = VacationEntitlementPeriod.objects.get(employee=self.employee, working_year_number=1)

        self.assertEqual(first_allocation.entitlement_period, first_period)
        self.assertEqual(first_allocation.allocated_days, 14)
        self.assertEqual(summary["used"], 14)

    def test_leave_summary_read_does_not_create_allocations(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date=self.today - timedelta(days=30),
            end_date=self.today - timedelta(days=21),
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )

        before_count = VacationEntitlementAllocation.objects.filter(employee=self.employee).count()
        summary = get_employee_leave_summary(self.employee, self.today)
        after_count = VacationEntitlementAllocation.objects.filter(employee=self.employee).count()

        self.assertEqual(before_count, 0)
        self.assertEqual(after_count, before_count)
        self.assertEqual(
            summary["used"],
            get_chargeable_leave_days(request_obj.start_date, request_obj.end_date, request_obj.vacation_type),
        )

    def test_future_summary_read_does_not_rewrite_allocation_state(self):
        schedule = VacationSchedule.objects.create(
            year=self.today.year,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        future_start = self.today + timedelta(days=40)
        item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=future_start,
            end_date=future_start + timedelta(days=6),
            vacation_type="paid",
            chargeable_days=7,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        rebuild_employee_leave_ledger(self.employee)
        before_snapshot = list(
            VacationEntitlementAllocation.objects.filter(schedule_item=item)
            .order_by("id")
            .values("id", "state", "allocated_days", "entitlement_period_id")
        )

        summary = get_employee_leave_summary(self.employee, self.today)
        after_snapshot = list(
            VacationEntitlementAllocation.objects.filter(schedule_item=item)
            .order_by("id")
            .values("id", "state", "allocated_days", "entitlement_period_id")
        )

        self.assertEqual(before_snapshot, after_snapshot)
        self.assertEqual(summary["reserved"], 7)
        self.assertEqual(summary["used"], 0)

    def test_future_approved_leave_becomes_used_after_start_date(self):
        schedule = VacationSchedule.objects.create(
            year=self.today.year,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        future_start = self.today + timedelta(days=40)
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=future_start,
            end_date=future_start + timedelta(days=6),
            vacation_type="paid",
            chargeable_days=7,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        before_start = get_employee_leave_summary(self.employee, self.today)
        after_start = get_employee_leave_summary(self.employee, future_start)

        self.assertEqual(before_start["reserved"], 7)
        self.assertEqual(before_start["used"], 0)
        self.assertEqual(after_start["reserved"], 0)
        self.assertEqual(after_start["used"], 7)

    def test_past_leave_uses_oldest_year_before_future_reservations(self):
        employee = Employees.objects.create(
            last_name="Старогодов",
            first_name="Алексей",
            middle_name="Сергеевич",
            login="oldest-year-before-future",
            position="Инженер",
            department=self.engineering,
            date_joined=date(2024, 7, 7),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=employee,
            start_date=date(2026, 5, 20),
            end_date=date(2026, 5, 22),
            vacation_type="paid",
            chargeable_days=3,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=employee,
            start_date=date(2025, 8, 1),
            end_date=date(2025, 9, 21),
            vacation_type="paid",
            chargeable_days=52,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        rows = get_employee_entitlement_rows(employee, as_of_date=date(2026, 4, 30), limit=6)
        rows_by_year = {row["working_year_number"]: row for row in rows}

        self.assertEqual(rows_by_year[1]["used_days"], 52)
        self.assertEqual(rows_by_year[1]["reserved_days"], 0)
        self.assertEqual(rows_by_year[1]["remaining_days"], 0)
        self.assertEqual(rows_by_year[2]["used_days"], 0)
        self.assertEqual(rows_by_year[2]["reserved_days"], 3)

    def test_late_leave_does_not_close_expired_working_year(self):
        employee = Employees.objects.create(
            last_name="Срочников",
            first_name="Павел",
            middle_name="Иванович",
            login="expired-deadline-balance",
            position="Инженер",
            department=self.engineering,
            date_joined=date(2025, 1, 4),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        historical_schedule = VacationSchedule.objects.create(
            year=2025,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        late_schedule = VacationSchedule.objects.create(
            year=2027,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=historical_schedule,
            employee=employee,
            start_date=date(2025, 7, 4),
            end_date=date(2025, 8, 21),
            vacation_type="paid",
            chargeable_days=49,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        late_start = date(2027, 1, 1)
        late_end = date(2027, 1, 12)
        late_item = VacationScheduleItem.objects.create(
            schedule=late_schedule,
            employee=employee,
            start_date=late_start,
            end_date=late_end,
            vacation_type="paid",
            chargeable_days=get_chargeable_leave_days(late_start, late_end, "paid"),
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        rebuild_employee_leave_ledger(employee, as_of_date=date(2027, 12, 31))
        rows = get_employee_entitlement_rows(employee, as_of_date=date(2027, 12, 31), limit=100)
        rows_by_year = {row["working_year_number"]: row for row in rows}
        first_period = VacationEntitlementPeriod.objects.get(employee=employee, working_year_number=1)

        self.assertEqual(first_period.must_use_by, date(2027, 1, 3))
        self.assertEqual(rows_by_year[1]["used_days"], 49)
        self.assertEqual(rows_by_year[1]["remaining_days"], 3)
        self.assertFalse(
            VacationEntitlementAllocation.objects.filter(
                entitlement_period=first_period,
                schedule_item=late_item,
            ).exists()
        )

    def test_entitlement_rows_hide_empty_future_years(self):
        employee = Employees.objects.create(
            last_name="Будущев",
            first_name="Олег",
            middle_name="Иванович",
            login="hidden-empty-future-year",
            position="Инженер",
            department=self.engineering,
            date_joined=date(2025, 7, 7),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=employee,
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 3),
            vacation_type="paid",
            chargeable_days=3,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        rows = get_employee_entitlement_rows(employee, as_of_date=date(2026, 4, 30), limit=6)

        self.assertIn("07.07.2025 - 06.07.2026", [row["period_label"] for row in rows])
        self.assertNotIn("07.07.2026 - 06.07.2027", [row["period_label"] for row in rows])

    def test_entitlement_rows_keep_future_years_with_reserved_days(self):
        employee = Employees.objects.create(
            last_name="Резервов",
            first_name="Олег",
            middle_name="Иванович",
            login="visible-reserved-future-year",
            position="Инженер",
            department=self.engineering,
            date_joined=date(2025, 7, 7),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=employee,
            start_date=date(2026, 5, 1),
            end_date=date(2026, 6, 21),
            vacation_type="paid",
            chargeable_days=52,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=employee,
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 3),
            vacation_type="paid",
            chargeable_days=3,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        rows = get_employee_entitlement_rows(employee, as_of_date=date(2026, 4, 30), limit=6)
        rows_by_label = {row["period_label"]: row for row in rows}

        self.assertIn("07.07.2026 - 06.07.2027", rows_by_label)
        self.assertEqual(rows_by_label["07.07.2026 - 06.07.2027"]["reserved_days"], 3)

    def test_entitlement_rows_expose_working_year_balances(self):
        rows = get_employee_entitlement_rows(self.employee, self.today)

        self.assertGreaterEqual(len(rows), 2)
        self.assertIn("period_label", rows[0])
        self.assertIn("remaining_days", rows[0])
        self.assertIn("status_label", rows[0])

    def test_entitlement_source_preview_uses_single_working_year(self):
        employee = Employees.objects.create(
            last_name="Источников",
            first_name="Иван",
            middle_name="Игоревич",
            login="single-source-preview",
            position="Инженер",
            department=self.engineering,
            date_joined=date(2025, 9, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=employee,
            start_date=date(2026, 4, 1),
            end_date=date(2026, 5, 22),
            vacation_type="paid",
            chargeable_days=52,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        preview = get_employee_entitlement_source_preview(
            employee,
            date(2026, 11, 13),
            date(2026, 12, 10),
            "paid",
        )

        self.assertEqual(preview["label"], "Дни будут списаны из рабочего года 10.09.2026 - 09.09.2027")
        self.assertEqual(len(preview["allocations"]), 1)
        self.assertEqual(preview["allocations"][0]["period_label"], "10.09.2026 - 09.09.2027")
        self.assertEqual(preview["allocations"][0]["days"], 28)

    def test_entitlement_source_preview_can_span_working_years(self):
        employee = Employees.objects.create(
            last_name="Переходов",
            first_name="Петр",
            middle_name="Игоревич",
            login="split-source-preview",
            position="Инженер",
            department=self.engineering,
            date_joined=date(2025, 9, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=employee,
            start_date=date(2026, 4, 1),
            end_date=date(2026, 5, 19),
            vacation_type="paid",
            chargeable_days=49,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        preview = get_employee_entitlement_source_preview(
            employee,
            date(2026, 11, 5),
            date(2026, 11, 14),
            "paid",
        )

        self.assertEqual(preview["label"], "Дни будут списаны из нескольких рабочих годов")
        self.assertEqual([row["period_label"] for row in preview["allocations"]], [
            "10.09.2025 - 09.09.2026",
            "10.09.2026 - 09.09.2027",
        ])
        self.assertEqual([row["days"] for row in preview["allocations"]], [3, 7])

    def test_entitlement_source_preview_keeps_existing_reservations_for_earlier_new_request(self):
        employee = Employees.objects.create(
            last_name="Резервова",
            first_name="Ирина",
            middle_name="Павловна",
            login="fixed-reservation-preview",
            position="Логист",
            department=self.engineering,
            date_joined=date(2025, 4, 11),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=employee,
            start_date=date(2026, 9, 1),
            end_date=date(2026, 10, 20),
            vacation_type="paid",
            chargeable_days=50,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        preview = get_employee_entitlement_source_preview(
            employee,
            date(2026, 8, 1),
            date(2026, 8, 15),
            "paid",
        )

        self.assertEqual(preview["label"], "Дни будут списаны из нескольких рабочих годов")
        self.assertEqual([row["period_label"] for row in preview["allocations"]], [
            "11.04.2025 - 10.04.2026",
            "11.04.2026 - 10.04.2027",
        ])
        self.assertEqual([row["days"] for row in preview["allocations"]], [2, 13])

    def test_entitlement_source_preview_ignores_non_paid_leave(self):
        preview = get_employee_entitlement_source_preview(
            self.employee,
            date(2026, 11, 1),
            date(2026, 11, 10),
            "unpaid",
        )

        self.assertEqual(preview["label"], "Оплачиваемый баланс не списывается")
        self.assertEqual(preview["allocations"], [])

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
