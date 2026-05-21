from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from django.urls import reverse
from django.utils import timezone

from apps.leave.models import (
    VacationRequest,
    VacationSchedule,
    VacationScheduleChangeRequest,
    VacationScheduleItem,
)
from apps.leave.services.schedule_changes import create_schedule_change_request

from .base import LeaveTestCase


class LeaveAccessTests(LeaveTestCase):
    def test_employee_cannot_open_management_sections(self):
        self.client.force_login(self.employee.user)

        applications_response = self.client.get(reverse("applications"))
        analytics_response = self.client.get(reverse("analytics"))

        self.assertRedirects(applications_response, reverse("main"))
        self.assertRedirects(analytics_response, reverse("main"))

    def test_hr_can_open_analytics(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("analytics"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Сводка модуля")
        self.assertContains(response, 'data-sidebar-key="analytics"')

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
        self.assertContains(response, "Недоступно")
        self.assertContains(response, "lock")
        self.assertEqual(response.context["pending_requests_count"], 1)
        change_request.refresh_from_db()
        self.assertFalse(response.context["change_requests"][0].can_approve)
        self.assertTrue(response.context["change_requests"][0].decision_locked)
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

    def test_vacation_detail_renders_role_based_action_forms(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-12-15",
            end_date="2026-12-17",
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
        )
        reviewed_request = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-12-18",
            end_date="2026-12-19",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_APPROVED,
        )
        management_request = VacationRequest.objects.create(
            employee=self.department_head,
            start_date="2026-12-20",
            end_date="2026-12-21",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )

        self.client.force_login(self.department_head.user)
        manager_response = self.client.get(reverse("vacation_detail", args=[request_obj.id]))
        reviewed_response = self.client.get(reverse("vacation_detail", args=[reviewed_request.id]))

        self.client.force_login(self.enterprise_head.user)
        enterprise_response = self.client.get(reverse("vacation_detail", args=[request_obj.id]))
        role_response = self.client.get(reverse("vacation_detail", args=[management_request.id]))

        self.assertEqual(manager_response.status_code, 200)
        self.assertContains(manager_response, reverse("approve_vacation", args=[request_obj.id]))
        self.assertContains(manager_response, reverse("reject_vacation", args=[request_obj.id]))
        self.assertContains(manager_response, reverse("delete_vacation", args=[request_obj.id]))
        self.assertContains(manager_response, "page-hero__toolset--vacation-detail")
        self.assertContains(manager_response, "vacation-detail-actions")
        self.assertContains(manager_response, "page-hero__center")
        self.assertContains(manager_response, "page-hero__back-link")
        self.assertContains(manager_response, "К заявкам")
        self.assertContains(manager_response, 'data-section-back-link="applications"')
        self.assertNotContains(manager_response, "vacation-decision-card__actions")
        manager_html = manager_response.content.decode()
        self.assertLess(manager_html.index("vacation-detail-actions"), manager_html.index("vacation-decision-card"))
        self.assertContains(manager_response, reverse("employee_profile", args=[self.employee.id]))
        self.assertContains(manager_response, f'{reverse("employee_profile", args=[self.employee.id])}?from=applications')
        self.assertContains(
            manager_response,
            f'{reverse("employee_profile", args=[self.employee.id])}?from=applications&amp;return_to=vacation&amp;vacation_id={request_obj.id}',
        )
        self.assertContains(manager_response, "vacation-employee-card")
        self.assertContains(manager_response, "vacation-employee-card__profile-icon--employee")
        self.assertContains(manager_response, 'aria-label="Открыть профиль сотрудника')
        self.assertNotContains(manager_response, "vacation-employee-card__badges")
        self.assertNotContains(manager_response, "vacation-decision-card__profile-link")
        self.assertNotContains(manager_response, "<span>Открыть профиль</span>", html=True)
        self.assertContains(manager_response, "vacation-decision-summary")
        self.assertContains(manager_response, "vacation-summary-item--position")
        self.assertContains(manager_response, "vacation-summary-item--department")
        self.assertContains(manager_response, "vacation-summary-item--group")
        self.assertContains(manager_response, "Должность")
        self.assertContains(manager_response, "Отдел")
        self.assertContains(manager_response, "Группа отдела")
        self.assertContains(manager_response, self.employee.position)
        self.assertContains(manager_response, self.engineering.name)
        self.assertContains(manager_response, self.engineering_group.name)
        self.assertContains(manager_response, "vacation-decision-panel")
        self.assertContains(manager_response, "vacation-action-button--approve")
        self.assertContains(manager_response, "На сегодня")
        self.assertContains(manager_response, "Доступно на дату начала")
        self.assertContains(manager_response, "Останется после заявки")
        self.assertContains(manager_response, "Источник дней")
        self.assertContains(manager_response, 'data-tooltip-title="Оценка риска"')
        self.assertContains(manager_response, 'data-tooltip-title="Источник дней"')
        self.assertContains(manager_response, "Дни будут списаны из рабочего года")
        self.assertContains(manager_response, "Списывается:")
        self.assertContains(manager_response, "Текущий оплачиваемый баланс")
        self.assertNotContains(manager_response, "Начислено к началу отпуска")
        self.assertNotContains(manager_response, "Баланс к началу отпуска")
        self.assertContains(manager_response, "Маршрут")
        self.assertContains(manager_response, "История заявки")
        self.assertContains(manager_response, "Оценка модуля")
        self.assertContains(manager_response, "На сейчас")
        self.assertNotContains(manager_response, "Рекомендация системы будет доступна после подключения аналитического модуля")
        self.assertContains(manager_response, "Руководитель отдела")
        request_obj.refresh_from_db()
        self.assertIsNone(request_obj.ai_score)

        self.assertEqual(reviewed_response.status_code, 200)
        self.assertContains(reviewed_response, "vacation-detail-actions")
        self.assertContains(reviewed_response, "Заявка уже рассмотрена")
        reviewed_html = reviewed_response.content.decode()
        self.assertLess(reviewed_html.index("Заявка уже рассмотрена"), reviewed_html.index("vacation-decision-card"))

        self.assertEqual(enterprise_response.status_code, 200)
        self.assertNotContains(enterprise_response, reverse("approve_vacation", args=[request_obj.id]))
        self.assertNotContains(enterprise_response, reverse("reject_vacation", args=[request_obj.id]))
        self.assertContains(enterprise_response, "Оценка модуля")
        self.assertContains(enterprise_response, "На сейчас")

        self.assertEqual(role_response.status_code, 200)
        self.assertContains(role_response, "vacation-employee-card__badges")
        self.assertContains(role_response, "vacation-employee-card__badge--department-head")
        self.assertContains(role_response, "Руководитель отдела")

    def test_vacation_detail_pending_request_shows_live_ai_with_submission_snapshot(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 12, 15),
            end_date=date(2026, 12, 17),
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
            ai_score=Decimal("84.25"),
            ai_confidence=Decimal("78.50"),
            ai_model_version="test-ai-v1",
            ai_recommendation="prefer",
            ai_explanation="Сохраненная подсказка модуля для руководителя.",
            ai_scorer_kind="test",
        )
        self.client.force_login(self.department_head.user)

        response = self.client.get(reverse("vacation_detail", args=[request_obj.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Оценка модуля")
        self.assertContains(response, "На сейчас")
        self.assertContains(response, "На момент подачи 84,25%")
        self.assertNotContains(response, "Сохраненная подсказка модуля для руководителя.")
        self.assertNotContains(response, "Пересчитано сейчас")

    def test_vacation_detail_resolved_request_shows_decision_ai_snapshot(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 12, 15),
            end_date=date(2026, 12, 17),
            vacation_type="unpaid",
            status=VacationRequest.STATUS_APPROVED,
            reviewed_by=self.department_head,
            reviewed_at=timezone.now(),
            ai_score=Decimal("51.00"),
            ai_confidence=Decimal("63.00"),
            ai_model_version="submit-ai-v1",
            ai_recommendation="normal",
            ai_explanation="Снимок на момент подачи.",
            ai_scorer_kind="test",
            decision_ai_score=Decimal("84.25"),
            decision_ai_confidence=Decimal("78.50"),
            decision_ai_model_version="decision-ai-v1",
            decision_ai_recommendation="prefer",
            decision_ai_explanation="Сохраненная подсказка решения для руководителя.",
            decision_ai_scorer_kind="test",
            decision_ai_evaluated_at=timezone.now(),
        )
        self.client.force_login(self.department_head.user)

        response = self.client.get(reverse("vacation_detail", args=[request_obj.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Оценка модуля")
        self.assertContains(response, "На момент решения")
        self.assertContains(response, "Хороший период · 84,25%")
        self.assertContains(response, "Уверенность 78,50%")
        self.assertContains(response, "decision-ai-v1")
        self.assertContains(response, "Сохраненная подсказка решения для руководителя.")
        self.assertContains(response, "На момент подачи 51,00%")
        self.assertNotContains(response, "На сейчас")
        self.assertNotContains(response, "Пересчитано сейчас")

    def test_vacation_detail_keeps_source_navigation_context(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-12-15",
            end_date="2026-12-17",
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
        )
        self.client.force_login(self.hr_employee.user)

        employee_response = self.client.get(reverse("vacation_detail", args=[request_obj.id]), {"from": "employees"})
        department_response = self.client.get(reverse("vacation_detail", args=[request_obj.id]), {"from": "departments"})

        self.assertEqual(employee_response.status_code, 200)
        self.assertEqual(employee_response.context["sidebar_section"], "employees")
        self.assertContains(employee_response, "К сотрудникам")
        self.assertContains(employee_response, 'data-section-back-link="employees"')
        self.assertContains(employee_response, f'{reverse("employee_profile", args=[self.employee.id])}?from=employees')
        self.assertContains(
            employee_response,
            'data-sidebar-key="employees" aria-label="Сотрудники" title="Сотрудники" aria-current="page"',
        )
        self.assertNotContains(
            employee_response,
            'data-sidebar-key="applications" aria-label="Заявки" title="Заявки" aria-current="page"',
        )

        self.assertEqual(department_response.status_code, 200)
        self.assertEqual(department_response.context["sidebar_section"], "departments")
        self.assertContains(department_response, "К отделам")
        self.assertContains(department_response, 'data-section-back-link="departments"')
        self.assertContains(department_response, f'{reverse("employee_profile", args=[self.employee.id])}?from=departments')
        self.assertContains(
            department_response,
            'data-sidebar-key="departments" aria-label="Отделы" title="Отделы" aria-current="page"',
        )
        self.assertNotContains(
            department_response,
            'data-sidebar-key="applications" aria-label="Заявки" title="Заявки" aria-current="page"',
        )

    def test_vacation_detail_uses_explicit_back_link_to_employee_profile(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-12-15",
            end_date="2026-12-17",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        employee_url = f"{reverse('employee_profile', args=[self.employee.id])}?from=employees"
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("vacation_detail", args=[request_obj.id]),
            {
                "from": "employees",
                "back_url": employee_url,
                "back_label": "К сотруднику",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "employees")
        self.assertContains(response, "К сотруднику")
        self.assertContains(response, f'href="{employee_url}"')
        self.assertNotContains(response, 'data-section-back-link="employees"')

    def test_vacation_detail_uses_explicit_back_link_to_calendar_modal(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-12-15",
            end_date="2026-12-17",
            vacation_type="study",
            status=VacationRequest.STATUS_APPROVED,
        )
        calendar_url = (
            f"{reverse('calendar')}?view=year&year=2026&employee={self.employee.id}"
            f"&calendar_modal=employee_detail&calendar_employee={self.employee.id}&calendar_modal_scroll=280"
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("vacation_detail", args=[request_obj.id]),
            {
                "from": "calendar",
                "back_url": calendar_url,
                "back_label": "К графику",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "calendar")
        self.assertEqual(response.context["vacation_detail_back_link"]["url"], calendar_url)
        self.assertContains(response, "К графику")
        self.assertContains(response, "calendar_modal=employee_detail")
        self.assertContains(response, "calendar_modal_scroll=280")

    def test_vacation_detail_calendar_link_opens_year_view_and_focuses_employee_period(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 10, 5),
            end_date=date(2026, 10, 12),
            vacation_type="study",
            status=VacationRequest.STATUS_PENDING,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("vacation_detail", args=[request_obj.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [metric["label"] for metric in response.context["vacation_decision_context"]["metrics"]],
            ["Состав", "Одновременно отсутствуют", "Нагрузка", "Замещение"],
        )
        calendar_url = response.context["vacation_decision_context"]["calendar_url"]
        query = parse_qs(urlparse(calendar_url).query)
        self.assertEqual(query["view"], ["year"])
        self.assertEqual(query["year"], ["2026"])
        self.assertNotIn("month", query)
        self.assertEqual(query["employee"], [str(self.employee.id)])
        self.assertEqual(query["calendar_focus_employee"], [str(self.employee.id)])
        self.assertEqual(query["calendar_focus_start"], ["2026-10-05"])
        self.assertEqual(query["calendar_focus_end"], ["2026-10-12"])
        self.assertContains(response, "Открыть период в графике")
        self.assertContains(response, "calendar_focus_start=2026-10-05")

    def test_schedule_change_detail_calendar_link_opens_year_view_and_focuses_new_period(self):
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
            new_start_date=date(future_year, 9, 2),
            new_end_date=date(future_year, 9, 15),
            reason="Семейные обстоятельства.",
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("schedule_change_detail", args=[change_request.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [metric["label"] for metric in response.context["schedule_change_decision_context"]["metrics"]],
            ["Состав", "Одновременно отсутствуют", "Нагрузка", "Замещение"],
        )
        calendar_url = response.context["schedule_change_decision_context"]["calendar_url"]
        query = parse_qs(urlparse(calendar_url).query)
        self.assertEqual(query["view"], ["year"])
        self.assertEqual(query["year"], [str(future_year)])
        self.assertNotIn("month", query)
        self.assertEqual(query["employee"], [str(self.employee.id)])
        self.assertEqual(query["calendar_focus_employee"], [str(self.employee.id)])
        self.assertEqual(query["calendar_focus_start"], [f"{future_year}-09-02"])
        self.assertEqual(query["calendar_focus_end"], [f"{future_year}-09-15"])
        self.assertContains(response, "Открыть новый период в графике")
        self.assertContains(response, f"calendar_focus_start={future_year}-09-02")

    def test_vacation_detail_uses_live_risk_context_and_shows_saved_snapshot_when_changed(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 9, 12),
            end_date=date(2026, 9, 18),
            vacation_type="study",
            status=VacationRequest.STATUS_PENDING,
            risk_score=10,
            risk_level=VacationRequest.RISK_LOW,
            overlapping_absences_count=0,
            remaining_staff_count=2,
            min_staff_required=0,
            department_load_level=1,
        )
        VacationRequest.objects.create(
            employee=self.department_head,
            start_date=date(2026, 9, 12),
            end_date=date(2026, 9, 18),
            vacation_type="unpaid",
            status=VacationRequest.STATUS_APPROVED,
        )
        self.client.force_login(self.department_head.user)

        response = self.client.get(reverse("vacation_detail", args=[request_obj.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Риск изменился после подачи заявки")
        self.assertContains(response, "Сейчас:")
        self.assertContains(response, "На момент подачи:")
        self.assertContains(response, "пересечения — 1 сотрудник")
        self.assertContains(response, "пересечения — 0 сотрудников")
        self.assertContains(
            response,
            "<dt>Одновременно отсутствуют</dt><dd>1 сотрудник</dd>",
            html=True,
        )
        self.assertNotContains(
            response,
            "<dt>Одновременно отсутствуют</dt><dd>0 сотрудников</dd>",
            html=True,
        )

    def test_vacation_detail_hides_paid_balance_for_non_paid_requests(self):
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

        self.client.force_login(self.department_head.user)
        unpaid_response = self.client.get(reverse("vacation_detail", args=[unpaid_request.id]))
        study_response = self.client.get(reverse("vacation_detail", args=[study_request.id]))

        self.assertEqual(unpaid_response.status_code, 200)
        self.assertContains(unpaid_response, "Неоплачиваемый отпуск оформляется без сохранения заработной платы")
        self.assertContains(unpaid_response, "Не списывается")
        self.assertContains(unpaid_response, "Текущий оплачиваемый баланс")
        self.assertNotContains(unpaid_response, "На сегодня")
        self.assertNotContains(unpaid_response, "После рассмотрения")
        self.assertNotContains(unpaid_response, "Останется после заявки")
        self.assertNotContains(unpaid_response, "Начислено к началу отпуска")
        self.assertNotContains(unpaid_response, "Баланс по рабочим годам")

        self.assertEqual(study_response.status_code, 200)
        self.assertContains(study_response, "Учебный отпуск не уменьшает остаток ежегодного оплачиваемого отпуска")
        self.assertContains(study_response, "Не списывается")
        self.assertContains(study_response, "Текущий оплачиваемый баланс")
        self.assertNotContains(study_response, "На сегодня")
        self.assertNotContains(study_response, "После рассмотрения")
        self.assertNotContains(study_response, "Останется после заявки")
        self.assertNotContains(study_response, "Начислено к началу отпуска")
        self.assertNotContains(study_response, "Баланс по рабочим годам")

    def test_non_paid_vacation_detail_ignores_invalid_paid_entitlement_rows(self):
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 1, 10),
            end_date=date(2026, 1, 11),
            vacation_type="paid",
            chargeable_days=500,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        unpaid_request = VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 3),
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )

        self.client.force_login(self.hr_employee.user)
        response = self.client.get(reverse("vacation_detail", args=[unpaid_request.id]), {"from": "employees"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Неоплачиваемый отпуск оформляется без сохранения заработной платы")
        self.assertContains(response, "Не списывается")
        self.assertNotContains(response, "Баланс по рабочим годам")

    def test_vacation_detail_redirects_when_request_was_deleted(self):
        deleted_request_id = 987654
        self.client.force_login(self.department_head.user)

        response = self.client.get(reverse("vacation_detail", args=[deleted_request_id]))

        self.assertRedirects(response, reverse("applications"))

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
