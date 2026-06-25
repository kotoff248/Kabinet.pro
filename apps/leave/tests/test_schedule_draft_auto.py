import json
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone

from apps.accounts.services import sync_employee_user
from apps.core.models import Notification
from apps.employees.models import Employees
from apps.leave.models import (
    DepartmentWorkload,
    VacationPreference,
    VacationPreferenceCollection,
    VacationSchedule,
    VacationScheduleAutoPlaceJob,
    VacationScheduleCandidate,
    VacationScheduleCandidateFeedback,
    VacationScheduleCandidatePackage,
    VacationScheduleDepartmentApproval,
    VacationScheduleItem,
    VacationUrgentClosureRequest,
)
from apps.leave.services.dates import add_months_safe, get_chargeable_leave_days
from apps.leave.services.preferences import (
    build_preference_collection_summary,
    get_employee_preference_pair_map,
    get_employee_preference_state_map,
    preference_readiness_url,
)
from apps.leave.services.schedule_drafts.auto_place import (
    _optimized_auto_place_group_state,
    _should_repeat_auto_place_pass,
    auto_place_remaining_schedule_draft,
)
from apps.leave.services.schedule_drafts.candidate_generation import (
    _auto_target_day_options,
    _build_auto_generation_candidates,
    _build_draft_generation_context,
    _build_preference_generation_candidates,
    _candidate_start_dates,
)
from apps.leave.services.schedule_drafts.constants import (
    AUTO_DRAFT_MAX_AUTO_PLACE_PASSES,
    AUTO_DRAFT_MAX_CANDIDATES_PER_EMPLOYEE,
)
from apps.leave.services.schedule_drafts.manual import place_manual_schedule_draft_items
from apps.leave.services.schedule_drafts.manual_suggestions import build_schedule_draft_auto_place_preview
from apps.leave.services.schedule_drafts.page_context import build_manual_schedule_draft_preview
from apps.leave.services.schedule_drafts.planning_need import _build_employee_schedule_planning_need_from_rows
from apps.leave.services.schedule_drafts.types import DraftGenerationCandidate, DraftGenerationCandidatePackage, DraftGenerationContext, DraftPlacement
from apps.leave.services.schedule_planning import schedule_planning_url
from apps.leave.ml.scoring import ACTIVE_CANDIDATE_SCORER_VERSION
from apps.leave.tests.base import LeaveTestCase


class ScheduleDraftAutoTests(LeaveTestCase):
    def _planning_need(self, days):
        days = Decimal(str(days))
        return {
            "needs_manual_attention": True,
            "has_blocker": False,
            "nearest_deadline": None,
            "open_required_days": days,
            "blocking_days": Decimal("0.00"),
            "target_days": days,
            "manual_task_label": f"Добрать {int(days)} д.",
            "status": {"label": "Нужно добавить"},
        }

    def test_global_optimizer_keeps_better_combination_not_first_package(self):
        year = self._year()
        schedule = VacationSchedule.objects.create(
            year=year,
            status=VacationSchedule.STATUS_DRAFT,
            created_by=self.hr_employee,
        )
        second_employee = Employees.objects.create(
            first_name="Иван",
            last_name="Глобальный",
            middle_name="Тестовый",
            role=Employees.ROLE_EMPLOYEE,
            department=self.employee.department,
            employee_position=self.employee.employee_position,
            date_joined=date(year - 2, 1, 10),
            annual_paid_leave_days=52,
            is_active_employee=True,
        )
        context = DraftGenerationContext(
            year=year,
            schedule=schedule,
            eligible_employees=[self.employee, second_employee],
            draft_items_by_employee={},
            preference_pair_by_employee={},
            preference_state_by_employee={},
            placements=[],
            planning_need_by_employee={
                self.employee.id: self._planning_need(52),
                second_employee.id: self._planning_need(52),
            },
        )
        january_start = date(year, 1, 15)
        march_start = date(year, 3, 15)
        february_start = date(year, 2, 15)

        def package(employee, start_date, score):
            return DraftGenerationCandidatePackage(
                employee=employee,
                candidates=[
                    DraftGenerationCandidate(
                        employee=employee,
                        start_date=start_date,
                        end_date=start_date + timedelta(days=13),
                        kind=VacationScheduleCandidate.KIND_AUTO,
                        source=VacationScheduleItem.SOURCE_GENERATED,
                        comment="Тестовый пакет.",
                        metadata={
                            "passed_hard_rules": True,
                            "chargeable_days": Decimal("14.00"),
                            "risk_score": 0,
                            "risk_level": VacationScheduleItem.RISK_LOW,
                            "scoring_score": Decimal(str(score)),
                            "scoring_confidence": Decimal("90.00"),
                        },
                    )
                ],
                source=VacationScheduleItem.SOURCE_GENERATED,
                explanation="Тестовый пакет.",
                metadata={
                    "package_score": Decimal(str(score)),
                    "total_chargeable_days": Decimal("14.00"),
                    "remaining_after_package": Decimal("0.00"),
                    "package_closes_need": True,
                    "auto_place_target_days": Decimal("14.00"),
                },
            )

        first_high = package(self.employee, january_start, 80)
        first_lower = package(self.employee, march_start, 52)
        second_high = package(second_employee, january_start, 95)
        second_low = package(second_employee, february_start, 14)

        def fake_packages(context_arg, employee, *, package_limit=4):
            if employee.id == self.employee.id:
                return [first_high, first_lower], first_high
            january_taken = any(
                placement.start_date <= january_start <= placement.end_date
                for placement in context_arg.placements
            )
            if january_taken:
                return [second_low], second_low
            return [second_high], second_high

        def fake_register(context_arg, selected_package):
            for candidate in selected_package.candidates:
                context_arg.placements.append(
                    DraftPlacement(candidate.employee.id, candidate.start_date, candidate.end_date, None)
                )

        with patch(
            "apps.leave.services.schedule_drafts.auto_place._auto_candidate_packages_for_employee",
            side_effect=fake_packages,
        ), patch(
            "apps.leave.services.schedule_drafts.auto_place._register_virtual_candidate_package",
            side_effect=fake_register,
        ), patch(
            "apps.leave.services.schedule_drafts.auto_place._manual_package_quality_score",
            side_effect=lambda item: item.metadata["package_score"],
        ):
            state = _optimized_auto_place_group_state(context, [self.employee, second_employee])

        self.assertIsNotNone(state)
        selected_starts = [
            selection["selected_package"].candidates[0].start_date
            for selection in state.selections
        ]
        self.assertEqual(selected_starts, [march_start, january_start])

    def test_auto_target_day_options_include_extended_lengths(self):
        self.assertEqual(
            _auto_target_day_options(Decimal("58.00")),
            [58, 52, 42, 35, 31, 28, 24, 21, 18, 14, 10, 7],
        )

    def test_candidate_start_dates_include_dense_anchors(self):
        year = self._year()

        starts = _candidate_start_dates(
            year,
            self.employee,
            date(year, 1, 1),
            date(year, 12, 31),
            low_workload_months=set(),
        )

        for day in (4, 11, 18, 25, 28):
            self.assertIn(date(year, 1, day), starts)

    def test_candidate_start_dates_spread_early_choices_across_quarters(self):
        year = self._year()

        starts = _candidate_start_dates(
            year,
            self.employee,
            date(year, 1, 1),
            date(year, 12, 31),
            low_workload_months=set(),
        )

        early_quarters = {(value.month - 1) // 3 for value in starts[:4]}
        self.assertEqual(len(early_quarters), 4)

    def test_candidate_start_dates_include_preference_neighbors(self):
        year = self._year()
        preference = VacationPreference(
            employee=self.employee,
            year=year,
            priority=VacationPreference.PRIORITY_PRIMARY,
            start_date=date(year, 7, 10),
            end_date=date(year, 7, 23),
        )

        starts = _candidate_start_dates(
            year,
            self.employee,
            date(year, 1, 1),
            date(year, 12, 31),
            low_workload_months=set(),
            preference_pair={VacationPreference.PRIORITY_PRIMARY: preference},
        )

        self.assertIn(date(year, 7, 3), starts)
        self.assertIn(date(year, 7, 10), starts)
        self.assertIn(date(year, 7, 17), starts)

    def test_candidate_start_dates_prioritize_low_workload_months(self):
        year = self._year()
        DepartmentWorkload.objects.create(
            department=self.employee.department,
            year=year,
            month=3,
            load_level=1,
            max_absent=3,
        )

        starts = _candidate_start_dates(
            year,
            self.employee,
            date(year, 1, 1),
            date(year, 12, 31),
        )

        self.assertEqual(starts[0].month, 3)

    def test_auto_place_repeats_after_conflict_cleanup(self):
        self.assertTrue(
            _should_repeat_auto_place_pass(
                placed_count=0,
                removed_conflicts=2,
                unresolved_count=1,
                pass_index=1,
            )
        )
        self.assertFalse(
            _should_repeat_auto_place_pass(
                placed_count=0,
                removed_conflicts=2,
                unresolved_count=1,
                pass_index=AUTO_DRAFT_MAX_AUTO_PLACE_PASSES,
            )
        )
        self.assertTrue(
            _should_repeat_auto_place_pass(
                placed_count=0,
                removed_conflicts=0,
                unresolved_count=1,
                pass_index=1,
                has_placeable_remainder=True,
            )
        )

    def test_schedule_draft_prepares_multiple_auto_generation_candidates(self):
        year = self._year()
        self.activate_only(self.employee)
        self.employee.date_joined = date(year - 1, 7, 1)
        self.employee.annual_paid_leave_days = 52
        self.employee.save(update_fields=["date_joined", "annual_paid_leave_days"])
        schedule = VacationSchedule.objects.create(
            year=year,
            status=VacationSchedule.STATUS_DRAFT,
            created_by=self.hr_employee,
        )
        context = _build_draft_generation_context(year, schedule)
        planning_need = context.planning_need_by_employee[self.employee.id]

        candidates = _build_auto_generation_candidates(
            context,
            self.employee,
            context.draft_items_by_employee.get(self.employee.id, []),
            planning_need,
        )

        self.assertGreater(len(candidates), 1)
        self.assertLessEqual(len(candidates), AUTO_DRAFT_MAX_CANDIDATES_PER_EMPLOYEE)
        self.assertEqual(len({(candidate.start_date, candidate.end_date) for candidate in candidates}), len(candidates))
        self.assertTrue(all(candidate.kind == "auto" for candidate in candidates))
        self.assertTrue(all(candidate.assessment["can_place"] for candidate in candidates))
        self.assertTrue(all(candidate.metadata["passed_hard_rules"] for candidate in candidates))
        self.assertTrue(all("block_reason_key" in candidate.metadata for candidate in candidates))

    def test_auto_place_preview_does_not_persist_records(self):
        year = self._year()
        self.activate_only(self.employee)
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 6, 1),
            primary_end=date(year, 6, 14),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 14),
        )
        schedule = self.create_minimal_draft(year=year)
        self.create_employee_draft_item(
            self.employee,
            schedule=schedule,
            start_date=date(year, 6, 1),
            end_date=date(year, 6, 14),
        )
        self.client.force_login(self.hr_employee.user)
        before_items = VacationScheduleItem.objects.filter(schedule=schedule).count()
        before_candidates = VacationScheduleCandidate.objects.filter(schedule=schedule).count()

        response = self.client.get(reverse("schedule_draft_auto_place_preview", args=[year]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertGreater(payload["placed_count"], 0)
        self.assertGreater(len(payload["options"]), 0)
        self.assertIn("calculation_note", payload["options"][0])
        self.assertIn("Осталось", payload["options"][0]["calculation_note"])
        self.assertIn("day_calculation", payload["options"][0])
        self.assertEqual(VacationScheduleItem.objects.filter(schedule=schedule).count(), before_items)
        self.assertEqual(VacationScheduleCandidate.objects.filter(schedule=schedule).count(), before_candidates)

    def test_auto_place_preview_and_confirm_prioritize_backup_preference_for_remaining_days(self):
        year = self._year()
        self.activate_only(self.employee)
        self.employee.date_joined = date(year - 1, 1, 1)
        self.employee.save(update_fields=["date_joined"])
        primary_start = date(year, 6, 1)
        primary_end = date(year, 6, 14)
        backup_start = date(year, 9, 1)
        backup_end = date(year, 9, 14)
        self._set_filled_preferences(
            self.employee,
            primary_start=primary_start,
            primary_end=primary_end,
            backup_start=backup_start,
            backup_end=backup_end,
        )
        schedule = self.create_minimal_draft(year=year)
        self.create_employee_draft_item(
            self.employee,
            schedule=schedule,
            start_date=primary_start,
            end_date=primary_end,
        )

        preview = build_schedule_draft_auto_place_preview(year=year, limit=3)

        self.assertGreater(preview["placed_count"], 0)
        first_option = preview["options"][0]
        self.assertTrue(first_option["is_preference_candidate"])
        self.assertEqual(first_option["kind"], "manual_package")
        self.assertEqual(first_option["preference_match"], "backup")
        self.assertEqual(first_option["periods"][0]["kind"], VacationScheduleCandidate.KIND_BACKUP_PREFERENCE)
        self.assertEqual(first_option["periods"][0]["start_date"], backup_start.isoformat())
        self.assertEqual(first_option["periods"][0]["end_date"], backup_end.isoformat())
        self.assertFalse(VacationScheduleCandidate.objects.filter(schedule=schedule).exists())

        result = auto_place_remaining_schedule_draft(year=year, actor=self.hr_employee, use_package_selection=True)

        self.assertGreater(result["placed_count"], 0)
        self.assertTrue(
            VacationScheduleItem.objects.filter(
                schedule=schedule,
                employee=self.employee,
                start_date=backup_start,
                end_date=backup_end,
                source=VacationScheduleItem.SOURCE_GENERATED,
            ).exists()
        )
        selected_backup = VacationScheduleCandidate.objects.filter(
            schedule=schedule,
            employee=self.employee,
            kind=VacationScheduleCandidate.KIND_BACKUP_PREFERENCE,
            decision=VacationScheduleCandidate.DECISION_SELECTED,
        ).get()
        self.assertTrue(selected_backup.features["auto_place_preference_seed"])
        self.assertTrue(
            VacationScheduleCandidatePackage.objects.filter(
                schedule=schedule,
                employee=self.employee,
                decision=VacationScheduleCandidatePackage.DECISION_SELECTED,
            ).exists()
        )

    def test_hr_auto_places_remaining_schedule_draft_items(self):
        year = self._year()
        self.activate_only(self.employee)
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 6, 1),
            primary_end=date(year, 6, 14),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 14),
        )
        self.finish_preference_collection(year)
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_draft_create", args=[year]))
        schedule = VacationSchedule.objects.get(year=year)
        initial_cache_version = schedule.manual_suggestion_cache_version
        before_count = VacationScheduleItem.objects.filter(schedule=schedule).count()
        draft_response = self.client.get(reverse("schedule_draft_detail", args=[year]))
        self.assertContains(draft_response, "Добрать незакрытые дни", status_code=200)
        self.assertContains(draft_response, "data-draft-auto-open")
        self.assertContains(draft_response, "data-auto-plan-days")
        self.assertNotContains(draft_response, "data-auto-preview-url")
        self.assertContains(draft_response, "schedule-draft-auto-modal")
        self.assertContains(draft_response, "data-draft-manual-open")
        self.assertNotContains(draft_response, "data-draft-suggestions-open")
        self.assertContains(draft_response, "data-manual-package-preview-url")
        self.assertContains(draft_response, "data-manual-calculation-url")
        self.assertContains(draft_response, "schedule-draft-placement-form")
        self.assertNotContains(draft_response, "schedule-draft-manual-form")

        with patch("apps.leave.views.schedule_draft.start_schedule_auto_place_process") as mocked_start:
            response = self.client.post(
                reverse("schedule_draft_auto_place", args=[year]),
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
                HTTP_ACCEPT="application/json",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], VacationScheduleAutoPlaceJob.STATUS_QUEUED)
        self.assertIn("status_url", payload)
        job = VacationScheduleAutoPlaceJob.objects.get(id=payload["job_id"])
        self.assertEqual(job.year, year)
        self.assertEqual(job.actor, self.hr_employee)
        self.assertEqual(job.schedule, schedule)
        self.assertEqual(mocked_start.call_count, 1)
        self.assertEqual(mocked_start.call_args.args[0].id, job.id)

        with patch("apps.leave.views.schedule_draft.start_schedule_auto_place_process") as mocked_second_start:
            second_response = self.client.post(
                reverse("schedule_draft_auto_place", args=[year]),
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
                HTTP_ACCEPT="application/json",
            )
        self.assertEqual(second_response.status_code, 200)
        second_payload = second_response.json()
        self.assertEqual(second_payload["job_id"], job.id)
        self.assertIn("уже выполняется", second_payload["message"])
        mocked_second_start.assert_not_called()
        self.assertEqual(
            VacationScheduleAutoPlaceJob.objects.filter(
                year=year,
                status__in=[
                    VacationScheduleAutoPlaceJob.STATUS_QUEUED,
                    VacationScheduleAutoPlaceJob.STATUS_RUNNING,
                ],
            ).count(),
            1,
        )

        status_response = self.client.get(payload["status_url"], HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["job_id"], job.id)
        invalid_status_response = self.client.get(
            reverse("schedule_draft_auto_place_status", args=[year, job.id]) + "?token=wrong",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(invalid_status_response.status_code, 403)

        schedule.refresh_from_db()
        self.assertEqual(schedule.manual_suggestion_cache_version, initial_cache_version)
        after_count = VacationScheduleItem.objects.filter(schedule=schedule).count()
        self.assertEqual(after_count, before_count)

    def test_schedule_draft_detail_shows_active_auto_place_job_without_normalizing(self):
        year = self._year()
        schedule = self.create_minimal_draft(year=year)
        job = VacationScheduleAutoPlaceJob.objects.create(
            token="draft-page-auto-token",
            year=year,
            schedule=schedule,
            actor=self.hr_employee,
            status=VacationScheduleAutoPlaceJob.STATUS_RUNNING,
            progress_percent=44,
            stage_label="Добрать незакрытые дни: 4 из 10",
            message="Подбираю лучший пакет.",
            processed_employees=4,
            total_employees=10,
            placed_count=6,
            unresolved_count=2,
        )
        self.client.force_login(self.hr_employee.user)

        with patch("apps.leave.services.schedule_drafts.normalization.normalize_schedule_draft_adjacent_items") as normalize_mock:
            response = self.client.get(reverse("schedule_draft_detail", args=[year]))

        self.assertEqual(response.status_code, 200)
        normalize_mock.assert_not_called()
        self.assertEqual(response.context["draft_auto_place_job"]["job_id"], job.id)
        self.assertEqual(response.context["draft_auto_place_job"]["progress_percent"], 44)
        self.assertContains(response, "data-draft-auto-job")
        self.assertContains(response, "draft-page-auto-token")
        self.assertContains(response, "Добрать незакрытые дни: 4 из 10")
        self.assertContains(response, "4 / 10")
        self.assertContains(response, "6")
        self.assertContains(response, "2")

    def test_schedule_draft_detail_normalizes_when_no_active_auto_place_job(self):
        year = self._year()
        self.create_minimal_draft(year=year)
        self.client.force_login(self.hr_employee.user)

        with patch("apps.leave.services.schedule_drafts.normalization.normalize_schedule_draft_adjacent_items", return_value=0) as normalize_mock:
            response = self.client.get(reverse("schedule_draft_detail", args=[year]))

        self.assertEqual(response.status_code, 200)
        normalize_mock.assert_called_once_with(year)
        self.assertIsNone(response.context["draft_auto_place_job"])
        self.assertNotContains(response, "data-draft-auto-job")

    def test_enterprise_head_can_read_auto_place_status_with_token(self):
        year = self._year()
        schedule = self.create_minimal_draft(year=year)
        job = VacationScheduleAutoPlaceJob.objects.create(
            token="draft-page-status-token",
            year=year,
            schedule=schedule,
            actor=self.hr_employee,
            status=VacationScheduleAutoPlaceJob.STATUS_RUNNING,
            progress_percent=33,
        )
        self.client.force_login(self.enterprise_head.user)

        response = self.client.get(
            reverse("schedule_draft_auto_place_status", args=[year, job.id]) + "?token=draft-page-status-token",
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["progress_percent"], 33)

    def test_auto_place_prefers_whole_long_leave_before_splitting(self):
        year = self._year()
        self.activate_only(self.employee)
        self.employee.date_joined = date(year - 1, 1, 1)
        self.employee.annual_paid_leave_days = 52
        self.employee.save(update_fields=["date_joined", "annual_paid_leave_days"])
        VacationPreference.objects.filter(employee=self.employee, year=year).delete()
        VacationPreference.objects.create(
            employee=self.employee,
            year=year,
            priority=VacationPreference.PRIORITY_PRIMARY,
            start_date=date(year, 6, 1),
            end_date=date(year, 7, 23),
            status=VacationPreference.STATUS_FILLED,
            remainder_policy=VacationPreference.REMAINDER_AUTO,
        )
        VacationPreference.objects.create(
            employee=self.employee,
            year=year,
            priority=VacationPreference.PRIORITY_BACKUP,
            start_date=date(year, 8, 1),
            end_date=date(year, 9, 22),
            status=VacationPreference.STATUS_FILLED,
            remainder_policy=VacationPreference.REMAINDER_AUTO,
        )
        schedule = VacationSchedule.objects.create(
            year=year,
            status=VacationSchedule.STATUS_DRAFT,
            created_by=self.hr_employee,
        )

        result = auto_place_remaining_schedule_draft(year=year, actor=self.hr_employee, use_package_selection=True)

        items = list(VacationScheduleItem.objects.filter(schedule=schedule, employee=self.employee))
        self.assertGreater(result["placed_count"], 0)
        self.assertTrue(any(item.chargeable_days >= Decimal("52.00") for item in items))
        self.assertFalse(any(item.chargeable_days == Decimal("28.00") for item in items))

    def test_auto_place_keeps_annual_plan_when_previous_year_closure_is_needed(self):
        year = self._year()
        self.activate_only(self.employee)
        self.employee.date_joined = date(year - 2, 1, 4)
        self.employee.annual_paid_leave_days = 52
        self.employee.save(update_fields=["date_joined", "annual_paid_leave_days"])
        VacationPreference.objects.filter(employee=self.employee, year=year).delete()
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 6, 1),
            primary_end=date(year, 6, 14),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 14),
            remainder_policy=VacationPreference.REMAINDER_AUTO,
        )
        schedule = VacationSchedule.objects.create(
            year=year,
            status=VacationSchedule.STATUS_DRAFT,
            created_by=self.hr_employee,
        )

        result = auto_place_remaining_schedule_draft(year=year, actor=self.hr_employee, use_package_selection=True)

        items = list(VacationScheduleItem.objects.filter(schedule=schedule, employee=self.employee))
        total_chargeable_days = sum((item.chargeable_days for item in items), Decimal("0.00"))
        self.assertGreater(result["placed_count"], 0)
        self.assertGreaterEqual(total_chargeable_days, Decimal("52.00"))

        self.client.force_login(self.hr_employee.user)
        response = self.client.get(reverse("schedule_draft_detail", args=[year]))
        planning_need = response.context["planning_need_by_employee"][self.employee.id]
        self.assertTrue(planning_need["has_blocker"])
        self.assertEqual(planning_need["blocking_days"], Decimal("52.00"))
        self.assertEqual(planning_need["open_required_days"], Decimal("52.00"))

    def test_auto_place_extends_adjacent_short_topup_instead_of_leaving_tail(self):
        year = self._year()
        self.activate_only(self.employee)
        self.employee.date_joined = date(year - 1, 7, 1)
        self.employee.annual_paid_leave_days = 52
        self.employee.save(update_fields=["date_joined", "annual_paid_leave_days"])
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 3, 1),
            primary_end=self._paid_period_for_chargeable_days(date(year, 3, 1), 52),
            backup_start=date(year, 9, 1),
            backup_end=self._paid_period_for_chargeable_days(date(year, 9, 1), 52),
            remainder_policy=VacationPreference.REMAINDER_AUTO,
        )
        schedule = VacationSchedule.objects.create(
            year=year,
            status=VacationSchedule.STATUS_DRAFT,
            created_by=self.hr_employee,
        )
        start_date = date(year, 3, 1)
        end_date = self._paid_period_for_chargeable_days(start_date, 51)
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=start_date,
            end_date=end_date,
            vacation_type="paid",
            chargeable_days=Decimal("51.00"),
            status=VacationScheduleItem.STATUS_DRAFT,
            source=VacationScheduleItem.SOURCE_GENERATED,
            risk_score=0,
            risk_level=VacationScheduleItem.RISK_LOW,
        )

        result = auto_place_remaining_schedule_draft(year=year, actor=self.hr_employee, use_package_selection=True)

        self.assertEqual(result["placed_count"], 1)
        self.assertEqual(VacationScheduleItem.objects.filter(schedule=schedule, employee=self.employee).count(), 1)
        item = VacationScheduleItem.objects.get(schedule=schedule, employee=self.employee)
        self.assertEqual(item.chargeable_days, Decimal("52.00"))
        self.assertGreater(item.end_date, end_date)

    def test_remainder_policy_approval_blocks_automatic_extra_days(self):
        year = self._year()
        self.activate_only(self.employee)
        self.employee.date_joined = date(year - 1, 7, 1)
        self.employee.annual_paid_leave_days = 52
        self.employee.save(update_fields=["date_joined", "annual_paid_leave_days"])
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 6, 1),
            primary_end=date(year, 6, 21),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 21),
            remainder_policy=VacationPreference.REMAINDER_APPROVAL,
        )
        self.finish_preference_collection(year)
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_draft_create", args=[year]))
        schedule = VacationSchedule.objects.get(year=year)
        before_count = VacationScheduleItem.objects.filter(schedule=schedule, employee=self.employee).count()

        result = auto_place_remaining_schedule_draft(year=year, actor=self.hr_employee, use_package_selection=True)

        self.assertEqual(result["placed_count"], 0)
        self.assertEqual(VacationScheduleItem.objects.filter(schedule=schedule, employee=self.employee).count(), before_count)
        response = self.client.get(reverse("schedule_draft_detail", args=[year]))
        planning_need = response.context["planning_need_by_employee"][self.employee.id]
        self.assertFalse(planning_need["needs_manual_attention"])
        self.assertGreater(planning_need["remainder_approval_days"], Decimal("0.00"))

    def test_remainder_policy_defer_does_not_require_annual_auto_topup(self):
        year = self._year()
        self.activate_only(self.employee)
        self.employee.date_joined = date(year - 1, 7, 1)
        self.employee.annual_paid_leave_days = 52
        self.employee.save(update_fields=["date_joined", "annual_paid_leave_days"])
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 6, 1),
            primary_end=date(year, 6, 21),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 21),
            remainder_policy=VacationPreference.REMAINDER_DEFER,
        )
        self.finish_preference_collection(year)
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_draft_create", args=[year]))
        result = auto_place_remaining_schedule_draft(year=year, actor=self.hr_employee, use_package_selection=True)

        self.assertEqual(result["placed_count"], 0)
        response = self.client.get(reverse("schedule_draft_detail", args=[year]))
        planning_need = response.context["planning_need_by_employee"][self.employee.id]
        self.assertFalse(planning_need["needs_manual_attention"])
        self.assertGreater(planning_need["employee_deferred_days"], Decimal("0.00"))
