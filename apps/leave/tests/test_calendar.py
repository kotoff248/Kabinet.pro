from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from apps.accounts.services import sync_employee_user
from apps.employees.models import DepartmentCoverageRule, Departments, EmployeePosition, Employees, ProductionGroup, ProductionGroupSubstitutionRule
from apps.leave.models import DepartmentStaffingRule, VacationRequest, VacationSchedule, VacationScheduleItem
from apps.leave.services.calendar import (
    build_calendar_base_data,
    build_calendar_month_totals,
    build_calendar_rows,
    build_employee_schedule_status_map,
)
from apps.leave.ml.scoring import CandidateScoringResult
from apps.leave.services.requests import approve_vacation_request
from apps.leave.services.schedule_changes import approve_schedule_change_request, create_schedule_change_request
from apps.leave.services.staffing import format_staff_absence, format_staff_count

from .base import LeaveTestCase


class CalendarTests(LeaveTestCase):
    def test_date_picker_periods_returns_current_employee_busy_periods(self):
        schedule = VacationSchedule.objects.create(year=2027, status=VacationSchedule.STATUS_DRAFT)
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 3, 1),
            end_date=date(2027, 3, 7),
            vacation_type="paid",
            chargeable_days=7,
            status=VacationScheduleItem.STATUS_DRAFT,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 5, 1),
            end_date=date(2027, 5, 7),
            vacation_type="paid",
            chargeable_days=7,
            status=VacationScheduleItem.STATUS_PLANNED,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 7, 1),
            end_date=date(2027, 7, 7),
            vacation_type="paid",
            chargeable_days=7,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 9, 1),
            end_date=date(2027, 9, 7),
            vacation_type="paid",
            chargeable_days=7,
            status=VacationScheduleItem.STATUS_CANCELLED,
        )
        VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2027, 11, 1),
            end_date=date(2027, 11, 3),
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2027, 12, 1),
            end_date=date(2027, 12, 3),
            vacation_type="unpaid",
            status=VacationRequest.STATUS_REJECTED,
        )

        self.client.force_login(self.employee.user)
        response = self.client.get(reverse("calendar_date_picker_periods"), {"year": 2027})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        periods = {
            (period["source_kind"], period["status"], period["start_date"], period["end_date"])
            for period in payload["periods"]
        }
        self.assertIn(("schedule", VacationScheduleItem.STATUS_DRAFT, "2027-03-01", "2027-03-07"), periods)
        self.assertIn(("schedule", VacationScheduleItem.STATUS_PLANNED, "2027-05-01", "2027-05-07"), periods)
        self.assertIn(("schedule", VacationScheduleItem.STATUS_APPROVED, "2027-07-01", "2027-07-07"), periods)
        self.assertIn(("request", VacationRequest.STATUS_PENDING, "2027-11-01", "2027-11-03"), periods)
        self.assertNotIn(("schedule", VacationScheduleItem.STATUS_CANCELLED, "2027-09-01", "2027-09-07"), periods)
        self.assertNotIn(("request", VacationRequest.STATUS_REJECTED, "2027-12-01", "2027-12-03"), periods)
        self.assertTrue(payload["holiday_dates"])

    def test_date_picker_periods_rejects_inaccessible_employee(self):
        self.client.force_login(self.department_head.user)

        response = self.client.get(
            reverse("calendar_date_picker_periods"),
            {"year": 2027, "employee_id": self.outsider.id},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["periods"], [])

    def test_date_picker_periods_excludes_source_schedule_item(self):
        schedule = VacationSchedule.objects.create(year=2027, status=VacationSchedule.STATUS_APPROVED)
        source_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 4, 1),
            end_date=date(2027, 4, 10),
            vacation_type="paid",
            chargeable_days=10,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        remaining_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 8, 1),
            end_date=date(2027, 8, 10),
            vacation_type="paid",
            chargeable_days=10,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        self.client.force_login(self.employee.user)
        response = self.client.get(
            reverse("calendar_date_picker_periods"),
            {
                "year": 2027,
                "employee_id": self.employee.id,
                "exclude_schedule_item": source_item.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        source_ids = {(period["start_date"], period["end_date"]) for period in response.json()["periods"]}
        self.assertNotIn(("2027-04-01", "2027-04-10"), source_ids)
        self.assertIn((remaining_item.start_date.isoformat(), remaining_item.end_date.isoformat()), source_ids)

    def test_date_picker_periods_ignores_invalid_parameters(self):
        self.client.force_login(self.employee.user)

        response = self.client.get(
            reverse("calendar_date_picker_periods"),
            {
                "year": "not-a-year",
                "employee_id": "bad-id",
                "exclude_schedule_item": "bad-item",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["periods"], [])
        self.assertIn("holiday_dates", payload)

    def test_staff_count_label_uses_russian_plural_rules(self):
        self.assertEqual(format_staff_count(1), "1 сотрудник")
        self.assertEqual(format_staff_count(2), "2 сотрудника")
        self.assertEqual(format_staff_count(4), "4 сотрудника")
        self.assertEqual(format_staff_count(5), "5 сотрудников")
        self.assertEqual(format_staff_count(11), "11 сотрудников")
        self.assertEqual(format_staff_count(14), "14 сотрудников")
        self.assertEqual(format_staff_count(21), "21 сотрудник")
        self.assertEqual(format_staff_count(22), "22 сотрудника")
        self.assertEqual(format_staff_count(25), "25 сотрудников")

    def test_staff_absence_label_uses_singular_and_plural_verbs(self):
        self.assertEqual(format_staff_absence(1), "отсутствует 1 сотрудник")
        self.assertEqual(format_staff_absence(2), "отсутствуют 2 сотрудника")
        self.assertEqual(format_staff_absence(11), "отсутствуют 11 сотрудников")
        self.assertEqual(format_staff_absence(21), "отсутствует 21 сотрудник")
        self.assertEqual(format_staff_absence(22), "отсутствуют 22 сотрудника")
        self.assertEqual(format_staff_absence(1, tense="future"), "будет отсутствовать 1 сотрудник")
        self.assertEqual(format_staff_absence(2, tense="future"), "будут отсутствовать 2 сотрудника")
        self.assertEqual(format_staff_absence(1, tense="past"), "отсутствовал 1 сотрудник")
        self.assertEqual(format_staff_absence(2, tense="past"), "отсутствовали 2 сотрудника")

    def test_calendar_month_total_issue_labels_use_russian_plural_forms(self):
        def month_cells(*, has_high_risk=False, has_conflict=False):
            return [
                {
                    "busy_days": 1,
                    "has_high_risk": has_high_risk,
                    "has_conflict": has_conflict,
                },
                *({"busy_days": 0, "has_high_risk": False, "has_conflict": False} for _ in range(11)),
            ]

        cases = (
            (1, "1 запись"),
            (2, "2 записи"),
            (4, "4 записи"),
            (5, "5 записей"),
            (11, "11 записей"),
            (21, "21 запись"),
            (24, "24 записи"),
        )

        for value, expected in cases:
            with self.subTest(value=value):
                rows = [
                    {"cells": month_cells(has_high_risk=True, has_conflict=True)}
                    for _ in range(value)
                ]
                total = build_calendar_month_totals(rows)[0]
                self.assertEqual(total["risk_count_label"], expected)
                self.assertEqual(total["conflict_count_label"], expected)

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

    def test_calendar_schedule_item_created_from_transfer_links_to_transfer_detail(self):
        schedule = VacationSchedule.objects.create(
            year=2027,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 7, 1),
            end_date=date(2027, 7, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.employee,
            new_start_date=date(2027, 8, 1),
            new_end_date=date(2027, 8, 14),
        )
        replacement_item = approve_schedule_change_request(change_request.id, reviewer=self.department_head)

        _, _, employee_entries = build_calendar_base_data(2027, employee_ids=[self.employee.id])
        replacement_entry = next(
            entry
            for entry in employee_entries[self.employee.id]
            if entry["source_id"] == replacement_item.id
        )

        self.assertEqual(replacement_entry["source_label"], "Перенос")
        self.assertEqual(replacement_entry["detail_url"], reverse("schedule_change_detail", args=[change_request.id]))
        self.assertEqual(replacement_entry["detail_label"], "Открыть перенос")

    def test_calendar_transferred_schedule_item_links_to_transfer_detail(self):
        schedule = VacationSchedule.objects.create(
            year=2027,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 7, 1),
            end_date=date(2027, 7, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.employee,
            new_start_date=date(2027, 8, 1),
            new_end_date=date(2027, 8, 14),
        )
        approve_schedule_change_request(change_request.id, reviewer=self.department_head)

        _, _, employee_entries = build_calendar_base_data(2027, employee_ids=[self.employee.id])
        transferred_entry = next(
            entry
            for entry in employee_entries[self.employee.id]
            if entry["source_id"] == schedule_item.id
        )

        self.assertEqual(transferred_entry["status_label"], "Перенесено")
        self.assertEqual(transferred_entry["detail_url"], reverse("schedule_change_detail", args=[change_request.id]))
        self.assertEqual(transferred_entry["detail_label"], "Открыть перенос")

    def test_calendar_plain_year_schedule_item_has_no_detail_reference(self):
        schedule = VacationSchedule.objects.create(
            year=2027,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 6, 1),
            end_date=date(2027, 6, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        _, _, employee_entries = build_calendar_base_data(2027, employee_ids=[self.employee.id])
        entry = next(
            entry
            for entry in employee_entries[self.employee.id]
            if entry["source_id"] == schedule_item.id
        )

        self.assertEqual(entry["detail_url"], "")
        self.assertEqual(entry["detail_label"], "")

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
        self.assertIn("calendar_month_details", payload)
        self.assertIn("year-board", payload["board_html"])
        self.assertNotIn("calendar-board-card", payload["board_html"])
        self.assertNotIn('id="calendar-filters-form"', payload["board_html"])
        self.assertNotIn("calendar-summary-grid", payload["board_html"])
        self.assertEqual(payload["calendar_details"], {})
        self.assertIn("timeline-employee-card__role", payload["board_html"])
        self.assertIn("timeline-employee-card__role--employee", payload["board_html"])
        self.assertNotIn("timeline-employee-card__profile-link", payload["board_html"])
        self.assertIn("timeline-employee-card__org-item--department", payload["board_html"])
        self.assertIn("timeline-employee-card__org-item--group", payload["board_html"])
        self.assertIn(self.employee.department.name, payload["board_html"])
        self.assertIn(self.engineering_group.name, payload["board_html"])
        self.assertNotIn("Отдел:", payload["board_html"])
        self.assertNotIn("Группа:", payload["board_html"])
        self.assertNotIn("timeline-employee-card__meta", payload["board_html"])
        self.assertNotIn(self.employee.position, payload["board_html"])
        self.assertIn(reverse("employee_profile", args=[self.employee.id]), payload["board_html"])
        self.assertEqual(payload["period_label"], "График отпусков на 2026 год")
        self.assertEqual(payload["calendar_month_details"], {})

        detail_response = self.client.get(
            reverse("calendar"),
            {"view": "year", "year": 2026, "calendar_detail_employee": self.employee.id},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(
            detail_response.json()["calendar_detail"]["employee_name"],
            self.employee.full_name,
        )

        month_detail_response = self.client.get(
            reverse("calendar"),
            {"view": "year", "year": 2026, "calendar_detail_month": 7},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(month_detail_response.status_code, 200)
        self.assertEqual(month_detail_response.json()["calendar_month_detail"]["month_number"], 7)

        month_response = self.client.get(
            reverse("calendar"),
            {"view": "month", "year": 2026, "month": 7},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(month_response.status_code, 200)
        month_payload = month_response.json()
        self.assertEqual(month_payload["period_label"], "График отпусков на июль 2026")
        self.assertIn("timeline-employee-card__org-item--department", month_payload["board_html"])
        self.assertIn("timeline-employee-card__org-item--group", month_payload["board_html"])
        self.assertIn(self.employee.department.name, month_payload["board_html"])
        self.assertIn(self.engineering_group.name, month_payload["board_html"])
        self.assertNotIn("Отдел:", month_payload["board_html"])
        self.assertNotIn("Группа:", month_payload["board_html"])
        self.assertNotIn("timeline-employee-card__meta", month_payload["board_html"])
        self.assertNotIn(self.employee.position, month_payload["board_html"])
        self.assertEqual(month_payload["calendar_month_details"], {})

    def test_calendar_page_uses_shared_vacation_modal_hooks(self):
        self.client.force_login(self.employee.user)

        response = self.client.get(reverse("calendar"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["calendar_view_mode"], "year")
        self.assertContains(response, 'data-segmented-index="0"')
        self.assertLess(
            response.content.decode().index('value="year"'),
            response.content.decode().index('value="month"'),
        )
        self.assertContains(response, 'name="view" value="year" checked')
        self.assertContains(response, 'class="calendar-select__native" disabled')
        self.assertContains(response, "year-board")
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
        page_html = response.content.decode()
        self.assertContains(response, "calendar-drawer__employee-card")
        self.assertContains(response, "calendar-drawer__stats calendar-drawer__stats--hero")
        self.assertLess(
            page_html.index("calendar-drawer__employee-card"),
            page_html.index("calendar-drawer__stats calendar-drawer__stats--hero"),
        )
        self.assertLess(
            page_html.index("calendar-drawer__stats calendar-drawer__stats--hero"),
            page_html.index('class="calendar-drawer__body"'),
        )
        self.assertContains(response, 'id="calendar-detail-profile-link"')
        self.assertContains(response, "calendar-drawer__profile-link--employee")
        self.assertContains(response, 'id="calendar-detail-position"')
        self.assertContains(response, 'id="calendar-detail-department"')
        self.assertContains(response, 'id="calendar-detail-group"')
        self.assertContains(response, 'id="calendar-detail-management-badges"')
        self.assertNotContains(response, ">Открыть профиль</span>")
        self.assertContains(response, "timeline-employee-card__role")
        self.assertContains(response, "timeline-employee-card__role--employee")
        self.assertContains(response, 'role="button"')
        self.assertContains(response, reverse("employee_profile", args=[self.employee.id]))

    def test_calendar_opened_from_profile_keeps_profile_navigation_and_back_link(self):
        self.client.force_login(self.employee.user)

        response = self.client.get(
            reverse("calendar"),
            {
                "from": "profile",
                "back_url": reverse("main"),
                "back_label": "К профилю",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "profile")
        self.assertEqual(response.context["page_header_back_link"]["label"], "К профилю")
        self.assertEqual(response.context["page_header_back_link"]["url"], reverse("main"))
        self.assertContains(response, "К профилю")
        self.assertContains(response, "calendar-page__header--with-back")
        self.assertContains(
            response,
            'data-sidebar-key="profile" aria-label="Профиль" title="Профиль" aria-current="page"',
        )
        self.assertNotContains(
            response,
            'data-sidebar-key="calendar" aria-label="График" title="График" aria-current="page"',
        )

    def test_calendar_opened_from_employee_profile_keeps_employees_navigation_and_back_link(self):
        self.client.force_login(self.hr_employee.user)
        profile_url = f"{reverse('employee_profile', args=[self.employee.id])}?from=employees"

        response = self.client.get(
            reverse("calendar"),
            {
                "from": "employees",
                "back_url": profile_url,
                "back_label": "К сотруднику",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "employees")
        self.assertEqual(response.context["page_header_back_link"]["label"], "К сотруднику")
        self.assertEqual(response.context["page_header_back_link"]["url"], profile_url)
        self.assertContains(response, "К сотруднику")
        self.assertContains(response, "calendar-page__header--with-back")
        self.assertContains(
            response,
            'data-sidebar-key="employees" aria-label="Сотрудники" title="Сотрудники" aria-current="page"',
        )
        self.assertNotContains(
            response,
            'data-sidebar-key="calendar" aria-label="График" title="График" aria-current="page"',
        )

    def test_calendar_opened_from_applications_keeps_applications_navigation_and_back_link(self):
        self.client.force_login(self.hr_employee.user)
        application_url = reverse("applications")

        response = self.client.get(
            reverse("calendar"),
            {
                "from": "applications",
                "back_url": application_url,
                "back_label": "К заявкам",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "applications")
        self.assertEqual(response.context["page_header_back_link"]["label"], "К заявкам")
        self.assertEqual(response.context["page_header_back_link"]["url"], application_url)
        self.assertContains(response, "К заявкам")
        self.assertContains(
            response,
            'data-sidebar-key="applications" aria-label="Заявки" title="Заявки" aria-current="page"',
        )
        self.assertNotContains(
            response,
            'data-sidebar-key="calendar" aria-label="График" title="График" aria-current="page"',
        )

    def test_calendar_opened_from_analytics_keeps_analytics_navigation(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("calendar"), {"from": "analytics"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "analytics")
        self.assertContains(
            response,
            'data-sidebar-key="analytics" aria-label="Аналитика" title="Аналитика" aria-current="page"',
        )
        self.assertNotContains(
            response,
            'data-sidebar-key="calendar" aria-label="График" title="График" aria-current="page"',
        )

    def test_calendar_detail_drawer_request_links_have_modal_return_hooks(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 9, 8),
            end_date=date(2026, 9, 12),
            vacation_type="study",
            status=VacationRequest.STATUS_APPROVED,
        )
        detail_url = reverse("vacation_detail", args=[request_obj.id])
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("calendar"),
            {
                "view": "year",
                "year": 2026,
                "employee": self.employee.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, detail_url)
        self.assertContains(response, "data-calendar-detail-return")
        self.assertContains(response, f'data-calendar-detail-return-url="{detail_url}"')

    def test_calendar_detail_drawer_shows_management_role_badge(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("calendar"), {"employee": self.department_head.id})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "calendar-drawer__employee-card")
        self.assertContains(response, "calendar-drawer__profile-link--department-head")
        self.assertContains(response, "calendar-drawer__employee-badge--department-head")
        self.assertContains(response, "Руководитель отдела")
        self.assertContains(response, self.department_head.position)
        self.assertContains(response, self.department_head.department.name)
        self.assertContains(response, self.engineering_leadership_group.name)

    def test_calendar_rows_and_drawer_mark_new_hires(self):
        self.employee.date_joined = timezone.localdate()
        self.employee.save(update_fields=["date_joined"])
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("calendar"),
            {
                "view": "year",
                "year": timezone.localdate().year,
                "employee": self.employee.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        detail = response.context["calendar_details"][str(self.employee.id)]
        self.assertEqual(detail["employee_new_hire_badge"]["label"], "Новичок")
        self.assertContains(response, 'class="new-hire-badge"')
        self.assertContains(response, "person_add")
        self.assertContains(response, "Работает меньше 6 месяцев")

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
        self.assertIn("risk_explanation", payload)
        self.assertIn("risk_short_reason", payload)
        self.assertIn("risk_recommended_action", payload)
        self.assertAlmostEqual(
            payload["remaining_after_request"],
            payload["available_on_start"] - payload["chargeable_days"],
            places=2,
        )
        self.assertIn("Заявку можно отправить", payload["message"])

    def test_vacation_request_preview_includes_ai_module_fields_for_all_types(self):
        VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        self.client.force_login(self.employee.user)

        for vacation_type in ("paid", "unpaid", "study"):
            with self.subTest(vacation_type=vacation_type):
                response = self.client.get(
                    reverse("vacation_request_preview"),
                    {
                        "start_date": "2026-09-01",
                        "end_date": "2026-09-07",
                        "vacation_type": vacation_type,
                    },
                )

                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertIn("module_score", payload)
                self.assertIn("module_confidence", payload)
                self.assertIn("module_recommendation", payload)
                self.assertIn("module_explanation", payload)
                self.assertIn("module_model_version", payload)
                self.assertIn("module_scorer_kind", payload)
                self.assertIn("module_alternatives", payload)

    def test_vacation_request_preview_keeps_blocked_request_unsubmittable_with_ai_score(self):
        VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        newcomer = Employees.objects.create(
            last_name="Фомин",
            first_name="Олег",
            middle_name="Олегович",
            login="newcomer-ai-blocked",
            position="Инженер",
            department=self.engineering,
            date_joined=date(2026, 2, 16),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        sync_employee_user(newcomer, raw_password="newcomer-ai-blocked-pass")
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
        self.assertEqual(payload["module_recommendation"], "blocked")
        self.assertEqual(payload["module_score"], 0)

    def test_vacation_request_preview_returns_ranked_ai_alternatives(self):
        def fake_score(features, *, passed_hard_rules=True, use_neural=True):
            score = Decimal(str(features["period_start_day_of_year"] % 100))
            return CandidateScoringResult(
                score=score,
                confidence=Decimal("80.00"),
                recommendation="prefer" if score >= Decimal("80.00") else "normal",
                explanation=f"Тестовая оценка {score}%.",
                model_version="test-ai",
                scorer_kind="test",
            )

        self.client.force_login(self.employee.user)
        with patch("apps.leave.ml.request_support.score_candidate_features", side_effect=fake_score):
            response = self.client.get(
                reverse("vacation_request_preview"),
                {
                    "start_date": "2026-05-01",
                    "end_date": "2026-05-05",
                    "vacation_type": "unpaid",
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        alternatives = payload["module_alternatives"]
        self.assertEqual(len(alternatives), 2)
        self.assertNotEqual(alternatives[0]["start_date"], "2026-05-01")
        self.assertGreaterEqual(alternatives[0]["module_score"], alternatives[1]["module_score"])

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

    def test_vacation_request_preview_keeps_existing_paid_reservations_for_non_paid_leave(self):
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 28),
            vacation_type="paid",
            chargeable_days=20,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        self.client.force_login(self.employee.user)

        response = self.client.get(
            reverse("vacation_request_preview"),
            {
                "start_date": "2026-08-10",
                "end_date": "2026-08-12",
                "vacation_type": "unpaid",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["can_submit"])
        self.assertEqual(payload["chargeable_days"], 0)
        self.assertEqual(payload["available_on_start"], payload["remaining_after_request"])

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

    def test_calendar_filters_by_department_and_search(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("calendar"),
            {
                "view": "year",
                "year": 2026,
                "department": self.engineering.id,
                "search": self.employee.first_name,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["calendar_filters"]["selected_department"], str(self.engineering.id))
        self.assertEqual(response.context["calendar_filters"]["search_query"], self.employee.first_name)
        self.assertContains(response, self.employee.full_name)
        self.assertNotContains(response, self.outsider.full_name)

    def test_calendar_invalid_department_filter_resets_to_all(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("calendar"), {"view": "year", "year": 2026, "department": 999999})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["calendar_filters"]["selected_department"], "all")
        self.assertContains(response, self.employee.full_name)
        self.assertContains(response, self.outsider.full_name)

    def test_calendar_group_filter_shows_only_selected_group(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("calendar"),
            {
                "view": "year",
                "year": 2026,
                "group": self.engineering_group.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["calendar_filters"]["selected_group"], str(self.engineering_group.id))
        row_names = {row["employee_name"] for row in response.context["calendar_rows"]}
        self.assertIn(self.employee.full_name, row_names)
        self.assertNotIn(self.department_head.full_name, row_names)
        self.assertNotIn(self.outsider.full_name, row_names)
        self.assertTrue(
            all(row["production_group_id"] == self.engineering_group.id for row in response.context["calendar_rows"])
        )

    def test_calendar_group_filter_resets_unavailable_group_for_department(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("calendar"),
            {
                "view": "year",
                "year": 2026,
                "department": self.engineering.id,
                "group": self.hr_group.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["calendar_filters"]["selected_department"], str(self.engineering.id))
        self.assertEqual(response.context["calendar_filters"]["selected_group"], "all")
        hr_group_option = next(
            group
            for group in response.context["calendar_filters"]["group_options"]
            if group.id == self.hr_group.id
        )
        self.assertFalse(hr_group_option.is_available_for_selected_department)
        self.assertContains(response, self.employee.full_name)
        self.assertNotContains(response, self.outsider.full_name)

    def test_calendar_sort_mode_switches_between_org_and_alpha(self):
        alpha_department = Departments.objects.create(name="Альфа отдел")
        alpha_group = ProductionGroup.objects.create(department=alpha_department, name="Монтаж")
        alpha_position = EmployeePosition.objects.create(
            department=alpha_department,
            production_group=alpha_group,
            title="Инженер",
        )
        yakovlev = Employees.objects.create(
            last_name="Яковлев",
            first_name="Павел",
            middle_name="Игоревич",
            login="calendar-sort-yakovlev",
            position="Инженер",
            employee_position=alpha_position,
            department=alpha_department,
            date_joined=date(2024, 1, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        omega_department = Departments.objects.create(name="Янтарный отдел")
        omega_group = ProductionGroup.objects.create(department=omega_department, name="Сервис")
        omega_position = EmployeePosition.objects.create(
            department=omega_department,
            production_group=omega_group,
            title="Инженер",
        )
        ageev = Employees.objects.create(
            last_name="Агеев",
            first_name="Роман",
            middle_name="Олегович",
            login="calendar-sort-ageev",
            position="Инженер",
            employee_position=omega_position,
            department=omega_department,
            date_joined=date(2024, 1, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        self.client.force_login(self.hr_employee.user)

        alpha_response = self.client.get(
            reverse("calendar"),
            {
                "view": "year",
                "year": 2026,
                "sort": "alpha",
                "employee_scope": f"{yakovlev.id},{ageev.id}",
            },
        )
        org_response = self.client.get(
            reverse("calendar"),
            {
                "view": "year",
                "year": 2026,
                "sort": "org",
                "employee_scope": f"{yakovlev.id},{ageev.id}",
            },
        )

        self.assertEqual(alpha_response.status_code, 200)
        self.assertEqual(org_response.status_code, 200)
        self.assertEqual(alpha_response.context["calendar_filters"]["selected_sort"], "alpha")
        self.assertEqual(org_response.context["calendar_filters"]["selected_sort"], "org")
        self.assertEqual(
            [row["employee_name"] for row in alpha_response.context["calendar_rows"]],
            [ageev.full_name, yakovlev.full_name],
        )
        self.assertEqual(alpha_response.context["calendar_row_groups"], [])
        self.assertNotContains(alpha_response, "data-calendar-collapse-toggle")
        self.assertEqual(
            [row["employee_name"] for row in org_response.context["calendar_rows"]],
            [yakovlev.full_name, ageev.full_name],
        )
        self.assertContains(org_response, "data-calendar-collapse-toggle")
        self.assertEqual([group["name"] for group in org_response.context["calendar_row_groups"]], ["Альфа отдел", "Янтарный отдел"])
        self.assertEqual(org_response.context["calendar_row_groups"][0]["groups"][0]["rows"][0]["employee_id"], yakovlev.id)

    def test_calendar_issue_filter_shows_only_high_risk_rows(self):
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 10),
            vacation_type="paid",
            chargeable_days=10,
            status=VacationScheduleItem.STATUS_APPROVED,
            risk_level=VacationScheduleItem.RISK_HIGH,
            risk_score=88,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.outsider,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 10),
            vacation_type="paid",
            chargeable_days=10,
            status=VacationScheduleItem.STATUS_APPROVED,
            risk_level=VacationScheduleItem.RISK_LOW,
            risk_score=10,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("calendar"), {"view": "year", "year": 2026, "issue": "risk"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["calendar_filters"]["selected_issue"], "risk")
        self.assertContains(response, self.employee.full_name)
        self.assertContains(response, "Высокий риск: 88%")
        self.assertContains(response, "year-month-total--issue-risk", count=1)
        self.assertContains(response, "year-month-card--issue-risk", count=1)
        self.assertContains(response, 'data-tooltip-title="Сводка месяца')
        self.assertContains(response, 'data-tooltip-title="Высокий риск"')
        self.assertNotContains(response, 'data-tooltip-text="Отпусков и отсутствий:')
        self.assertNotContains(response, "year-month-card--issue-conflict")
        self.assertNotContains(response, self.outsider.full_name)
        detail = response.context["calendar_details"][str(self.employee.id)]
        self.assertTrue(detail["has_high_risk"])
        self.assertFalse(detail["has_conflict"])
        self.assertEqual(detail["issue_label"], "Высокий риск")
        self.assertEqual(detail["risk_details"]["status"], "risk")
        self.assertEqual(detail["risk_details"]["label"], "Высокий риск")
        self.assertTrue(
            any("88%" in problem["text"] for problem in detail["risk_details"]["problems"])
        )
        self.assertEqual(detail["selected_entries"][0]["risk_score"], 88)
        self.assertEqual(detail["selected_entries"][0]["risk_label"], "Высокий")
        self.assertTrue(detail["selected_entries"][0]["has_high_risk"])
        self.assertEqual(detail["selected_entries"][0]["anchor"]["employee_id"], self.employee.id)

        june_response = self.client.get(
            reverse("calendar"),
            {"view": "month", "year": 2026, "month": 6, "issue": "risk"},
        )
        self.assertEqual(june_response.status_code, 200)
        self.assertContains(june_response, self.employee.full_name)
        self.assertContains(june_response, "timeline-head--issue-risk", count=10)
        self.assertContains(june_response, "timeline-day--issue-risk", count=10)
        self.assertContains(june_response, 'data-tooltip-title="Высокий риск дня"')
        self.assertContains(june_response, 'data-tooltip-title="Высокий риск"')
        self.assertNotContains(june_response, 'data-tooltip-title="Свободный день"')
        self.assertNotContains(june_response, 'data-tooltip-title="Запись графика"')
        self.assertContains(june_response, "Есть высокий риск")
        self.assertNotContains(june_response, "timeline-day--issue-conflict")
        self.assertTrue(june_response.context["month_day_headers"][0]["has_high_risk"])
        self.assertFalse(june_response.context["month_day_headers"][0]["has_conflict"])

        month_response = self.client.get(
            reverse("calendar"),
            {"view": "month", "year": 2026, "month": 4, "issue": "risk"},
        )
        self.assertNotContains(month_response, self.employee.full_name)

    def test_calendar_draft_schedule_item_high_risk_is_calendar_issue(self):
        schedule = VacationSchedule.objects.create(
            year=2027,
            status=VacationSchedule.STATUS_DRAFT,
            created_by=self.hr_employee,
        )
        draft_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 6, 1),
            end_date=date(2027, 6, 10),
            vacation_type="paid",
            chargeable_days=10,
            status=VacationScheduleItem.STATUS_DRAFT,
            risk_level=VacationScheduleItem.RISK_HIGH,
            risk_score=88,
        )
        self.assertNotIn(VacationScheduleItem.STATUS_DRAFT, VacationScheduleItem.ACTIVE_STATUSES)
        self.assertNotIn(VacationScheduleItem.STATUS_DRAFT, VacationScheduleItem.BALANCE_STATUSES)

        employees, employee_day_status, employee_entries = build_calendar_base_data(
            2027,
            employee_ids=[self.employee.id],
        )
        self.assertFalse(employee_entries[self.employee.id][0]["is_active_absence"])
        rows, details = build_calendar_rows(
            employees,
            employee_day_status,
            employee_entries,
            year=2027,
            month=6,
            view_mode="year",
            today=date(2027, 1, 1),
        )

        row = rows[0]
        detail = details[str(self.employee.id)]
        june_cell = row["cells"][5]
        self.assertTrue(row["has_high_risk"])
        self.assertFalse(row["has_conflict"])
        self.assertTrue(june_cell["has_high_risk"])
        self.assertFalse(june_cell["has_conflict"])
        self.assertEqual(detail["risk_details"]["status"], "risk")
        self.assertEqual(detail["primary_entries"][0]["source_id"], draft_item.id)
        self.assertTrue(detail["primary_entries"][0]["has_high_risk"])
        self.assertFalse(detail["primary_entries"][0]["has_conflict"])
        self.assertFalse(detail["primary_entries"][0]["can_request_transfer"])

        self.client.force_login(self.hr_employee.user)
        response = self.client.get(reverse("calendar"), {"view": "year", "year": 2027, "issue": "risk"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.employee.full_name)
        self.assertContains(response, "Высокий риск: 88%")
        self.assertContains(response, "year-month-card--issue-risk")
        self.assertNotContains(response, "year-month-card--issue-conflict")
        self.assertNotContains(response, self.outsider.full_name)

    def test_calendar_draft_schedule_items_create_live_staffing_conflict(self):
        DepartmentStaffingRule.objects.create(
            department=self.engineering,
            min_staff_required=0,
            max_absent=10,
            criticality_level=3,
        )
        DepartmentCoverageRule.objects.create(
            department=self.engineering,
            production_group=self.engineering_group,
            min_staff_required=2,
            max_absent=1,
            criticality_level=5,
        )
        coworker = Employees.objects.create(
            last_name="Черновиков",
            first_name="Артем",
            middle_name="Иванович",
            login="calendar-draft-conflict-coworker",
            position="Инженер",
            employee_position=self.engineering_engineer_position,
            department=self.engineering,
            date_joined=date(2024, 1, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        schedule = VacationSchedule.objects.create(
            year=2027,
            status=VacationSchedule.STATUS_DRAFT,
            created_by=self.hr_employee,
        )
        for employee in (self.employee, coworker):
            VacationScheduleItem.objects.create(
                schedule=schedule,
                employee=employee,
                start_date=date(2027, 7, 22),
                end_date=date(2027, 7, 23),
                vacation_type="paid",
                chargeable_days=2,
                status=VacationScheduleItem.STATUS_DRAFT,
                risk_level=VacationScheduleItem.RISK_LOW,
            )

        self.client.force_login(self.hr_employee.user)
        response = self.client.get(reverse("calendar"), {"view": "month", "year": 2027, "month": 7, "issue": "conflict"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.employee.full_name)
        self.assertContains(response, coworker.full_name)
        self.assertContains(response, "timeline-day--issue-conflict")
        self.assertContains(response, "timeline-head--issue-conflict", count=2)
        self.assertNotContains(response, self.outsider.full_name)

        employee_detail = response.context["calendar_details"][str(self.employee.id)]
        coworker_detail = response.context["calendar_details"][str(coworker.id)]
        self.assertTrue(employee_detail["has_conflict"])
        self.assertTrue(coworker_detail["has_conflict"])
        self.assertFalse(employee_detail["has_high_risk"])
        self.assertEqual(employee_detail["risk_details"]["status"], "conflict")
        self.assertTrue(employee_detail["primary_entries"][0]["has_conflict"])
        self.assertIn("будут отсутствовать 2", employee_detail["conflict_summary"])
        self.assertTrue(response.context["month_day_headers"][21]["has_conflict"])

        status_map = build_employee_schedule_status_map([self.employee.id, coworker.id], year=2027)
        self.assertEqual(status_map[self.employee.id]["key"], "conflict")
        self.assertEqual(status_map[coworker.id]["key"], "conflict")

        employees, _, employee_entries = build_calendar_base_data(
            2027,
            employee_ids=[self.employee.id, coworker.id],
        )
        self.assertTrue(employees)
        self.assertFalse(employee_entries[self.employee.id][0]["is_active_absence"])
        self.assertFalse(employee_entries[coworker.id][0]["is_active_absence"])

    def test_calendar_month_drawer_splits_selected_month_and_other_year_entries(self):
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        may_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 5, 10),
            end_date=date(2026, 5, 14),
            vacation_type="paid",
            chargeable_days=5,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        july_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 7),
            vacation_type="paid",
            chargeable_days=7,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        employees, employee_day_status, employee_entries = build_calendar_base_data(
            2026,
            employee_ids=[self.employee.id],
        )
        _, details = build_calendar_rows(
            employees,
            employee_day_status,
            employee_entries,
            year=2026,
            month=5,
            view_mode="month",
            today=date(2026, 5, 1),
        )

        detail = details[str(self.employee.id)]
        self.assertEqual(detail["view_mode"], "month")
        self.assertFalse(detail["is_year_view"])
        self.assertEqual(detail["primary_entries_title"], "Отпуска в выбранном месяце")
        self.assertEqual([entry["source_id"] for entry in detail["primary_entries"]], [may_item.id])
        self.assertEqual([entry["source_id"] for entry in detail["secondary_entries"]], [july_item.id])
        self.assertEqual(len(detail["year_entries"]), 2)

    def test_calendar_year_drawer_uses_single_primary_entries_list(self):
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        first_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 5, 10),
            end_date=date(2026, 5, 14),
            vacation_type="paid",
            chargeable_days=5,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        second_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 7),
            vacation_type="paid",
            chargeable_days=7,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        employees, employee_day_status, employee_entries = build_calendar_base_data(
            2026,
            employee_ids=[self.employee.id],
        )
        _, details = build_calendar_rows(
            employees,
            employee_day_status,
            employee_entries,
            year=2026,
            month=5,
            view_mode="year",
            today=date(2026, 5, 1),
        )

        detail = details[str(self.employee.id)]
        self.assertEqual(detail["view_mode"], "year")
        self.assertTrue(detail["is_year_view"])
        self.assertEqual(detail["primary_entries_title"], "Записи за год")
        self.assertEqual(
            [entry["source_id"] for entry in detail["primary_entries"]],
            [first_item.id, second_item.id],
        )
        self.assertEqual(detail["secondary_entries"], [])

    def test_calendar_drawer_entries_include_stage_labels(self):
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        past_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 2),
            vacation_type="paid",
            chargeable_days=2,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        current_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 5, 10),
            end_date=date(2026, 5, 12),
            vacation_type="paid",
            chargeable_days=3,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        upcoming_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 5, 20),
            end_date=date(2026, 5, 22),
            vacation_type="paid",
            chargeable_days=3,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        employees, employee_day_status, employee_entries = build_calendar_base_data(
            2026,
            employee_ids=[self.employee.id],
        )
        _, details = build_calendar_rows(
            employees,
            employee_day_status,
            employee_entries,
            year=2026,
            month=5,
            view_mode="year",
            today=date(2026, 5, 10),
        )

        entries = details[str(self.employee.id)]["primary_entries"]
        entries_by_source_id = {entry["source_id"]: entry for entry in entries}
        self.assertEqual(entries_by_source_id[past_item.id]["stage"], "past")
        self.assertEqual(entries_by_source_id[past_item.id]["stage_label"], "Прошел")
        self.assertEqual(entries_by_source_id[past_item.id]["stage_icon"], "task_alt")
        self.assertEqual(entries_by_source_id[current_item.id]["stage"], "current")
        self.assertEqual(entries_by_source_id[current_item.id]["stage_label"], "Идет сейчас")
        self.assertEqual(entries_by_source_id[current_item.id]["stage_icon"], "beach_access")
        self.assertEqual(entries_by_source_id[upcoming_item.id]["stage"], "upcoming")
        self.assertEqual(entries_by_source_id[upcoming_item.id]["stage_label"], "Предстоит")
        self.assertEqual(entries_by_source_id[upcoming_item.id]["stage_icon"], "event")

    def test_calendar_drawer_entries_include_transfer_actions_for_employee_and_manager(self):
        schedule = VacationSchedule.objects.create(
            year=2027,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 7, 1),
            end_date=date(2027, 7, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        employees, employee_day_status, employee_entries = build_calendar_base_data(
            2027,
            employee_ids=[self.employee.id],
        )
        _, employee_details = build_calendar_rows(
            employees,
            employee_day_status,
            employee_entries,
            year=2027,
            month=7,
            view_mode="year",
            today=date(2027, 1, 1),
            current_employee=self.employee,
        )
        _, manager_details = build_calendar_rows(
            employees,
            employee_day_status,
            employee_entries,
            year=2027,
            month=7,
            view_mode="year",
            today=date(2027, 1, 1),
            current_employee=self.department_head,
        )

        employee_entry = employee_details[str(self.employee.id)]["primary_entries"][0]
        manager_entry = manager_details[str(self.employee.id)]["primary_entries"][0]
        self.assertEqual(employee_entry["source_id"], schedule_item.id)
        self.assertTrue(employee_entry["can_request_transfer"])
        self.assertEqual(employee_entry["transfer_preview_url"], reverse("schedule_change_request_preview", args=[schedule_item.id]))
        self.assertEqual(employee_entry["transfer_action_label"], "Запросить перенос")
        self.assertEqual(employee_entry["transfer_modal_title"], "Запросить перенос отпуска")
        self.assertEqual(
            employee_entry["transfer_modal_subtitle"],
            "Выберите новые даты и укажите причину. Запрос уйдёт руководителю на согласование.",
        )
        self.assertEqual(manager_entry["source_id"], schedule_item.id)
        self.assertTrue(manager_entry["can_request_transfer"])
        self.assertEqual(manager_entry["transfer_preview_url"], reverse("schedule_change_request_preview", args=[schedule_item.id]))
        self.assertEqual(manager_entry["transfer_action_label"], "Предложить перенос")
        self.assertEqual(manager_entry["transfer_submit_label"], "Отправить предложение")
        self.assertEqual(
            manager_entry["transfer_hint"],
            "Сотрудник получит уведомление и сможет принять или отклонить перенос.",
        )
        self.assertEqual(manager_entry["transfer_modal_title"], "Предложить перенос отпуска")
        self.assertEqual(
            manager_entry["transfer_modal_subtitle"],
            "Выберите новые даты и укажите причину. Предложение уйдёт сотруднику.",
        )

    def test_calendar_board_marks_month_cells_by_stage(self):
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 5, 1),
            end_date=date(2026, 5, 2),
            vacation_type="paid",
            chargeable_days=2,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 5, 20),
            end_date=date(2026, 5, 22),
            vacation_type="paid",
            chargeable_days=3,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        employees, employee_day_status, employee_entries = build_calendar_base_data(
            2026,
            employee_ids=[self.employee.id],
        )
        rows, _ = build_calendar_rows(
            employees,
            employee_day_status,
            employee_entries,
            year=2026,
            month=5,
            view_mode="month",
            today=date(2026, 5, 10),
        )

        cells_by_day = {cell["day"]: cell for cell in rows[0]["cells"]}
        self.assertEqual(cells_by_day[1]["stage"], "past")
        self.assertEqual(cells_by_day[20]["stage"], "upcoming")
        self.assertEqual(cells_by_day[10]["stage"], "")

        html = render_to_string(
            "includes/calendar/board_content.html",
            {
                "calendar_view_mode": "month",
                "month_day_headers": [],
                "calendar_rows": rows,
                "selected_employee_id": None,
            },
        )
        self.assertIn("timeline-day--stage-past", html)
        self.assertIn("timeline-day--stage-upcoming", html)

    def test_calendar_board_marks_year_segments_by_stage(self):
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 2, 10),
            end_date=date(2026, 2, 14),
            vacation_type="paid",
            chargeable_days=5,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 11, 3),
            end_date=date(2026, 11, 9),
            vacation_type="paid",
            chargeable_days=7,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        employees, employee_day_status, employee_entries = build_calendar_base_data(
            2026,
            employee_ids=[self.employee.id],
        )
        rows, _ = build_calendar_rows(
            employees,
            employee_day_status,
            employee_entries,
            year=2026,
            month=5,
            view_mode="year",
            today=date(2026, 5, 10),
        )

        cells_by_month = {cell["month_number"]: cell for cell in rows[0]["cells"]}
        self.assertEqual(cells_by_month[2]["stage"], "past")
        self.assertEqual(cells_by_month[2]["segments"][0]["stage"], "past")
        self.assertEqual(cells_by_month[11]["stage"], "upcoming")
        self.assertEqual(cells_by_month[11]["segments"][0]["stage"], "upcoming")

        html = render_to_string(
            "includes/calendar/board_content.html",
            {
                "calendar_view_mode": "year",
                "calendar_month_totals": [],
                "calendar_rows": rows,
                "selected_employee_id": None,
            },
        )
        self.assertIn("year-month-card__segment--stage-past", html)
        self.assertIn("year-month-card__segment--stage-upcoming", html)

    def test_calendar_detail_drawer_marks_past_entries_in_html(self):
        past_end = self.today - timedelta(days=7)
        past_start = past_end - timedelta(days=3)
        upcoming_start = self.today + timedelta(days=30)
        upcoming_end = upcoming_start + timedelta(days=4)
        schedule = VacationSchedule.objects.create(
            year=past_start.year,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=past_start,
            end_date=past_end,
            vacation_type="paid",
            chargeable_days=4,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=upcoming_start,
            end_date=upcoming_end,
            vacation_type="paid",
            chargeable_days=5,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("calendar"),
            {
                "view": "year",
                "year": past_start.year,
                "employee": self.employee.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "calendar-drawer__entry--stage-past")
        self.assertContains(response, "calendar-drawer__entry--stage-upcoming")
        self.assertContains(response, "calendar-drawer__entry-stage--past")
        self.assertContains(response, "calendar-drawer__entry-stage--upcoming")
        self.assertContains(response, "Прошел")
        self.assertContains(response, "Предстоит")

    def test_calendar_year_month_details_summarize_visible_month(self):
        coworker = Employees.objects.create(
            last_name="Месячный",
            first_name="Артем",
            middle_name="Иванович",
            login="calendar-month-summary-coworker",
            position="Инженер",
            employee_position=self.engineering_engineer_position,
            department=self.engineering,
            date_joined=date(2024, 1, 10),
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
            employee=self.employee,
            start_date=date(2026, 2, 10),
            end_date=date(2026, 2, 12),
            vacation_type="paid",
            chargeable_days=3,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=coworker,
            start_date=date(2026, 2, 11),
            end_date=date(2026, 2, 12),
            vacation_type="paid",
            chargeable_days=2,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.outsider,
            start_date=date(2026, 2, 10),
            end_date=date(2026, 2, 12),
            vacation_type="paid",
            chargeable_days=3,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("calendar"),
            {
                "view": "year",
                "year": 2026,
                "department": self.engineering.id,
                "calendar_detail_month": 2,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        month_detail = response.json()["calendar_month_detail"]
        self.assertEqual(month_detail["title"], "Февраль 2026")
        self.assertEqual(month_detail["employee_count"], 2)
        self.assertEqual(month_detail["busy_days"], 5)
        self.assertEqual(month_detail["days"][9]["employee_count"], 1)
        self.assertEqual(month_detail["days"][10]["employee_count"], 2)
        self.assertEqual(month_detail["days"][11]["employee_count"], 2)
        self.assertEqual(len(month_detail["absence_groups"]), 1)
        group = month_detail["absence_groups"][0]
        self.assertEqual(group["department"], self.engineering.name)
        self.assertEqual(group["production_group"], self.engineering_group.name)
        self.assertEqual(group["employee_count"], 2)
        employee_names = {employee["employee_name"] for employee in group["employees"]}
        self.assertEqual(employee_names, {self.employee.full_name, coworker.full_name})
        employees_by_name = {employee["employee_name"]: employee for employee in group["employees"]}
        self.assertEqual(
            employees_by_name[self.employee.full_name]["profile_url"],
            f"{reverse('employee_profile', args=[self.employee.id])}?from=calendar",
        )
        self.assertNotIn(self.outsider.full_name, employee_names)

    def test_calendar_year_month_details_clip_problem_periods_to_month(self):
        DepartmentStaffingRule.objects.create(
            department=self.engineering,
            min_staff_required=0,
            max_absent=10,
            criticality_level=3,
        )
        DepartmentCoverageRule.objects.create(
            department=self.engineering,
            production_group=self.engineering_group,
            min_staff_required=2,
            max_absent=1,
            criticality_level=5,
        )
        coworker = Employees.objects.create(
            last_name="Стыковой",
            first_name="Артем",
            middle_name="Иванович",
            login="calendar-month-summary-boundary",
            position="Инженер",
            employee_position=self.engineering_engineer_position,
            department=self.engineering,
            date_joined=date(2024, 1, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        for employee in (self.employee, coworker):
            VacationScheduleItem.objects.create(
                schedule=schedule,
                employee=employee,
                start_date=date(2026, 3, 30),
                end_date=date(2026, 4, 2),
                vacation_type="paid",
                chargeable_days=4,
                status=VacationScheduleItem.STATUS_APPROVED,
            )
        self.client.force_login(self.hr_employee.user)

        march_response = self.client.get(
            reverse("calendar"),
            {"view": "year", "year": 2026, "issue": "conflict", "calendar_detail_month": 3},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        april_response = self.client.get(
            reverse("calendar"),
            {"view": "year", "year": 2026, "issue": "conflict", "calendar_detail_month": 4},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        march_detail = march_response.json()["calendar_month_detail"]
        april_detail = april_response.json()["calendar_month_detail"]
        march_problem = march_detail["problems"][0]
        april_problem = april_detail["problems"][0]
        self.assertEqual(march_problem["title"], "Группа не проходит по составу")
        self.assertEqual(march_problem["start_date"], "2026-03-30")
        self.assertEqual(march_problem["end_date"], "2026-03-31")
        self.assertEqual(march_problem["period_label"], "30-31 марта")
        self.assertIn("отсутствовали 2 сотрудника", march_problem["text"])
        self.assertIn("осталось 0 сотрудников", march_problem["text"])
        self.assertEqual(april_problem["start_date"], "2026-04-01")
        self.assertEqual(april_problem["end_date"], "2026-04-02")
        self.assertEqual(april_problem["period_label"], "1-2 апреля")
        self.assertIn("отсутствовали 2 сотрудника", april_problem["text"])
        self.assertIn("осталось 0 сотрудников", april_problem["text"])
        march_affected_by_name = {employee["name"]: employee for employee in march_problem["affected_employees"]}
        self.assertEqual(set(march_problem["affected_employee_ids"]), {self.employee.id, coworker.id})
        self.assertEqual(
            march_affected_by_name["Календарев Иван"]["profile_url"],
            f"{reverse('employee_profile', args=[self.employee.id])}?from=calendar",
        )
        self.assertTrue(march_detail["days"][29]["has_conflict"])
        self.assertTrue(april_detail["days"][0]["has_conflict"])

    def test_calendar_issue_filter_shows_only_staffing_conflicts(self):
        DepartmentStaffingRule.objects.create(
            department=self.engineering,
            min_staff_required=1,
            max_absent=1,
            criticality_level=3,
        )
        coworker = Employees.objects.create(
            last_name="Соседов",
            first_name="Артем",
            middle_name="Иванович",
            login="calendar-conflict-coworker",
            position="Инженер",
            department=self.engineering,
            date_joined=date(2024, 1, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        sync_employee_user(coworker, raw_password="coworker-pass")
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        for employee in (self.employee, coworker):
            VacationScheduleItem.objects.create(
                schedule=schedule,
                employee=employee,
                start_date=date(2026, 8, 1),
                end_date=date(2026, 8, 7),
                vacation_type="paid",
                chargeable_days=7,
                status=VacationScheduleItem.STATUS_APPROVED,
            )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.outsider,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 7),
            vacation_type="paid",
            chargeable_days=7,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("calendar"), {"view": "month", "year": 2026, "month": 8, "issue": "conflict"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["calendar_filters"]["selected_issue"], "conflict")
        self.assertContains(response, self.employee.full_name)
        self.assertContains(response, coworker.full_name)
        self.assertContains(response, "Конфликт: будут отсутствовать 2")
        self.assertContains(response, 'data-tooltip-title="Конфликт дня"')
        self.assertContains(response, 'data-tooltip-title="Конфликт состава"')
        self.assertNotContains(response, self.outsider.full_name)
        detail = response.context["calendar_details"][str(self.employee.id)]
        self.assertTrue(detail["has_conflict"])
        self.assertEqual(detail["issue_label"], "Конфликт")
        self.assertEqual(detail["risk_details"]["status"], "conflict")
        problem = detail["risk_details"]["problems"][0]
        self.assertEqual(problem["period_label"], "1-7 августа")
        self.assertEqual(problem["title"], "Превышен лимит отсутствующих")
        self.assertIn("будут отсутствовать 2 сотрудника при лимите 1 сотрудник", problem["text"])
        self.assertEqual(problem["impact_label"], "Превышение: 1 сотрудник")
        self.assertIn("Календарев Иван", problem["affected_names"])
        self.assertIn("Соседов Артем", problem["affected_names"])
        affected_by_name = {employee["name"]: employee for employee in problem["affected_employees"]}
        self.assertEqual(
            affected_by_name["Календарев Иван"]["profile_url"],
            f"{reverse('employee_profile', args=[self.employee.id])}?from=calendar",
        )
        self.assertEqual(affected_by_name["Календарев Иван"]["id"], self.employee.id)
        self.assertEqual(set(problem["affected_employee_ids"]), {self.employee.id, coworker.id})
        self.assertContains(response, 'class="calendar-drawer__affected-link"')
        self.assertIn("будут отсутствовать 2", detail["conflict_summary"])
        self.assertTrue(detail["selected_entries"][0]["has_conflict"])
        self.assertIn("будут отсутствовать 2", detail["selected_entries"][0]["conflict_summary"])
        self.assertEqual(detail["selected_entries"][0]["risk_short_reason"], "")

    def test_calendar_employee_scope_filters_requested_accessible_employees(self):
        coworker = Employees.objects.create(
            last_name="Фокусный",
            first_name="Артем",
            middle_name="Иванович",
            login="calendar-scope-coworker",
            position="Инженер",
            department=self.engineering,
            date_joined=date(2024, 1, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("calendar"),
            {
                "view": "year",
                "year": 2026,
                "department": self.hr_department.id,
                "group": self.hr_group.id,
                "search": "НетТакого",
                "employee_scope": f"{self.employee.id},bad,{coworker.id}",
            },
        )

        self.assertEqual(response.status_code, 200)
        row_names = {row["employee_name"] for row in response.context["calendar_rows"]}
        self.assertEqual(row_names, {self.employee.full_name, coworker.full_name})
        self.assertEqual(response.context["calendar_filters"]["selected_department"], "all")
        self.assertEqual(response.context["calendar_filters"]["selected_group"], "all")
        self.assertEqual(response.context["calendar_filters"]["search_query"], "")
        scope = response.context["calendar_filters"]["employee_scope"]
        self.assertTrue(scope["is_active"])
        self.assertEqual(scope["value"], f"{self.employee.id},{coworker.id}")
        self.assertEqual(scope["count"], 2)
        self.assertContains(response, "Показаны участники конфликта: 2 сотрудника")
        self.assertNotContains(response, self.outsider.full_name)

    def test_calendar_employee_scope_drops_inaccessible_and_ajax_returns_scoped_rows(self):
        coworker = Employees.objects.create(
            last_name="Доступный",
            first_name="Артем",
            middle_name="Иванович",
            login="calendar-scope-ajax-coworker",
            position="Инженер",
            department=self.engineering,
            date_joined=date(2024, 1, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        self.client.force_login(self.department_head.user)

        response = self.client.get(
            reverse("calendar"),
            {
                "view": "year",
                "year": 2026,
                "employee_scope": f"{self.employee.id},{coworker.id},{self.outsider.id},broken",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn(self.employee.full_name, payload["board_html"])
        self.assertIn(coworker.full_name, payload["board_html"])
        self.assertNotIn(self.outsider.full_name, payload["board_html"])
        self.assertEqual(payload["calendar_details"], {})

    def test_calendar_ajax_returns_grouped_board_for_group_filter(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("calendar"),
            {
                "view": "year",
                "year": 2026,
                "sort": "org",
                "group": self.engineering_group.id,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("calendar-row-group-heading--department", payload["board_html"])
        self.assertIn("calendar-row-group-heading--group", payload["board_html"])
        self.assertIn("calendar-row-section--department", payload["board_html"])
        self.assertIn("calendar-row-section--group", payload["board_html"])
        self.assertIn("data-calendar-collapse-toggle", payload["board_html"])
        self.assertIn('data-calendar-collapse-level="department"', payload["board_html"])
        self.assertIn('data-calendar-collapse-level="group"', payload["board_html"])
        self.assertIn('aria-expanded="true"', payload["board_html"])
        self.assertIn('data-calendar-collapse-body', payload["board_html"])
        self.assertIn(self.engineering.name, payload["board_html"])
        self.assertIn(self.engineering_group.name, payload["board_html"])
        self.assertIn(self.employee.full_name, payload["board_html"])
        self.assertNotIn(self.department_head.full_name, payload["board_html"])
        self.assertNotIn(self.outsider.full_name, payload["board_html"])
        self.assertEqual(payload["calendar_details"], {})

    def test_calendar_conflict_detects_production_group_shortage(self):
        DepartmentStaffingRule.objects.create(
            department=self.engineering,
            min_staff_required=0,
            max_absent=10,
            criticality_level=3,
        )
        DepartmentCoverageRule.objects.create(
            department=self.engineering,
            production_group=self.engineering_group,
            min_staff_required=1,
            max_absent=5,
            criticality_level=5,
        )
        coworker = Employees.objects.create(
            last_name="Группов",
            first_name="Артем",
            middle_name="Иванович",
            login="calendar-group-coworker",
            position="Инженер",
            employee_position=self.engineering_engineer_position,
            department=self.engineering,
            date_joined=date(2024, 1, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        for employee in (self.employee, coworker):
            VacationScheduleItem.objects.create(
                schedule=schedule,
                employee=employee,
                start_date=date(2026, 9, 1),
                end_date=date(2026, 9, 7),
                vacation_type="paid",
                chargeable_days=7,
                status=VacationScheduleItem.STATUS_APPROVED,
            )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("calendar"), {"view": "month", "year": 2026, "month": 9, "issue": "conflict"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.employee.full_name)
        self.assertContains(response, coworker.full_name)
        detail = response.context["calendar_details"][str(self.employee.id)]
        self.assertTrue(detail["has_conflict"])
        self.assertIn("не хватает: Инженеры", detail["conflict_summary"])

    def test_calendar_conflict_details_group_adjacent_days_into_period(self):
        DepartmentStaffingRule.objects.create(
            department=self.engineering,
            min_staff_required=0,
            max_absent=10,
            criticality_level=3,
        )
        DepartmentCoverageRule.objects.create(
            department=self.engineering,
            production_group=self.engineering_group,
            min_staff_required=0,
            max_absent=1,
            criticality_level=5,
        )
        coworker = Employees.objects.create(
            last_name="Белов",
            first_name="Андрей",
            middle_name="Андреевич",
            login="calendar-below-conflict",
            position="Инженер",
            employee_position=self.engineering_engineer_position,
            department=self.engineering,
            date_joined=date(2024, 1, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        for employee in (self.employee, coworker):
            VacationScheduleItem.objects.create(
                schedule=schedule,
                employee=employee,
                start_date=date(2026, 7, 22),
                end_date=date(2026, 7, 23),
                vacation_type="paid",
                chargeable_days=2,
                status=VacationScheduleItem.STATUS_APPROVED,
            )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("calendar"), {"view": "month", "year": 2026, "month": 7, "issue": "conflict"})

        detail = response.context["calendar_details"][str(self.employee.id)]
        problems = detail["risk_details"]["problems"]
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]["period_label"], "22-23 июля")
        self.assertEqual(problems[0]["title"], "Превышен лимит отсутствующих")
        self.assertIn("Инженеры: будут отсутствовать 2 сотрудника при лимите 1 сотрудник", problems[0]["text"])
        self.assertEqual(problems[0]["impact_label"], "Превышение: 1 сотрудник")
        self.assertIn("Белов Андрей", problems[0]["affected_names"])

    def test_calendar_conflict_details_merge_group_limit_and_shortage(self):
        DepartmentStaffingRule.objects.create(
            department=self.engineering,
            min_staff_required=0,
            max_absent=10,
            criticality_level=3,
        )
        DepartmentCoverageRule.objects.create(
            department=self.engineering,
            production_group=self.engineering_group,
            min_staff_required=2,
            max_absent=1,
            criticality_level=5,
        )
        coworker = Employees.objects.create(
            last_name="Разный",
            first_name="Артем",
            middle_name="Иванович",
            login="calendar-different-problem-kind",
            position="Инженер",
            employee_position=self.engineering_engineer_position,
            department=self.engineering,
            date_joined=date(2024, 1, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        for employee in (self.employee, coworker):
            VacationScheduleItem.objects.create(
                schedule=schedule,
                employee=employee,
                start_date=date(2026, 7, 22),
                end_date=date(2026, 7, 23),
                vacation_type="paid",
                chargeable_days=2,
                status=VacationScheduleItem.STATUS_APPROVED,
            )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("calendar"), {"view": "month", "year": 2026, "month": 7, "issue": "conflict"})

        detail = response.context["calendar_details"][str(self.employee.id)]
        problems = detail["risk_details"]["problems"]
        self.assertContains(response, "timeline-head--issue-conflict", count=2)
        self.assertContains(response, "timeline-day--issue-conflict")
        self.assertContains(response, "Есть конфликт состава")
        self.assertTrue(response.context["month_day_headers"][21]["has_conflict"])
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]["kind"], "group_staffing_combined")
        self.assertEqual(problems[0]["title"], "Группа не проходит по составу")
        self.assertEqual(problems[0]["period_label"], "22-23 июля")
        self.assertIn("Инженеры: будут отсутствовать 2 сотрудника, останется 0 сотрудников", problems[0]["text"])
        self.assertEqual(problems[0]["impact_label"], "Не хватает: двух сотрудников")
        self.assertIn("Календарев Иван", problems[0]["affected_names"])
        self.assertIn("Разный Артем", problems[0]["affected_names"])

        year_response = self.client.get(reverse("calendar"), {"view": "year", "year": 2026, "issue": "conflict"})
        self.assertEqual(year_response.status_code, 200)
        self.assertContains(year_response, "year-month-total--issue-conflict", count=1)
        self.assertContains(year_response, "year-month-card--issue-conflict")
        self.assertContains(year_response, 'data-tooltip-title="Конфликт состава"')
        self.assertNotContains(year_response, 'data-tooltip-text="Отпусков и отсутствий:')
        self.assertNotContains(year_response, "year-month-card--issue-risk")

    def test_calendar_issue_highlight_prefers_conflict_over_risk(self):
        DepartmentStaffingRule.objects.create(
            department=self.engineering,
            min_staff_required=0,
            max_absent=10,
            criticality_level=3,
        )
        DepartmentCoverageRule.objects.create(
            department=self.engineering,
            production_group=self.engineering_group,
            min_staff_required=2,
            max_absent=1,
            criticality_level=5,
        )
        coworker = Employees.objects.create(
            last_name="Приоритетов",
            first_name="Артем",
            middle_name="Иванович",
            login="calendar-issue-highlight-priority",
            position="Инженер",
            employee_position=self.engineering_engineer_position,
            department=self.engineering,
            date_joined=date(2024, 1, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        for employee in (self.employee, coworker):
            VacationScheduleItem.objects.create(
                schedule=schedule,
                employee=employee,
                start_date=date(2026, 7, 22),
                end_date=date(2026, 7, 23),
                vacation_type="paid",
                chargeable_days=2,
                status=VacationScheduleItem.STATUS_APPROVED,
                risk_level=VacationScheduleItem.RISK_HIGH,
                risk_score=90,
            )
        self.client.force_login(self.hr_employee.user)

        month_response = self.client.get(reverse("calendar"), {"view": "month", "year": 2026, "month": 7, "issue": "conflict"})
        self.assertEqual(month_response.status_code, 200)
        self.assertContains(month_response, "timeline-day--issue-conflict")
        self.assertContains(month_response, "timeline-head--issue-conflict", count=2)
        self.assertNotContains(month_response, "timeline-day--issue-risk")
        self.assertNotContains(month_response, "timeline-head--issue-risk")

        year_response = self.client.get(reverse("calendar"), {"view": "year", "year": 2026, "issue": "conflict"})
        self.assertEqual(year_response.status_code, 200)
        self.assertContains(year_response, "year-month-card--issue-conflict")
        self.assertNotContains(year_response, "year-month-card--issue-risk")

    def test_calendar_conflict_details_keep_separate_group_staffing_periods(self):
        DepartmentStaffingRule.objects.create(
            department=self.engineering,
            min_staff_required=0,
            max_absent=10,
            criticality_level=3,
        )
        DepartmentCoverageRule.objects.create(
            department=self.engineering,
            production_group=self.engineering_group,
            min_staff_required=2,
            max_absent=1,
            criticality_level=5,
        )
        coworker = Employees.objects.create(
            last_name="Периодов",
            first_name="Артем",
            middle_name="Иванович",
            login="calendar-different-period-kind",
            position="Инженер",
            employee_position=self.engineering_engineer_position,
            department=self.engineering,
            date_joined=date(2024, 1, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        for start_date, end_date in ((date(2026, 7, 22), date(2026, 7, 23)), (date(2026, 7, 25), date(2026, 7, 26))):
            for employee in (self.employee, coworker):
                VacationScheduleItem.objects.create(
                    schedule=schedule,
                    employee=employee,
                    start_date=start_date,
                    end_date=end_date,
                    vacation_type="paid",
                    chargeable_days=(end_date - start_date).days + 1,
                    status=VacationScheduleItem.STATUS_APPROVED,
                )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("calendar"), {"view": "month", "year": 2026, "month": 7, "issue": "conflict"})

        detail = response.context["calendar_details"][str(self.employee.id)]
        problems = detail["risk_details"]["problems"]
        self.assertEqual(len(problems), 2)
        self.assertEqual([problem["title"] for problem in problems], ["Группа не проходит по составу", "Группа не проходит по составу"])
        self.assertEqual([problem["period_label"] for problem in problems], ["22-23 июля", "25-26 июля"])
        self.assertTrue(all("Периодов Артем" in problem["affected_names"] for problem in problems))

    def test_calendar_conflict_details_merge_substitution_into_conflict_problem(self):
        DepartmentStaffingRule.objects.create(
            department=self.engineering,
            min_staff_required=0,
            max_absent=10,
            criticality_level=3,
        )
        substitute_group = ProductionGroup.objects.create(department=self.engineering, name="Резерв качества")
        substitute_position = EmployeePosition.objects.create(
            department=self.engineering,
            production_group=substitute_group,
            title="Резервный инженер",
        )
        ProductionGroupSubstitutionRule.objects.create(
            department=self.engineering,
            source_group=self.engineering_group,
            substitute_group=substitute_group,
        )
        DepartmentCoverageRule.objects.create(
            department=self.engineering,
            production_group=self.engineering_group,
            min_staff_required=1,
            max_absent=1,
            criticality_level=5,
        )
        coworker = Employees.objects.create(
            last_name="Замещаемый",
            first_name="Артем",
            middle_name="Иванович",
            login="calendar-merged-substitution-coworker",
            position="Инженер",
            employee_position=self.engineering_engineer_position,
            department=self.engineering,
            date_joined=date(2024, 1, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        Employees.objects.create(
            last_name="Резервный",
            first_name="Петр",
            middle_name="Иванович",
            login="calendar-merged-substitution-reserve",
            position="Резервный инженер",
            employee_position=substitute_position,
            department=self.engineering,
            date_joined=date(2024, 1, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        for employee in (self.employee, coworker):
            VacationScheduleItem.objects.create(
                schedule=schedule,
                employee=employee,
                start_date=date(2026, 7, 22),
                end_date=date(2026, 7, 23),
                vacation_type="paid",
                chargeable_days=2,
                status=VacationScheduleItem.STATUS_APPROVED,
            )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("calendar"), {"view": "month", "year": 2026, "month": 7, "issue": "conflict"})

        detail = response.context["calendar_details"][str(self.employee.id)]
        problems = detail["risk_details"]["problems"]
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0]["title"], "Превышен лимит отсутствующих")
        self.assertIn("Замещение покрывает одного сотрудника", problems[0]["substitution_label"])
        self.assertIn("лимит всё равно превышен на одного сотрудника", problems[0]["substitution_label"])

    def test_calendar_substitution_prevents_group_shortage_conflict(self):
        DepartmentStaffingRule.objects.create(
            department=self.engineering,
            min_staff_required=0,
            max_absent=10,
            criticality_level=3,
        )
        substitute_group = ProductionGroup.objects.create(department=self.engineering, name="Замещающая группа")
        substitute_position = EmployeePosition.objects.create(
            department=self.engineering,
            production_group=substitute_group,
            title="Сменный специалист",
        )
        ProductionGroupSubstitutionRule.objects.create(
            department=self.engineering,
            source_group=self.engineering_group,
            substitute_group=substitute_group,
        )
        DepartmentCoverageRule.objects.create(
            department=self.engineering,
            production_group=self.engineering_group,
            min_staff_required=1,
            max_absent=5,
            criticality_level=5,
        )
        Employees.objects.create(
            last_name="Замещающий",
            first_name="Петр",
            middle_name="Иванович",
            login="calendar-substitute",
            position="Сменный специалист",
            employee_position=substitute_position,
            department=self.engineering,
            date_joined=date(2024, 1, 10),
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
            employee=self.employee,
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 7),
            vacation_type="paid",
            chargeable_days=7,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("calendar"), {"view": "month", "year": 2026, "month": 10, "issue": "conflict"})

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.employee.full_name)

        risk_response = self.client.get(reverse("calendar"), {"view": "month", "year": 2026, "month": 10, "issue": "risk"})

        self.assertContains(risk_response, self.employee.full_name)
        detail = risk_response.context["calendar_details"][str(self.employee.id)]
        self.assertTrue(detail["has_high_risk"])
        self.assertIn("закрыта замещением", detail["risk_summary"])
        self.assertFalse(detail["has_conflict"])
        self.assertEqual(detail["risk_details"]["problems"][0]["title"], "Нужно замещение")
        self.assertIn("Замещение покрывает", detail["risk_details"]["problems"][0]["impact_label"])

    def test_calendar_substitution_respects_capacity_and_free_staff(self):
        DepartmentStaffingRule.objects.create(
            department=self.engineering,
            min_staff_required=0,
            max_absent=10,
            criticality_level=3,
        )
        substitute_group = ProductionGroup.objects.create(department=self.engineering, name="Ограниченная замена")
        substitute_position = EmployeePosition.objects.create(
            department=self.engineering,
            production_group=substitute_group,
            title="Сменный эксперт",
        )
        ProductionGroupSubstitutionRule.objects.create(
            department=self.engineering,
            source_group=self.engineering_group,
            substitute_group=substitute_group,
            max_covered_absences=1,
        )
        DepartmentCoverageRule.objects.create(
            department=self.engineering,
            production_group=self.engineering_group,
            min_staff_required=2,
            max_absent=5,
            criticality_level=5,
        )
        coworker = Employees.objects.create(
            last_name="Группов",
            first_name="Артем",
            middle_name="Иванович",
            login="calendar-capacity-coworker",
            position="Инженер",
            employee_position=self.engineering_engineer_position,
            department=self.engineering,
            date_joined=date(2024, 1, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        Employees.objects.create(
            last_name="Замещающий",
            first_name="Петр",
            middle_name="Иванович",
            login="calendar-capacity-substitute",
            position="Сменный эксперт",
            employee_position=substitute_position,
            department=self.engineering,
            date_joined=date(2024, 1, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        for employee in (self.employee, coworker):
            VacationScheduleItem.objects.create(
                schedule=schedule,
                employee=employee,
                start_date=date(2026, 10, 1),
                end_date=date(2026, 10, 7),
                vacation_type="paid",
                chargeable_days=7,
                status=VacationScheduleItem.STATUS_APPROVED,
            )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("calendar"), {"view": "month", "year": 2026, "month": 10, "issue": "conflict"})

        self.assertContains(response, self.employee.full_name)
        detail = response.context["calendar_details"][str(self.employee.id)]
        self.assertTrue(detail["has_conflict"])
        self.assertIn("не хватает: Инженеры", detail["conflict_summary"])

    def test_calendar_conflict_detects_department_head_and_deputy_absence(self):
        self.engineering.deputy = self.employee
        self.engineering.save(update_fields=["deputy"])
        DepartmentStaffingRule.objects.create(
            department=self.engineering,
            min_staff_required=0,
            max_absent=10,
            criticality_level=3,
        )
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        for employee in (self.employee, self.department_head):
            VacationScheduleItem.objects.create(
                schedule=schedule,
                employee=employee,
                start_date=date(2026, 11, 1),
                end_date=date(2026, 11, 7),
                vacation_type="paid",
                chargeable_days=7,
                status=VacationScheduleItem.STATUS_APPROVED,
            )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("calendar"), {"view": "month", "year": 2026, "month": 11, "issue": "conflict"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.employee.full_name)
        detail = response.context["calendar_details"][str(self.employee.id)]
        self.assertIn("руководитель отдела и заместитель будут отсутствовать", detail["conflict_summary"])

    def test_calendar_ajax_preserves_filter_parameters(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("calendar"),
            {
                "view": "year",
                "year": 2026,
                "department": self.engineering.id,
                "search": self.employee.last_name,
                "issue": "all",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn(self.employee.full_name, payload["board_html"])
        self.assertNotIn(self.outsider.full_name, payload["board_html"])

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
        self.assertEqual(row["profile_url"], f"{reverse('employee_profile', args=[old_employee.id])}?from=calendar")
        self.assertEqual(row["role_icon"], "person")
        self.assertEqual(row["role_icon_type"], "material")
        self.assertEqual(row["role_variant"], "employee")
        self.assertEqual(row["role_label"], "Сотрудник")
        self.assertEqual(row["selected_schedule_days"], 14)
        self.assertEqual(row["status"], "schedule-approved")
        self.assertEqual(details[str(old_employee.id)]["selected_entries"][0]["status_label"], "График утвержден")
        self.assertEqual(details[str(old_employee.id)]["selected_entries"][0]["source_label"], "Годовой график")
        self.assertEqual(details[str(old_employee.id)]["selected_entries"][0]["detail_url"], "")
        self.assertEqual(details[str(old_employee.id)]["selected_entries"][0]["risk_label"], "Низкий")
        self.assertEqual(details[str(old_employee.id)]["selected_entries"][0]["anchor"]["start_date"], "2015-07-01")
        self.assertEqual(
            details[str(old_employee.id)]["profile_url"],
            f"{reverse('employee_profile', args=[old_employee.id])}?from=calendar",
        )
        self.assertEqual(details[str(old_employee.id)]["role_icon"], "person")
        self.assertEqual(details[str(old_employee.id)]["role_variant"], "employee")

    def test_calendar_year_view_uses_compact_legend_and_simple_month_cards(self):
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
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("calendar"), {"view": "year", "year": 2026, "employee": self.employee.id})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "calendar-legend-toggle")
        self.assertContains(response, "calendar-legend-popover")
        self.assertContains(response, "calendar-legend__item--schedule-approved")
        self.assertContains(response, "calendar-legend__item--issue-risk")
        self.assertContains(response, "calendar-legend__item--issue-conflict")
        self.assertContains(response, "year-grid--totals")
        self.assertNotContains(response, "year-grid--head")
        self.assertNotContains(response, "year-head--month")
        self.assertContains(response, "Итого")
        self.assertContains(response, "data-calendar-month")
        self.assertContains(response, "bolt")
        self.assertContains(response, "⚔")
        self.assertNotContains(response, "timeline-employee-card__stats")
        self.assertNotContains(response, "year-month-card__stats")
        self.assertContains(response, "data-calendar-focus-entry")
        self.assertContains(response, "data-calendar-focus-upcoming")
        july_total = response.context["calendar_month_totals"][6]
        self.assertEqual(july_total["employee_count"], 1)
        self.assertEqual(july_total["busy_days"], 14)
        detail = response.context["calendar_details"][str(self.employee.id)]
        self.assertEqual(detail["upcoming_anchor"]["employee_id"], self.employee.id)
        self.assertEqual(detail["upcoming_anchor"]["start_date"], "2026-07-01")

    def test_calendar_month_view_does_not_render_year_totals(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("calendar"), {"view": "month", "year": 2026, "month": 7})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["calendar_month_totals"], [])
        self.assertNotContains(response, "year-grid--totals")
        month_html = response.content.decode()
        self.assertIn(
            '<div class="timeline-head timeline-head--employee">\n                <span>Дата</span>\n            </div>',
            month_html,
        )
        self.assertNotIn(
            '<div class="timeline-head timeline-head--employee">\n                <span>Сотрудник</span>\n            </div>',
            month_html,
        )

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
