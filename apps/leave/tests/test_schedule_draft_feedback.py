import json
from datetime import date, timedelta
from decimal import Decimal

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
from apps.leave.services.schedule_drafts.auto_place import auto_place_remaining_schedule_draft
from apps.leave.services.schedule_drafts.candidate_generation import (
    _build_auto_generation_candidates,
    _build_draft_generation_context,
    _build_preference_generation_candidates,
)
from apps.leave.services.schedule_drafts.manual import place_manual_schedule_draft_items
from apps.leave.services.schedule_drafts.manual_suggestions import build_schedule_draft_auto_place_preview
from apps.leave.services.schedule_drafts.page_context import build_manual_schedule_draft_preview
from apps.leave.services.schedule_drafts.planning_need import _build_employee_schedule_planning_need_from_rows
from apps.leave.services.schedule_planning import schedule_planning_url
from apps.leave.ml.scoring import ACTIVE_CANDIDATE_SCORER_VERSION
from apps.leave.tests.base import LeaveTestCase


class ScheduleDraftFeedbackAccessTests(LeaveTestCase):
    def test_hr_saves_feedback_for_selected_schedule_candidate(self):
        year = self._year()
        self._start_collection()
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 6, 1),
            primary_end=date(year, 6, 14),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 14),
        )
        VacationPreferenceCollection.objects.filter(year=year).update(
            status=VacationPreferenceCollection.STATUS_FINISHED,
            finished_by=self.hr_employee,
            finished_at=timezone.now(),
        )
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_draft_create", args=[year]))

        item = VacationScheduleItem.objects.select_related("selected_candidate", "generation_run").get(
            schedule__year=year,
            employee=self.employee,
        )
        response = self.client.post(
            reverse("schedule_draft_candidate_feedback", args=[year, item.id]),
            {
                "decision": VacationScheduleCandidateFeedback.DECISION_NEEDS_CHANGE,
                "comment": "Лучше проверить нагрузку отдела.",
                "next": reverse("schedule_draft_detail", args=[year]),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"{reverse('schedule_draft_detail', args=[year])}#draft-item-{item.id}")
        feedback = VacationScheduleCandidateFeedback.objects.get(schedule_item=item, reviewer=self.hr_employee)
        self.assertEqual(feedback.reviewer_role, VacationScheduleCandidateFeedback.ROLE_HR)
        self.assertEqual(feedback.decision, VacationScheduleCandidateFeedback.DECISION_NEEDS_CHANGE)
        self.assertEqual(feedback.comment, "Лучше проверить нагрузку отдела.")
        self.assertEqual(feedback.candidate_id, item.selected_candidate_id)
        self.assertEqual(feedback.generation_run_id, item.generation_run_id)
        self.assertEqual(feedback.score_snapshot, item.ai_score)
        self.assertEqual(feedback.confidence_snapshot, item.ai_confidence)
        self.assertEqual(feedback.model_version_snapshot, ACTIVE_CANDIDATE_SCORER_VERSION)

        detail_response = self.client.get(reverse("schedule_draft_detail", args=[year]))
        placed_row = detail_response.context["placed_rows"][0]
        self.assertTrue(placed_row["feedback"]["can_submit"])
        self.assertEqual(
            placed_row["feedback"]["current"]["decision"],
            VacationScheduleCandidateFeedback.DECISION_NEEDS_CHANGE,
        )
        self.assertEqual(placed_row["feedback"]["summary"]["total"], 1)
        self.assertContains(detail_response, "Проверка")
        review_response = self.client.get(reverse("schedule_draft_item_review", args=[year, item.id]))
        review_html = review_response.json()["html"]
        self.assertIn("Ваш отзыв", review_html)
        self.assertIn("Лучше проверить нагрузку отдела.", review_html)

        ajax_response = self.client.post(
            reverse("schedule_draft_candidate_feedback", args=[year, item.id]),
            {
                "decision": VacationScheduleCandidateFeedback.DECISION_AGREE,
                "comment": "После проверки вариант подходит.",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(ajax_response.status_code, 200)
        ajax_payload = ajax_response.json()
        self.assertTrue(ajax_payload["ok"])
        self.assertEqual(ajax_payload["feedback"]["summary"]["total"], 1)
        self.assertEqual(
            ajax_payload["feedback"]["current"]["decision"],
            VacationScheduleCandidateFeedback.DECISION_AGREE,
        )
        self.assertEqual(
            ajax_payload["feedback"]["current"]["comment"],
            "После проверки вариант подходит.",
        )

    def test_hr_reject_returns_draft_item_to_manual_tasks(self):
        year = self._year()
        self._start_collection()
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 6, 1),
            primary_end=date(year, 6, 14),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 14),
        )
        VacationPreferenceCollection.objects.filter(year=year).update(
            status=VacationPreferenceCollection.STATUS_FINISHED,
            finished_by=self.hr_employee,
            finished_at=timezone.now(),
        )
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_draft_create", args=[year]))

        item = VacationScheduleItem.objects.select_related("schedule").get(
            schedule__year=year,
            employee=self.employee,
        )
        schedule = item.schedule
        VacationScheduleManualSuggestionCache.objects.create(
            schedule=schedule,
            employee=self.employee,
            version=schedule.manual_suggestion_cache_version,
            payload={"options": []},
        )

        response = self.client.post(
            reverse("schedule_draft_candidate_feedback", args=[year, item.id]),
            {
                "decision": VacationScheduleCandidateFeedback.DECISION_REJECT,
                "comment": "Период не подходит.",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["item_returned_to_manual"])
        self.assertIn(f"#draft-manual-{self.employee.id}", payload["redirect_url"])

        item.refresh_from_db()
        self.assertEqual(item.status, VacationScheduleItem.STATUS_CANCELLED)
        self.assertTrue(item.was_changed_by_manager)
        self.assertIn("Отклонено при проверке рекомендации", item.manager_comment)

        feedback = VacationScheduleCandidateFeedback.objects.get(schedule_item=item, reviewer=self.hr_employee)
        self.assertEqual(feedback.decision, VacationScheduleCandidateFeedback.DECISION_REJECT)
        self.assertEqual(feedback.comment, "Период не подходит.")

        schedule.refresh_from_db()
        self.assertEqual(schedule.manual_suggestion_cache_version, 1)
        self.assertFalse(VacationScheduleManualSuggestionCache.objects.filter(schedule=schedule).exists())

        detail_response = self.client.get(reverse("schedule_draft_detail", args=[year]))
        self.assertEqual([row["employee"].id for row in detail_response.context["placed_rows"]], [])
        self.assertIn(
            self.employee.id,
            [row["employee"].id for row in detail_response.context["manual_rows"]],
        )

    def test_department_head_reject_only_saves_feedback(self):
        year = self._year()
        self._start_collection()
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 6, 1),
            primary_end=date(year, 6, 14),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 14),
        )
        VacationPreferenceCollection.objects.filter(year=year).update(
            status=VacationPreferenceCollection.STATUS_FINISHED,
            finished_by=self.hr_employee,
            finished_at=timezone.now(),
        )
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_draft_create", args=[year]))
        item = VacationScheduleItem.objects.get(schedule__year=year, employee=self.employee)

        self.client.force_login(self.department_head.user)
        response = self.client.post(
            reverse("schedule_draft_candidate_feedback", args=[year, item.id]),
            {"decision": VacationScheduleCandidateFeedback.DECISION_REJECT},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["item_returned_to_manual"])
        item.refresh_from_db()
        self.assertEqual(item.status, VacationScheduleItem.STATUS_DRAFT)
        feedback = VacationScheduleCandidateFeedback.objects.get(schedule_item=item, reviewer=self.department_head)
        self.assertEqual(feedback.decision, VacationScheduleCandidateFeedback.DECISION_REJECT)

    def test_department_head_can_feedback_only_for_own_department_draft_item(self):
        year = self._year()
        self._start_collection()
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 6, 1),
            primary_end=date(year, 6, 14),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 14),
        )
        VacationPreferenceCollection.objects.filter(year=year).update(
            status=VacationPreferenceCollection.STATUS_FINISHED,
            finished_by=self.hr_employee,
            finished_at=timezone.now(),
        )
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_draft_create", args=[year]))
        item = VacationScheduleItem.objects.get(schedule__year=year, employee=self.employee)

        self.client.force_login(self.department_head.user)
        own_response = self.client.get(reverse("schedule_draft_detail", args=[year]))
        self.assertEqual(own_response.status_code, 200)
        self.assertEqual([row["employee"].id for row in own_response.context["placed_rows"]], [self.employee.id])
        self.assertTrue(own_response.context["placed_rows"][0]["feedback"]["can_submit"])
        self.client.post(
            reverse("schedule_draft_candidate_feedback", args=[year, item.id]),
            {"decision": VacationScheduleCandidateFeedback.DECISION_AGREE},
        )
        feedback = VacationScheduleCandidateFeedback.objects.get(schedule_item=item, reviewer=self.department_head)
        self.assertEqual(feedback.reviewer_role, VacationScheduleCandidateFeedback.ROLE_DEPARTMENT_HEAD)
        self.assertEqual(feedback.decision, VacationScheduleCandidateFeedback.DECISION_AGREE)

        self.client.force_login(self.foreign_department_head.user)
        foreign_response = self.client.post(
            reverse("schedule_draft_candidate_feedback", args=[year, item.id]),
            {"decision": VacationScheduleCandidateFeedback.DECISION_REJECT},
        )

        self.assertEqual(foreign_response.status_code, 302)
        self.assertFalse(
            VacationScheduleCandidateFeedback.objects.filter(
                schedule_item=item,
                reviewer=self.foreign_department_head,
            ).exists()
        )

    def test_regular_employee_cannot_leave_schedule_candidate_feedback(self):
        year = self._year()
        self._start_collection()
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 6, 1),
            primary_end=date(year, 6, 14),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 14),
        )
        VacationPreferenceCollection.objects.filter(year=year).update(
            status=VacationPreferenceCollection.STATUS_FINISHED,
            finished_by=self.hr_employee,
            finished_at=timezone.now(),
        )
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_draft_create", args=[year]))
        item = VacationScheduleItem.objects.get(schedule__year=year, employee=self.employee)

        self.client.force_login(self.employee.user)
        response = self.client.post(
            reverse("schedule_draft_candidate_feedback", args=[year, item.id]),
            {"decision": VacationScheduleCandidateFeedback.DECISION_AGREE},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(VacationScheduleCandidateFeedback.objects.filter(schedule_item=item).exists())

    def test_enterprise_head_views_draft_without_create_action(self):
        year = self._year()
        self._start_collection()
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 6, 1),
            primary_end=date(year, 6, 14),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 14),
        )
        VacationPreferenceCollection.objects.filter(year=year).update(
            status=VacationPreferenceCollection.STATUS_FINISHED,
            finished_by=self.hr_employee,
            finished_at=timezone.now(),
        )
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_draft_create", args=[year]))

        self.client.force_login(self.enterprise_head.user)
        response = self.client.get(reverse("schedule_draft_detail", args=[year]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Черновик графика")
        self.assertNotContains(response, "Создать черновик")
        create_response = self.client.post(reverse("schedule_draft_create", args=[year]))
        self.assertEqual(create_response.status_code, 302)
