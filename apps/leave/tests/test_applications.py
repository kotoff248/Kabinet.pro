from datetime import date

from django.urls import reverse

from apps.leave.models import VacationRequest, VacationSchedule, VacationScheduleItem
from apps.leave.services.schedule_changes import create_schedule_change_request

from .base import LeaveTestCase


class ApplicationsBoardTests(LeaveTestCase):
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
        self.assertEqual(payload["vacations"][0]["profile_url"], reverse("employee_profile", args=[self.employee.id]))
        self.assertIn("period_label", payload["vacations"][0])
        self.assertIn("vacations_html", payload)
        self.assertIn("change_requests_html", payload)
        self.assertIn(f'data-vacation-id="{request_obj.id}"', payload["vacations_html"])
        self.assertIn(f'data-href="{reverse("vacation_detail", args=[request_obj.id])}"', payload["vacations_html"])
        self.assertIn(f'href="{reverse("employee_profile", args=[self.employee.id])}"', payload["vacations_html"])
        self.assertIn('role="link"', payload["vacations_html"])

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
        self.assertIn(f'data-vacation-id="{request_obj.id}"', payload["vacations_html"])
        self.assertIn(f'data-change-request-id="{change_request.id}"', payload["change_requests_html"])
        self.assertIn(f'href="{reverse("employee_profile", args=[self.employee.id])}"', payload["change_requests_html"])

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
        self.assertIn("Заявки по выбранным фильтрам не найдены.", payload["vacations_html"])
        self.assertIn("Переносы графика по выбранным фильтрам не найдены.", payload["change_requests_html"])

    def test_applications_page_uses_sectioned_cards_and_custom_department_select(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-11-01",
            end_date="2026-11-03",
            vacation_type="study",
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
            reason="Проверка карточек.",
        )
        self.client.force_login(self.department_head.user)

        response = self.client.get(reverse("applications"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-applications-page")
        self.assertContains(response, "applications-board--transfers")
        self.assertContains(response, "applications-board--requests")
        self.assertContains(response, "data-applications-transfer-scroll")
        self.assertContains(response, "data-applications-request-scroll")
        self.assertContains(response, f'data-vacation-id="{request_obj.id}"')
        self.assertContains(response, f'data-change-request-id="{change_request.id}"')
        self.assertContains(response, reverse("employee_profile", args=[self.employee.id]))
        self.assertContains(response, reverse("schedule_change_approve", args=[change_request.id]))
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
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
        self.assertEqual(payload["change_requests"][0]["profile_url"], reverse("employee_profile", args=[self.employee.id]))
        self.assertIn("approve_url", payload["change_requests"][0])
        self.assertIn("reject_url", payload["change_requests"][0])
        self.assertIn(f'data-vacation-id="{pending_request.id}"', payload["vacations_html"])
        self.assertIn(f'data-change-request-id="{change_request.id}"', payload["change_requests_html"])
        self.assertIn(reverse("schedule_change_approve", args=[change_request.id]), payload["change_requests_html"])
        self.assertIn(reverse("schedule_change_reject", args=[change_request.id]), payload["change_requests_html"])
        self.assertIn('name="csrfmiddlewaretoken"', payload["change_requests_html"])
