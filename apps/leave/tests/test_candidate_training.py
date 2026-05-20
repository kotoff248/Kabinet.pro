from contextlib import contextmanager
import importlib.util
import json
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import override_settings
from django.utils import timezone

from apps.leave.models import (
    VacationSchedule,
    VacationScheduleCandidate,
    VacationScheduleCandidateFeedback,
    VacationScheduleCandidatePackage,
    VacationScheduleCandidatePackagePeriod,
    VacationScheduleGenerationRun,
    VacationScheduleItem,
)
from apps.leave.ml.runtime import (
    DEFAULT_NEURAL_CANDIDATE_SCORER_VERSION,
    candidate_model_filename,
    load_candidate_mlp_model,
    reset_candidate_mlp_model_cache,
)
from apps.leave.ml.scoring import score_candidate_features
from apps.leave.ml.package_runtime import (
    DEFAULT_NEURAL_PACKAGE_RANKER_VERSION,
    package_model_filename,
    reset_package_ranker_model_cache,
)
from apps.leave.ml.package_scoring import score_generation_package
from apps.leave.ml.package_training import (
    PACKAGE_HIDDEN_NODE_NAMES,
    PACKAGE_TARGET_HEADS,
    build_package_training_targets,
    collect_package_training_dataset,
)
from apps.leave.ml.training import (
    HIDDEN_NODE_NAMES,
    TARGET_HEADS,
    build_candidate_training_targets,
    collect_candidate_training_dataset,
)
from apps.leave.services.schedule_drafts.types import DraftGenerationCandidate, DraftGenerationCandidatePackage
from apps.leave.tests.base import LeaveTestCase


class CandidateTrainingTests(LeaveTestCase):
    def setUp(self):
        super().setUp()
        self.training_year = timezone.localdate().year - 1
        self.training_schedule = VacationSchedule.objects.create(
            year=self.training_year,
            status=VacationSchedule.STATUS_ARCHIVED,
            created_by=self.hr_employee,
            approved_by=self.enterprise_head,
            approved_at=timezone.now(),
        )
        self.training_run = VacationScheduleGenerationRun.objects.create(
            schedule=self.training_schedule,
            year=self.training_year,
            mode=VacationScheduleGenerationRun.MODE_HYBRID,
            status=VacationScheduleGenerationRun.STATUS_COMPLETED,
            actor=self.hr_employee,
            model_version=DEFAULT_NEURAL_CANDIDATE_SCORER_VERSION,
        )

    def tearDown(self):
        reset_candidate_mlp_model_cache()
        reset_package_ranker_model_cache()
        super().tearDown()

    @override_settings(VACATION_CANDIDATE_SCORER_VERSION="vacation-candidate-mlp-v1")
    def test_default_loader_uses_v1(self):
        reset_candidate_mlp_model_cache()

        model = load_candidate_mlp_model()

        self.assertEqual(model["version"], DEFAULT_NEURAL_CANDIDATE_SCORER_VERSION)

    def test_dataset_uses_saved_historical_features_only(self):
        selected = self._create_candidate(
            decision=VacationScheduleCandidate.DECISION_SELECTED,
            feedback_decision=VacationScheduleCandidateFeedback.DECISION_AGREE,
            risk_score=11,
            staff_margin=4,
        )
        draft_schedule = VacationSchedule.objects.create(
            year=self.training_year + 2,
            status=VacationSchedule.STATUS_DRAFT,
            created_by=self.hr_employee,
        )
        draft_run = VacationScheduleGenerationRun.objects.create(
            schedule=draft_schedule,
            year=draft_schedule.year,
            mode=VacationScheduleGenerationRun.MODE_HYBRID,
            status=VacationScheduleGenerationRun.STATUS_COMPLETED,
            actor=self.hr_employee,
        )
        VacationScheduleCandidate.objects.create(
            generation_run=draft_run,
            schedule=draft_schedule,
            employee=self.employee,
            start_date=date(draft_schedule.year, 6, 1),
            end_date=date(draft_schedule.year, 6, 7),
            chargeable_days=7,
            kind=VacationScheduleCandidate.KIND_AUTO,
            passed_hard_rules=True,
            features=self._features(
                date(draft_schedule.year, 6, 1),
                date(draft_schedule.year, 6, 7),
                risk_score=99,
            ),
            decision=VacationScheduleCandidate.DECISION_SELECTED,
        )

        dataset = collect_candidate_training_dataset(current_year=self.training_year)

        self.assertEqual([example.candidate_id for example in dataset.examples], [selected.id])
        self.assertEqual(dataset.examples[0].inputs["has_risk_payload"], 1.0)
        self.assertGreater(dataset.examples[0].inputs["staff_margin_positive"], 0.0)
        self.assertEqual(dataset.class_balance, {"selected_agree": 1})

    def test_dataset_can_include_approved_planning_year(self):
        historical = self._create_candidate(
            decision=VacationScheduleCandidate.DECISION_SELECTED,
            feedback_decision=VacationScheduleCandidateFeedback.DECISION_AGREE,
            index=1,
        )
        planning_year = self.training_year + 2
        planning_schedule = VacationSchedule.objects.create(
            year=planning_year,
            status=VacationSchedule.STATUS_APPROVED,
            created_by=self.hr_employee,
            approved_by=self.enterprise_head,
            approved_at=timezone.now(),
        )
        planning_run = VacationScheduleGenerationRun.objects.create(
            schedule=planning_schedule,
            year=planning_year,
            mode=VacationScheduleGenerationRun.MODE_HYBRID,
            status=VacationScheduleGenerationRun.STATUS_COMPLETED,
            actor=self.hr_employee,
        )
        start_date = date(planning_year, 6, 1)
        planning_candidate = VacationScheduleCandidate.objects.create(
            generation_run=planning_run,
            schedule=planning_schedule,
            employee=self.employee,
            start_date=start_date,
            end_date=start_date + timedelta(days=6),
            chargeable_days=7,
            kind=VacationScheduleCandidate.KIND_AUTO,
            passed_hard_rules=True,
            features=self._features(start_date, start_date + timedelta(days=6)),
            decision=VacationScheduleCandidate.DECISION_SELECTED,
        )
        planning_item = VacationScheduleItem.objects.create(
            schedule=planning_schedule,
            employee=self.employee,
            start_date=start_date,
            end_date=start_date + timedelta(days=6),
            vacation_type="paid",
            chargeable_days=7,
            status=VacationScheduleItem.STATUS_APPROVED,
            selected_candidate=planning_candidate,
            generation_run=planning_run,
        )
        VacationScheduleCandidateFeedback.objects.create(
            schedule_item=planning_item,
            candidate=planning_candidate,
            generation_run=planning_run,
            reviewer=self.enterprise_head,
            reviewer_role=VacationScheduleCandidateFeedback.ROLE_ENTERPRISE_HEAD,
            decision=VacationScheduleCandidateFeedback.DECISION_AGREE,
        )

        current_year_dataset = collect_candidate_training_dataset(current_year=self.training_year)
        planning_dataset = collect_candidate_training_dataset(max_schedule_year=planning_year)

        self.assertEqual([example.candidate_id for example in current_year_dataset.examples], [historical.id])
        self.assertIn(planning_candidate.id, [example.candidate_id for example in planning_dataset.examples])
        self.assertIn(planning_year, planning_dataset.years)

    def test_training_labels_distinguish_feedback_and_decisions(self):
        agree = self._create_candidate(
            decision=VacationScheduleCandidate.DECISION_SELECTED,
            feedback_decision=VacationScheduleCandidateFeedback.DECISION_AGREE,
            candidate_kind=VacationScheduleCandidate.KIND_PRIMARY_PREFERENCE,
            feature_overrides={
                "candidate_kind": VacationScheduleCandidate.KIND_PRIMARY_PREFERENCE,
                "preference_priority": "primary",
                "preference_has_preference": True,
                "preference_exact_period_match": True,
                "planning_open_required_days": 52,
                "planning_candidate_coverage_ratio": 14 / 52,
                "period_calendar_days": 14,
                "period_chargeable_days": 14,
            },
            index=1,
        )
        needs_change = self._create_candidate(
            decision=VacationScheduleCandidate.DECISION_SELECTED,
            feedback_decision=VacationScheduleCandidateFeedback.DECISION_NEEDS_CHANGE,
            index=2,
        )
        reject = self._create_candidate(
            decision=VacationScheduleCandidate.DECISION_SELECTED,
            feedback_decision=VacationScheduleCandidateFeedback.DECISION_REJECT,
            outcome="later_transferred",
            item_status=VacationScheduleItem.STATUS_TRANSFERRED,
            index=3,
        )
        rejected = self._create_candidate(
            decision=VacationScheduleCandidate.DECISION_REJECTED,
            index=4,
        )
        blocked = self._create_candidate(
            decision=VacationScheduleCandidate.DECISION_BLOCKED,
            passed_hard_rules=False,
            package_decision=VacationScheduleCandidatePackage.DECISION_BLOCKED,
            index=5,
        )

        labels = {
            "agree": build_candidate_training_targets(agree),
            "needs_change": build_candidate_training_targets(needs_change),
            "reject": build_candidate_training_targets(reject),
            "rejected": build_candidate_training_targets(rejected),
            "blocked": build_candidate_training_targets(blocked),
        }

        self.assertEqual(labels["agree"][0], "selected_agree")
        self.assertGreater(labels["agree"][1]["score"], 0.8)
        self.assertEqual(labels["needs_change"][0], "selected_needs_change")
        self.assertLess(labels["needs_change"][1]["score"], labels["agree"][1]["score"])
        self.assertEqual(labels["reject"][0], "selected_reject")
        self.assertLess(labels["reject"][1]["score"], labels["needs_change"][1]["score"])
        self.assertEqual(labels["rejected"][0], "rejected")
        self.assertEqual(labels["blocked"][0], "blocked")
        self.assertEqual(labels["blocked"][1]["score"], 0.0)
        self.assertEqual(labels["blocked"][1]["avoid"], 1.0)

    def test_v2_json_loads_through_scorer(self):
        with self._temporary_model_dir() as tmp_dir:
            self._write_simple_model(tmp_dir, "vacation-candidate-mlp-v2")
            with override_settings(
                VACATION_CANDIDATE_MODEL_DIR=tmp_dir,
                VACATION_CANDIDATE_SCORER_VERSION="vacation-candidate-mlp-v2",
            ):
                reset_candidate_mlp_model_cache()

                result = score_candidate_features(
                    self._features(date(self.training_year, 6, 1), date(self.training_year, 6, 7)),
                    passed_hard_rules=True,
                )

        self.assertEqual(result.model_version, "vacation-candidate-mlp-v2")
        self.assertGreaterEqual(result.score, 0)
        self.assertGreaterEqual(result.confidence, 0)
        self.assertIn(result.recommendation, {"prefer", "normal", "avoid"})

    def test_loader_falls_back_when_v2_is_missing(self):
        with override_settings(VACATION_CANDIDATE_SCORER_VERSION="vacation-candidate-mlp-v2-missing"):
            reset_candidate_mlp_model_cache()

            result = score_candidate_features(
                self._features(date(self.training_year, 6, 1), date(self.training_year, 6, 7)),
                passed_hard_rules=True,
            )

        self.assertEqual(result.model_version, DEFAULT_NEURAL_CANDIDATE_SCORER_VERSION)
        self.assertEqual(result.scorer_kind, "tabular_mlp")

    @override_settings(VACATION_CANDIDATE_SCORER_VERSION="vacation-candidate-mlp-v2")
    def test_preference_period_scores_are_calibrated_without_runtime_floor(self):
        reset_candidate_mlp_model_cache()

        good_primary_features = self._features(
            date(self.training_year, 7, 1),
            date(self.training_year, 7, 14),
            candidate_kind=VacationScheduleCandidate.KIND_PRIMARY_PREFERENCE,
            risk_score=21,
            staff_margin=3,
            load_level=1.2,
            preference_priority="primary",
            preference_has_preference=True,
            preference_exact_period_match=True,
            planning_open_required_days=52,
            planning_candidate_coverage_ratio=14 / 52,
            period_chargeable_days=14,
        )
        risky_primary_features = good_primary_features | {
            "risk_score": 82,
            "risk_level_weight": 3,
            "risk_staff_margin": 0,
            "risk_department_load_level": 4.2,
            "risk_has_substitution_capacity": False,
        }

        good_primary = score_candidate_features(good_primary_features, passed_hard_rules=True)
        risky_primary = score_candidate_features(risky_primary_features, passed_hard_rules=True)
        blocked = score_candidate_features(risky_primary_features, passed_hard_rules=False)

        self.assertEqual(good_primary.model_version, "vacation-candidate-mlp-v2")
        self.assertGreaterEqual(good_primary.score, Decimal("68.00"))
        self.assertLessEqual(good_primary.score, Decimal("90.00"))
        self.assertNotEqual(good_primary.score, Decimal("68.00"))
        self.assertGreater(good_primary.score, risky_primary.score + Decimal("15.00"))
        self.assertNotEqual(good_primary.recommendation, "blocked")
        self.assertEqual(blocked.score, Decimal("0.00"))
        self.assertEqual(blocked.recommendation, "blocked")

    @override_settings(VACATION_CANDIDATE_SCORER_VERSION="vacation-candidate-mlp-v2")
    def test_safe_backup_can_beat_risky_primary(self):
        reset_candidate_mlp_model_cache()

        risky_primary_features = self._features(
            date(self.training_year, 7, 1),
            date(self.training_year, 7, 14),
            candidate_kind=VacationScheduleCandidate.KIND_PRIMARY_PREFERENCE,
            risk_score=82,
            staff_margin=0,
            load_level=4.2,
            preference_priority="primary",
            preference_has_preference=True,
            preference_exact_period_match=True,
            planning_open_required_days=52,
            planning_candidate_coverage_ratio=14 / 52,
            period_chargeable_days=14,
        )
        risky_primary_features["risk_level_weight"] = 3
        risky_primary_features["risk_has_substitution_capacity"] = False
        safe_backup_features = risky_primary_features | {
            "candidate_kind": VacationScheduleCandidate.KIND_BACKUP_PREFERENCE,
            "preference_priority": "backup",
            "risk_score": 18,
            "risk_level_weight": 1,
            "risk_staff_margin": 4,
            "risk_department_load_level": 1.1,
            "risk_has_substitution_capacity": True,
        }

        risky_primary = score_candidate_features(risky_primary_features, passed_hard_rules=True)
        safe_backup = score_candidate_features(safe_backup_features, passed_hard_rules=True)

        self.assertGreater(safe_backup.score, risky_primary.score)
        self.assertEqual(safe_backup.recommendation, "prefer")
        self.assertEqual(risky_primary.recommendation, "avoid")

    def test_training_command_reports_missing_historical_traces(self):
        VacationScheduleCandidate.objects.all().delete()

        with self.assertRaisesMessage(CommandError, "Исторические ML-следы не найдены"):
            call_command(
                "train_vacation_candidate_model",
                min_examples=1,
                stdout=StringIO(),
            )

    def test_training_command_writes_v2_json_and_metrics(self):
        if importlib.util.find_spec("torch") is None:
            self.skipTest("PyTorch is not installed in the current virtual environment.")

        for index in range(12):
            if index % 4 == 0:
                self._create_candidate(
                    decision=VacationScheduleCandidate.DECISION_SELECTED,
                    feedback_decision=VacationScheduleCandidateFeedback.DECISION_AGREE,
                    index=index,
                )
            elif index % 4 == 1:
                self._create_candidate(
                    decision=VacationScheduleCandidate.DECISION_SELECTED,
                    feedback_decision=VacationScheduleCandidateFeedback.DECISION_NEEDS_CHANGE,
                    risk_score=70,
                    index=index,
                )
            elif index % 4 == 2:
                self._create_candidate(
                    decision=VacationScheduleCandidate.DECISION_REJECTED,
                    index=index,
                )
            else:
                self._create_candidate(
                    decision=VacationScheduleCandidate.DECISION_BLOCKED,
                    passed_hard_rules=False,
                    package_decision=VacationScheduleCandidatePackage.DECISION_BLOCKED,
                    index=index,
                )

        with self._temporary_model_dir() as tmp_dir:
            call_command(
                "train_vacation_candidate_model",
                output_version="vacation-candidate-mlp-v2-test",
                output_dir=tmp_dir,
                epochs=2,
                lr=0.01,
                seed=7,
                min_examples=5,
                stdout=StringIO(),
            )
            model_file = Path(tmp_dir) / candidate_model_filename("vacation-candidate-mlp-v2-test")
            metrics_file = Path(tmp_dir) / candidate_model_filename("vacation-candidate-mlp-v2-test-metrics")

            model = json.loads(model_file.read_text(encoding="utf-8"))
            metrics = json.loads(metrics_file.read_text(encoding="utf-8"))

        self.assertEqual(model["version"], "vacation-candidate-mlp-v2-test")
        self.assertIn("score", model["heads"])
        self.assertEqual(metrics["examples_count"], 12)
        self.assertIn("train", metrics["metrics"])

    def test_package_dataset_and_labels_use_historical_packages(self):
        selected = self._create_candidate(
            decision=VacationScheduleCandidate.DECISION_SELECTED,
            feedback_decision=VacationScheduleCandidateFeedback.DECISION_AGREE,
            package_decision=VacationScheduleCandidatePackage.DECISION_SELECTED,
            index=1,
        )
        rejected = self._create_candidate(
            decision=VacationScheduleCandidate.DECISION_REJECTED,
            package_decision=VacationScheduleCandidatePackage.DECISION_REJECTED,
            index=2,
        )
        blocked = self._create_candidate(
            decision=VacationScheduleCandidate.DECISION_BLOCKED,
            passed_hard_rules=False,
            package_decision=VacationScheduleCandidatePackage.DECISION_BLOCKED,
            index=3,
        )

        dataset = collect_package_training_dataset(current_year=self.training_year)
        package_labels = {
            example.package_id: example.label_bucket
            for example in dataset.examples
        }
        selected_package = selected.package_periods.get().candidate_package
        rejected_package = rejected.package_periods.get().candidate_package
        blocked_package = blocked.package_periods.get().candidate_package

        self.assertEqual(package_labels[selected_package.id], "selected_package_agree")
        self.assertEqual(package_labels[rejected_package.id], "rejected_package")
        self.assertEqual(package_labels[blocked_package.id], "blocked")
        self.assertIn("coverage_fit", dataset.input_names)
        self.assertIn("period_score_avg", dataset.input_names)

        label, targets = build_package_training_targets(selected_package)
        self.assertEqual(label, "selected_package_agree")
        self.assertGreater(targets["score"], 0.5)

    def test_v3_package_json_loads_through_package_scorer(self):
        year = self.training_year
        package = DraftGenerationCandidatePackage(
            employee=self.employee,
            candidates=[
                DraftGenerationCandidate(
                    employee=self.employee,
                    start_date=date(year, 6, 1),
                    end_date=date(year, 6, 14),
                    kind=VacationScheduleCandidate.KIND_AUTO,
                    source=VacationScheduleItem.SOURCE_GENERATED,
                    comment="Тестовый пакет.",
                    metadata={
                        "passed_hard_rules": True,
                        "chargeable_days": Decimal("14.00"),
                        "open_required_days": Decimal("14.00"),
                        "risk_score": 14,
                        "risk_level": VacationScheduleItem.RISK_LOW,
                        "scoring_score": Decimal("84.00"),
                        "scoring_confidence": Decimal("88.00"),
                    },
                )
            ],
            source=VacationScheduleItem.SOURCE_GENERATED,
            explanation="Тестовый пакет.",
            metadata={
                "total_chargeable_days": Decimal("14.00"),
                "remaining_after_package": Decimal("0.00"),
                "package_closes_need": True,
            },
        )

        with self._temporary_model_dir() as tmp_dir:
            self._write_simple_package_model(tmp_dir, "vacation-package-ranker-v3-test")
            with override_settings(
                VACATION_PACKAGE_MODEL_DIR=tmp_dir,
                VACATION_PACKAGE_RANKER_VERSION="vacation-package-ranker-v3-test",
            ):
                reset_package_ranker_model_cache()

                result = score_generation_package(package)

        self.assertEqual(result.model_version, "vacation-package-ranker-v3-test")
        self.assertGreaterEqual(result.score, 0)
        self.assertGreaterEqual(result.confidence, 0)
        self.assertIn(result.recommendation, {"prefer", "normal", "avoid"})
        self.assertEqual(package.metadata["package_model_version"], "vacation-package-ranker-v3-test")

    def test_package_ranker_falls_back_when_v3_is_missing(self):
        year = self.training_year
        package = DraftGenerationCandidatePackage(
            employee=self.employee,
            candidates=[
                DraftGenerationCandidate(
                    employee=self.employee,
                    start_date=date(year, 9, 1),
                    end_date=date(year, 9, 14),
                    kind=VacationScheduleCandidate.KIND_AUTO,
                    source=VacationScheduleItem.SOURCE_GENERATED,
                    comment="Тестовый пакет.",
                    metadata={
                        "passed_hard_rules": True,
                        "chargeable_days": Decimal("14.00"),
                        "risk_score": 10,
                        "risk_level": VacationScheduleItem.RISK_LOW,
                        "scoring_score": Decimal("82.00"),
                        "scoring_confidence": Decimal("86.00"),
                    },
                )
            ],
            source=VacationScheduleItem.SOURCE_GENERATED,
            explanation="Тестовый пакет.",
            metadata={"total_chargeable_days": Decimal("14.00"), "package_closes_need": True},
        )

        with override_settings(VACATION_PACKAGE_RANKER_VERSION="vacation-package-ranker-v3-missing"):
            reset_package_ranker_model_cache()

            result = score_generation_package(package)

        self.assertIn(DEFAULT_NEURAL_PACKAGE_RANKER_VERSION, result.model_version)
        self.assertEqual(result.scorer_kind, "package_baseline_fallback")
        self.assertGreater(result.score, Decimal("0.00"))

    def test_package_training_command_writes_v3_json_and_metrics(self):
        if importlib.util.find_spec("torch") is None:
            self.skipTest("PyTorch is not installed in the current virtual environment.")

        for index in range(10):
            if index % 3 == 0:
                self._create_candidate(
                    decision=VacationScheduleCandidate.DECISION_SELECTED,
                    feedback_decision=VacationScheduleCandidateFeedback.DECISION_AGREE,
                    package_decision=VacationScheduleCandidatePackage.DECISION_SELECTED,
                    index=index,
                )
            elif index % 3 == 1:
                self._create_candidate(
                    decision=VacationScheduleCandidate.DECISION_REJECTED,
                    package_decision=VacationScheduleCandidatePackage.DECISION_REJECTED,
                    index=index,
                )
            else:
                self._create_candidate(
                    decision=VacationScheduleCandidate.DECISION_BLOCKED,
                    passed_hard_rules=False,
                    package_decision=VacationScheduleCandidatePackage.DECISION_BLOCKED,
                    index=index,
                )

        with self._temporary_model_dir() as tmp_dir:
            call_command(
                "train_vacation_package_model",
                output_version="vacation-package-ranker-v3-test",
                output_dir=tmp_dir,
                epochs=2,
                lr=0.01,
                seed=7,
                min_examples=5,
                stdout=StringIO(),
            )
            model_file = Path(tmp_dir) / package_model_filename("vacation-package-ranker-v3-test")
            metrics_file = Path(tmp_dir) / package_model_filename("vacation-package-ranker-v3-test-metrics")

            model = json.loads(model_file.read_text(encoding="utf-8"))
            metrics = json.loads(metrics_file.read_text(encoding="utf-8"))

        self.assertEqual(model["version"], "vacation-package-ranker-v3-test")
        self.assertEqual(model["kind"], "package_tabular_mlp")
        self.assertIn("score", model["heads"])
        self.assertEqual(metrics["examples_count"], 10)
        self.assertIn("train", metrics["metrics"])

    def _create_candidate(
        self,
        *,
        decision,
        feedback_decision=None,
        outcome="approved",
        passed_hard_rules=True,
        package_decision=None,
        item_status=VacationScheduleItem.STATUS_APPROVED,
        index=0,
        risk_score=20,
        staff_margin=3,
        candidate_kind=VacationScheduleCandidate.KIND_AUTO,
        feature_overrides=None,
    ):
        start_date = date(self.training_year, 2, 1) + timedelta(days=index * 20)
        end_date = start_date + timedelta(days=6)
        features = self._features(
            start_date,
            end_date,
            outcome=outcome,
            passed_hard_rules=passed_hard_rules,
            risk_score=risk_score,
            staff_margin=staff_margin,
            candidate_kind=candidate_kind,
        )
        features.update(feature_overrides or {})
        candidate = VacationScheduleCandidate.objects.create(
            generation_run=self.training_run,
            schedule=self.training_schedule,
            employee=self.employee,
            start_date=start_date,
            end_date=end_date,
            vacation_type="paid",
            chargeable_days=7,
            kind=candidate_kind,
            source=VacationScheduleItem.SOURCE_GENERATED,
            passed_hard_rules=passed_hard_rules,
            block_reason_key="" if passed_hard_rules else "staffing_conflict",
            block_reason="" if passed_hard_rules else "Недостаточный резерв состава.",
            risk_score=risk_score,
            risk_level=VacationScheduleItem.RISK_HIGH if risk_score >= 70 else VacationScheduleItem.RISK_LOW,
            features=features,
            score=80 if decision == VacationScheduleCandidate.DECISION_SELECTED else 30,
            confidence=85,
            model_version=DEFAULT_NEURAL_CANDIDATE_SCORER_VERSION,
            explanation="Исторический тестовый кандидат.",
            decision=decision,
            decision_rank=index + 1,
            selected_at=timezone.now() if decision == VacationScheduleCandidate.DECISION_SELECTED else None,
        )
        schedule_item = None
        if decision == VacationScheduleCandidate.DECISION_SELECTED:
            schedule_item = VacationScheduleItem.objects.create(
                schedule=self.training_schedule,
                employee=self.employee,
                start_date=start_date,
                end_date=end_date,
                vacation_type="paid",
                chargeable_days=7,
                status=item_status,
                source=VacationScheduleItem.SOURCE_GENERATED,
                risk_score=risk_score,
                risk_level=VacationScheduleItem.RISK_HIGH if risk_score >= 70 else VacationScheduleItem.RISK_LOW,
                generated_by_ai=True,
                generation_run=self.training_run,
                selected_candidate=candidate,
                ai_score=candidate.score,
                ai_confidence=candidate.confidence,
                ai_model_version=candidate.model_version,
                ai_explanation=candidate.explanation,
            )
        if feedback_decision:
            VacationScheduleCandidateFeedback.objects.create(
                schedule_item=schedule_item,
                candidate=candidate,
                generation_run=self.training_run,
                reviewer=self.hr_employee,
                reviewer_role=VacationScheduleCandidateFeedback.ROLE_HR,
                decision=feedback_decision,
                score_snapshot=candidate.score,
                confidence_snapshot=candidate.confidence,
                model_version_snapshot=candidate.model_version,
                explanation_snapshot=candidate.explanation,
            )
        if package_decision:
            package = VacationScheduleCandidatePackage.objects.create(
                generation_run=self.training_run,
                schedule=self.training_schedule,
                employee=self.employee,
                periods_count=1,
                total_chargeable_days=7,
                source=VacationScheduleItem.SOURCE_GENERATED,
                passed_hard_rules=passed_hard_rules,
                block_reason_key=candidate.block_reason_key,
                block_reason=candidate.block_reason,
                risk_score=candidate.risk_score,
                risk_level=candidate.risk_level,
                features={"feature_schema_version": 1},
                score=candidate.score,
                confidence=candidate.confidence,
                model_version=candidate.model_version,
                explanation=candidate.explanation,
                decision=package_decision,
            )
            VacationScheduleCandidatePackagePeriod.objects.create(
                candidate_package=package,
                candidate=candidate,
                schedule_item=schedule_item if package_decision == VacationScheduleCandidatePackage.DECISION_SELECTED else None,
                start_date=start_date,
                end_date=end_date,
                chargeable_days=7,
                passed_hard_rules=passed_hard_rules,
                block_reason_key=candidate.block_reason_key,
                block_reason=candidate.block_reason,
                risk_score=candidate.risk_score,
                risk_level=candidate.risk_level,
                features=candidate.features,
            )
        return candidate

    def _temporary_model_dir(self):
        base_dir = Path(settings.BASE_DIR) / ".tmp" / "candidate-training-tests"
        target_dir = base_dir / self._testMethodName
        target_dir.mkdir(parents=True, exist_ok=True)
        return _yield_directory(target_dir)

    def _features(
        self,
        start_date,
        end_date,
        *,
        outcome="approved",
        passed_hard_rules=True,
        risk_score=20,
        staff_margin=3,
        load_level=1.2,
        candidate_kind=VacationScheduleCandidate.KIND_AUTO,
        preference_priority="",
        preference_has_preference=False,
        preference_exact_period_match=False,
        planning_open_required_days=7,
        planning_candidate_coverage_ratio=1.0,
        period_chargeable_days=7,
    ):
        return {
            "feature_schema_version": 1,
            "historical_seed_trace": True,
            "historical_outcome": outcome,
            "candidate_kind": candidate_kind,
            "passed_hard_rules": passed_hard_rules,
            "planning_year": start_date.year,
            "planning_candidate_coverage_ratio": planning_candidate_coverage_ratio,
            "planning_open_required_days": planning_open_required_days,
            "planning_blocking_days": 0,
            "planning_has_blocker": False,
            "planning_ends_by_nearest_deadline": False,
            "period_calendar_days": (end_date - start_date).days + 1,
            "period_chargeable_days": period_chargeable_days,
            "period_summer_overlap_days": 0,
            "period_overlaps_summer": False,
            "period_cross_month": start_date.month != end_date.month,
            "risk_score": risk_score,
            "risk_level_weight": 3 if risk_score >= 70 else 1,
            "risk_staff_margin": staff_margin,
            "risk_department_load_level": load_level,
            "risk_is_conflict": not passed_hard_rules,
            "risk_has_substitution_capacity": staff_margin > 0,
            "preference_priority": preference_priority,
            "preference_has_preference": preference_has_preference,
            "preference_exact_period_match": preference_exact_period_match,
            "employee_tenure_days_at_year_end": 900,
            "employee_is_manager": False,
        }

    def _write_simple_model(self, directory, version):
        artifact = {
            "version": version,
            "kind": "tabular_mlp",
            "feature_schema_version": 1,
            "description": "Test v2 model.",
            "hidden_activation": "tanh",
            "output_activation": "sigmoid",
            "hidden_layer": [
                {
                    "name": name,
                    "bias": 0.1 if name == "coverage_balance" else 0.0,
                    "weights": {"coverage_fit": 0.8, "risk_pressure": -0.2},
                }
                for name in HIDDEN_NODE_NAMES
            ],
            "heads": {
                head: {
                    "bias": 0.1,
                    "weights": {name: 0.2 for name in HIDDEN_NODE_NAMES},
                }
                for head in TARGET_HEADS
            },
        }
        path = Path(directory) / candidate_model_filename(version)
        path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")

    def _write_simple_package_model(self, directory, version):
        artifact = {
            "version": version,
            "kind": "package_tabular_mlp",
            "feature_schema_version": 1,
            "description": "Test v3 package model.",
            "hidden_activation": "tanh",
            "output_activation": "sigmoid",
            "hidden_layer": [
                {
                    "name": name,
                    "bias": 0.1 if name == "coverage_quality" else 0.0,
                    "weights": {"coverage_fit": 0.8, "risk_pressure": -0.2},
                }
                for name in PACKAGE_HIDDEN_NODE_NAMES
            ],
            "heads": {
                head: {
                    "bias": 0.1,
                    "weights": {name: 0.2 for name in PACKAGE_HIDDEN_NODE_NAMES},
                }
                for head in PACKAGE_TARGET_HEADS
            },
        }
        path = Path(directory) / package_model_filename(version)
        path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")


@contextmanager
def _yield_directory(path):
    yield str(path)
