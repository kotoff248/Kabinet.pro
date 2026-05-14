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
    VacationScheduleCandidate,
    VacationScheduleCandidateFeedback,
    VacationScheduleCandidatePackage,
    VacationScheduleCandidatePackagePeriod,
    VacationScheduleGenerationRun,
    VacationScheduleDepartmentApproval,
    VacationScheduleItem,
    VacationScheduleManualSuggestionCache,
    VacationUrgentClosureRequest,
)
from apps.leave.services.dates import add_months_safe, get_chargeable_leave_days
from apps.leave.services.preferences import (
    build_preference_collection_summary,
    get_employee_preference_pair_map,
    get_employee_preference_state_map,
    preference_readiness_url,
)
from apps.leave.services.schedule_drafts import (
    DraftGenerationCandidate,
    DraftGenerationCandidatePackage,
    _build_employee_schedule_planning_need_from_rows,
    _build_auto_generation_candidates,
    _build_draft_generation_context,
    _build_preference_generation_candidates,
    _manual_package_payload,
    _rank_manual_candidate_packages,
    auto_place_remaining_schedule_draft,
    build_manual_schedule_draft_preview,
    build_schedule_draft_auto_place_preview,
    place_manual_schedule_draft_items,
)
from apps.leave.services.schedule_planning import schedule_planning_url
from apps.leave.services.candidate_scoring import ACTIVE_CANDIDATE_SCORER_VERSION
from apps.leave.tests.base import LeaveTestCase


class ScheduleDraftManualTests(LeaveTestCase):
    def test_manual_suggestions_preview_does_not_persist_records(self):
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
        self.warm_manual_suggestion_cache(year=year, employee=self.employee)
        schedule.refresh_from_db()
        self.client.force_login(self.hr_employee.user)
        cached_suggestions = VacationScheduleManualSuggestionCache.objects.filter(schedule=schedule, employee=self.employee)
        self.assertTrue(cached_suggestions.exists())
        before_items = VacationScheduleItem.objects.filter(schedule=schedule).count()
        before_candidates = VacationScheduleCandidate.objects.filter(schedule=schedule).count()
        before_packages = VacationScheduleCandidatePackage.objects.filter(schedule=schedule).count()
        before_cache_count = VacationScheduleManualSuggestionCache.objects.filter(schedule=schedule).count()

        response = self.client.get(reverse("schedule_draft_manual_suggestions", args=[year, self.employee.id]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["from_cache"])
        self.assertEqual(payload["cache_version"], schedule.manual_suggestion_cache_version)
        self.assertTrue(payload["db_items_unchanged"])
        self.assertGreater(len(payload["options"]), 0)
        self.assertIn("periods", payload["options"][0])
        self.assertEqual(VacationScheduleItem.objects.filter(schedule=schedule).count(), before_items)
        self.assertEqual(VacationScheduleCandidate.objects.filter(schedule=schedule).count(), before_candidates)
        self.assertEqual(VacationScheduleCandidatePackage.objects.filter(schedule=schedule).count(), before_packages)
        self.assertEqual(VacationScheduleManualSuggestionCache.objects.filter(schedule=schedule).count(), before_cache_count)

    def test_manual_suggestions_prioritize_safe_backup_preference_after_primary_placed(self):
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
        self.warm_manual_suggestion_cache(year=year, employee=self.employee)
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("schedule_draft_manual_suggestions", args=[year, self.employee.id]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["preference_option"]["can_apply"])
        self.assertEqual(payload["preference_option"]["preference_match"], "backup")
        first_option = payload["options"][0]
        self.assertTrue(first_option["is_preference_candidate"])
        self.assertEqual(first_option["preference_match"], "backup")
        self.assertEqual(first_option["periods"][0]["start_date"], date(year, 9, 1).isoformat())
        self.assertEqual(first_option["periods"][0]["end_date"], date(year, 9, 14).isoformat())

    def test_manual_suggestions_offer_partial_backup_when_remaining_is_smaller_than_backup(self):
        year = self._year()
        self.employee.date_joined = date(year - 1, 1, 1)
        self.employee.save(update_fields=["date_joined"])
        primary_start = date(year, 3, 1)
        primary_end = self._paid_period_for_chargeable_days(primary_start, 45)
        placed_end = self._paid_period_for_chargeable_days(primary_start, 97)
        backup_start = date(year, 9, 1)
        backup_end = date(year, 9, 14)
        expected_partial_end = self._paid_period_for_chargeable_days(backup_start, 7)
        self._set_filled_preferences(
            self.employee,
            primary_start=primary_start,
            primary_end=primary_end,
            backup_start=backup_start,
            backup_end=backup_end,
        )
        schedule = VacationSchedule.objects.create(
            year=year,
            status=VacationSchedule.STATUS_DRAFT,
            created_by=self.hr_employee,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=primary_start,
            end_date=placed_end,
            vacation_type="paid",
            chargeable_days=Decimal("97.00"),
            status=VacationScheduleItem.STATUS_DRAFT,
            source=VacationScheduleItem.SOURCE_GENERATED,
            risk_score=0,
            risk_level=VacationScheduleItem.RISK_LOW,
        )
        self.client.force_login(self.hr_employee.user)
        self.assertFalse(VacationScheduleManualSuggestionCache.objects.filter(schedule=schedule).exists())

        response = self.client.get(reverse("schedule_draft_manual_suggestions", args=[year, self.employee.id]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["from_cache"])
        self.assertTrue(payload["preference_option"]["can_apply"])
        self.assertEqual(payload["preference_option"]["preference_match"], "backup_partial")
        self.assertEqual(payload["preference_option"]["start_date"], backup_start.isoformat())
        self.assertEqual(payload["preference_option"]["end_date"], expected_partial_end.isoformat())
        self.assertEqual(payload["options"][0]["preference_match"], "backup_partial")
        self.assertTrue(VacationScheduleManualSuggestionCache.objects.filter(schedule=schedule, employee=self.employee).exists())

    def test_manual_suggestions_show_blocked_backup_preference_status(self):
        year = self._year()
        self.employee.date_joined = date(year - 1, 1, 1)
        self.employee.save(update_fields=["date_joined"])
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 3, 1),
            primary_end=date(year, 3, 14),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 14),
        )
        schedule = VacationSchedule.objects.create(
            year=year,
            status=VacationSchedule.STATUS_DRAFT,
            created_by=self.hr_employee,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(year, 3, 1),
            end_date=date(year, 3, 14),
            vacation_type="paid",
            chargeable_days=get_chargeable_leave_days(date(year, 3, 1), date(year, 3, 14), "paid"),
            status=VacationScheduleItem.STATUS_DRAFT,
            source=VacationScheduleItem.SOURCE_GENERATED,
            risk_score=0,
            risk_level=VacationScheduleItem.RISK_LOW,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(year, 9, 1),
            end_date=date(year, 9, 5),
            vacation_type="paid",
            chargeable_days=get_chargeable_leave_days(date(year, 9, 1), date(year, 9, 5), "paid"),
            status=VacationScheduleItem.STATUS_DRAFT,
            source=VacationScheduleItem.SOURCE_GENERATED,
            risk_score=0,
            risk_level=VacationScheduleItem.RISK_LOW,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("schedule_draft_manual_suggestions", args=[year, self.employee.id]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["preference_option"]["can_apply"])
        self.assertEqual(payload["preference_option"]["preference_match"], "backup")
        self.assertEqual(payload["preference_option"]["status_label"], "Не подходит")
        self.assertTrue(payload["preference_option"]["reason"])

    def test_manual_package_ranking_compares_whole_package_quality(self):
        year = self._year()

        def candidate(start_date, days, score, risk_score=20):
            return DraftGenerationCandidate(
                employee=self.employee,
                start_date=start_date,
                end_date=start_date + timedelta(days=int(days) - 1),
                kind=VacationScheduleCandidate.KIND_AUTO,
                source=VacationScheduleItem.SOURCE_GENERATED,
                comment="Тестовый вариант.",
                metadata={
                    "passed_hard_rules": True,
                    "chargeable_days": Decimal(str(days)),
                    "open_required_days": Decimal("58.00"),
                    "risk_score": risk_score,
                    "risk_level": VacationScheduleItem.RISK_LOW,
                    "scoring_score": Decimal(str(score)),
                    "scoring_confidence": Decimal("80.00"),
                },
            )

        one_period = DraftGenerationCandidatePackage(
            employee=self.employee,
            candidates=[candidate(date(year, 2, 8), 58, "71.32", 27)],
            source=VacationScheduleItem.SOURCE_GENERATED,
            explanation="Один период закрывает потребность.",
            metadata={
                "total_chargeable_days": Decimal("58.00"),
                "remaining_after_package": Decimal("0.00"),
                "package_closes_need": True,
            },
        )
        three_periods = DraftGenerationCandidatePackage(
            employee=self.employee,
            candidates=[
                candidate(date(year, 1, 15), 21, "69.07", 17),
                candidate(date(year, 11, 8), 28, "68.13", 32),
                candidate(date(year, 2, 22), 9, "75.97", 22),
            ],
            source=VacationScheduleItem.SOURCE_GENERATED,
            explanation="Три периода закрывают потребность.",
            metadata={
                "total_chargeable_days": Decimal("58.00"),
                "remaining_after_package": Decimal("0.00"),
                "package_closes_need": True,
            },
        )

        with patch("apps.leave.services.schedule_drafts._apply_candidate_scoring", side_effect=lambda item: item):
            ranked = _rank_manual_candidate_packages([three_periods, one_period])
            first_payload = _manual_package_payload(ranked[0], rank=1)
            second_payload = _manual_package_payload(ranked[1], rank=2)

        self.assertIs(ranked[0], one_period)
        self.assertEqual(first_payload["periods_count"], 1)
        self.assertEqual(second_payload["periods_count"], 3)
        self.assertGreater(first_payload["score"], second_payload["score"])

    def test_manual_preview_rejects_late_period_that_does_not_close_deadline_blocker(self):
        year = self._year()
        Employees.objects.exclude(id=self.employee.id).update(is_active_employee=False)
        self.employee.date_joined = date(year - 2, 1, 11)
        self.employee.annual_paid_leave_days = 52
        self.employee.save(update_fields=["date_joined", "annual_paid_leave_days"])
        VacationSchedule.objects.create(
            year=year,
            status=VacationSchedule.STATUS_DRAFT,
            created_by=self.hr_employee,
        )

        preview = build_manual_schedule_draft_preview(
            year=year,
            employee_id=self.employee.id,
            start_date=date(year, 6, 1),
            end_date=date(year, 6, 14),
        )

        self.assertFalse(preview["can_submit"])
        self.assertIn("не закрывает срочный остаток", preview["message"])
        self.assertGreater(preview["blocking_after_placement"], Decimal("0.00"))

    def test_hr_can_manually_place_schedule_draft_item(self):
        year = self._year()
        self.employee.date_joined = date(year - 1, 7, 1)
        self.employee.save(update_fields=["date_joined"])
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 2, 3),
            primary_end=date(year, 3, 2),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 28),
            remainder_policy=VacationPreference.REMAINDER_DEFER,
        )
        VacationSchedule.objects.create(year=year, status=VacationSchedule.STATUS_DRAFT, created_by=self.hr_employee)
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(
            reverse("schedule_draft_manual_place", args=[year, self.employee.id]),
            {
                "start_date": date(year, 2, 3).isoformat(),
                "end_date": date(year, 2, 16).isoformat(),
            },
        )

        self.assertRedirects(response, reverse("schedule_draft_detail", args=[year]))
        item = VacationScheduleItem.objects.get(schedule__year=year, employee=self.employee)
        self.assertEqual(item.source, VacationScheduleItem.SOURCE_MANUAL)
        self.assertTrue(item.was_changed_by_manager)
        self.assertEqual(item.start_date, date(year, 2, 3))

    def test_manual_placement_invalidates_manual_suggestion_cache(self):
        year = self._year()
        self.employee.date_joined = date(year - 1, 1, 1)
        self.department_head.date_joined = date(year - 1, 1, 1)
        self.employee.save(update_fields=["date_joined"])
        self.department_head.save(update_fields=["date_joined"])
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 3, 1),
            primary_end=date(year, 3, 14),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 14),
        )
        self._set_filled_preferences(
            self.department_head,
            primary_start=date(year, 4, 1),
            primary_end=date(year, 4, 14),
            backup_start=date(year, 10, 1),
            backup_end=date(year, 10, 14),
        )
        schedule = VacationSchedule.objects.create(year=year, status=VacationSchedule.STATUS_DRAFT, created_by=self.hr_employee)
        for employee, start_date in ((self.employee, date(year, 1, 10)), (self.department_head, date(year, 1, 12))):
            end_date = self._paid_period_for_chargeable_days(start_date, 97)
            VacationScheduleItem.objects.create(
                schedule=schedule,
                employee=employee,
                start_date=start_date,
                end_date=end_date,
                vacation_type="paid",
                chargeable_days=Decimal("97.00"),
                status=VacationScheduleItem.STATUS_DRAFT,
                source=VacationScheduleItem.SOURCE_GENERATED,
                risk_score=0,
                risk_level=VacationScheduleItem.RISK_LOW,
            )
        self.client.force_login(self.hr_employee.user)
        self.client.get(reverse("schedule_draft_manual_suggestions", args=[year, self.employee.id]))
        schedule.refresh_from_db()
        initial_version = schedule.manual_suggestion_cache_version
        self.assertTrue(
            VacationScheduleManualSuggestionCache.objects.filter(
                schedule=schedule,
                employee=self.employee,
                version=initial_version,
            ).exists()
        )
        self.assertFalse(
            VacationScheduleManualSuggestionCache.objects.filter(
                schedule=schedule,
                employee=self.department_head,
                version=initial_version,
            ).exists()
        )
        start_date = date(year, 9, 1)
        periods = [
            {
                "start_date": start_date,
                "end_date": self._paid_period_for_chargeable_days(start_date, 7),
            }
        ]

        place_manual_schedule_draft_items(
            year=year,
            employee_id=self.employee.id,
            periods=periods,
            actor=self.hr_employee,
        )

        schedule.refresh_from_db()
        self.assertGreater(schedule.manual_suggestion_cache_version, initial_version)
        self.assertFalse(VacationScheduleManualSuggestionCache.objects.filter(schedule=schedule).exists())

    def test_manual_package_preview_does_not_persist_records(self):
        year = self._year()
        self.employee.date_joined = date(year - 1, 7, 1)
        self.employee.save(update_fields=["date_joined"])
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 2, 3),
            primary_end=date(year, 3, 2),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 28),
            remainder_policy=VacationPreference.REMAINDER_DEFER,
        )
        schedule = VacationSchedule.objects.create(year=year, status=VacationSchedule.STATUS_DRAFT, created_by=self.hr_employee)
        self.client.force_login(self.hr_employee.user)
        before_items = VacationScheduleItem.objects.filter(schedule=schedule).count()
        before_candidates = VacationScheduleCandidate.objects.filter(schedule=schedule).count()
        before_packages = VacationScheduleCandidatePackage.objects.filter(schedule=schedule).count()

        response = self.client.post(
            reverse("schedule_draft_manual_package_preview", args=[year, self.employee.id]),
            data=json.dumps(
                {
                    "periods": [
                        {"start_date": date(year, 2, 3).isoformat(), "end_date": date(year, 2, 16).isoformat()},
                        {"start_date": date(year, 4, 1).isoformat(), "end_date": date(year, 4, 13).isoformat()},
                    ]
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["can_submit"])
        self.assertEqual(len(payload["periods"]), 2)
        self.assertEqual(VacationScheduleItem.objects.filter(schedule=schedule).count(), before_items)
        self.assertEqual(VacationScheduleCandidate.objects.filter(schedule=schedule).count(), before_candidates)
        self.assertEqual(VacationScheduleCandidatePackage.objects.filter(schedule=schedule).count(), before_packages)

    def test_manual_package_preview_rejects_invalid_periods(self):
        year = self._year()
        self.employee.date_joined = date(year - 1, 7, 1)
        self.employee.save(update_fields=["date_joined"])
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 2, 3),
            primary_end=date(year, 3, 2),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 28),
            remainder_policy=VacationPreference.REMAINDER_DEFER,
        )
        VacationSchedule.objects.create(year=year, status=VacationSchedule.STATUS_DRAFT, created_by=self.hr_employee)
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(
            reverse("schedule_draft_manual_package_preview", args=[year, self.employee.id]),
            data=json.dumps(
                {
                    "periods": [
                        {"start_date": date(year, 2, 3).isoformat(), "end_date": date(year, 2, 16).isoformat()},
                        {"start_date": date(year, 2, 10).isoformat(), "end_date": date(year, 2, 20).isoformat()},
                    ]
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("не должны пересекаться", response.json()["message"])

    def test_manual_package_preview_requires_one_continuous_paid_part_of_fourteen_days(self):
        year = self._year()
        self.employee.date_joined = date(year - 1, 7, 1)
        self.employee.save(update_fields=["date_joined"])
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 2, 3),
            primary_end=date(year, 3, 2),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 28),
            remainder_policy=VacationPreference.REMAINDER_DEFER,
        )
        VacationSchedule.objects.create(year=year, status=VacationSchedule.STATUS_DRAFT, created_by=self.hr_employee)
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(
            reverse("schedule_draft_manual_package_preview", args=[year, self.employee.id]),
            data=json.dumps(
                {
                    "periods": [
                        {"start_date": date(year, 2, 3).isoformat(), "end_date": date(year, 2, 9).isoformat()},
                        {"start_date": date(year, 4, 1).isoformat(), "end_date": date(year, 4, 7).isoformat()},
                    ]
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["can_submit"])
        self.assertIn("не меньше 14 дней", payload["message"])

    def test_manual_package_preview_allows_short_part_when_long_part_already_exists(self):
        year = self._year()
        self.employee.date_joined = date(year - 1, 7, 1)
        self.employee.save(update_fields=["date_joined"])
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 2, 3),
            primary_end=date(year, 3, 2),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 28),
            remainder_policy=VacationPreference.REMAINDER_DEFER,
        )
        schedule = VacationSchedule.objects.create(year=year, status=VacationSchedule.STATUS_DRAFT, created_by=self.hr_employee)
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(year, 1, 10),
            end_date=date(year, 1, 23),
            vacation_type="paid",
            chargeable_days=get_chargeable_leave_days(date(year, 1, 10), date(year, 1, 23), "paid"),
            status=VacationScheduleItem.STATUS_DRAFT,
            source=VacationScheduleItem.SOURCE_GENERATED,
            risk_score=0,
            risk_level=VacationScheduleItem.RISK_LOW,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(
            reverse("schedule_draft_manual_package_preview", args=[year, self.employee.id]),
            data=json.dumps(
                {
                    "periods": [
                        {"start_date": date(year, 4, 1).isoformat(), "end_date": date(year, 4, 7).isoformat()},
                    ]
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["can_submit"])

    def test_manual_package_place_rejects_only_short_parts(self):
        year = self._year()
        self.employee.date_joined = date(year - 1, 7, 1)
        self.employee.save(update_fields=["date_joined"])
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 2, 3),
            primary_end=date(year, 3, 2),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 28),
            remainder_policy=VacationPreference.REMAINDER_DEFER,
        )
        schedule = VacationSchedule.objects.create(year=year, status=VacationSchedule.STATUS_DRAFT, created_by=self.hr_employee)
        self.client.force_login(self.hr_employee.user)
        periods = [
            {"start_date": date(year, 2, 3).isoformat(), "end_date": date(year, 2, 9).isoformat()},
            {"start_date": date(year, 4, 1).isoformat(), "end_date": date(year, 4, 7).isoformat()},
        ]

        response = self.client.post(
            reverse("schedule_draft_manual_place", args=[year, self.employee.id]),
            {"periods_json": json.dumps(periods)},
        )

        self.assertRedirects(response, reverse("schedule_draft_detail", args=[year]))
        self.assertFalse(VacationScheduleItem.objects.filter(schedule=schedule, employee=self.employee).exists())

    def test_hr_can_manually_place_multiple_schedule_draft_items(self):
        year = self._year()
        self.employee.date_joined = date(year - 1, 7, 1)
        self.employee.save(update_fields=["date_joined"])
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 2, 3),
            primary_end=date(year, 3, 2),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 28),
            remainder_policy=VacationPreference.REMAINDER_DEFER,
        )
        schedule = VacationSchedule.objects.create(year=year, status=VacationSchedule.STATUS_DRAFT, created_by=self.hr_employee)
        self.client.force_login(self.hr_employee.user)

        periods = [
            {"start_date": date(year, 2, 3).isoformat(), "end_date": date(year, 2, 16).isoformat()},
            {"start_date": date(year, 4, 1).isoformat(), "end_date": date(year, 4, 13).isoformat()},
        ]
        response = self.client.post(
            reverse("schedule_draft_manual_place", args=[year, self.employee.id]),
            {"periods_json": json.dumps(periods)},
        )

        self.assertRedirects(response, reverse("schedule_draft_detail", args=[year]))
        items = VacationScheduleItem.objects.filter(schedule=schedule, employee=self.employee).order_by("start_date")
        self.assertEqual(items.count(), 2)
        self.assertTrue(items.filter(start_date=date(year, 2, 3), source=VacationScheduleItem.SOURCE_MANUAL).exists())
        generation_run = schedule.generation_runs.order_by("-started_at", "-id").first()
        self.assertIsNotNone(generation_run)
        self.assertTrue(
            VacationScheduleCandidatePackage.objects.filter(
                generation_run=generation_run,
                decision=VacationScheduleCandidatePackage.DECISION_SELECTED,
                periods_count=2,
            ).exists()
        )
        self.assertEqual(
            VacationScheduleCandidatePackagePeriod.objects.filter(
                candidate_package__generation_run=generation_run,
                schedule_item__isnull=False,
            ).count(),
            2,
        )

    def test_manual_draft_placement_merges_adjacent_parts(self):
        year = self._year()
        self.employee.date_joined = date(year - 1, 7, 1)
        self.employee.save(update_fields=["date_joined"])
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 3, 1),
            primary_end=date(year, 3, 20),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 20),
            remainder_policy=VacationPreference.REMAINDER_DEFER,
        )
        schedule = VacationSchedule.objects.create(year=year, status=VacationSchedule.STATUS_DRAFT, created_by=self.hr_employee)
        self.client.force_login(self.hr_employee.user)
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(year, 3, 1),
            end_date=date(year, 3, 6),
            vacation_type="paid",
            chargeable_days=get_chargeable_leave_days(date(year, 3, 1), date(year, 3, 6), "paid"),
            status=VacationScheduleItem.STATUS_DRAFT,
            source=VacationScheduleItem.SOURCE_GENERATED,
            risk_score=0,
            risk_level=VacationScheduleItem.RISK_LOW,
        )

        response = self.client.post(
            reverse("schedule_draft_manual_place", args=[year, self.employee.id]),
            {
                "start_date": date(year, 3, 7).isoformat(),
                "end_date": date(year, 3, 20).isoformat(),
            },
        )

        self.assertRedirects(response, reverse("schedule_draft_detail", args=[year]))
        items = list(VacationScheduleItem.objects.filter(schedule=schedule, employee=self.employee))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].start_date, date(year, 3, 1))
        self.assertEqual(items[0].end_date, date(year, 3, 20))
        self.assertEqual(
            items[0].chargeable_days,
            get_chargeable_leave_days(date(year, 3, 1), date(year, 3, 20), "paid"),
        )

    def test_manual_draft_preview_reports_days_risk_and_merge(self):
        year = self._year()
        self.employee.date_joined = date(year - 1, 7, 1)
        self.employee.save(update_fields=["date_joined"])
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 3, 1),
            primary_end=date(year, 3, 20),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 20),
            remainder_policy=VacationPreference.REMAINDER_DEFER,
        )
        schedule = VacationSchedule.objects.create(year=year, status=VacationSchedule.STATUS_DRAFT, created_by=self.hr_employee)
        self.client.force_login(self.hr_employee.user)
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(year, 3, 1),
            end_date=date(year, 3, 6),
            vacation_type="paid",
            chargeable_days=get_chargeable_leave_days(date(year, 3, 1), date(year, 3, 6), "paid"),
            status=VacationScheduleItem.STATUS_DRAFT,
            source=VacationScheduleItem.SOURCE_GENERATED,
            risk_score=0,
            risk_level=VacationScheduleItem.RISK_LOW,
        )

        response = self.client.get(
            reverse("schedule_draft_manual_preview", args=[year, self.employee.id]),
            {
                "start_date": date(year, 3, 7).isoformat(),
                "end_date": date(year, 3, 20).isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["can_submit"])
        self.assertTrue(payload["will_merge"])
        self.assertEqual(payload["merged_period_label"], f"01.03.{year} - 20.03.{year}")
        self.assertGreater(payload["chargeable_days"], 0)
        self.assertIn("risk_label", payload)

    def test_schedule_draft_manual_rows_include_pending_skipped_and_double_conflict(self):
        year = self._year()
        self.department_head.date_joined = date(year - 1, 2, 1)
        self.department_head.save(update_fields=["date_joined"])
        self._start_collection()
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 6, 1),
            primary_end=date(year, 6, 14),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 14),
        )
        self._set_filled_preferences(
            self.department_head,
            primary_start=date(year, 6, 1),
            primary_end=date(year, 6, 14),
            backup_start=date(year, 6, 10),
            backup_end=date(year, 6, 20),
        )
        self._set_skipped_preferences(self.outsider)
        VacationPreferenceCollection.objects.filter(year=year).update(
            status=VacationPreferenceCollection.STATUS_FINISHED,
            finished_by=self.hr_employee,
            finished_at=timezone.now(),
        )
        self.client.force_login(self.hr_employee.user)

        self.client.post(reverse("schedule_draft_create", args=[year]))
        response = self.client.get(reverse("schedule_draft_detail", args=[year]))

        self.assertEqual(response.status_code, 200)
        manual_employee_ids = {row["employee"].id for row in response.context["manual_rows"]}
        self.assertIn(self.department_head.id, manual_employee_ids)
        self.assertIn(self.outsider.id, manual_employee_ids)
        self.assertIn(self.employee.id, manual_employee_ids)
        employee_manual_row = next(row for row in response.context["manual_rows"] if row["employee"].id == self.employee.id)
        self.assertIn(employee_manual_row["reason"]["kind"], {"deadline_blocker", "remaining_plan"})
        self.assertTrue(employee_manual_row["planning_need"]["needs_manual_attention"])
        self.assertContains(response, "schedule-draft-manual-card--staffing_conflict")
        self.assertFalse(VacationScheduleItem.objects.filter(schedule__year=year, employee=self.department_head).exists())

    def test_schedule_draft_manual_rows_exclude_pending_employee_with_closed_plan(self):
        year = self._year()
        Employees.objects.exclude(id=self.employee.id).update(is_active_employee=False)
        self.employee.date_joined = date(year - 1, 1, 1)
        self.employee.annual_paid_leave_days = 52
        self.employee.save(update_fields=["date_joined", "annual_paid_leave_days"])
        self._start_collection()
        VacationPreferenceCollection.objects.filter(year=year).update(
            status=VacationPreferenceCollection.STATUS_FINISHED,
            finished_by=self.hr_employee,
            finished_at=timezone.now(),
        )
        schedule = VacationSchedule.objects.create(
            year=year,
            status=VacationSchedule.STATUS_DRAFT,
            created_by=self.hr_employee,
        )
        start_date = date(year, 2, 1)
        end_date = self._paid_period_for_chargeable_days(start_date, 104)
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=start_date,
            end_date=end_date,
            vacation_type="paid",
            chargeable_days=Decimal("104.00"),
            status=VacationScheduleItem.STATUS_DRAFT,
            source=VacationScheduleItem.SOURCE_GENERATED,
            risk_score=0,
            risk_level=VacationScheduleItem.RISK_LOW,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("schedule_draft_detail", args=[year]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["manual_rows"], [])
        self.assertEqual(response.context["draft_summary"]["manual"], 0)
        self.assertNotContains(response, "data-draft-manual-open")

    def test_schedule_draft_shows_active_urgent_closure_for_closed_plan(self):
        year = self._year()
        Employees.objects.exclude(id=self.employee.id).update(is_active_employee=False)
        self.employee.date_joined = date(year - 1, 1, 1)
        self.employee.annual_paid_leave_days = 52
        self.employee.save(update_fields=["date_joined", "annual_paid_leave_days"])
        self._start_collection()
        VacationPreferenceCollection.objects.filter(year=year).update(
            status=VacationPreferenceCollection.STATUS_FINISHED,
            finished_by=self.hr_employee,
            finished_at=timezone.now(),
        )
        schedule = VacationSchedule.objects.create(
            year=year,
            status=VacationSchedule.STATUS_DRAFT,
            created_by=self.hr_employee,
        )
        start_date = date(year, 2, 1)
        end_date = self._paid_period_for_chargeable_days(start_date, 104)
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=start_date,
            end_date=end_date,
            vacation_type="paid",
            chargeable_days=Decimal("104.00"),
            status=VacationScheduleItem.STATUS_DRAFT,
            source=VacationScheduleItem.SOURCE_GENERATED,
            risk_score=0,
            risk_level=VacationScheduleItem.RISK_LOW,
        )
        closure_request = VacationUrgentClosureRequest.objects.create(
            employee=self.employee,
            planning_year=year,
            closure_year=year - 1,
            required_days=Decimal("3.00"),
            deadline=date(year, 1, 3),
            proposed_start_date=date(year - 1, 12, 29),
            proposed_end_date=date(year - 1, 12, 31),
            created_by=self.hr_employee,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("schedule_draft_detail", args=[year]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["draft_summary"]["manual"], 1)
        self.assertEqual(response.context["draft_summary"]["blocking"], 0)
        self.assertFalse(response.context["approval_blocked"])
        manual_row = response.context["manual_rows"][0]
        self.assertEqual(manual_row["employee"].id, self.employee.id)
        self.assertEqual(manual_row["reason"]["kind"], "deadline_blocker")
        self.assertEqual(manual_row["urgent_closure"]["active_request"], closure_request)
        self.assertContains(response, reverse("urgent_closure_detail", args=[closure_request.id]))
        self.assertContains(response, f"back_url=/calendar/drafts/{year}/")
        self.assertContains(response, "back_label=%D0%9A%20%D1%87%D0%B5%D1%80%D0%BD%D0%BE%D0%B2%D0%B8%D0%BA%D1%83")

    def test_schedule_draft_marks_urgent_balance_as_approval_blocker(self):
        year = self._year()
        self.employee.date_joined = date(year - 2, 1, 4)
        self.employee.save(update_fields=["date_joined"])
        self._start_collection()
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 6, 1),
            primary_end=date(year, 6, 28),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 28),
        )
        VacationPreferenceCollection.objects.filter(year=year).update(
            status=VacationPreferenceCollection.STATUS_FINISHED,
            finished_by=self.hr_employee,
            finished_at=timezone.now(),
        )
        self.client.force_login(self.hr_employee.user)

        self.client.post(reverse("schedule_draft_create", args=[year]))
        response = self.client.get(reverse("schedule_draft_detail", args=[year]))

        planning_need = response.context["planning_need_by_employee"][self.employee.id]
        self.assertTrue(planning_need["has_blocker"])
        self.assertEqual(planning_need["nearest_deadline"], date(year, 1, 3))
        self.assertGreater(planning_need["blocking_days"], 0)
        self.assertTrue(response.context["approval_blocked"])
        manual_row = next(row for row in response.context["manual_rows"] if row["employee"].id == self.employee.id)
        self.assertEqual(manual_row["reason"]["kind"], "deadline_blocker")
        self.assertContains(response, "Сначала")
        self.assertContains(response, f"03.01.{year}")

    def test_schedule_draft_target_separates_year_plan_from_future_reserve(self):
        year = self._year()
        entitlement_rows = [
            {
                "period_start": date(year - 2, 1, 20),
                "period_end": date(year - 1, 1, 19),
                "remaining_days": Decimal("2.00"),
                "available_from": date(year - 2, 1, 20),
                "must_use_by": date(year, 1, 19),
            },
            {
                "period_start": date(year - 1, 1, 20),
                "period_end": date(year, 1, 19),
                "remaining_days": Decimal("52.00"),
                "available_from": date(year - 1, 1, 20),
                "must_use_by": date(year + 1, 1, 19),
            },
            {
                "period_start": date(year, 1, 20),
                "period_end": date(year + 1, 1, 19),
                "remaining_days": Decimal("52.00"),
                "available_from": date(year, 1, 20),
                "must_use_by": date(year + 2, 1, 19),
            },
        ]
        draft_items = [
            VacationScheduleItem(
                employee=self.employee,
                start_date=date(year, 1, 1),
                end_date=date(year, 1, 10),
                vacation_type="paid",
                chargeable_days=Decimal("2.00"),
            ),
            VacationScheduleItem(
                employee=self.employee,
                start_date=date(year, 9, 10),
                end_date=date(year, 9, 30),
                vacation_type="paid",
                chargeable_days=Decimal("21.00"),
            ),
        ]

        planning_need = _build_employee_schedule_planning_need_from_rows(
            self.employee,
            year,
            draft_items,
            Decimal("106.00"),
            Decimal("54.00"),
            entitlement_rows,
            requested_preference_days=Decimal("21.00"),
            remainder_policy=VacationPreference.REMAINDER_AUTO,
            preference_state=VacationPreference.STATUS_FILLED,
        )

        self.assertEqual(planning_need["target_days"], Decimal("54.00"))
        self.assertEqual(planning_need["placed_days"], Decimal("23.00"))
        self.assertEqual(planning_need["open_required_days"], Decimal("31.00"))
        self.assertEqual(planning_need["future_available_days"], Decimal("52.00"))
        self.assertIn("незакрытых дней", planning_need["action_text"])
        self.assertNotIn("83", planning_need["action_text"])

    def test_schedule_draft_hides_future_reserve_when_plan_is_closed(self):
        year = self._year()
        entitlement_rows = [
            {
                "period_start": date(year - 1, 1, 20),
                "period_end": date(year, 1, 19),
                "remaining_days": Decimal("52.00"),
                "available_from": date(year - 1, 1, 20),
                "must_use_by": date(year + 1, 1, 19),
            },
            {
                "period_start": date(year, 1, 20),
                "period_end": date(year + 1, 1, 19),
                "remaining_days": Decimal("52.00"),
                "available_from": date(year, 1, 20),
                "must_use_by": date(year + 2, 1, 19),
            },
        ]
        draft_items = [
            VacationScheduleItem(
                employee=self.employee,
                start_date=date(year, 6, 1),
                end_date=date(year, 7, 22),
                vacation_type="paid",
                chargeable_days=Decimal("52.00"),
            ),
        ]

        planning_need = _build_employee_schedule_planning_need_from_rows(
            self.employee,
            year,
            draft_items,
            Decimal("104.00"),
            Decimal("52.00"),
            entitlement_rows,
            requested_preference_days=Decimal("14.00"),
            remainder_policy=VacationPreference.REMAINDER_AUTO,
            preference_state=VacationPreference.STATUS_FILLED,
        )

        self.assertEqual(planning_need["future_available_days"], Decimal("52.00"))
        self.assertEqual(planning_need["action_text"], f"План на {year} год закрыт.")
        self.assertNotIn("Будущий резерв", [item["label"] for item in planning_need["plan_breakdown"]])

    def test_pending_preference_creates_annual_manual_task_without_employee_dates(self):
        year = self._year()
        Employees.objects.exclude(id=self.employee.id).update(is_active_employee=False)
        self.employee.date_joined = date(year - 1, 7, 1)
        self.employee.annual_paid_leave_days = 52
        self.employee.save(update_fields=["date_joined", "annual_paid_leave_days"])
        self._start_collection()
        VacationPreferenceCollection.objects.filter(year=year).update(
            status=VacationPreferenceCollection.STATUS_FINISHED,
            finished_by=self.hr_employee,
            finished_at=timezone.now(),
        )
        VacationSchedule.objects.create(year=year, status=VacationSchedule.STATUS_DRAFT, created_by=self.hr_employee)
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("schedule_draft_detail", args=[year]))

        planning_need = response.context["planning_need_by_employee"][self.employee.id]
        self.assertEqual(planning_need["target_days"], Decimal("52.00"))
        self.assertTrue(planning_need["needs_manual_attention"])
        self.assertEqual(len(response.context["manual_rows"]), 1)
