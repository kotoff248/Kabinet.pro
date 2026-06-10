from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.core.models import Notification
from apps.leave.models import VacationSchedule, VacationScheduleItem, VacationUrgentClosureRequest
from apps.leave.services.urgent_closures import (
    accept_urgent_closure_by_employee,
    approve_urgent_closure_by_manager,
    build_urgent_closure_options,
    create_urgent_closure_request,
    detect_previous_year_closure_need,
    finalize_urgent_closure,
    propose_urgent_closure_period_by_employee,
    reject_urgent_closure,
)

from .base import LeaveTestCase


class UrgentClosureWorkflowTests(LeaveTestCase):
    def _create_closure(self):
        return create_urgent_closure_request(
            employee=self.employee,
            planning_year=2027,
            required_days=3,
            deadline=date(2027, 1, 3),
            start_date=date(2026, 12, 29),
            end_date=date(2026, 12, 31),
            actor=self.hr_employee,
            reason="Остаток нельзя закрыть в графике 2027 года.",
        )

    def _post_create_urgent_closure(self, **extra_payload):
        self.client.force_login(self.hr_employee.user)
        payload = {
            "required_days": "3",
            "deadline": "2027-01-03",
            "manual_start_date": "2026-12-29",
            "manual_end_date": "2026-12-31",
            "next": reverse("schedule_draft_detail", args=[2027]),
        }
        payload.update(extra_payload)
        return self.client.post(reverse("urgent_closure_create", args=[2027, self.employee.id]), payload)

    def test_urgent_closure_options_close_january_deadline_before_planning_year(self):
        options = build_urgent_closure_options(
            self.employee,
            planning_year=2027,
            required_days=3,
            deadline=date(2027, 1, 3),
        )

        self.assertTrue(options)
        first_option = options[0]
        self.assertLess(first_option["end_date"], date(2027, 1, 1))
        self.assertEqual(first_option["chargeable_days"], 3)
        self.assertTrue(first_option["can_submit"])
        self.assertIn("module_score", first_option)
        self.assertIn("module_model_version", first_option)

    def test_detect_previous_year_closure_ignores_already_expired_deadline(self):
        planning_need = {
            "mandatory_rows": [
                {
                    "open_days": Decimal("22.00"),
                    "must_use_by": date(2024, 7, 12),
                }
            ]
        }

        with patch("apps.leave.services.urgent_closures.timezone.localdate", return_value=date(2026, 6, 11)):
            closure_need = detect_previous_year_closure_need(
                self.employee,
                2027,
                planning_need,
                include_options=False,
            )

        self.assertIsNone(closure_need)

    def test_detect_previous_year_closure_allows_future_deadline_before_planning_year(self):
        planning_need = {
            "mandatory_rows": [
                {
                    "open_days": Decimal("16.00"),
                    "must_use_by": date(2026, 7, 1),
                }
            ]
        }

        with patch("apps.leave.services.urgent_closures.timezone.localdate", return_value=date(2026, 6, 11)):
            closure_need = detect_previous_year_closure_need(
                self.employee,
                2027,
                planning_need,
                include_options=False,
            )

        self.assertIsNotNone(closure_need)
        self.assertEqual(closure_need["required_days"], Decimal("16.00"))
        self.assertEqual(closure_need["deadline"], date(2026, 7, 1))
        self.assertEqual(closure_need["closure_year"], 2026)
        self.assertTrue(closure_need["can_create"])

    def test_urgent_closure_options_rank_by_neural_score_after_hard_rules(self):
        def fake_score(features, *, passed_hard_rules=True):
            score = Decimal("91.00") if features["period_end_month"] == 11 else Decimal("52.00")
            return SimpleNamespace(
                score=score,
                confidence=Decimal("88.00"),
                recommendation="prefer" if score > 80 else "normal",
                explanation="Тестовая оценка.",
                model_version="test-v2",
                scorer_kind="test",
            )

        with (
            patch(
                "apps.leave.services.urgent_closures._candidate_end_dates",
                return_value=[date(2026, 12, 31), date(2026, 11, 30)],
            ),
            patch("apps.leave.services.urgent_closures.score_candidate_features", side_effect=fake_score),
        ):
            options = build_urgent_closure_options(
                self.employee,
                planning_year=2027,
                required_days=3,
                deadline=date(2027, 1, 3),
            )

        self.assertGreaterEqual(len(options), 2)
        self.assertEqual(options[0]["end_date"], date(2026, 11, 30))
        self.assertEqual(options[0]["module_score"], Decimal("91.00"))

    def test_create_urgent_closure_saves_ai_snapshot(self):
        scoring = SimpleNamespace(
            score=Decimal("82.00"),
            confidence=Decimal("91.00"),
            recommendation="avoid",
            explanation="Тестовая оценка срочного закрытия.",
            model_version="urgent-test-ai",
            scorer_kind="test",
        )

        with patch("apps.leave.services.urgent_closures.score_candidate_features", return_value=scoring):
            closure_request = self._create_closure()

        self.assertEqual(closure_request.ai_score, Decimal("82.00"))
        self.assertEqual(closure_request.ai_confidence, Decimal("91.00"))
        self.assertEqual(closure_request.ai_recommendation, "avoid")
        self.assertEqual(closure_request.ai_explanation, "Тестовая оценка срочного закрытия.")
        self.assertEqual(closure_request.ai_model_version, "urgent-test-ai")
        self.assertEqual(closure_request.ai_scorer_kind, "test")
        self.assertIsNotNone(closure_request.ai_evaluated_at)

    def test_manager_date_change_refreshes_urgent_closure_ai_snapshot(self):
        closure_request = self._create_closure()
        scoring = SimpleNamespace(
            score=Decimal("91.00"),
            confidence=Decimal("88.00"),
            recommendation="prefer",
            explanation="Новый период лучше для срочного закрытия.",
            model_version="urgent-manager-ai",
            scorer_kind="test",
        )

        with patch("apps.leave.services.urgent_closures.score_candidate_features", return_value=scoring):
            approve_urgent_closure_by_manager(
                closure_request.id,
                reviewer=self.department_head,
                start_date=date(2026, 11, 25),
                end_date=date(2026, 11, 27),
                comment="Лучше другой период.",
            )

        closure_request.refresh_from_db()
        self.assertEqual(closure_request.proposed_start_date, date(2026, 11, 25))
        self.assertEqual(closure_request.proposed_end_date, date(2026, 11, 27))
        self.assertEqual(closure_request.ai_score, Decimal("91.00"))
        self.assertEqual(closure_request.ai_recommendation, "prefer")
        self.assertEqual(closure_request.ai_model_version, "urgent-manager-ai")

    def test_urgent_closure_routes_through_manager_employee_and_hr(self):
        closure_request = self._create_closure()
        detail_url = reverse("urgent_closure_detail", args=[closure_request.id])

        self.assertEqual(closure_request.status, VacationUrgentClosureRequest.STATUS_DEPARTMENT_REVIEW)
        manager_task = Notification.objects.get(
            recipient=self.department_head,
            event_type=Notification.TYPE_URGENT_CLOSURE_DEPARTMENT_REVIEW,
        )
        self.assertEqual(manager_task.action_url, detail_url)
        self.assertTrue(manager_task.requires_action)

        approve_urgent_closure_by_manager(
            closure_request.id,
            reviewer=self.department_head,
            comment="По составу отдела подходит.",
        )
        closure_request.refresh_from_db()
        manager_task.refresh_from_db()

        self.assertEqual(closure_request.status, VacationUrgentClosureRequest.STATUS_EMPLOYEE_REVIEW)
        self.assertEqual(manager_task.status, Notification.STATUS_DONE)
        employee_task = Notification.objects.get(
            recipient=self.employee,
            event_type=Notification.TYPE_URGENT_CLOSURE_EMPLOYEE_REVIEW,
        )
        self.assertEqual(employee_task.action_url, detail_url)

        accept_urgent_closure_by_employee(
            closure_request.id,
            employee=self.employee,
            comment="Период подходит.",
        )
        closure_request.refresh_from_db()
        employee_task.refresh_from_db()

        self.assertEqual(closure_request.status, VacationUrgentClosureRequest.STATUS_HR_FINALIZATION)
        self.assertEqual(employee_task.status, Notification.STATUS_DONE)
        hr_task = Notification.objects.get(
            recipient=self.hr_employee,
            event_type=Notification.TYPE_URGENT_CLOSURE_HR_FINALIZATION,
        )
        self.assertEqual(hr_task.action_url, detail_url)

        schedule_item = finalize_urgent_closure(
            closure_request.id,
            actor=self.hr_employee,
            comment="Согласовано всеми участниками.",
        )
        closure_request.refresh_from_db()
        hr_task.refresh_from_db()

        self.assertEqual(closure_request.status, VacationUrgentClosureRequest.STATUS_COMPLETED)
        self.assertEqual(hr_task.status, Notification.STATUS_DONE)
        self.assertEqual(schedule_item.schedule.year, 2026)
        self.assertEqual(schedule_item.status, VacationScheduleItem.STATUS_APPROVED)
        self.assertEqual(schedule_item.source, VacationScheduleItem.SOURCE_MANUAL)
        self.assertEqual(schedule_item.chargeable_days, 3)
        self.assertEqual(closure_request.created_schedule_item_id, schedule_item.id)

    def test_finalize_saves_decision_ai_and_detail_shows_module_recommendation(self):
        closure_request = self._create_closure()
        approve_urgent_closure_by_manager(closure_request.id, reviewer=self.department_head)
        accept_urgent_closure_by_employee(closure_request.id, employee=self.employee)
        scoring = SimpleNamespace(
            score=Decimal("77.00"),
            confidence=Decimal("86.00"),
            recommendation="normal",
            explanation="Финальная оценка срочного закрытия.",
            model_version="urgent-decision-ai",
            scorer_kind="test",
        )

        with patch("apps.leave.services.urgent_closures.score_candidate_features", return_value=scoring):
            finalize_urgent_closure(
                closure_request.id,
                actor=self.hr_employee,
                comment="Согласовано всеми участниками.",
            )
        closure_request.refresh_from_db()

        self.assertEqual(closure_request.decision_ai_score, Decimal("77.00"))
        self.assertEqual(closure_request.decision_ai_confidence, Decimal("86.00"))
        self.assertEqual(closure_request.decision_ai_recommendation, "normal")
        self.assertEqual(closure_request.decision_ai_explanation, "Финальная оценка срочного закрытия.")
        self.assertEqual(closure_request.decision_ai_model_version, "urgent-decision-ai")
        self.assertEqual(closure_request.decision_ai_scorer_kind, "test")
        self.assertIsNotNone(closure_request.decision_ai_evaluated_at)

        self.client.force_login(self.hr_employee.user)
        response = self.client.get(reverse("urgent_closure_detail", args=[closure_request.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Оценка модуля")
        self.assertContains(response, "На момент решения")
        self.assertContains(response, "77,00%")
        self.assertContains(response, "urgent-decision-ai")

    def test_reject_saves_decision_ai_snapshot(self):
        closure_request = self._create_closure()
        scoring = SimpleNamespace(
            score=Decimal("42.00"),
            confidence=Decimal("79.00"),
            recommendation="avoid",
            explanation="Оценка при отклонении срочного закрытия.",
            model_version="urgent-reject-ai",
            scorer_kind="test",
        )

        with patch("apps.leave.services.urgent_closures.score_candidate_features", return_value=scoring):
            reject_urgent_closure(
                closure_request.id,
                actor=self.department_head,
                comment="Период не подходит.",
            )
        closure_request.refresh_from_db()

        self.assertEqual(closure_request.status, VacationUrgentClosureRequest.STATUS_REJECTED)
        self.assertEqual(closure_request.decision_ai_score, Decimal("42.00"))
        self.assertEqual(closure_request.decision_ai_recommendation, "avoid")
        self.assertEqual(closure_request.decision_ai_model_version, "urgent-reject-ai")

    def test_employee_can_propose_another_period_back_to_manager(self):
        closure_request = self._create_closure()
        approve_urgent_closure_by_manager(closure_request.id, reviewer=self.department_head)
        initial_manager_task = Notification.objects.get(
            recipient=self.department_head,
            event_type=Notification.TYPE_URGENT_CLOSURE_DEPARTMENT_REVIEW,
        )
        employee_task = Notification.objects.get(
            recipient=self.employee,
            event_type=Notification.TYPE_URGENT_CLOSURE_EMPLOYEE_REVIEW,
        )

        scoring = SimpleNamespace(
            score=Decimal("84.00"),
            confidence=Decimal("82.00"),
            recommendation="prefer",
            explanation="Предложение сотрудника подходит модулю.",
            model_version="urgent-employee-ai",
            scorer_kind="test",
        )
        with patch("apps.leave.services.urgent_closures.score_candidate_features", return_value=scoring):
            propose_urgent_closure_period_by_employee(
                closure_request.id,
                employee=self.employee,
                start_date=date(2026, 11, 25),
                end_date=date(2026, 11, 27),
                comment="Так удобнее.",
            )
        closure_request.refresh_from_db()

        self.assertEqual(closure_request.status, VacationUrgentClosureRequest.STATUS_DEPARTMENT_REVIEW)
        self.assertEqual(closure_request.proposed_start_date, date(2026, 11, 25))
        self.assertEqual(closure_request.proposed_end_date, date(2026, 11, 27))
        self.assertEqual(closure_request.ai_score, Decimal("84.00"))
        self.assertEqual(closure_request.ai_model_version, "urgent-employee-ai")
        initial_manager_task.refresh_from_db()
        employee_task.refresh_from_db()
        self.assertEqual(initial_manager_task.status, Notification.STATUS_DONE)
        self.assertEqual(employee_task.status, Notification.STATUS_DONE)

        manager_tasks = Notification.objects.filter(
            recipient=self.department_head,
            event_type=Notification.TYPE_URGENT_CLOSURE_DEPARTMENT_REVIEW,
            action_url=reverse("urgent_closure_detail", args=[closure_request.id]),
        )
        self.assertEqual(manager_tasks.count(), 2)
        new_manager_task = manager_tasks.get(status=Notification.STATUS_NEW)
        self.assertTrue(new_manager_task.requires_action)
        self.assertEqual(new_manager_task.title, "Сотрудник предложил другой период")
        self.assertNotEqual(new_manager_task.dedupe_key, initial_manager_task.dedupe_key)

    def test_manager_repeat_approval_creates_new_employee_notification(self):
        closure_request = self._create_closure()
        approve_urgent_closure_by_manager(closure_request.id, reviewer=self.department_head)
        first_employee_task = Notification.objects.get(
            recipient=self.employee,
            event_type=Notification.TYPE_URGENT_CLOSURE_EMPLOYEE_REVIEW,
        )
        propose_urgent_closure_period_by_employee(
            closure_request.id,
            employee=self.employee,
            start_date=date(2026, 11, 25),
            end_date=date(2026, 11, 27),
            comment="Так удобнее.",
        )

        approve_urgent_closure_by_manager(
            closure_request.id,
            reviewer=self.department_head,
            comment="Новый период подходит.",
        )
        first_employee_task.refresh_from_db()

        self.assertEqual(first_employee_task.status, Notification.STATUS_DONE)
        employee_tasks = Notification.objects.filter(
            recipient=self.employee,
            event_type=Notification.TYPE_URGENT_CLOSURE_EMPLOYEE_REVIEW,
            action_url=reverse("urgent_closure_detail", args=[closure_request.id]),
        )
        self.assertEqual(employee_tasks.count(), 2)
        new_employee_task = employee_tasks.get(status=Notification.STATUS_NEW)
        self.assertTrue(new_employee_task.requires_action)
        self.assertNotEqual(new_employee_task.dedupe_key, first_employee_task.dedupe_key)

    def test_foreign_user_cannot_view_urgent_closure_detail(self):
        closure_request = self._create_closure()

        self.client.force_login(self.foreign_department_head.user)
        response = self.client.get(reverse("urgent_closure_detail", args=[closure_request.id]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("main"))

    def test_department_head_sees_urgent_closure_detail_actions(self):
        closure_request = self._create_closure()

        self.client.force_login(self.department_head.user)
        response = self.client.get(reverse("urgent_closure_detail", args=[closure_request.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [metric["label"] for metric in response.context["urgent_closure_decision_context"]["metrics"]],
            ["Состав", "Одновременно отсутствуют", "Нагрузка", "Замещение"],
        )
        self.assertContains(response, "Закрытие остатка отпуска")
        self.assertContains(response, "Отправить сотруднику")
        self.assertContains(response, "29.12.2026 - 31.12.2026")

    def test_employee_review_page_shows_live_period_preview_controls(self):
        closure_request = self._create_closure()
        approve_urgent_closure_by_manager(closure_request.id, reviewer=self.department_head)

        self.client.force_login(self.employee.user)
        response = self.client.get(reverse("urgent_closure_detail", args=[closure_request.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-urgent-closure-employee-preview-form")
        self.assertContains(response, reverse("urgent_closure_employee_preview", args=[closure_request.id]))
        self.assertContains(response, "Нужно выбрать 3 д.")
        self.assertContains(response, "data-urgent-closure-propose-submit disabled")

    def test_create_urgent_closure_redirect_uses_draft_back_link(self):
        draft_url = reverse("schedule_draft_detail", args=[2027])

        response = self._post_create_urgent_closure(next=draft_url)

        closure_request = VacationUrgentClosureRequest.objects.get()
        parsed = urlsplit(response.url)
        query = parse_qs(parsed.query)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(parsed.path, reverse("urgent_closure_detail", args=[closure_request.id]))
        self.assertEqual(query["from"], ["calendar"])
        self.assertEqual(query["back_url"], [draft_url])
        self.assertEqual(query["back_label"], ["К черновику"])
        self.assertEqual(closure_request.status, VacationUrgentClosureRequest.STATUS_DEPARTMENT_REVIEW)

    def test_create_urgent_closure_demo_manager_approve_waits_employee(self):
        response = self._post_create_urgent_closure(demo_manager_approve="on")

        closure_request = VacationUrgentClosureRequest.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(closure_request.status, VacationUrgentClosureRequest.STATUS_EMPLOYEE_REVIEW)
        self.assertEqual(closure_request.department_reviewer, self.department_head)
        self.assertEqual(closure_request.department_comment, "Демо: руководитель подтвердил период.")
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.employee,
                event_type=Notification.TYPE_URGENT_CLOSURE_EMPLOYEE_REVIEW,
            ).exists()
        )

    def test_create_urgent_closure_demo_employee_accept_waits_hr(self):
        response = self._post_create_urgent_closure(
            demo_manager_approve="on",
            demo_employee_reply="on",
            demo_employee_response="accept",
        )

        closure_request = VacationUrgentClosureRequest.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(closure_request.status, VacationUrgentClosureRequest.STATUS_HR_FINALIZATION)
        self.assertEqual(closure_request.employee_comment, "Демо: сотрудник принял предложенный период.")
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.hr_employee,
                event_type=Notification.TYPE_URGENT_CLOSURE_HR_FINALIZATION,
            ).exists()
        )

    def test_create_urgent_closure_demo_employee_propose_returns_to_manager(self):
        response = self._post_create_urgent_closure(
            demo_manager_approve="on",
            demo_employee_reply="on",
            demo_employee_response="propose",
        )

        closure_request = VacationUrgentClosureRequest.objects.get()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(closure_request.status, VacationUrgentClosureRequest.STATUS_DEPARTMENT_REVIEW)
        self.assertEqual(closure_request.employee_comment, "Демо: сотрудник предложил другой период.")
        self.assertIsNone(closure_request.department_reviewer)
        self.assertNotEqual(
            (closure_request.proposed_start_date, closure_request.proposed_end_date),
            (date(2026, 12, 29), date(2026, 12, 31)),
        )

    def test_urgent_closure_detail_accepts_schedule_planning_back_source(self):
        closure_request = self._create_closure()
        draft_url = f"{reverse('schedule_draft_detail', args=[2027])}?from=schedule_planning"

        self.client.force_login(self.hr_employee.user)
        response = self.client.get(
            reverse("urgent_closure_detail", args=[closure_request.id]),
            {
                "from": "schedule_planning",
                "back_url": draft_url,
                "back_label": "К черновику",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "schedule_planning")
        self.assertEqual(response.context["urgent_closure_detail_back_link"]["url"], draft_url)
        self.assertContains(response, "К черновику")

    def test_hr_cannot_create_duplicate_active_closure(self):
        self._create_closure()

        with self.assertRaises(ValidationError):
            self._create_closure()

        self.assertEqual(VacationUrgentClosureRequest.objects.count(), 1)

    def test_finalized_closure_creates_or_reuses_approved_previous_year_schedule(self):
        schedule = VacationSchedule.objects.create(year=2026, status=VacationSchedule.STATUS_DRAFT)
        closure_request = self._create_closure()
        approve_urgent_closure_by_manager(closure_request.id, reviewer=self.department_head)
        accept_urgent_closure_by_employee(closure_request.id, employee=self.employee)

        schedule_item = finalize_urgent_closure(closure_request.id, actor=self.hr_employee)
        schedule.refresh_from_db()

        self.assertEqual(schedule.status, VacationSchedule.STATUS_APPROVED)
        self.assertEqual(schedule_item.schedule_id, schedule.id)

    def test_hr_can_preview_valid_urgent_closure_period(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("urgent_closure_preview", args=[2027, self.employee.id]),
            {
                "required_days": "3",
                "deadline": "2027-01-03",
                "start_date": "2026-12-29",
                "end_date": "2026-12-31",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["can_submit"])
        self.assertEqual(payload["chargeable_days"], 3)
        self.assertIn("29.12.2026", payload["period_label"])
        self.assertIn("risk_label", payload)
        self.assertIn("module_score", payload)
        self.assertIn("module_model_version", payload)

    def test_employee_can_preview_proposed_urgent_closure_period(self):
        closure_request = self._create_closure()
        approve_urgent_closure_by_manager(closure_request.id, reviewer=self.department_head)
        self.client.force_login(self.employee.user)

        response = self.client.get(
            reverse("urgent_closure_employee_preview", args=[closure_request.id]),
            {
                "start_date": "2026-12-29",
                "end_date": "2026-12-31",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["can_submit"])
        self.assertEqual(payload["chargeable_days"], 3)
        self.assertIn("module_score", payload)
        self.assertIn("module_model_version", payload)

    def test_employee_preview_rejects_wrong_urgent_closure_day_count(self):
        closure_request = self._create_closure()
        approve_urgent_closure_by_manager(closure_request.id, reviewer=self.department_head)
        self.client.force_login(self.employee.user)

        response = self.client.get(
            reverse("urgent_closure_employee_preview", args=[closure_request.id]),
            {
                "start_date": "2026-12-30",
                "end_date": "2026-12-31",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["can_submit"])
        self.assertIn("ровно 3 д.", payload["message"])

    def test_foreign_user_cannot_preview_employee_urgent_closure_period(self):
        closure_request = self._create_closure()
        approve_urgent_closure_by_manager(closure_request.id, reviewer=self.department_head)
        self.client.force_login(self.foreign_department_head.user)

        response = self.client.get(
            reverse("urgent_closure_employee_preview", args=[closure_request.id]),
            {
                "start_date": "2026-12-29",
                "end_date": "2026-12-31",
            },
        )

        self.assertEqual(response.status_code, 403)

    def test_urgent_closure_preview_rejects_reversed_dates(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("urgent_closure_preview", args=[2027, self.employee.id]),
            {
                "required_days": "3",
                "deadline": "2027-01-03",
                "start_date": "2026-06-12",
                "end_date": "2026-06-05",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["can_submit"])
        self.assertEqual(payload["message"], "Дата окончания не может быть раньше даты начала.")

    def test_urgent_closure_preview_rejects_employee_overlap(self):
        schedule = VacationSchedule.objects.create(year=2026, status=VacationSchedule.STATUS_DRAFT)
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 12, 29),
            end_date=date(2026, 12, 31),
            chargeable_days=3,
            status=VacationScheduleItem.STATUS_PLANNED,
            source=VacationScheduleItem.SOURCE_MANUAL,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("urgent_closure_preview", args=[2027, self.employee.id]),
            {
                "required_days": "3",
                "deadline": "2027-01-03",
                "start_date": "2026-12-29",
                "end_date": "2026-12-31",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["can_submit"])
        self.assertEqual(payload["message"], "У сотрудника уже есть отпуск или активная заявка на эти даты.")

    def test_urgent_closure_preview_keeps_conflict_visible_without_blocking(self):
        self.client.force_login(self.hr_employee.user)

        with (
            patch(
                "apps.leave.services.urgent_closures.calculate_vacation_request_risk",
                return_value={
                    "risk_score": 92,
                    "risk_level": "high",
                    "department_load_level": 5,
                    "overlapping_absences_count": 4,
                    "remaining_staff_count": 1,
                    "min_staff_required": 3,
                    "balance_after_request": 40,
                },
            ),
            patch(
                "apps.leave.services.urgent_closures.build_vacation_request_risk_explanation",
                return_value={
                    "short_reason": "Не хватает людей в группе.",
                    "recommended_action": "Проверьте замену.",
                    "is_conflict": True,
                },
            ),
        ):
            response = self.client.get(
                reverse("urgent_closure_preview", args=[2027, self.employee.id]),
                {
                    "required_days": "3",
                    "deadline": "2027-01-03",
                    "start_date": "2026-12-29",
                    "end_date": "2026-12-31",
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["can_submit"])
        self.assertTrue(payload["risk_is_conflict"])
        self.assertEqual(payload["risk_score"], 92)
        self.assertEqual(payload["risk_short_reason"], "Не хватает людей в группе.")
