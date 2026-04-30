from datetime import date

from django.urls import reverse

from apps.accounts.services import sync_employee_user
from apps.employees.models import Employees
from apps.leave.models import VacationRequest, VacationSchedule, VacationScheduleItem
from apps.leave.services.calendar import build_calendar_base_data, build_calendar_rows
from apps.leave.services.requests import approve_vacation_request

from .base import LeaveTestCase


class CalendarTests(LeaveTestCase):
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
        self.assertEqual(entries[0]["detail_url"], reverse("vacation_detail", args=[approved_request.id]))
        self.assertEqual(entries[0]["detail_label"], "Открыть заявку")

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
        self.assertIn("timeline-employee-card__profile-link", payload["board_html"])
        self.assertIn(reverse("employee_profile", args=[self.employee.id]), payload["board_html"])
        self.assertEqual(payload["period_label"], "График отпусков на 2026 год")

        month_response = self.client.get(
            reverse("calendar"),
            {"view": "month", "year": 2026, "month": 7},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(month_response.status_code, 200)
        self.assertEqual(month_response.json()["period_label"], "График отпусков на июль 2026")

    def test_calendar_page_uses_shared_vacation_modal_hooks(self):
        self.client.force_login(self.employee.user)

        response = self.client.get(reverse("calendar"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-modal-open="vacation-modal"')
        self.assertContains(response, 'id="vacation-modal"')
        self.assertContains(response, 'id="chargeable_days"')
        self.assertContains(response, 'id="available_on_start"')
        self.assertContains(response, 'id="entitlement_source_label"')
        self.assertContains(response, 'id="entitlement_source_list"')
        self.assertContains(response, "Источник дней")
        self.assertContains(response, 'id="calendar-charge-preview"')
        self.assertContains(response, 'data-preview-url')
        self.assertContains(response, 'name="reason"')
        self.assertContains(response, "Отправить заявку")
        self.assertContains(response, "Должность")
        self.assertNotContains(response, "Дата права на оплачиваемый отпуск")
        self.assertContains(response, 'data-modal-close')
        self.assertContains(response, 'data-date-field')
        self.assertContains(response, 'id="calendar-filters-form"', count=1)
        self.assertNotContains(response, "calendar-summary-grid")
        self.assertContains(response, 'id="calendar-detail-profile-link"')
        self.assertContains(response, "timeline-employee-card__profile-link")
        self.assertContains(response, 'role="button"')
        self.assertContains(response, reverse("employee_profile", args=[self.employee.id]))

    def test_calendar_page_always_shows_paid_request_option(self):
        newcomer = Employees.objects.create(
            last_name="Фомин",
            first_name="Олег",
            middle_name="Олегович",
            login="newcomer-calendar-paid-visible",
            position="Инженер",
            department=self.engineering,
            date_joined=date(2026, 2, 16),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        sync_employee_user(newcomer, raw_password="newcomer-pass")
        self.client.force_login(newcomer.user)

        response = self.client.get(reverse("calendar"), {"view": "month", "year": 2026, "month": 4})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<option value="paid" selected>Ежегодный оплачиваемый</option>', html=True)
        self.assertContains(response, "Дата права на оплачиваемый отпуск")

    def test_vacation_request_preview_allows_new_hire_paid_leave_after_six_months(self):
        VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        newcomer = Employees.objects.create(
            last_name="Фомин",
            first_name="Олег",
            middle_name="Олегович",
            login="newcomer-preview-allowed",
            position="Инженер",
            department=self.engineering,
            date_joined=date(2026, 2, 16),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        sync_employee_user(newcomer, raw_password="newcomer-preview-pass")
        self.client.force_login(newcomer.user)

        response = self.client.get(
            reverse("vacation_request_preview"),
            {
                "start_date": "2026-10-10",
                "end_date": "2026-10-16",
                "vacation_type": "paid",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["can_submit"])
        self.assertEqual(payload["available_from"], "2026-08-16")
        self.assertGreater(payload["chargeable_days"], 0)
        self.assertEqual(payload["entitlement_source_label"], "Дни будут списаны из рабочего года 16.02.2026 - 15.02.2027")
        self.assertEqual(len(payload["entitlement_allocations"]), 1)
        self.assertEqual(payload["entitlement_allocations"][0]["period_label"], "16.02.2026 - 15.02.2027")
        self.assertAlmostEqual(
            payload["remaining_after_request"],
            payload["available_on_start"] - payload["chargeable_days"],
            places=2,
        )
        self.assertIn("Заявку можно отправить", payload["message"])

    def test_vacation_request_preview_blocks_paid_leave_before_six_months(self):
        VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        newcomer = Employees.objects.create(
            last_name="Фомин",
            first_name="Олег",
            middle_name="Олегович",
            login="newcomer-preview-blocked",
            position="Инженер",
            department=self.engineering,
            date_joined=date(2026, 2, 16),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        sync_employee_user(newcomer, raw_password="newcomer-blocked-pass")
        self.client.force_login(newcomer.user)

        response = self.client.get(
            reverse("vacation_request_preview"),
            {
                "start_date": "2026-06-10",
                "end_date": "2026-06-16",
                "vacation_type": "paid",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["can_submit"])
        self.assertEqual(payload["available_from"], "2026-08-16")
        self.assertIn("Оплачиваемый отпуск доступен с", payload["message"])

    def test_vacation_request_preview_does_not_charge_unpaid_or_study_leave(self):
        self.client.force_login(self.employee.user)

        for vacation_type in ("unpaid", "study"):
            with self.subTest(vacation_type=vacation_type):
                response = self.client.get(
                    reverse("vacation_request_preview"),
                    {
                        "start_date": "2026-08-10",
                        "end_date": "2026-08-12",
                        "vacation_type": vacation_type,
                    },
                )

                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertTrue(payload["can_submit"])
                self.assertEqual(payload["chargeable_days"], 0)
                self.assertEqual(payload["entitlement_source_label"], "Оплачиваемый баланс не списывается")
                self.assertEqual(payload["entitlement_allocations"], [])
                self.assertEqual(payload["available_on_start"], payload["remaining_after_request"])
                self.assertIn("не уменьшает оплачиваемый баланс", payload["message"])

    def test_vacation_request_preview_forbids_authorized_person(self):
        self.client.force_login(self.authorized_person.user)

        response = self.client.get(
            reverse("vacation_request_preview"),
            {
                "start_date": "2026-08-10",
                "end_date": "2026-08-12",
                "vacation_type": "unpaid",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json()["can_submit"])

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
        self.assertEqual(row["profile_url"], reverse("employee_profile", args=[old_employee.id]))
        self.assertEqual(row["selected_schedule_days"], 14)
        self.assertEqual(row["status"], "schedule-approved")
        self.assertEqual(details[str(old_employee.id)]["selected_entries"][0]["status_label"], "График утвержден")
        self.assertEqual(details[str(old_employee.id)]["selected_entries"][0]["source_label"], "Годовой график")
        self.assertEqual(details[str(old_employee.id)]["profile_url"], reverse("employee_profile", args=[old_employee.id]))

    def test_calendar_hides_employees_not_hired_by_selected_year_end(self):
        employees, _, _ = build_calendar_base_data(2015)

        self.assertNotIn(self.employee.id, [employee.id for employee in employees])

    def test_calendar_rows_include_rejected_requests_in_month_and_year_views(self):
        rejected_request = VacationRequest.objects.create(
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
        self.assertEqual(
            month_details[str(self.employee.id)]["selected_entries"][0]["detail_url"],
            reverse("vacation_detail", args=[rejected_request.id]),
        )

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
