from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from apps.leave.ml.training_sources import ensure_approved_schedule_training_feedback
from apps.leave.models import (
    VacationNeuralTrainingJob,
    VacationSchedule,
    VacationScheduleCandidate,
    VacationScheduleCandidateFeedback,
    VacationScheduleCandidatePackage,
    VacationScheduleCandidatePackagePeriod,
    VacationScheduleGenerationRun,
    VacationScheduleItem,
)
from apps.leave.services.neural_training_jobs import get_or_create_neural_training_job
from apps.leave.tests.base import LeaveTestCase


class VacationNeuralTrainingJobTests(LeaveTestCase):
    def test_start_training_requires_approved_schedule(self):
        year = self._year()
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(reverse("schedule_neural_training_start", args=[year]))

        self.assertEqual(response.status_code, 400)
        self.assertIn("Сначала", response.json()["message"])
        self.assertFalse(VacationNeuralTrainingJob.objects.exists())

    def test_hr_can_start_training_job_and_duplicate_click_reuses_it(self):
        schedule = self._approved_schedule_with_ml_trace()
        self.client.force_login(self.hr_employee.user)

        with patch("apps.leave.views.schedule_planning.start_neural_training_process") as starter:
            first = self.client.post(reverse("schedule_neural_training_start", args=[schedule.year]))
            second = self.client.post(reverse("schedule_neural_training_start", args=[schedule.year]))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(VacationNeuralTrainingJob.objects.count(), 1)
        self.assertEqual(first.json()["job_id"], second.json()["job_id"])
        starter.assert_called_once()

    def test_employee_cannot_start_training_job(self):
        schedule = self._approved_schedule_with_ml_trace()
        self.client.force_login(self.employee.user)

        response = self.client.post(reverse("schedule_neural_training_start", args=[schedule.year]))

        self.assertEqual(response.status_code, 400)
        self.assertFalse(VacationNeuralTrainingJob.objects.exists())

    def test_status_endpoint_requires_token(self):
        schedule = self._approved_schedule_with_ml_trace()
        job, _ = get_or_create_neural_training_job(year=schedule.year, actor=self.hr_employee)
        self.client.force_login(self.hr_employee.user)

        forbidden = self.client.get(reverse("schedule_neural_training_status", args=[schedule.year, job.id]), {"token": "bad"})
        ok = self.client.get(reverse("schedule_neural_training_status", args=[schedule.year, job.id]), {"token": job.token})

        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["job_id"], job.id)

    def test_approved_schedule_feedback_is_created_without_duplicates(self):
        schedule = self._approved_schedule_with_ml_trace()

        created = ensure_approved_schedule_training_feedback(schedule, actor=self.enterprise_head)
        created_again = ensure_approved_schedule_training_feedback(schedule, actor=self.enterprise_head)

        self.assertEqual(created, 1)
        self.assertEqual(created_again, 0)
        feedback = VacationScheduleCandidateFeedback.objects.get(
            schedule_item__schedule=schedule,
            reviewer=self.enterprise_head,
        )
        self.assertEqual(feedback.decision, VacationScheduleCandidateFeedback.DECISION_AGREE)
        self.assertEqual(feedback.reviewer_role, VacationScheduleCandidateFeedback.ROLE_ENTERPRISE_HEAD)

    def test_orchestration_command_updates_job_and_includes_year(self):
        schedule = self._approved_schedule_with_ml_trace()
        job, _ = get_or_create_neural_training_job(year=schedule.year, actor=self.hr_employee)
        candidate_result = self._training_result("vacation-candidate-mlp-v2", examples_count=12)
        package_result = self._training_result("vacation-package-ranker-v3", examples_count=6)

        with (
            patch("apps.leave.ml.retraining.train_candidate_mlp_model", return_value=candidate_result) as candidate_train,
            patch("apps.leave.ml.retraining.train_package_ranker_model", return_value=package_result) as package_train,
            patch("apps.leave.ml.retraining.load_candidate_mlp_model"),
            patch("apps.leave.ml.retraining.load_package_ranker_model"),
        ):
            call_command(
                "train_vacation_neural_models",
                year=schedule.year,
                job_id=job.id,
                candidate_epochs=1,
                package_epochs=1,
                candidate_min_examples=1,
                package_min_examples=1,
            )

        job.refresh_from_db()
        self.assertEqual(job.status, VacationNeuralTrainingJob.STATUS_SUCCEEDED)
        self.assertEqual(job.progress_percent, 100)
        self.assertIn(schedule.year, job.metrics_payload["source"]["years"])
        self.assertEqual(job.metrics_payload["candidate"]["examples_count"], 12)
        self.assertEqual(job.metrics_payload["package"]["examples_count"], 6)
        candidate_train.assert_called_once()
        package_train.assert_called_once()

    def _approved_schedule_with_ml_trace(self, *, year=None):
        year = year or self._year()
        schedule = VacationSchedule.objects.create(
            year=year,
            status=VacationSchedule.STATUS_APPROVED,
            created_by=self.hr_employee,
            approved_by=self.enterprise_head,
            approved_at=timezone.now(),
        )
        run = VacationScheduleGenerationRun.objects.create(
            schedule=schedule,
            year=year,
            mode=VacationScheduleGenerationRun.MODE_HYBRID,
            status=VacationScheduleGenerationRun.STATUS_COMPLETED,
            actor=self.hr_employee,
            model_version="vacation-candidate-mlp-v2",
        )
        start_date = date(year, 6, 1)
        end_date = start_date + timedelta(days=6)
        candidate = VacationScheduleCandidate.objects.create(
            generation_run=run,
            schedule=schedule,
            employee=self.employee,
            start_date=start_date,
            end_date=end_date,
            vacation_type="paid",
            chargeable_days=7,
            kind=VacationScheduleCandidate.KIND_AUTO,
            source=VacationScheduleItem.SOURCE_GENERATED,
            passed_hard_rules=True,
            risk_score=0,
            risk_level=VacationScheduleItem.RISK_LOW,
            features={
                "feature_schema_version": 1,
                "planning_year": year,
                "period_chargeable_days": 7,
                "period_calendar_days": 7,
                "planning_candidate_coverage_ratio": 1,
                "risk_score": 0,
                "risk_staff_margin": 3,
            },
            score=82,
            confidence=88,
            model_version="vacation-candidate-mlp-v2",
            explanation="Тестовый выбранный период.",
            decision=VacationScheduleCandidate.DECISION_SELECTED,
            decision_rank=1,
            selected_at=timezone.now(),
        )
        item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=start_date,
            end_date=end_date,
            vacation_type="paid",
            chargeable_days=Decimal("7.00"),
            status=VacationScheduleItem.STATUS_APPROVED,
            source=VacationScheduleItem.SOURCE_GENERATED,
            risk_score=0,
            risk_level=VacationScheduleItem.RISK_LOW,
            generated_by_ai=True,
            generation_run=run,
            selected_candidate=candidate,
            ai_score=candidate.score,
            ai_confidence=candidate.confidence,
            ai_model_version=candidate.model_version,
            ai_explanation=candidate.explanation,
        )
        package = VacationScheduleCandidatePackage.objects.create(
            generation_run=run,
            schedule=schedule,
            employee=self.employee,
            periods_count=1,
            total_chargeable_days=7,
            source=VacationScheduleItem.SOURCE_GENERATED,
            passed_hard_rules=True,
            risk_score=0,
            risk_level=VacationScheduleItem.RISK_LOW,
            features={"feature_schema_version": 1},
            score=84,
            confidence=89,
            model_version="vacation-package-ranker-v3",
            explanation="Тестовый выбранный пакет.",
            decision=VacationScheduleCandidatePackage.DECISION_SELECTED,
            decision_rank=1,
            selected_at=timezone.now(),
        )
        VacationScheduleCandidatePackagePeriod.objects.create(
            candidate_package=package,
            candidate=candidate,
            schedule_item=item,
            start_date=start_date,
            end_date=end_date,
            chargeable_days=7,
            passed_hard_rules=True,
            risk_score=0,
            risk_level=VacationScheduleItem.RISK_LOW,
            features=candidate.features,
        )
        return schedule

    def _training_result(self, version, *, examples_count):
        return SimpleNamespace(
            model_path=f"/tmp/{version}.json",
            metrics_path=f"/tmp/{version}_metrics.json",
            examples_count=examples_count,
            class_balance={"selected": examples_count},
            metrics={"train": {"mae": 0.1}, "val": {"mae": 0.2}, "test": {"mae": 0.3}},
            model_artifact={"version": version},
            metrics_artifact={"years": [self._year()]},
        )
