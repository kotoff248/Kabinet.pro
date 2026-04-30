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
        self.assertEqual(payload["vacations"][0]["employee_role_icon"], "person")
        self.assertEqual(payload["vacations"][0]["employee_role_icon_type"], "material")
        self.assertEqual(payload["vacations"][0]["employee_role_variant"], "employee")
        self.assertEqual(payload["vacations"][0]["employee_role_label"], "Сотрудник")
        self.assertEqual(payload["vacations"][0]["employee_secondary_label"], self.employee.department.name)
        self.assertEqual(payload["vacations"][0]["period_label"], "01.11.2026 - 03.11.2026")
        self.assertIn("period_label", payload["vacations"][0])
        self.assertIn("vacations_html", payload)
        self.assertIn("change_requests_html", payload)
        self.assertIn(f'data-vacation-id="{request_obj.id}"', payload["vacations_html"])
        self.assertIn(f'data-href="{reverse("vacation_detail", args=[request_obj.id])}"', payload["vacations_html"])
        self.assertIn(f'href="{reverse("employee_profile", args=[self.employee.id])}"', payload["vacations_html"])
        self.assertIn("application-card__profile-icon application-card__profile-icon--employee", payload["vacations_html"])
        self.assertIn('aria-label="Открыть профиль сотрудника', payload["vacations_html"])
        self.assertIn('<span class="application-card__label">ФИО</span>', payload["vacations_html"])
        self.assertIn("01.11.2026 - 03.11.2026", payload["vacations_html"])
        self.assertNotIn("ноября", payload["vacations_html"])
        self.assertNotIn('<span class="application-card__label">Сотрудник</span>', payload["vacations_html"])
        self.assertNotIn("<span>Профиль</span>", payload["vacations_html"])
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
        self.assertIn("application-card__profile-icon application-card__profile-icon--employee", payload["change_requests_html"])
        self.assertIn('<span class="application-card__label">ФИО</span>', payload["change_requests_html"])
        self.assertNotIn('<span class="application-card__label">Сотрудник</span>', payload["change_requests_html"])
        self.assertNotIn("<span>Профиль</span>", payload["change_requests_html"])

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
        self.assertContains(response, "application-card__profile-icon application-card__profile-icon--employee")
        self.assertContains(response, '<span class="application-card__label">ФИО</span>')
        self.assertNotContains(response, '<span class="application-card__label">Сотрудник</span>')
        self.assertNotContains(response, "<span>Профиль</span>")
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
        self.assertEqual(payload["change_requests"][0]["employee_role_icon"], "person")
        self.assertEqual(payload["change_requests"][0]["employee_role_icon_type"], "material")
        self.assertEqual(payload["change_requests"][0]["employee_role_variant"], "employee")
        self.assertEqual(payload["change_requests"][0]["employee_role_label"], "Сотрудник")
        self.assertEqual(payload["change_requests"][0]["employee_secondary_label"], self.employee.department.name)
        self.assertIn("approve_url", payload["change_requests"][0])
        self.assertIn("reject_url", payload["change_requests"][0])
        self.assertIn(f'data-vacation-id="{pending_request.id}"', payload["vacations_html"])
        self.assertIn(f'data-change-request-id="{change_request.id}"', payload["change_requests_html"])
        self.assertIn(reverse("schedule_change_approve", args=[change_request.id]), payload["change_requests_html"])
        self.assertIn(reverse("schedule_change_reject", args=[change_request.id]), payload["change_requests_html"])
        self.assertIn('name="csrfmiddlewaretoken"', payload["change_requests_html"])

    def test_applications_employee_identity_uses_role_icons_and_secondary_labels(self):
        hr_request = VacationRequest.objects.create(
            employee=self.hr_employee,
            start_date="2026-12-01",
            end_date="2026-12-03",
            vacation_type="study",
            status=VacationRequest.STATUS_PENDING,
        )
        department_head_request = VacationRequest.objects.create(
            employee=self.department_head,
            start_date="2026-12-05",
            end_date="2026-12-07",
            vacation_type="study",
            status=VacationRequest.STATUS_PENDING,
        )
        self.client.force_login(self.enterprise_head.user)

        response = self.client.get(
            reverse("applications"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        requests_by_id = {item["id"]: item for item in payload["vacations"]}
        self.assertEqual(requests_by_id[hr_request.id]["employee_role_icon"], "manage_accounts")
        self.assertEqual(requests_by_id[hr_request.id]["employee_role_variant"], "hr")
        self.assertEqual(requests_by_id[hr_request.id]["employee_secondary_label"], self.hr_employee.department.name)
        self.assertEqual(
            requests_by_id[department_head_request.id]["employee_role_icon"],
            "admin_panel_settings",
        )
        self.assertEqual(
            requests_by_id[department_head_request.id]["employee_role_variant"],
            "department-head",
        )
        self.assertEqual(
            requests_by_id[department_head_request.id]["employee_secondary_label"],
            self.department_head.position,
        )
        self.assertIn("application-card__profile-icon--hr", payload["vacations_html"])
        self.assertIn("application-card__profile-icon--department-head", payload["vacations_html"])
        self.assertIn(self.department_head.position, payload["vacations_html"])

    def test_applications_enterprise_head_identity_uses_crown_symbol(self):
        request_obj = VacationRequest.objects.create(
            employee=self.enterprise_head,
            start_date="2026-12-10",
            end_date="2026-12-12",
            vacation_type="study",
            status=VacationRequest.STATUS_PENDING,
        )
        self.client.force_login(self.authorized_person.user)

        response = self.client.get(
            reverse("applications"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        requests_by_id = {item["id"]: item for item in payload["vacations"]}
        self.assertEqual(requests_by_id[request_obj.id]["employee_role_icon"], "♛")
        self.assertEqual(requests_by_id[request_obj.id]["employee_role_icon_type"], "symbol")
        self.assertEqual(requests_by_id[request_obj.id]["employee_role_variant"], "enterprise-head")
        self.assertEqual(
            requests_by_id[request_obj.id]["employee_secondary_label"],
            self.enterprise_head.department.name,
        )
        self.assertIn("application-card__profile-icon--enterprise-head", payload["vacations_html"])
        self.assertIn('<span class="application-card__profile-symbol" aria-hidden="true">♛</span>', payload["vacations_html"])
