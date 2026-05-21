from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.urls import reverse

from apps.leave.models import (
    VacationRequest,
    VacationSchedule,
    VacationScheduleChangeRequest,
    VacationScheduleItem,
    VacationUrgentClosureRequest,
)
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
            reason="Нужно закрыть учебную сессию.",
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
        self.assertEqual(
            payload["vacations"][0]["profile_url"],
            f'{reverse("employee_profile", args=[self.employee.id])}?from=applications',
        )
        self.assertEqual(payload["vacations"][0]["employee_role_icon"], "person")
        self.assertEqual(payload["vacations"][0]["employee_role_icon_type"], "material")
        self.assertEqual(payload["vacations"][0]["employee_role_variant"], "employee")
        self.assertEqual(payload["vacations"][0]["employee_role_label"], "Сотрудник")
        self.assertEqual(payload["vacations"][0]["employee_secondary_label"], self.employee.department.name)
        self.assertEqual(payload["vacations"][0]["period_label"], "01.11.2026 - 03.11.2026")
        self.assertEqual(payload["vacations"][0]["reason_preview"], "Нужно закрыть учебную сессию.")
        self.assertIsNone(payload["vacations"][0]["module_badge"])
        self.assertIn("period_label", payload["vacations"][0])
        self.assertIn("vacations_html", payload)
        self.assertIn("change_requests_html", payload)
        self.assertIn(f'data-vacation-id="{request_obj.id}"', payload["vacations_html"])
        self.assertIn(f'data-href="{reverse("vacation_detail", args=[request_obj.id])}"', payload["vacations_html"])
        self.assertIn(f'href="{reverse("employee_profile", args=[self.employee.id])}?from=applications"', payload["vacations_html"])
        self.assertIn("application-card__profile-icon application-card__profile-icon--employee", payload["vacations_html"])
        self.assertIn('aria-label="Открыть профиль сотрудника', payload["vacations_html"])
        self.assertIn('<span class="application-card__label">ФИО</span>', payload["vacations_html"])
        self.assertIn("01.11.2026 - 03.11.2026", payload["vacations_html"])
        self.assertNotIn("Нужно закрыть учебную сессию.", payload["vacations_html"])
        self.assertNotIn("ноября", payload["vacations_html"])
        self.assertNotIn('<span class="application-card__label">Сотрудник</span>', payload["vacations_html"])
        self.assertNotIn("<span>Профиль</span>", payload["vacations_html"])
        self.assertNotIn("application-card__module-badge", payload["vacations_html"])
        self.assertIn('role="link"', payload["vacations_html"])

    def test_applications_request_card_shows_submission_module_badge(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-11-01",
            end_date="2026-11-03",
            vacation_type="study",
            status=VacationRequest.STATUS_PENDING,
            ai_score="82.00",
            ai_recommendation="avoid",
            ai_explanation="Модуль советует проверить соседние даты.",
        )
        self.client.force_login(self.department_head.user)

        response = self.client.get(
            reverse("applications"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        badge = payload["vacations"][0]["module_badge"]
        self.assertEqual(payload["vacations"][0]["id"], request_obj.id)
        self.assertEqual(badge["score_label"], "82%")
        self.assertEqual(badge["recommendation_label"], "лучше проверить")
        self.assertEqual(badge["variant"], "medium")
        self.assertEqual(badge["source_label"], "На момент подачи")
        self.assertIn("Модуль 82% · лучше проверить", payload["vacations_html"])
        self.assertIn("application-card__module-badge--medium", payload["vacations_html"])

    def test_applications_request_card_prefers_decision_module_badge_for_reviewed_request(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-12-01",
            end_date="2026-12-03",
            vacation_type="study",
            status=VacationRequest.STATUS_APPROVED,
            ai_score="10.00",
            ai_recommendation="prefer",
            ai_explanation="Старая оценка на момент подачи.",
            decision_ai_score="92.00",
            decision_ai_recommendation="blocked",
            decision_ai_explanation="На момент решения есть блокирующие правила.",
        )
        self.client.force_login(self.department_head.user)

        response = self.client.get(
            reverse("applications"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        badge = payload["vacations"][0]["module_badge"]
        self.assertEqual(payload["vacations"][0]["id"], request_obj.id)
        self.assertEqual(badge["score_label"], "92%")
        self.assertEqual(badge["recommendation_label"], "есть ограничения")
        self.assertEqual(badge["variant"], "risk")
        self.assertEqual(badge["source_label"], "На момент решения")
        self.assertIn("Модуль 92% · есть ограничения", payload["vacations_html"])
        self.assertNotIn("Модуль 10%", payload["vacations_html"])

    def test_reviewed_vacation_detail_uses_saved_decision_risk_snapshot(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2027, 8, 1),
            end_date=date(2027, 8, 14),
            vacation_type="paid",
            status=VacationRequest.STATUS_REJECTED,
            reviewed_by=self.department_head,
            risk_score=95,
            risk_level=VacationRequest.RISK_HIGH,
            department_load_level=5,
            overlapping_absences_count=7,
            remaining_staff_count=13,
            min_staff_required=13,
            balance_after_request=Decimal("11.00"),
        )

        self.client.force_login(self.department_head.user)
        with patch(
            "apps.leave.services.requests.build_vacation_object_risk_explanation",
            side_effect=AssertionError("Reviewed detail must use the saved decision snapshot."),
        ):
            response = self.client.get(reverse("vacation_detail", args=[request_obj.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["vacation"].balance_after_request, Decimal("11.00"))
        self.assertFalse(response.context["vacation_saved_risk_snapshot_changed"])
        self.assertNotContains(response, "Недостаточно дней")
        self.assertContains(response, "По сохраненному расчету отдел остается ровно на минимуме")

    def test_applications_transfer_card_shows_saved_module_badge(self):
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
        change_request = VacationScheduleChangeRequest.objects.create(
            schedule_item=schedule_item,
            employee=self.employee,
            old_start_date=schedule_item.start_date,
            old_end_date=schedule_item.end_date,
            new_start_date=date(2027, 8, 1),
            new_end_date=date(2027, 8, 14),
            requested_by=self.employee,
            risk_score=48,
            risk_level=VacationScheduleItem.RISK_MEDIUM,
            ai_score="82.00",
            ai_recommendation="avoid",
            ai_explanation="Модуль советует проверить перенос перед решением.",
        )
        self.client.force_login(self.department_head.user)

        response = self.client.get(
            reverse("applications"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        badge = payload["change_requests"][0]["module_badge"]
        self.assertEqual(payload["change_requests"][0]["id"], change_request.id)
        self.assertEqual(badge["score_label"], "82%")
        self.assertEqual(badge["recommendation_label"], "лучше проверить")
        self.assertEqual(badge["variant"], "medium")
        self.assertEqual(badge["source_label"], "При создании переноса")
        self.assertIn("Модуль 82% · лучше проверить", payload["change_requests_html"])
        self.assertIn("application-card__module-badge--medium", payload["change_requests_html"])

    def test_applications_reviewed_transfer_card_shows_decision_module_badge(self):
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
        change_request = VacationScheduleChangeRequest.objects.create(
            schedule_item=schedule_item,
            employee=self.employee,
            old_start_date=schedule_item.start_date,
            old_end_date=schedule_item.end_date,
            new_start_date=date(2027, 8, 1),
            new_end_date=date(2027, 8, 14),
            requested_by=self.employee,
            status=VacationScheduleChangeRequest.STATUS_APPROVED,
            reviewed_by=self.department_head,
            risk_score=48,
            risk_level=VacationScheduleItem.RISK_MEDIUM,
            ai_score="82.00",
            ai_recommendation="avoid",
            ai_explanation="Модуль советовал проверить перенос при создании.",
            decision_ai_score="92.00",
            decision_ai_recommendation="blocked",
            decision_ai_explanation="На момент решения перенос получил ограничения.",
        )
        self.client.force_login(self.department_head.user)

        with patch("apps.leave.services.schedule_changes.build_schedule_change_ai_support") as scorer:
            response = self.client.get(
                reverse("applications"),
                {"status": VacationScheduleChangeRequest.STATUS_APPROVED},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        badge = payload["change_requests"][0]["module_badge"]
        self.assertEqual(payload["change_requests"][0]["id"], change_request.id)
        self.assertEqual(badge["score_label"], "92%")
        self.assertEqual(badge["recommendation_label"], "есть ограничения")
        self.assertEqual(badge["variant"], "risk")
        self.assertEqual(badge["source_label"], "На момент решения")
        self.assertIn("Модуль 92% · есть ограничения", payload["change_requests_html"])
        self.assertNotIn("Модуль 82%", payload["change_requests_html"])
        scorer.assert_not_called()

    def test_applications_urgent_closure_card_shows_active_module_badge(self):
        closure_request = VacationUrgentClosureRequest.objects.create(
            employee=self.employee,
            planning_year=2027,
            closure_year=2026,
            required_days="3.00",
            deadline=date(2027, 1, 3),
            proposed_start_date=date(2026, 12, 29),
            proposed_end_date=date(2026, 12, 31),
            created_by=self.hr_employee,
            risk_score=34,
            risk_level=VacationScheduleItem.RISK_LOW,
            ai_score="78.00",
            ai_recommendation="normal",
            ai_explanation="Срочный остаток можно отправлять по маршруту.",
        )
        self.client.force_login(self.department_head.user)

        response = self.client.get(
            reverse("applications"),
            {"status": VacationRequest.STATUS_PENDING},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        badge = payload["change_requests"][0]["module_badge"]
        self.assertEqual(payload["change_requests"][0]["id"], closure_request.id)
        self.assertEqual(badge["score_label"], "78%")
        self.assertEqual(badge["recommendation_label"], "можно одобрять")
        self.assertEqual(badge["variant"], "info")
        self.assertEqual(badge["source_label"], "Текущий период")
        self.assertIn("Модуль 78% · можно одобрять", payload["change_requests_html"])

    def test_applications_completed_urgent_closure_card_shows_decision_module_badge(self):
        closure_request = VacationUrgentClosureRequest.objects.create(
            employee=self.employee,
            planning_year=2027,
            closure_year=2026,
            required_days="3.00",
            deadline=date(2027, 1, 3),
            proposed_start_date=date(2026, 12, 29),
            proposed_end_date=date(2026, 12, 31),
            created_by=self.hr_employee,
            finalized_by=self.hr_employee,
            status=VacationUrgentClosureRequest.STATUS_COMPLETED,
            risk_score=58,
            risk_level=VacationScheduleItem.RISK_MEDIUM,
            ai_score="64.00",
            ai_recommendation="avoid",
            ai_explanation="Снимок до решения.",
            decision_ai_score="84.00",
            decision_ai_recommendation="prefer",
            decision_ai_explanation="Финальный период подходит для закрытия остатка.",
        )
        self.client.force_login(self.department_head.user)

        response = self.client.get(
            reverse("applications"),
            {"status": VacationRequest.STATUS_APPROVED},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        badge = payload["change_requests"][0]["module_badge"]
        self.assertEqual(payload["change_requests"][0]["id"], closure_request.id)
        self.assertEqual(badge["score_label"], "84%")
        self.assertEqual(badge["recommendation_label"], "удачный период")
        self.assertEqual(badge["variant"], "planned")
        self.assertEqual(badge["source_label"], "На момент решения")
        self.assertIn("Модуль 84% · удачный период", payload["change_requests_html"])
        self.assertNotIn("Модуль 64%", payload["change_requests_html"])

    def test_applications_transfer_card_calculates_live_module_badge_without_saved_score(self):
        schedule = VacationSchedule.objects.create(
            year=2027,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 9, 1),
            end_date=date(2027, 9, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        change_request = VacationScheduleChangeRequest.objects.create(
            schedule_item=schedule_item,
            employee=self.employee,
            old_start_date=schedule_item.start_date,
            old_end_date=schedule_item.end_date,
            new_start_date=date(2027, 10, 1),
            new_end_date=date(2027, 10, 14),
            requested_by=self.employee,
            risk_score=12,
            risk_level=VacationScheduleItem.RISK_LOW,
        )
        self.client.force_login(self.department_head.user)

        with patch(
            "apps.leave.services.schedule_changes.build_schedule_change_ai_support",
            return_value={
                "module_score": 76.2,
                "module_recommendation": "normal",
                "module_explanation": "Живая оценка для старого переноса.",
            },
        ) as scorer:
            response = self.client.get(
                reverse("applications"),
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        badge = payload["change_requests"][0]["module_badge"]
        self.assertEqual(payload["change_requests"][0]["id"], change_request.id)
        self.assertEqual(badge["score_label"], "76%")
        self.assertEqual(badge["recommendation_label"], "можно одобрять")
        self.assertEqual(badge["variant"], "info")
        self.assertEqual(badge["source_label"], "На сейчас")
        self.assertIn("Модуль 76% · можно одобрять", payload["change_requests_html"])
        scorer.assert_called_once()
        self.assertEqual(scorer.call_args.kwargs["exclude_change_request_id"], change_request.id)

    def test_applications_reviewed_transfer_card_reconstructs_badge_without_decision_snapshot(self):
        schedule = VacationSchedule.objects.create(
            year=2027,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 9, 1),
            end_date=date(2027, 9, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        change_request = VacationScheduleChangeRequest.objects.create(
            schedule_item=schedule_item,
            employee=self.employee,
            old_start_date=schedule_item.start_date,
            old_end_date=schedule_item.end_date,
            new_start_date=date(2027, 10, 1),
            new_end_date=date(2027, 10, 14),
            requested_by=self.employee,
            status=VacationScheduleChangeRequest.STATUS_APPROVED,
            reviewed_by=self.department_head,
            risk_score=12,
            risk_level=VacationScheduleItem.RISK_LOW,
        )
        self.client.force_login(self.department_head.user)

        with patch(
            "apps.leave.services.schedule_changes.build_schedule_change_ai_support",
            return_value={
                "module_score": 69.4,
                "module_recommendation": "avoid",
                "module_explanation": "Восстановленная оценка закрытого переноса.",
            },
        ) as scorer:
            response = self.client.get(
                reverse("applications"),
                {"status": VacationScheduleChangeRequest.STATUS_APPROVED},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["change_requests"][0]["id"], change_request.id)
        badge = payload["change_requests"][0]["module_badge"]
        self.assertEqual(badge["score_label"], "69%")
        self.assertEqual(badge["recommendation_label"], "лучше проверить")
        self.assertEqual(badge["variant"], "medium")
        self.assertEqual(badge["source_label"], "На момент решения")
        self.assertIn("Модуль 69% · лучше проверить", payload["change_requests_html"])
        scorer.assert_called_once()
        self.assertTrue(scorer.call_args.kwargs["can_submit"])
        self.assertEqual(scorer.call_args.kwargs["exclude_change_request_id"], change_request.id)

    def test_applications_search_filters_requests_and_transfers_by_employee_name(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-11-01",
            end_date="2026-11-03",
            vacation_type="study",
            status=VacationRequest.STATUS_PENDING,
            reason="Причина заявки для карточки.",
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
        self.assertEqual(payload["change_requests"][0]["reason_preview"], "Search test")
        self.assertIn(f'data-vacation-id="{request_obj.id}"', payload["vacations_html"])
        self.assertIn(f'data-change-request-id="{change_request.id}"', payload["change_requests_html"])
        self.assertNotIn("Search test", payload["change_requests_html"])
        self.assertIn(f'href="{reverse("employee_profile", args=[self.employee.id])}?from=applications"', payload["change_requests_html"])
        self.assertIn("application-card__profile-icon application-card__profile-icon--employee", payload["change_requests_html"])
        self.assertIn('<span class="application-card__label">ФИО</span>', payload["change_requests_html"])
        self.assertNotIn('<span class="application-card__label">Сотрудник</span>', payload["change_requests_html"])
        self.assertNotIn("<span>Профиль</span>", payload["change_requests_html"])

    def test_schedule_change_detail_page_shows_context_and_controls(self):
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
            reason="Нужно перенести отпуск.",
        )
        detail_url = reverse("schedule_change_detail", args=[change_request.id])

        self.client.force_login(self.employee.user)
        employee_response = self.client.get(detail_url)
        self.assertEqual(employee_response.status_code, 200)
        self.assertContains(employee_response, "Перенос отпуска")
        self.assertContains(employee_response, "01.07.2027 - 14.07.2027")
        self.assertContains(employee_response, "01.08.2027 - 14.08.2027")
        self.assertContains(employee_response, "Запрос сотрудника")
        self.assertContains(employee_response, "Решение недоступно для вашей роли")
        self.assertNotContains(employee_response, reverse("schedule_change_approve", args=[change_request.id]))

        self.client.force_login(self.department_head.user)
        approver_response = self.client.get(detail_url)
        self.assertEqual(approver_response.status_code, 200)
        self.assertContains(approver_response, reverse("schedule_change_approve", args=[change_request.id]))
        self.assertContains(approver_response, reverse("schedule_change_reject", args=[change_request.id]))
        self.assertContains(approver_response, 'name="csrfmiddlewaretoken"')

        self.client.force_login(self.outsider.user)
        outsider_response = self.client.get(detail_url)
        self.assertRedirects(outsider_response, reverse("main"))

    def test_schedule_change_detail_approve_redirects_back_to_detail(self):
        schedule = VacationSchedule.objects.create(
            year=2027,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 9, 1),
            end_date=date(2027, 9, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.employee,
            new_start_date=date(2027, 10, 1),
            new_end_date=date(2027, 10, 14),
            reason="Нужно перенести отпуск.",
        )
        self.client.force_login(self.department_head.user)

        response = self.client.post(reverse("schedule_change_approve", args=[change_request.id]))

        self.assertRedirects(response, reverse("schedule_change_detail", args=[change_request.id]))
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, change_request.STATUS_APPROVED)

    def test_schedule_change_create_redirects_to_safe_next_url(self):
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
        next_url = f'{reverse("main")}?section=schedule'
        self.client.force_login(self.employee.user)

        response = self.client.post(
            reverse("schedule_change_request_create", args=[schedule_item.id]),
            {
                "new_start_date": "2027-08-01",
                "new_end_date": "2027-08-14",
                "reason": "Нужно перенести отпуск.",
                "next_url": next_url,
            },
        )

        self.assertRedirects(response, next_url)

    def test_manager_initiated_schedule_change_detail_uses_employee_decision_labels(self):
        schedule = VacationSchedule.objects.create(
            year=2027,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 9, 1),
            end_date=date(2027, 9, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.department_head,
            new_start_date=date(2027, 10, 1),
            new_end_date=date(2027, 10, 14),
            reason="Предложение руководителя.",
        )
        detail_url = reverse("schedule_change_detail", args=[change_request.id])

        self.client.force_login(self.department_head.user)
        manager_response = self.client.get(detail_url)
        self.assertEqual(manager_response.status_code, 200)
        self.assertContains(manager_response, "Предложение руководителя")
        self.assertContains(manager_response, "Ожидается решение сотрудника")
        self.assertNotContains(manager_response, "Принять перенос")

        self.client.force_login(self.employee.user)
        employee_response = self.client.get(detail_url)
        self.assertEqual(employee_response.status_code, 200)
        self.assertContains(employee_response, "Принять перенос")
        self.assertContains(employee_response, "Отклонить предложение")

        response = self.client.post(reverse("schedule_change_approve", args=[change_request.id]))
        self.assertRedirects(response, detail_url)
        change_request.refresh_from_db()
        self.assertEqual(change_request.status, change_request.STATUS_APPROVED)
        self.assertEqual(change_request.reviewed_by_id, self.employee.id)

    def test_manager_initiated_schedule_change_card_waits_for_employee(self):
        schedule = VacationSchedule.objects.create(
            year=2027,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 11, 1),
            end_date=date(2027, 11, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.department_head,
            new_start_date=date(2027, 12, 1),
            new_end_date=date(2027, 12, 14),
            reason="Предложение руководителя.",
        )
        self.client.force_login(self.department_head.user)

        response = self.client.get(reverse("applications"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Предложение руководителя")
        self.assertContains(response, "Ожидает сотрудника")
        self.assertContains(response, "hourglass_top")
        self.assertNotContains(response, reverse("schedule_change_approve", args=[change_request.id]))
        self.assertNotContains(response, reverse("schedule_change_reject", args=[change_request.id]))

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

    def test_applications_filters_requests_and_transfers_by_group(self):
        matching_request = VacationRequest.objects.create(
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
        matching_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        foreign_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.outsider,
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        matching_change = create_schedule_change_request(
            matching_item.id,
            requested_by=self.employee,
            new_start_date=date(2026, 8, 1),
            new_end_date=date(2026, 8, 14),
            reason="Group test",
        )
        create_schedule_change_request(
            foreign_item.id,
            requested_by=self.outsider,
            new_start_date=date(2026, 10, 1),
            new_end_date=date(2026, 10, 14),
            reason="Foreign group test",
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("applications"),
            {"group": self.engineering_group.id},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item["id"] for item in payload["vacations"]], [matching_request.id])
        self.assertEqual([item["id"] for item in payload["change_requests"]], [matching_change.id])
        self.assertIn(f'data-vacation-id="{matching_request.id}"', payload["vacations_html"])
        self.assertIn(f'data-change-request-id="{matching_change.id}"', payload["change_requests_html"])

    def test_applications_group_filter_marks_both_board_selects_selected(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("applications"),
            {"group": self.engineering_group.id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_group"], str(self.engineering_group.id))
        self.assertContains(
            response,
            f'<option value="{self.engineering_group.id}" data-department-id="{self.engineering.id}"',
            count=2,
            html=False,
        )
        content = response.content.decode(response.charset or "utf-8")
        self.assertNotIn(
            f'<option value="{self.engineering_group.id}" data-department-id="{self.engineering.id}" hidden disabled',
            content,
        )
        self.assertContains(
            response,
            (
                'class="employee-select__option is-selected" '
                f'data-employee-select-option data-value="{self.engineering_group.id}"'
            ),
            count=2,
            html=False,
        )

    def test_applications_department_filter_hides_foreign_group_options(self):
        self.client.force_login(self.enterprise_head.user)

        response = self.client.get(
            reverse("applications"),
            {"department": self.engineering.id},
        )

        self.assertEqual(response.status_code, 200)
        content = response.content.decode(response.charset or "utf-8")
        self.assertEqual(
            content.count(
                f'<option value="{self.hr_group.id}" data-department-id="{self.hr_department.id}" hidden disabled'
            ),
            2,
        )
        self.assertEqual(
            content.count(
                f'data-value="{self.hr_group.id}" data-department-id="{self.hr_department.id}" hidden disabled'
            ),
            2,
        )
        self.assertNotIn(
            f'<option value="{self.engineering_group.id}" data-department-id="{self.engineering.id}" hidden disabled',
            content,
        )

    def test_applications_filters_vacation_requests_by_type(self):
        study_request = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-11-01",
            end_date="2026-11-03",
            vacation_type="study",
            status=VacationRequest.STATUS_PENDING,
        )
        VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-12-01",
            end_date="2026-12-03",
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
        )
        VacationRequest.objects.create(
            employee=self.employee,
            start_date="2027-01-10",
            end_date="2027-01-12",
            vacation_type="unpaid",
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
            reason="Type filter test",
        )
        self.client.force_login(self.department_head.user)

        response = self.client.get(
            reverse("applications"),
            {"vacation_type": "study"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item["id"] for item in payload["vacations"]], [study_request.id])
        self.assertEqual([item["id"] for item in payload["change_requests"]], [change_request.id])
        self.assertIn("Учебный", payload["vacations_html"])
        self.assertNotIn("Оплачиваемый вне графика", payload["vacations_html"])
        self.assertIn(f'data-change-request-id="{change_request.id}"', payload["change_requests_html"])

    def test_department_head_applications_show_group_filter_without_department_select(self):
        self.client.force_login(self.department_head.user)

        response = self.client.get(reverse("applications"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="applications-transfers-group"')
        self.assertContains(response, 'id="applications-requests-group"')
        self.assertContains(response, "data-applications-group-filter", count=2)
        self.assertContains(response, 'id="vacation-type-filter"')
        self.assertContains(response, "Все отпуска")
        self.assertNotContains(response, 'id="department"')
        self.assertNotContains(response, 'id="applications-department-form"')
        self.assertContains(response, self.engineering_group.name)
        self.assertNotContains(response, self.hr_group.name)

    def test_applications_page_places_linked_department_and_group_filters_on_boards(self):
        self.client.force_login(self.enterprise_head.user)

        response = self.client.get(reverse("applications"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="applications-department-form"')
        self.assertContains(response, 'id="applications-transfers-department"')
        self.assertContains(response, 'id="applications-requests-department"')
        self.assertContains(response, 'id="applications-transfers-group"')
        self.assertContains(response, 'id="applications-requests-group"')
        self.assertContains(response, "data-applications-department-filter", count=2)
        self.assertContains(response, "data-applications-group-filter", count=2)

        content = response.content.decode(response.charset or "utf-8")
        self.assertLess(
            content.index('id="applications-status-form-transfers"'),
            content.index('id="applications-transfers-department"'),
        )
        self.assertLess(
            content.index('id="applications-transfers-department"'),
            content.index('id="applications-transfers-group"'),
        )
        self.assertLess(
            content.index('id="applications-transfers-group"'),
            content.index('id="applications-search-transfers"'),
        )
        self.assertLess(
            content.index('id="applications-status-form-requests"'),
            content.index('id="applications-requests-department"'),
        )
        self.assertLess(
            content.index('id="applications-requests-department"'),
            content.index('id="applications-requests-group"'),
        )
        self.assertLess(
            content.index('id="applications-requests-group"'),
            content.index('id="vacation-type-filter"'),
        )
        self.assertLess(
            content.index('id="vacation-type-filter"'),
            content.index('id="applications-search-requests"'),
        )

    def test_applications_combines_status_department_group_and_search_filters(self):
        matching_request = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-11-01",
            end_date="2026-11-03",
            vacation_type="study",
            status=VacationRequest.STATUS_PENDING,
        )
        VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-12-01",
            end_date="2026-12-03",
            vacation_type="study",
            status=VacationRequest.STATUS_APPROVED,
        )
        VacationRequest.objects.create(
            employee=self.outsider,
            start_date="2026-11-05",
            end_date="2026-11-07",
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
        )
        self.client.force_login(self.enterprise_head.user)

        response = self.client.get(
            reverse("applications"),
            {
                "status": VacationRequest.STATUS_PENDING,
                "department": self.engineering.id,
                "group": self.engineering_group.id,
                "search": self.employee.first_name,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item["id"] for item in payload["vacations"]], [matching_request.id])
        self.assertEqual(payload["change_requests"], [])

    def test_applications_page_uses_sectioned_cards_and_custom_department_select(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-11-01",
            end_date="2026-11-03",
            vacation_type="study",
            status=VacationRequest.STATUS_PENDING,
            reason="Причина заявки для карточки.",
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
        self.assertNotContains(response, "Причина заявки для карточки.")
        self.assertNotContains(response, "Проверка карточек.")
        self.assertNotContains(response, "data-applications-task-scope-form")
        self.assertNotContains(response, "data-task-scope-input")
        self.assertNotContains(response, "Мои задачи")
        self.assertContains(response, 'data-tooltip-title="Риск заявки"')
        self.assertContains(response, 'data-tooltip-title="Риск переноса"')
        self.assertContains(response, "data-schedule-status-tooltip")
        self.assertContains(response, reverse("employee_profile", args=[self.employee.id]))
        self.assertContains(response, "application-card__profile-icon application-card__profile-icon--employee")
        self.assertContains(response, '<span class="application-card__label">ФИО</span>')
        self.assertNotContains(response, "application-card__reason")
        self.assertNotContains(response, "application-card__initiator-name")
        self.assertNotContains(response, '<span class="application-card__label">Сотрудник</span>')
        self.assertNotContains(response, "<span>Профиль</span>")
        self.assertContains(response, reverse("schedule_change_detail", args=[change_request.id]))
        self.assertContains(response, "Запрос сотрудника")
        self.assertContains(response, "Инициатор:")
        self.assertContains(response, "application-card__initiator")
        self.assertContains(response, "Ваша задача")
        self.assertNotContains(response, reverse("schedule_change_approve", args=[change_request.id]))
        self.assertNotContains(response, reverse("schedule_change_reject", args=[change_request.id]))
        self.assertContains(response, 'class="employee-select__native"')
        self.assertContains(response, "data-applications-group-filter", count=2)
        self.assertNotContains(response, 'id="applications-department-form"')
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
        self.assertEqual(
            payload["change_requests"][0]["profile_url"],
            f'{reverse("employee_profile", args=[self.employee.id])}?from=applications',
        )
        self.assertEqual(payload["change_requests"][0]["employee_role_icon"], "person")
        self.assertEqual(payload["change_requests"][0]["employee_role_icon_type"], "material")
        self.assertEqual(payload["change_requests"][0]["employee_role_variant"], "employee")
        self.assertEqual(payload["change_requests"][0]["employee_role_label"], "Сотрудник")
        self.assertEqual(payload["change_requests"][0]["employee_secondary_label"], self.employee.department.name)
        self.assertEqual(payload["change_requests"][0]["employee_position_label"], self.employee.position)
        self.assertEqual(payload["change_requests"][0]["employee_department_label"], self.employee.department.name)
        self.assertEqual(payload["change_requests"][0]["employee_production_group_label"], self.engineering_group.name)
        self.assertEqual(payload["change_requests"][0]["detail_url"], reverse("schedule_change_detail", args=[change_request.id]))
        self.assertEqual(payload["change_requests"][0]["origin_label"], "Запрос сотрудника")
        self.assertEqual(payload["change_requests"][0]["initiator_name"], self.employee.full_name)
        self.assertIn("approve_url", payload["change_requests"][0])
        self.assertIn("reject_url", payload["change_requests"][0])
        self.assertIn(f'data-vacation-id="{pending_request.id}"', payload["vacations_html"])
        self.assertIn(f'data-change-request-id="{change_request.id}"', payload["change_requests_html"])
        self.assertIn(reverse("schedule_change_detail", args=[change_request.id]), payload["change_requests_html"])
        self.assertIn("Запрос сотрудника", payload["change_requests_html"])
        self.assertIn("Ваша задача", payload["change_requests_html"])
        self.assertNotIn(reverse("schedule_change_approve", args=[change_request.id]), payload["change_requests_html"])
        self.assertNotIn(reverse("schedule_change_reject", args=[change_request.id]), payload["change_requests_html"])
        self.assertNotIn('name="csrfmiddlewaretoken"', payload["change_requests_html"])

    def test_applications_my_tasks_filter_is_ignored_for_department_head(self):
        pending_request = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-10-01",
            end_date="2026-10-03",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
            reason="Нужна короткая отлучка.",
        )
        approved_request = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-10-10",
            end_date="2026-10-12",
            vacation_type="study",
            status=VacationRequest.STATUS_APPROVED,
        )
        schedule = VacationSchedule.objects.create(
            year=2027,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        employee_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 7, 1),
            end_date=date(2027, 7, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        employee_change = create_schedule_change_request(
            employee_item.id,
            requested_by=self.employee,
            new_start_date=date(2027, 8, 1),
            new_end_date=date(2027, 8, 14),
            reason="Сотрудник просит перенос.",
        )
        manager_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 9, 1),
            end_date=date(2027, 9, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        manager_proposal = create_schedule_change_request(
            manager_item.id,
            requested_by=self.department_head,
            new_start_date=date(2027, 10, 1),
            new_end_date=date(2027, 10, 14),
            reason="Руководитель предлагает перенос.",
        )
        self.client.force_login(self.department_head.user)

        response = self.client.get(
            reverse("applications"),
            {"task_scope": "mine"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual({item["id"] for item in payload["vacations"]}, {pending_request.id, approved_request.id})
        self.assertEqual({item["id"] for item in payload["change_requests"]}, {employee_change.id, manager_proposal.id})
        self.assertNotIn("Нужна короткая отлучка.", payload["vacations_html"])
        self.assertNotIn("Сотрудник просит перенос.", payload["change_requests_html"])
        self.assertIn(f'data-change-request-id="{manager_proposal.id}"', payload["change_requests_html"])
        self.assertIn("Запрос сотрудника", payload["change_requests_html"])
        self.assertIn("Предложение руководителя", payload["change_requests_html"])
        self.assertNotIn("Руководитель предлагает перенос.", payload["change_requests_html"])

        page_response = self.client.get(reverse("applications"), {"task_scope": "mine"})
        self.assertEqual(page_response.status_code, 200)
        self.assertEqual(page_response.context["selected_task_scope"], "all")
        self.assertFalse(page_response.context["show_task_scope_filter"])
        self.assertNotContains(page_response, "data-applications-task-scope-form")
        self.assertNotContains(page_response, "data-task-scope-input")

    def test_applications_my_tasks_filter_is_hidden_for_hr(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-11-01",
            end_date="2026-11-03",
            vacation_type="study",
            status=VacationRequest.STATUS_PENDING,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("applications"), {"task_scope": "mine"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_task_scope"], "all")
        self.assertFalse(response.context["show_task_scope_filter"])
        self.assertContains(response, f'data-vacation-id="{request_obj.id}"')
        self.assertNotContains(response, "data-applications-task-scope-form")
        self.assertNotContains(response, "Мои задачи")

    def test_applications_my_tasks_filter_shows_only_enterprise_head_decidable_items(self):
        hr_request = VacationRequest.objects.create(
            employee=self.hr_employee,
            start_date="2026-10-01",
            end_date="2026-10-03",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
            reason="HR заявка на согласование.",
        )
        employee_request = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-10-10",
            end_date="2026-10-12",
            vacation_type="study",
            status=VacationRequest.STATUS_PENDING,
            reason="Обычная заявка сотрудника.",
        )
        schedule = VacationSchedule.objects.create(
            year=2027,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        hr_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.hr_employee,
            start_date=date(2027, 3, 1),
            end_date=date(2027, 3, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        hr_change = create_schedule_change_request(
            hr_item.id,
            requested_by=self.hr_employee,
            new_start_date=date(2027, 4, 1),
            new_end_date=date(2027, 4, 14),
            reason="HR просит перенос.",
        )
        employee_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 5, 1),
            end_date=date(2027, 5, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        employee_change = create_schedule_change_request(
            employee_item.id,
            requested_by=self.employee,
            new_start_date=date(2027, 7, 1),
            new_end_date=date(2027, 7, 14),
            reason="Сотрудник просит перенос.",
        )
        self.client.force_login(self.enterprise_head.user)

        response = self.client.get(
            reverse("applications"),
            {"task_scope": "mine"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item["id"] for item in payload["vacations"]], [hr_request.id])
        self.assertEqual([item["id"] for item in payload["change_requests"]], [hr_change.id])
        self.assertNotIn("HR заявка на согласование.", payload["vacations_html"])
        self.assertNotIn("HR просит перенос.", payload["change_requests_html"])
        self.assertNotIn(f'data-vacation-id="{employee_request.id}"', payload["vacations_html"])
        self.assertNotIn(f'data-change-request-id="{employee_change.id}"', payload["change_requests_html"])

        page_response = self.client.get(reverse("applications"), {"task_scope": "mine"})
        self.assertEqual(page_response.status_code, 200)
        self.assertEqual(page_response.context["selected_task_scope"], "mine")
        self.assertTrue(page_response.context["show_task_scope_filter"])
        self.assertContains(page_response, "data-applications-task-scope-form", count=2)
        self.assertContains(page_response, "data-task-scope-input", count=2)

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
        self.assertEqual(requests_by_id[hr_request.id]["employee_position_label"], self.hr_employee.position)
        self.assertEqual(requests_by_id[hr_request.id]["employee_production_group_label"], self.hr_group.name)
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
        self.assertEqual(
            requests_by_id[department_head_request.id]["employee_management_badges"],
            [
                {
                    "label": "Руководитель отдела",
                    "icon": "admin_panel_settings",
                    "icon_type": "material",
                    "variant": "department-head",
                }
            ],
        )
        self.assertIn("application-card__profile-icon--hr", payload["vacations_html"])
        self.assertIn("application-card__profile-icon--department-head", payload["vacations_html"])
        self.assertIn("application-card__management-badge application-card__management-badge--department-head", payload["vacations_html"])
        self.assertIn("application-card__org-item--department", payload["vacations_html"])
        self.assertIn("application-card__org-item--group", payload["vacations_html"])
        self.assertIn(self.department_head.department.name, payload["vacations_html"])
        self.assertIn(self.engineering_leadership_group.name, payload["vacations_html"])
        self.assertNotIn("Отдел:", payload["vacations_html"])
        self.assertNotIn("Группа:", payload["vacations_html"])
        self.assertIn(self.department_head.position, payload["vacations_html"])

    def test_applications_identity_marks_department_deputy_like_employee_cards(self):
        self.engineering.deputy = self.employee
        self.engineering.save(update_fields=["deputy"])
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-12-01",
            end_date="2026-12-03",
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
            reason="Deputy identity test",
        )
        self.employee.date_joined = self.today
        self.employee.save(update_fields=["date_joined"])
        self.client.force_login(self.department_head.user)

        response = self.client.get(
            reverse("applications"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        requests_by_id = {item["id"]: item for item in payload["vacations"]}
        changes_by_id = {item["id"]: item for item in payload["change_requests"]}
        for item in (requests_by_id[request_obj.id], changes_by_id[change_request.id]):
            self.assertEqual(item["employee_role_icon"], "supervisor_account")
            self.assertEqual(item["employee_role_variant"], "department-deputy")
            self.assertEqual(
                item["employee_management_badges"],
                [
                    {
                        "label": "Заместитель отдела",
                        "icon": "supervisor_account",
                        "icon_type": "material",
                        "variant": "department-deputy",
                    }
                ],
            )
            self.assertEqual(item["employee_position_label"], self.employee.position)
            self.assertEqual(item["employee_department_label"], self.engineering.name)
            self.assertEqual(item["employee_production_group_label"], self.engineering_group.name)
            self.assertEqual(item["employee_new_hire_badge"]["label"], "Новичок")
        self.assertIn("application-card__profile-icon--department-deputy", payload["vacations_html"])
        self.assertIn("application-card__management-badge application-card__management-badge--department-deputy", payload["vacations_html"])
        self.assertIn("application-card__management-badge application-card__management-badge--department-deputy", payload["change_requests_html"])
        self.assertIn("new-hire-badge", payload["vacations_html"])
        self.assertIn("person_add", payload["change_requests_html"])
        self.assertIn(self.engineering.name, payload["vacations_html"])
        self.assertIn(self.engineering_group.name, payload["change_requests_html"])
        self.assertNotIn("Отдел:", payload["vacations_html"])
        self.assertNotIn("Группа:", payload["change_requests_html"])

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
