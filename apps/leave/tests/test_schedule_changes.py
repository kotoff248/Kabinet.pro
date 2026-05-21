from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.accounts.services import can_initiate_schedule_change_for_item, can_review_schedule_change_request
from apps.leave.ml.scoring import CandidateScoringResult
from apps.leave.models import DepartmentStaffingRule, VacationSchedule, VacationScheduleChangeRequest, VacationScheduleItem
from apps.leave.services.schedule_changes import (
    approve_schedule_change_request,
    build_schedule_change_transfer_action,
    create_schedule_change_request,
    enrich_schedule_change_request,
    reject_schedule_change_request,
    serialize_schedule_change_request_row,
)
from apps.leave.services.risk import (
    build_schedule_change_risk_explanation,
    calculate_schedule_change_request_risk,
    calculate_schedule_change_risk,
    calculate_vacation_request_risk_with_explanation,
)

from .base import LeaveTestCase


class ScheduleChangeRequestTests(LeaveTestCase):
    def _create_schedule_item(self, employee=None, *, start_date=date(2027, 7, 1), end_date=date(2027, 7, 14)):
        schedule, _ = VacationSchedule.objects.get_or_create(
            year=start_date.year,
            defaults={
                "status": VacationSchedule.STATUS_APPROVED,
                "approved_by": self.enterprise_head,
            },
        )
        return VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=employee or self.employee,
            start_date=start_date,
            end_date=end_date,
            vacation_type="paid",
            chargeable_days=(end_date - start_date).days + 1,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

    def _preview_transfer(self, schedule_item, actor, start_date, end_date):
        self.client.force_login(actor.user)
        return self.client.get(
            reverse("schedule_change_request_preview", args=[schedule_item.id]),
            {
                "new_start_date": start_date.isoformat(),
                "new_end_date": end_date.isoformat(),
            },
        )

    def test_schedule_change_initiation_permission_matrix(self):
        employee_item = self._create_schedule_item()
        hr_item = self._create_schedule_item(self.hr_employee, start_date=date(2027, 9, 1), end_date=date(2027, 9, 14))
        department_head_item = self._create_schedule_item(
            self.department_head,
            start_date=date(2027, 10, 1),
            end_date=date(2027, 10, 14),
        )
        enterprise_head_item = self._create_schedule_item(
            self.enterprise_head,
            start_date=date(2027, 11, 1),
            end_date=date(2027, 11, 14),
        )

        self.assertTrue(can_initiate_schedule_change_for_item(self.employee, employee_item))
        self.assertTrue(can_initiate_schedule_change_for_item(self.department_head, employee_item))
        self.assertFalse(can_initiate_schedule_change_for_item(self.foreign_department_head, employee_item))
        self.assertFalse(can_initiate_schedule_change_for_item(self.enterprise_head, employee_item))
        self.assertFalse(can_initiate_schedule_change_for_item(self.hr_employee, employee_item))
        self.assertTrue(can_initiate_schedule_change_for_item(self.enterprise_head, hr_item))
        self.assertTrue(can_initiate_schedule_change_for_item(self.enterprise_head, department_head_item))
        self.assertFalse(can_initiate_schedule_change_for_item(self.authorized_person, enterprise_head_item))

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

    def test_schedule_change_request_saves_ai_snapshot_on_create(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 7, 1), end_date=date(2027, 7, 14))
        scoring = CandidateScoringResult(
            score=Decimal("82.00"),
            confidence=Decimal("91.00"),
            recommendation="avoid",
            explanation="Тестовая оценка переноса.",
            model_version="test-transfer-ai",
            scorer_kind="test",
        )

        with patch("apps.leave.ml.request_support.score_candidate_features", return_value=scoring):
            change_request = create_schedule_change_request(
                schedule_item.id,
                requested_by=self.employee,
                new_start_date=date(2027, 8, 1),
                new_end_date=date(2027, 8, 14),
                reason="Нужно перенести.",
            )

        self.assertEqual(change_request.ai_score, Decimal("82.00"))
        self.assertEqual(change_request.ai_confidence, Decimal("91.00"))
        self.assertEqual(change_request.ai_recommendation, "avoid")
        self.assertIn("test-transfer-ai", change_request.ai_explanation)
        self.assertIn("лучше проверить", change_request.ai_explanation)
        self.assertEqual(change_request.ai_model_version, "test-transfer-ai")
        self.assertEqual(change_request.ai_scorer_kind, "test")
        self.assertIsNotNone(change_request.ai_evaluated_at)

    def test_schedule_change_detail_shows_live_ai_recommendation_for_pending_request(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 7, 1), end_date=date(2027, 7, 14))
        saved_scoring = CandidateScoringResult(
            score=Decimal("82.00"),
            confidence=Decimal("91.00"),
            recommendation="avoid",
            explanation="Сохраненная оценка при создании переноса.",
            model_version="saved-transfer-ai",
            scorer_kind="test",
        )
        live_scoring = CandidateScoringResult(
            score=Decimal("66.00"),
            confidence=Decimal("88.00"),
            recommendation="normal",
            explanation="Живая оценка деталей переноса.",
            model_version="live-transfer-ai",
            scorer_kind="test",
        )

        with patch("apps.leave.ml.request_support.score_candidate_features", return_value=saved_scoring):
            change_request = create_schedule_change_request(
                schedule_item.id,
                requested_by=self.employee,
                new_start_date=date(2027, 8, 1),
                new_end_date=date(2027, 8, 14),
                reason="Нужно перенести.",
            )

        hard_rule_flags = []

        def score_live(features, *, passed_hard_rules=True, use_neural=True):
            hard_rule_flags.append(passed_hard_rules)
            return live_scoring

        self.client.force_login(self.department_head.user)
        with patch("apps.leave.ml.request_support.score_candidate_features", side_effect=score_live):
            response = self.client.get(reverse("schedule_change_detail", args=[change_request.id]))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(hard_rule_flags)
        self.assertTrue(all(hard_rule_flags))
        self.assertContains(response, "Оценка модуля")
        self.assertContains(response, "На сейчас")
        self.assertContains(response, "66,00%")
        self.assertContains(response, "При создании 82,00%")
        self.assertContains(response, "live-transfer-ai")

    def test_schedule_change_detail_uses_decision_ai_recommendation_for_reviewed_request(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 7, 1), end_date=date(2027, 7, 14))
        saved_scoring = CandidateScoringResult(
            score=Decimal("82.00"),
            confidence=Decimal("91.00"),
            recommendation="avoid",
            explanation="Сохраненная оценка при создании переноса.",
            model_version="saved-transfer-ai",
            scorer_kind="test",
        )
        decision_scoring = CandidateScoringResult(
            score=Decimal("64.00"),
            confidence=Decimal("87.00"),
            recommendation="prefer",
            explanation="Оценка переноса при решении.",
            model_version="decision-transfer-ai",
            scorer_kind="test",
        )
        with patch("apps.leave.ml.request_support.score_candidate_features", return_value=saved_scoring):
            change_request = create_schedule_change_request(
                schedule_item.id,
                requested_by=self.employee,
                new_start_date=date(2027, 8, 1),
                new_end_date=date(2027, 8, 14),
                reason="Нужно перенести.",
            )

        with patch("apps.leave.ml.request_support.score_candidate_features", return_value=decision_scoring):
            reject_schedule_change_request(change_request.id, reviewer=self.department_head)
        change_request.refresh_from_db()
        self.assertEqual(change_request.decision_ai_score, Decimal("64.00"))
        self.assertEqual(change_request.decision_ai_confidence, Decimal("87.00"))
        self.assertEqual(change_request.decision_ai_recommendation, "prefer")
        self.assertIn("decision-transfer-ai", change_request.decision_ai_explanation)
        self.assertEqual(change_request.decision_ai_model_version, "decision-transfer-ai")
        self.assertEqual(change_request.decision_ai_scorer_kind, "test")
        self.assertIsNotNone(change_request.decision_ai_evaluated_at)

        self.client.force_login(self.department_head.user)
        with patch("apps.leave.ml.request_support.score_candidate_features", side_effect=AssertionError):
            response = self.client.get(reverse("schedule_change_detail", args=[change_request.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Оценка модуля")
        self.assertContains(response, "На момент решения")
        self.assertContains(response, "64,00%")
        self.assertContains(response, "При создании 82,00%")
        self.assertContains(response, "decision-transfer-ai")
        self.assertNotContains(response, "На сейчас")

    def test_schedule_change_detail_shows_decision_ai_without_creation_snapshot(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 7, 1), end_date=date(2027, 7, 14))
        decision_scoring = CandidateScoringResult(
            score=Decimal("71.00"),
            confidence=Decimal("83.00"),
            recommendation="normal",
            explanation="Оценка переноса при решении без снимка создания.",
            model_version="decision-transfer-ai",
            scorer_kind="test",
        )
        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.employee,
            new_start_date=date(2027, 8, 1),
            new_end_date=date(2027, 8, 14),
            reason="Нужно перенести.",
        )
        change_request.ai_score = None
        change_request.ai_confidence = None
        change_request.ai_model_version = ""
        change_request.ai_recommendation = ""
        change_request.ai_explanation = ""
        change_request.ai_scorer_kind = ""
        change_request.ai_evaluated_at = None
        change_request.save(
            update_fields=[
                "ai_score",
                "ai_confidence",
                "ai_model_version",
                "ai_recommendation",
                "ai_explanation",
                "ai_scorer_kind",
                "ai_evaluated_at",
            ]
        )

        with patch("apps.leave.ml.request_support.score_candidate_features", return_value=decision_scoring):
            reject_schedule_change_request(change_request.id, reviewer=self.department_head)
        self.client.force_login(self.department_head.user)
        with patch("apps.leave.ml.request_support.score_candidate_features", side_effect=AssertionError):
            response = self.client.get(reverse("schedule_change_detail", args=[change_request.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Оценка модуля")
        self.assertContains(response, "На момент решения")
        self.assertContains(response, "71,00%")
        self.assertContains(response, "decision-transfer-ai")
        self.assertNotContains(response, "При создании переноса")

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

    def test_schedule_change_approve_requires_valid_reviewer(self):
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
        )

        for reviewer in (None, self.employee, self.foreign_department_head, self.hr_employee):
            with self.subTest(reviewer=reviewer):
                with self.assertRaises(ValidationError):
                    approve_schedule_change_request(change_request.id, reviewer=reviewer)

        change_request.refresh_from_db()
        schedule_item.refresh_from_db()
        self.assertEqual(change_request.status, VacationScheduleChangeRequest.STATUS_PENDING)
        self.assertEqual(schedule_item.status, VacationScheduleItem.STATUS_APPROVED)

    def test_schedule_change_reject_requires_valid_reviewer(self):
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.employee,
            new_start_date=date(2026, 10, 1),
            new_end_date=date(2026, 10, 14),
        )

        with self.assertRaises(ValidationError):
            reject_schedule_change_request(change_request.id, reviewer=self.employee)
        with self.assertRaises(ValidationError):
            reject_schedule_change_request(change_request.id, reviewer=None)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, VacationScheduleChangeRequest.STATUS_PENDING)

    def test_department_head_can_propose_transfer_and_employee_accepts(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 8, 1), end_date=date(2027, 8, 14))

        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.department_head,
            new_start_date=date(2027, 9, 1),
            new_end_date=date(2027, 9, 14),
            reason="Нужно сохранить покрытие отдела.",
        )

        self.assertNotEqual(change_request.requested_by_id, change_request.employee_id)
        self.assertTrue(can_review_schedule_change_request(self.employee, change_request))
        self.assertFalse(can_review_schedule_change_request(self.department_head, change_request))

        replacement = approve_schedule_change_request(change_request.id, reviewer=self.employee)
        schedule_item.refresh_from_db()
        change_request.refresh_from_db()

        self.assertEqual(schedule_item.status, VacationScheduleItem.STATUS_TRANSFERRED)
        self.assertEqual(change_request.status, VacationScheduleChangeRequest.STATUS_APPROVED)
        self.assertEqual(change_request.reviewed_by_id, self.employee.id)
        self.assertEqual(replacement.created_from_change_request_id, change_request.id)

    def test_transfer_action_is_hidden_when_schedule_item_has_pending_change(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 8, 1), end_date=date(2027, 8, 14))
        create_schedule_change_request(
            schedule_item.id,
            requested_by=self.employee,
            new_start_date=date(2027, 9, 1),
            new_end_date=date(2027, 9, 14),
        )

        action = build_schedule_change_transfer_action(
            actor=self.employee,
            employee=self.employee,
            schedule_item_id=schedule_item.id,
            start_date=schedule_item.start_date,
            end_date=schedule_item.end_date,
            vacation_type_label=schedule_item.get_vacation_type_display(),
            schedule_status=schedule_item.status,
            today=date(2027, 1, 1),
        )

        self.assertFalse(action["can_request_transfer"])
        self.assertEqual(action["transfer_url"], "")

    def test_transfer_action_uses_prefetched_pending_flag_without_query(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 8, 1), end_date=date(2027, 8, 14))

        with self.assertNumQueries(0):
            action = build_schedule_change_transfer_action(
                actor=self.employee,
                employee=self.employee,
                schedule_item_id=schedule_item.id,
                start_date=schedule_item.start_date,
                end_date=schedule_item.end_date,
                vacation_type_label=schedule_item.get_vacation_type_display(),
                schedule_status=schedule_item.status,
                today=date(2027, 1, 1),
                pending_change_exists=True,
            )

        self.assertFalse(action["can_request_transfer"])
        self.assertEqual(action["transfer_url"], "")

    def test_manager_initiated_transfer_can_be_rejected_only_by_employee(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 8, 15), end_date=date(2027, 8, 28))
        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.department_head,
            new_start_date=date(2027, 10, 1),
            new_end_date=date(2027, 10, 14),
        )

        with self.assertRaises(ValidationError):
            approve_schedule_change_request(change_request.id, reviewer=self.department_head)

        reject_schedule_change_request(change_request.id, reviewer=self.employee)
        schedule_item.refresh_from_db()
        change_request.refresh_from_db()

        self.assertEqual(schedule_item.status, VacationScheduleItem.STATUS_APPROVED)
        self.assertEqual(change_request.status, VacationScheduleChangeRequest.STATUS_REJECTED)

    def test_enterprise_head_proposes_only_for_hr_and_department_heads(self):
        employee_item = self._create_schedule_item(start_date=date(2027, 9, 15), end_date=date(2027, 9, 28))
        hr_item = self._create_schedule_item(self.hr_employee, start_date=date(2027, 10, 15), end_date=date(2027, 10, 28))

        with self.assertRaises(ValidationError):
            create_schedule_change_request(
                employee_item.id,
                requested_by=self.enterprise_head,
                new_start_date=date(2027, 11, 1),
                new_end_date=date(2027, 11, 14),
            )

        change_request = create_schedule_change_request(
            hr_item.id,
            requested_by=self.enterprise_head,
            new_start_date=date(2027, 12, 1),
            new_end_date=date(2027, 12, 14),
        )

        self.assertEqual(change_request.requested_by_id, self.enterprise_head.id)
        self.assertTrue(can_review_schedule_change_request(self.hr_employee, change_request))

    def test_schedule_change_preview_allows_employee_and_returns_delta_payload(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 7, 1), end_date=date(2027, 7, 14))

        response = self._preview_transfer(schedule_item, self.employee, date(2027, 8, 1), date(2027, 8, 14))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["can_submit"])
        self.assertEqual(payload["old_calendar_days"], 14)
        self.assertEqual(payload["new_calendar_days"], 14)
        self.assertEqual(payload["old_chargeable_days"], 14)
        self.assertEqual(payload["new_chargeable_days"], 14)
        self.assertEqual(payload["chargeable_days_delta"], 0)
        self.assertEqual(payload["chargeable_days_delta_label"], "Без изменения")
        self.assertGreaterEqual(payload["balance_after_change"], 0)
        self.assertIn("risk_explanation", payload)
        self.assertIn("risk_short_reason", payload)
        self.assertIn("risk_recommended_action", payload)

    def test_equal_schedule_change_risk_excludes_old_item_from_paid_balance(self):
        self.employee.date_joined = date(2027, 1, 1)
        self.employee.annual_paid_leave_days = 14
        self.employee.save(update_fields=["date_joined", "annual_paid_leave_days"])
        schedule_item = self._create_schedule_item(start_date=date(2027, 7, 1), end_date=date(2027, 7, 14))

        risk_payload = calculate_schedule_change_risk(schedule_item, date(2027, 8, 1), date(2027, 8, 14))

        self.assertEqual(risk_payload["balance_after_change"], Decimal("0.00"))

        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.employee,
            new_start_date=date(2027, 8, 1),
            new_end_date=date(2027, 8, 14),
        )
        risk_explanation = build_schedule_change_risk_explanation(change_request)
        detail_kinds = {detail["kind"] for detail in risk_explanation["details"]}

        self.assertEqual(change_request.balance_after_change, Decimal("0.00"))
        self.assertNotIn("negative_balance", detail_kinds)

    def test_pending_schedule_change_display_uses_live_balance_when_saved_snapshot_is_stale(self):
        self.employee.date_joined = date(2027, 1, 1)
        self.employee.annual_paid_leave_days = 14
        self.employee.save(update_fields=["date_joined", "annual_paid_leave_days"])
        schedule_item = self._create_schedule_item(start_date=date(2027, 7, 1), end_date=date(2027, 7, 14))
        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.employee,
            new_start_date=date(2027, 8, 1),
            new_end_date=date(2027, 8, 14),
        )
        change_request.balance_after_change = Decimal("-14.00")
        change_request.risk_score = 95
        change_request.risk_level = "high"
        change_request.save(update_fields=["balance_after_change", "risk_score", "risk_level"])

        enrich_schedule_change_request(change_request, include_live_risk_explanation=True)
        row = serialize_schedule_change_request_row(change_request)

        self.assertEqual(change_request.balance_after_change, Decimal("0.00"))
        self.assertLess(row["risk_score"], 95)

    def test_reviewed_schedule_change_detail_uses_saved_decision_balance(self):
        self.employee.date_joined = date(2027, 1, 1)
        self.employee.annual_paid_leave_days = 14
        self.employee.save(update_fields=["date_joined", "annual_paid_leave_days"])
        schedule_item = self._create_schedule_item(start_date=date(2027, 7, 1), end_date=date(2027, 7, 14))
        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.employee,
            new_start_date=date(2027, 8, 1),
            new_end_date=date(2027, 8, 14),
        )

        reject_schedule_change_request(change_request.id, reviewer=self.department_head)
        change_request.refresh_from_db()
        change_request.balance_after_change = Decimal("11.00")
        change_request.risk_score = 95
        change_request.risk_level = "high"
        change_request.department_load_level = 5
        change_request.overlapping_absences_count = 7
        change_request.remaining_staff_count = 13
        change_request.min_staff_required = 13
        change_request.save(
            update_fields=[
                "balance_after_change",
                "risk_score",
                "risk_level",
                "department_load_level",
                "overlapping_absences_count",
                "remaining_staff_count",
                "min_staff_required",
            ]
        )

        self.client.force_login(self.department_head.user)
        with patch(
            "apps.leave.services.schedule_changes.calculate_schedule_change_request_risk",
            side_effect=AssertionError("Reviewed detail must use the saved decision snapshot."),
        ):
            response = self.client.get(reverse("schedule_change_detail", args=[change_request.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["change_request"].balance_after_change, Decimal("11.00"))
        self.assertFalse(response.context["schedule_change_saved_risk_snapshot_changed"])
        self.assertNotContains(response, "Недостаточно дней")
        self.assertContains(response, "По сохраненному расчету отдел остается ровно на минимуме")

    def test_reviewed_schedule_change_live_risk_excludes_created_replacement_item(self):
        self.employee.date_joined = date(2027, 1, 1)
        self.employee.annual_paid_leave_days = 14
        self.employee.save(update_fields=["date_joined", "annual_paid_leave_days"])
        schedule_item = self._create_schedule_item(start_date=date(2027, 7, 1), end_date=date(2027, 7, 14))
        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.employee,
            new_start_date=date(2027, 8, 1),
            new_end_date=date(2027, 8, 14),
        )

        approve_schedule_change_request(change_request.id, reviewer=self.department_head)
        change_request.refresh_from_db()
        live_risk = calculate_schedule_change_request_risk(change_request)
        risk_explanation = build_schedule_change_risk_explanation(change_request)
        detail_kinds = {detail["kind"] for detail in risk_explanation["details"]}

        enrich_schedule_change_request(change_request, include_live_risk_explanation=True)

        self.assertEqual(live_risk["balance_after_change"], Decimal("0.00"))
        self.assertEqual(change_request.balance_after_change, Decimal("0.00"))
        self.assertNotIn("negative_balance", detail_kinds)

    def test_schedule_change_still_blocks_real_paid_balance_overrun(self):
        self.employee.date_joined = date(2027, 1, 1)
        self.employee.annual_paid_leave_days = 14
        self.employee.save(update_fields=["date_joined", "annual_paid_leave_days"])
        schedule_item = self._create_schedule_item(start_date=date(2027, 7, 1), end_date=date(2027, 7, 14))

        risk_payload = calculate_schedule_change_risk(schedule_item, date(2027, 8, 1), date(2027, 8, 21))
        request_risk = calculate_vacation_request_risk_with_explanation(
            self.employee,
            date(2027, 8, 1),
            date(2027, 8, 1),
            "paid",
        )
        request_detail_kinds = {
            detail["kind"]
            for detail in request_risk["risk_explanation"]["details"]
        }

        self.assertEqual(risk_payload["balance_after_change"], Decimal("-7.00"))
        with self.assertRaisesMessage(ValidationError, "превышает доступный баланс"):
            create_schedule_change_request(
                schedule_item.id,
                requested_by=self.employee,
                new_start_date=date(2027, 8, 1),
                new_end_date=date(2027, 8, 21),
            )
        self.assertLess(request_risk["balance_after_request"], 0)
        self.assertIn("negative_balance", request_detail_kinds)

    def test_schedule_change_preview_includes_ai_module_fields(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 7, 1), end_date=date(2027, 7, 14))

        response = self._preview_transfer(schedule_item, self.employee, date(2027, 8, 1), date(2027, 8, 14))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["can_submit"])
        self.assertIn("module_score", payload)
        self.assertIn("module_score_label", payload)
        self.assertIn("module_confidence", payload)
        self.assertIn("module_recommendation", payload)
        self.assertIn("module_recommendation_label", payload)
        self.assertIn("module_action", payload)
        self.assertIn("module_explanation", payload)
        self.assertIn("module_model_version", payload)
        self.assertIn("module_scorer_kind", payload)
        self.assertEqual(payload["module_alternatives"], [])

    def test_schedule_change_preview_allows_manager_proposal(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 8, 1), end_date=date(2027, 8, 14))

        response = self._preview_transfer(schedule_item, self.department_head, date(2027, 9, 1), date(2027, 9, 14))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["can_submit"])

    def test_schedule_change_preview_forbids_unavailable_actor(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 8, 1), end_date=date(2027, 8, 14))

        response = self._preview_transfer(schedule_item, self.foreign_department_head, date(2027, 9, 1), date(2027, 9, 14))

        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json()["can_submit"])

    def test_schedule_change_preview_blocks_when_no_fourteen_day_part_remains(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 8, 1), end_date=date(2027, 8, 14))
        self._create_schedule_item(start_date=date(2027, 10, 1), end_date=date(2027, 10, 7))

        response = self._preview_transfer(schedule_item, self.employee, date(2027, 9, 1), date(2027, 9, 7))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["can_submit"])
        self.assertIn("не меньше 14 дней", payload["message"])
        self.assertEqual(payload["new_chargeable_days"], 7)
        self.assertEqual(payload["chargeable_days_delta_label"], "Освободится 7 д.")

    def test_schedule_change_preview_scores_invalid_transfer_as_blocked(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 8, 1), end_date=date(2027, 8, 14))
        self._create_schedule_item(start_date=date(2027, 10, 1), end_date=date(2027, 10, 7))

        response = self._preview_transfer(schedule_item, self.employee, date(2027, 9, 1), date(2027, 9, 7))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["can_submit"])
        self.assertEqual(payload["module_recommendation"], "blocked")
        self.assertEqual(payload["module_score"], 0)
        self.assertIn("module_explanation", payload)

    def test_schedule_change_preview_allows_shortening_when_another_fourteen_day_part_exists(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 8, 1), end_date=date(2027, 8, 14))
        self._create_schedule_item(start_date=date(2027, 10, 1), end_date=date(2027, 10, 14))

        response = self._preview_transfer(schedule_item, self.employee, date(2027, 9, 1), date(2027, 9, 7))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["can_submit"])
        self.assertEqual(payload["old_chargeable_days"], 14)
        self.assertEqual(payload["new_chargeable_days"], 7)
        self.assertEqual(payload["chargeable_days_delta"], -7)
        self.assertEqual(payload["chargeable_days_delta_label"], "Освободится 7 д.")

    def test_schedule_change_preview_allows_shortening_initial_short_leave_when_long_part_exists(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 7, 1), end_date=date(2027, 7, 7))
        schedule_item.chargeable_days = 7
        schedule_item.save(update_fields=["chargeable_days"])
        self._create_schedule_item(start_date=date(2027, 10, 1), end_date=date(2027, 10, 14))

        response = self._preview_transfer(schedule_item, self.employee, date(2027, 7, 15), date(2027, 7, 19))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["can_submit"])
        self.assertEqual(payload["old_chargeable_days"], 7)
        self.assertEqual(payload["new_chargeable_days"], 5)
        self.assertEqual(payload["chargeable_days_delta"], -2)
        self.assertEqual(payload["chargeable_days_delta_label"], "Освободится 2 д.")

    def test_schedule_change_preview_warns_about_staffing_conflict_without_blocking(self):
        DepartmentStaffingRule.objects.create(
            department=self.engineering,
            min_staff_required=10,
            max_absent=10,
            criticality_level=5,
        )
        schedule_item = self._create_schedule_item(start_date=date(2027, 10, 1), end_date=date(2027, 10, 14))

        response = self._preview_transfer(schedule_item, self.employee, date(2027, 9, 1), date(2027, 9, 14))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["can_submit"])
        self.assertTrue(payload["risk_is_conflict"])
        self.assertGreaterEqual(payload["risk_score"], 70)
        self.assertIn("конфликт", payload["message"].lower())
