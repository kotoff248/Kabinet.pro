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
    VacationPlanningCycle,
    VacationPreference,
    VacationPreferenceCollection,
    VacationSchedule,
    VacationScheduleAutoPlaceJob,
    VacationScheduleCandidate,
    VacationScheduleCandidateFeedback,
    VacationScheduleCandidatePackage,
    VacationScheduleCandidatePackagePeriod,
    VacationScheduleGenerationRun,
    VacationScheduleDepartmentApproval,
    VacationScheduleEnterpriseApproval,
    VacationScheduleItem,
    VacationScheduleManualSuggestionCache,
    VacationUrgentClosureRequest,
)
from apps.leave.services.dates import add_months_safe, get_chargeable_leave_days
from apps.leave.services.preferences import (
    build_preference_collection_summary,
    get_employee_preference_pair_map,
    get_employee_preference_state_map,
    get_eligible_preference_employees,
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


class VacationPreferenceCollectionTests(LeaveTestCase):
    def test_bulk_preference_state_map_matches_single_employee_states(self):
        year = self._year()
        VacationPreference.objects.filter(year=year).delete()
        VacationPreference.objects.bulk_create(
            [
                VacationPreference(
                    employee=self.employee,
                    year=year,
                    priority=VacationPreference.PRIORITY_PRIMARY,
                    start_date=date(year, 6, 1),
                    end_date=date(year, 6, 14),
                    status=VacationPreference.STATUS_FILLED,
                ),
                VacationPreference(
                    employee=self.employee,
                    year=year,
                    priority=VacationPreference.PRIORITY_BACKUP,
                    start_date=date(year, 8, 1),
                    end_date=date(year, 8, 14),
                    status=VacationPreference.STATUS_FILLED,
                ),
                VacationPreference(
                    employee=self.department_head,
                    year=year,
                    priority=VacationPreference.PRIORITY_PRIMARY,
                    status=VacationPreference.STATUS_SKIPPED,
                ),
                VacationPreference(
                    employee=self.hr_employee,
                    year=year,
                    priority=VacationPreference.PRIORITY_PRIMARY,
                    status=VacationPreference.STATUS_PENDING,
                ),
            ]
        )

        with self.assertNumQueries(1):
            state_by_employee = get_employee_preference_state_map(
                [
                    self.employee.id,
                    self.department_head.id,
                    self.hr_employee.id,
                    self.outsider.id,
                ],
                year,
            )

        self.assertEqual(state_by_employee[self.employee.id], VacationPreference.STATUS_FILLED)
        self.assertEqual(state_by_employee[self.department_head.id], VacationPreference.STATUS_SKIPPED)
        self.assertEqual(state_by_employee[self.hr_employee.id], VacationPreference.STATUS_PENDING)
        self.assertEqual(state_by_employee[self.outsider.id], "missing")

    def test_bulk_preference_pair_map_uses_first_preference_by_priority(self):
        year = self._year()
        VacationPreference.objects.filter(year=year).delete()
        first_primary = VacationPreference.objects.create(
            employee=self.employee,
            year=year,
            priority=VacationPreference.PRIORITY_PRIMARY,
            start_date=date(year, 6, 1),
            end_date=date(year, 6, 14),
            status=VacationPreference.STATUS_FILLED,
        )
        VacationPreference.objects.create(
            employee=self.employee,
            year=year,
            priority=VacationPreference.PRIORITY_PRIMARY,
            start_date=date(year, 7, 1),
            end_date=date(year, 7, 14),
            status=VacationPreference.STATUS_FILLED,
        )
        backup = VacationPreference.objects.create(
            employee=self.employee,
            year=year,
            priority=VacationPreference.PRIORITY_BACKUP,
            start_date=date(year, 9, 1),
            end_date=date(year, 9, 14),
            status=VacationPreference.STATUS_FILLED,
        )

        with self.assertNumQueries(1):
            pair_by_employee = get_employee_preference_pair_map([self.employee.id, self.outsider.id], year)

        self.assertEqual(pair_by_employee[self.employee.id][VacationPreference.PRIORITY_PRIMARY], first_primary)
        self.assertEqual(pair_by_employee[self.employee.id][VacationPreference.PRIORITY_BACKUP], backup)
        self.assertIsNone(pair_by_employee[self.outsider.id][VacationPreference.PRIORITY_PRIMARY])

    def test_only_hr_can_start_and_finish_collection(self):
        year = self._year()
        self.client.force_login(self.employee.user)
        response = self.client.post(
            reverse("preferences_collection_start"),
            {"year": year, "deadline": self._deadline().isoformat()},
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(VacationPreferenceCollection.objects.filter(year=year).exists())

        self._start_collection()
        collection = VacationPreferenceCollection.objects.get(year=year)
        self.assertEqual(collection.status, VacationPreferenceCollection.STATUS_OPEN)
        self.assertEqual(collection.started_by_id, self.hr_employee.id)

        self.client.force_login(self.department_head.user)
        response = self.client.post(reverse("preferences_collection_finish", args=[year]))
        collection.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(collection.status, VacationPreferenceCollection.STATUS_OPEN)

        self.client.force_login(self.hr_employee.user)
        response = self.client.post(reverse("preferences_collection_finish", args=[year]))
        collection.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(collection.status, VacationPreferenceCollection.STATUS_FINISHED)
        self.assertEqual(collection.finished_by_id, self.hr_employee.id)

    def test_calendar_collection_actions_target_next_planning_year(self):
        current_year = timezone.localdate().year
        planning_year = self._year()
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(f"{reverse('calendar')}?view=year&year={current_year}")
        self.assertEqual(response.context["calendar_preference_collection"]["year"], planning_year)
        self.assertContains(response, f'name="year" value="{planning_year}"')

        response = self.client.post(
            reverse("preferences_collection_start"),
            {"year": current_year, "deadline": self._deadline().isoformat()},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(VacationPreferenceCollection.objects.filter(year=current_year).exists())

    def test_enterprise_head_sees_collection_readiness_without_management_actions(self):
        year = self._year()
        self._start_collection()

        self.client.force_login(self.enterprise_head.user)
        response = self.client.get(f"{reverse('calendar')}?view=year&year={year}")

        self.assertEqual(response.status_code, 200)
        collection_context = response.context["calendar_preference_collection"]
        self.assertTrue(collection_context["can_view"])
        self.assertFalse(collection_context["can_manage"])
        self.assertEqual(collection_context["readiness_status_key"], "open")
        self.assertContains(response, "Сбор пожеланий")
        self.assertContains(response, "Сбор идет")
        self.assertContains(response, "Не ответили")
        self.assertContains(response, "Без пожеланий")
        self.assertNotContains(response, "Начать сбор пожеланий")
        self.assertNotContains(response, "Завершить сбор")

        finish_response = self.client.post(reverse("preferences_collection_finish", args=[year]))
        collection = VacationPreferenceCollection.objects.get(year=year)
        self.assertEqual(finish_response.status_code, 302)
        self.assertEqual(collection.status, VacationPreferenceCollection.STATUS_OPEN)

        self.client.force_login(self.hr_employee.user)
        response = self.client.get(f"{reverse('calendar')}?view=year&year={year}")
        self.assertContains(response, "Завершить сбор")
        self.client.post(reverse("preferences_collection_finish", args=[year]))

        self.client.force_login(self.enterprise_head.user)
        response = self.client.get(f"{reverse('calendar')}?view=year&year={year}")

        collection_context = response.context["calendar_preference_collection"]
        self.assertEqual(collection_context["readiness_status_key"], "ready")
        self.assertTrue(collection_context["draft_ready"])
        self.assertContains(response, "Готово к черновику")
        self.assertNotContains(response, "Завершить сбор")

    def test_calendar_preference_status_links_to_readiness_page(self):
        year = self._year()
        self._start_collection()
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(f"{reverse('calendar')}?view=year&year={year}")

        self.assertContains(response, f'href="{preference_readiness_url(year)}"')
        self.assertContains(response, "data-app-link")

    def test_hr_can_view_and_finish_readiness_page(self):
        year = self._year()
        self._start_collection()
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("preference_collection_readiness", args=[year]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Готовность сбора")
        self.assertContains(response, "Ответили")
        self.assertContains(response, "Не ответили")
        self.assertContains(response, "Без пожеланий")
        self.assertContains(response, "Завершить сбор")
        self.assertContains(response, "preference-readiness-segmented")
        self.assertContains(response, "data-preference-readiness-search")
        self.assertContains(response, "js/pages/preference-readiness.js")
        self.assertEqual(response.context["summary"]["not_answered"], response.context["summary"]["total"])

        response = self.client.post(
            reverse("preferences_collection_finish", args=[year]),
            {"next": reverse("preference_collection_readiness", args=[year])},
        )

        self.assertRedirects(response, reverse("preference_collection_readiness", args=[year]))
        collection = VacationPreferenceCollection.objects.get(year=year)
        self.assertEqual(collection.status, VacationPreferenceCollection.STATUS_FINISHED)

    def test_readiness_page_marks_new_hires(self):
        year = self._year()
        self.employee.date_joined = timezone.localdate()
        self.employee.save(update_fields=["date_joined"])
        self._start_collection()
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("preference_collection_readiness", args=[year]))

        self.assertEqual(response.status_code, 200)
        rows_by_employee = {row["employee"].id: row for row in response.context["rows"]}
        self.assertEqual(rows_by_employee[self.employee.id]["new_hire_badge"]["label"], "Новичок")
        self.assertContains(response, 'class="new-hire-badge"')
        self.assertContains(response, "person_add")
        self.assertContains(response, "Работает меньше 6 месяцев")

    def test_hr_can_start_collection_from_planning_stage(self):
        year = self._year()
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(f"{reverse('schedule_planning', args=[year])}?stage=collection")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_start_collection"])
        self.assertContains(response, "Открыть сбор")
        self.assertContains(response, "Начать сбор пожеланий")
        self.assertContains(response, "calendar-action-btn calendar-action-btn--preferences")
        self.assertContains(response, "fact_check")
        self.assertEqual(
            response.context["calendar_preference_collection"]["start_next_url"],
            schedule_planning_url(year, "collection"),
        )

    def test_hr_can_start_collection_from_readiness_page(self):
        year = self._year()
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("preference_collection_readiness", args=[year]),
            {
                "from": "schedule_planning",
                "back_url": f"{reverse('schedule_planning', args=[year])}?stage=collection",
                "back_label": "К планированию",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_start_collection"])
        self.assertContains(response, "Не начат")
        self.assertContains(response, "Начать сбор пожеланий")
        self.assertContains(response, "calendar-action-btn calendar-action-btn--preferences")
        self.assertContains(response, "fact_check")
        self.assertEqual(response.context["calendar_preference_collection"]["start_next_url"], response.context["current_path"])

    def test_start_collection_respects_next_url(self):
        year = self._year()
        next_url = schedule_planning_url(year, "collection")
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(
            reverse("preferences_collection_start"),
            {
                "year": year,
                "deadline": self._deadline().isoformat(),
                "next": next_url,
            },
        )

        self.assertRedirects(response, next_url)
        self.assertTrue(VacationPreferenceCollection.objects.filter(year=year).exists())

    def test_hr_can_open_schedule_planning_hub(self):
        year = self._year()
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("schedule_planning", args=[year]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "schedule_planning")
        self.assertEqual(response.context["selected_stage"], "calendar")
        self.assertContains(response, "Планирование графика")
        self.assertContains(response, "schedule-planning-roadmap")
        self.assertNotContains(response, "schedule-planning-stage-nav")
        self.assertContains(response, "css/pages/schedule-planning.css")
        self.assertContains(response, 'data-sidebar-key="schedule-planning"')
        self.assertContains(response, 'aria-current="page"')
        self.assertContains(response, "from=schedule_planning")
        for label in ["График", "Сбор", "Черновик", "Проверка", "Финал"]:
            self.assertContains(response, label)

    def test_schedule_planning_draft_shows_active_auto_place_job(self):
        year = self._year()
        schedule = VacationSchedule.objects.create(
            year=year,
            status=VacationSchedule.STATUS_DRAFT,
            created_by=self.hr_employee,
        )
        VacationScheduleAutoPlaceJob.objects.create(
            token="planning-auto-token",
            year=year,
            schedule=schedule,
            actor=self.hr_employee,
            status=VacationScheduleAutoPlaceJob.STATUS_RUNNING,
            progress_percent=42,
            stage_label="Проверка пакетов",
            message="Обработано несколько сотрудников.",
            processed_employees=3,
            total_employees=9,
            placed_count=5,
            unresolved_count=2,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("schedule_planning", args=[year]), {"stage": "draft"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["draft_auto_place_job"]["progress_percent"], 42)
        self.assertContains(response, "data-planning-auto-job")
        self.assertContains(response, "planning-auto-token")
        self.assertContains(response, "Проверка пакетов")
        self.assertContains(response, "42%")
        self.assertContains(response, "3 / 9")
        self.assertContains(response, "5")
        self.assertContains(response, "2")

    def _create_ready_draft_schedule(self, year=None):
        year = year or self._year()
        VacationSchedule.objects.filter(year=year).delete()
        schedule = VacationSchedule.objects.create(
            year=year,
            status=VacationSchedule.STATUS_DRAFT,
            created_by=self.hr_employee,
        )
        eligible_employees = get_eligible_preference_employees(year)
        Employees.objects.filter(id__in=[employee.id for employee in eligible_employees]).update(
            date_joined=date(year, 1, 1)
        )
        for employee in eligible_employees:
            employee.date_joined = date(year, 1, 1)
        for index, employee in enumerate(eligible_employees, start=1):
            VacationScheduleItem.objects.create(
                schedule=schedule,
                employee=employee,
                start_date=date(year, 7, 1) + timedelta(days=index),
                end_date=date(year, 8, 23) + timedelta(days=index),
                chargeable_days=employee.annual_paid_leave_days,
                status=VacationScheduleItem.STATUS_DRAFT,
                source=VacationScheduleItem.SOURCE_GENERATED,
            )
        return schedule

    def _end_date_for_chargeable_days(self, start_date, chargeable_days):
        current = start_date
        while current.year == start_date.year:
            if get_chargeable_leave_days(start_date, current, "paid") == chargeable_days:
                return current
            current += timedelta(days=1)
        raise AssertionError(f"Не удалось подобрать период на {chargeable_days} д.")

    def _replace_employee_schedule_package(self, schedule, employee, day_counts, *, status=None):
        status = status or VacationScheduleItem.STATUS_DRAFT
        schedule.items.filter(employee=employee).delete()
        start_date = date(schedule.year, 7, 1)
        items = []
        for days in day_counts:
            end_date = self._end_date_for_chargeable_days(start_date, days)
            items.append(
                VacationScheduleItem.objects.create(
                    schedule=schedule,
                    employee=employee,
                    start_date=start_date,
                    end_date=end_date,
                    chargeable_days=Decimal(days),
                    status=status,
                    source=VacationScheduleItem.SOURCE_GENERATED,
                )
            )
            start_date = end_date + timedelta(days=7)
        return items

    def _return_department_for_rework(self, year=None):
        year = year or self._year()
        schedule = self._create_ready_draft_schedule(year)
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_department_review_start", args=[year]))
        approval = VacationScheduleDepartmentApproval.objects.get(schedule=schedule, department=self.engineering)
        self.client.force_login(self.department_head.user)
        self.client.post(
            reverse("schedule_department_review_return", args=[year, approval.id]),
            {"comment": "Нужно поменять даты механиков.", "next": schedule_planning_url(year, "review")},
        )
        approval.refresh_from_db()
        schedule.refresh_from_db()
        return schedule, approval

    def _approve_all_departments_for_final(self, year=None):
        year = year or self._year()
        schedule = self._create_ready_draft_schedule(year)
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_department_review_start", args=[year]))
        for approval in VacationScheduleDepartmentApproval.objects.filter(schedule=schedule).select_related(
            "department_head"
        ):
            self.client.force_login(approval.department_head.user)
            self.client.post(
                reverse("schedule_department_review_approve", args=[year, approval.id]),
                {"next": schedule_planning_url(year, "review")},
            )
        schedule.refresh_from_db()
        return schedule

    def test_hr_can_send_ready_schedule_to_department_review(self):
        year = self._year()
        schedule = self._create_ready_draft_schedule(year)
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(
            reverse("schedule_department_review_start", args=[year]),
            {"next": schedule_planning_url(year, "review")},
        )

        self.assertRedirects(response, schedule_planning_url(year, "review"))
        schedule.refresh_from_db()
        self.assertEqual(schedule.status, VacationSchedule.STATUS_DEPARTMENT_REVIEW)
        self.assertFalse(schedule.items.filter(status=VacationScheduleItem.STATUS_DRAFT).exists())
        self.assertEqual(
            schedule.items.filter(status=VacationScheduleItem.STATUS_PLANNED).count(),
            len(get_eligible_preference_employees(year)),
        )
        approvals = VacationScheduleDepartmentApproval.objects.filter(schedule=schedule)
        self.assertEqual(approvals.count(), 2)
        self.assertTrue(approvals.filter(department=self.engineering, department_head=self.department_head).exists())
        self.assertTrue(approvals.filter(department=self.hr_department, department_head=self.foreign_department_head).exists())
        self.assertEqual(
            Notification.objects.filter(
                event_type=Notification.TYPE_SCHEDULE_REVIEW_REQUESTED,
                action_url=schedule_planning_url(year, "review"),
                requires_action=True,
            ).count(),
            2,
        )

        repeat_response = self.client.post(
            reverse("schedule_department_review_start", args=[year]),
            {"next": schedule_planning_url(year, "review")},
        )
        self.assertRedirects(repeat_response, schedule_planning_url(year, "review"))
        self.assertEqual(VacationScheduleDepartmentApproval.objects.filter(schedule=schedule).count(), 2)
        self.assertEqual(Notification.objects.filter(event_type=Notification.TYPE_SCHEDULE_REVIEW_REQUESTED).count(), 2)

    def test_sent_department_review_still_shows_draft_on_planning_page(self):
        year = self._year()
        schedule = self._create_ready_draft_schedule(year)
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_department_review_start", args=[year]))

        response = self.client.get(reverse("schedule_planning", args=[year]), {"stage": "draft"})

        self.assertEqual(response.status_code, 200)
        schedule.refresh_from_db()
        self.assertEqual(schedule.status, VacationSchedule.STATUS_DEPARTMENT_REVIEW)
        self.assertTrue(response.context["draft_status"]["exists"])
        self.assertTrue(response.context["draft_status"]["sent_to_review"])
        self.assertFalse(response.context["draft_is_editable"])
        self.assertTrue(response.context["draft_sent_to_review"])
        self.assertGreater(response.context["draft_summary"]["placed"], 0)
        self.assertContains(response, "Черновик отправлен")
        self.assertContains(response, "Открыть черновик")
        self.assertNotContains(response, "Черновик не создан")

    def test_sent_department_review_draft_detail_is_read_only_but_visible(self):
        year = self._year()
        self._create_ready_draft_schedule(year)
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_department_review_start", args=[year]))

        response = self.client.get(reverse("schedule_draft_detail", args=[year]))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["draft_exists"])
        self.assertFalse(response.context["draft_is_editable"])
        self.assertTrue(response.context["draft_sent_to_review"])
        self.assertGreater(response.context["draft_summary"]["placed"], 0)
        self.assertContains(response, "Черновик отправлен на проверку отделов")
        self.assertContains(response, "Данные сохранены и доступны для просмотра")
        self.assertNotContains(response, "Черновик еще не создан")

    def test_non_hr_cannot_send_schedule_to_department_review(self):
        year = self._year()
        schedule = self._create_ready_draft_schedule(year)
        self.client.force_login(self.department_head.user)

        response = self.client.post(reverse("schedule_department_review_start", args=[year]))

        self.assertEqual(response.status_code, 302)
        schedule.refresh_from_db()
        self.assertEqual(schedule.status, VacationSchedule.STATUS_DRAFT)
        self.assertFalse(VacationScheduleDepartmentApproval.objects.filter(schedule=schedule).exists())

    def test_department_review_start_blocks_manual_conflict_and_active_job(self):
        year = self._year()
        schedule = VacationSchedule.objects.create(
            year=year,
            status=VacationSchedule.STATUS_DRAFT,
            created_by=self.hr_employee,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(reverse("schedule_department_review_start", args=[year]))

        self.assertEqual(response.status_code, 302)
        schedule.refresh_from_db()
        self.assertEqual(schedule.status, VacationSchedule.STATUS_DRAFT)
        self.assertFalse(VacationScheduleDepartmentApproval.objects.filter(schedule=schedule).exists())

        schedule.delete()
        schedule = self._create_ready_draft_schedule(year)
        VacationScheduleAutoPlaceJob.objects.create(
            token="review-auto-token",
            year=year,
            schedule=schedule,
            actor=self.hr_employee,
            status=VacationScheduleAutoPlaceJob.STATUS_RUNNING,
        )
        response = self.client.post(reverse("schedule_department_review_start", args=[year]))
        schedule.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(schedule.status, VacationSchedule.STATUS_DRAFT)
        self.assertFalse(VacationScheduleDepartmentApproval.objects.filter(schedule=schedule).exists())

        VacationScheduleAutoPlaceJob.objects.filter(schedule=schedule).update(
            status=VacationScheduleAutoPlaceJob.STATUS_SUCCEEDED
        )
        generation_run = VacationScheduleGenerationRun.objects.create(
            schedule=schedule,
            year=year,
            mode=VacationScheduleGenerationRun.MODE_HYBRID,
            status=VacationScheduleGenerationRun.STATUS_COMPLETED,
            actor=self.hr_employee,
        )
        conflict_item = schedule.items.first()
        conflict_candidate = VacationScheduleCandidate.objects.create(
            generation_run=generation_run,
            schedule=schedule,
            employee=conflict_item.employee,
            start_date=conflict_item.start_date,
            end_date=conflict_item.end_date,
            chargeable_days=conflict_item.chargeable_days,
            kind=VacationScheduleCandidate.KIND_AUTO,
            passed_hard_rules=True,
            risk_level=VacationScheduleItem.RISK_HIGH,
            features={"risk_is_conflict": True},
            decision=VacationScheduleCandidate.DECISION_SELECTED,
            decision_rank=1,
        )
        conflict_item.selected_candidate = conflict_candidate
        conflict_item.save(update_fields=["selected_candidate"])

        response = self.client.post(reverse("schedule_department_review_start", args=[year]))

        schedule.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(schedule.status, VacationSchedule.STATUS_DRAFT)
        self.assertFalse(VacationScheduleDepartmentApproval.objects.filter(schedule=schedule).exists())

    def test_schedule_planning_shows_department_review_start_button_for_ready_draft(self):
        year = self._year()
        self._create_ready_draft_schedule(year)
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("schedule_planning", args=[year]), {"stage": "draft"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.context["can_start_department_review"],
            response.context["department_review_start_block_reason"],
        )
        self.assertContains(response, "Отправить на проверку отделов")
        self.assertContains(response, reverse("schedule_department_review_start", args=[year]))

    def test_schedule_draft_detail_shows_department_review_start_button_for_ready_draft(self):
        year = self._year()
        self._create_ready_draft_schedule(year)
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("schedule_draft_detail", args=[year]))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_start_department_review"])
        self.assertContains(response, "Отправить на проверку отделов")
        self.assertContains(response, reverse("schedule_department_review_start", args=[year]))
        self.assertContains(response, "schedule-review-submit-button")

    def test_department_head_sees_only_own_department_review_actions(self):
        year = self._year()
        schedule = self._create_ready_draft_schedule(year)
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_department_review_start", args=[year]))
        self.client.force_login(self.department_head.user)

        response = self.client.get(reverse("schedule_planning", args=[year]), {"stage": "review"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "schedule_planning")
        self.assertEqual(len(response.context["review_summary"]["rows"]), 1)
        row = response.context["review_summary"]["rows"][0]
        self.assertEqual(row["department"], self.engineering)
        self.assertTrue(row["can_review"])
        self.assertContains(response, self.engineering.name)
        self.assertContains(response, "Согласовать отдел")
        self.assertContains(response, "Вернуть на доработку")
        self.assertContains(response, "schedule-planning-review-row__top")
        self.assertContains(response, "schedule-planning-review-actions")

    def test_department_head_can_approve_own_department_review(self):
        year = self._year()
        schedule = self._create_ready_draft_schedule(year)
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_department_review_start", args=[year]))
        approval = VacationScheduleDepartmentApproval.objects.get(schedule=schedule, department=self.engineering)
        notification = Notification.objects.get(
            dedupe_key=f"{Notification.TYPE_SCHEDULE_REVIEW_REQUESTED}:department:{schedule.id}:{self.engineering.id}"
        )
        self.client.force_login(self.department_head.user)

        response = self.client.post(
            reverse("schedule_department_review_approve", args=[year, approval.id]),
            {"next": schedule_planning_url(year, "review")},
        )

        self.assertRedirects(response, schedule_planning_url(year, "review"))
        approval.refresh_from_db()
        notification.refresh_from_db()
        self.assertEqual(approval.status, VacationScheduleDepartmentApproval.STATUS_APPROVED)
        self.assertIsNotNone(approval.approved_at)
        self.assertEqual(notification.status, Notification.STATUS_DONE)

    def test_department_head_can_return_own_department_with_comment(self):
        year = self._year()
        schedule = self._create_ready_draft_schedule(year)
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_department_review_start", args=[year]))
        approval = VacationScheduleDepartmentApproval.objects.get(schedule=schedule, department=self.engineering)
        self.client.force_login(self.department_head.user)

        response = self.client.post(
            reverse("schedule_department_review_return", args=[year, approval.id]),
            {"comment": "Нужно поменять даты механиков.", "next": schedule_planning_url(year, "review")},
        )

        self.assertRedirects(response, schedule_planning_url(year, "review"))
        approval.refresh_from_db()
        self.assertEqual(approval.status, VacationScheduleDepartmentApproval.STATUS_REJECTED)
        self.assertEqual(approval.comment, "Нужно поменять даты механиков.")
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.hr_employee,
                status=Notification.STATUS_NEW,
                requires_action=True,
                title="Отдел вернул график на доработку",
                action_url=reverse("schedule_department_review_rework", args=[year, approval.id]),
            ).exists()
        )

    def test_hr_sees_rework_action_for_returned_department(self):
        year = self._year()
        _, approval = self._return_department_for_rework(year)
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("schedule_planning", args=[year]), {"stage": "review"})

        self.assertEqual(response.status_code, 200)
        row = next(item for item in response.context["review_summary"]["rows"] if item["department"] == self.engineering)
        self.assertTrue(row["can_rework"])
        self.assertContains(response, "Доработать отдел")
        self.assertContains(response, "Повторно отправить отдел")
        self.assertContains(response, reverse("schedule_department_review_rework", args=[year, approval.id]))
        self.assertContains(response, "Нужно поменять даты механиков.")

    def test_hr_opens_rework_page_for_returned_department_only(self):
        year = self._year()
        _, approval = self._return_department_for_rework(year)
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("schedule_department_review_rework", args=[year, approval.id]))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["draft_rework_mode"])
        self.assertEqual(response.context["department_rework_name"], self.engineering.name)
        self.assertContains(response, "Доработка отдела")
        self.assertContains(response, "Изменить пакет")
        self.assertContains(response, f'action="{reverse("schedule_department_review_rework", args=[year, approval.id])}"')
        self.assertContains(response, self.employee.full_name)
        self.assertNotContains(response, self.hr_employee.full_name)

        search_response = self.client.get(
            reverse("schedule_department_review_rework", args=[year, approval.id]),
            {"q": self.employee.last_name},
        )
        self.assertEqual(search_response.status_code, 200)
        self.assertTrue(search_response.context["draft_rework_mode"])
        self.assertEqual(search_response.context["sidebar_section"], "schedule_planning")
        self.assertContains(search_response, self.employee.full_name)
        self.assertNotContains(search_response, self.hr_employee.full_name)

        self.client.force_login(self.department_head.user)
        denied = self.client.get(reverse("schedule_department_review_rework", args=[year, approval.id]))
        self.assertEqual(denied.status_code, 302)

    def test_rework_package_action_is_visible_on_every_employee_item(self):
        year = self._year()
        schedule = self._create_ready_draft_schedule(year)
        self._replace_employee_schedule_package(schedule, self.employee, [14, 14, 14, 10])
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_department_review_start", args=[year]))
        approval = VacationScheduleDepartmentApproval.objects.get(schedule=schedule, department=self.engineering)
        self.client.force_login(self.department_head.user)
        self.client.post(
            reverse("schedule_department_review_return", args=[year, approval.id]),
            {"comment": "Нужно поменять даты механиков.", "next": schedule_planning_url(year, "review")},
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("schedule_department_review_rework", args=[year, approval.id]))

        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertEqual(content.count(f'data-manual-employee-id="{self.employee.id}"'), 4)
        self.assertContains(response, 'data-manual-max-periods="4"', count=4)
        self.assertContains(response, "Вы заменяете весь пакет отпусков сотрудника за год")
        self.assertContains(response, "Текущий пакет: 4 период(а)")

    def test_rework_package_preview_requires_same_days(self):
        year = self._year()
        _, approval = self._return_department_for_rework(year)
        item = VacationScheduleItem.objects.filter(
            schedule=approval.schedule,
            employee=self.employee,
            status=VacationScheduleItem.STATUS_PLANNED,
        ).first()
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(
            reverse("schedule_department_review_rework_package_preview", args=[year, approval.id, self.employee.id]),
            data=json.dumps(
                {
                    "periods": [
                        {
                            "start_date": date(year, 9, 15).isoformat(),
                            "end_date": date(year, 9, 15).isoformat(),
                        }
                    ]
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["can_submit"])
        self.assertIn(f"ровно {int(item.chargeable_days)}", payload["message"])

    def test_rework_suggestions_treat_current_package_as_replacement(self):
        year = self._year()
        schedule = self._create_ready_draft_schedule(year)
        self._replace_employee_schedule_package(schedule, self.employee, [14, 14, 14, 10])
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_department_review_start", args=[year]))
        approval = VacationScheduleDepartmentApproval.objects.get(schedule=schedule, department=self.engineering)
        self.client.force_login(self.department_head.user)
        self.client.post(
            reverse("schedule_department_review_return", args=[year, approval.id]),
            {"comment": "Нужно поменять даты механиков.", "next": schedule_planning_url(year, "review")},
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("schedule_department_review_rework_suggestions", args=[year, approval.id, self.employee.id])
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["max_periods"], 4)
        self.assertNotIn("Недостаточно отпускных прав", json.dumps(payload, ensure_ascii=False))

    def test_hr_replaces_rework_package_and_resubmits_department(self):
        year = self._year()
        schedule, approval = self._return_department_for_rework(year)
        old_item = VacationScheduleItem.objects.get(
            schedule=schedule,
            employee=self.employee,
            status=VacationScheduleItem.STATUS_PLANNED,
        )
        target_days = int(old_item.chargeable_days)
        new_start = date(year, 9, 15)
        new_end = self._end_date_for_chargeable_days(new_start, target_days)
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(
            reverse("schedule_department_review_rework_place", args=[year, approval.id, self.employee.id]),
            {
                "periods_json": json.dumps(
                    [
                        {
                            "start_date": new_start.isoformat(),
                            "end_date": new_end.isoformat(),
                        }
                    ]
                ),
                "next": reverse("schedule_department_review_rework", args=[year, approval.id]),
            },
        )

        self.assertRedirects(response, reverse("schedule_department_review_rework", args=[year, approval.id]))
        old_item.refresh_from_db()
        self.assertEqual(old_item.status, VacationScheduleItem.STATUS_CANCELLED)
        new_item = VacationScheduleItem.objects.get(
            schedule=schedule,
            employee=self.employee,
            status=VacationScheduleItem.STATUS_PLANNED,
            start_date=new_start,
            end_date=new_end,
        )
        self.assertEqual(new_item.source, VacationScheduleItem.SOURCE_MANUAL)
        self.assertIsNotNone(new_item.generation_run)
        self.assertIsNotNone(new_item.selected_candidate)
        self.assertTrue(new_item.ai_model_version)

        response = self.client.post(
            reverse("schedule_department_review_resubmit", args=[year, approval.id]),
            {"next": schedule_planning_url(year, "review")},
        )

        self.assertRedirects(response, schedule_planning_url(year, "review"))
        approval.refresh_from_db()
        self.assertEqual(approval.status, VacationScheduleDepartmentApproval.STATUS_PENDING)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.department_head,
                status=Notification.STATUS_NEW,
                requires_action=True,
                dedupe_key__startswith=f"{Notification.TYPE_SCHEDULE_REVIEW_REQUESTED}:department:{schedule.id}:{self.engineering.id}:resubmit:",
            ).exists()
        )
        self.client.force_login(self.department_head.user)
        review_response = self.client.get(reverse("schedule_planning", args=[year]), {"stage": "review"})
        self.assertContains(review_response, "Согласовать отдел")
        self.assertContains(review_response, "Вернуть на доработку")

    def test_hr_can_submit_schedule_to_enterprise_review_after_departments_approve(self):
        year = self._year()
        schedule = self._approve_all_departments_for_final(year)
        self.client.force_login(self.hr_employee.user)

        page_response = self.client.get(reverse("schedule_planning", args=[year]), {"stage": "final"})
        self.assertEqual(page_response.status_code, 200)
        self.assertTrue(page_response.context["can_submit_enterprise_review"])
        self.assertContains(page_response, "Отправить на финальное утверждение")
        self.assertNotContains(page_response, "Уполномоченное лицо")

        response = self.client.post(
            reverse("schedule_final_review_submit", args=[year]),
            {"next": schedule_planning_url(year, "final")},
        )

        self.assertRedirects(response, schedule_planning_url(year, "final"))
        approval = VacationScheduleEnterpriseApproval.objects.get(schedule=schedule)
        self.assertEqual(approval.status, VacationScheduleEnterpriseApproval.STATUS_PENDING)
        self.assertEqual(approval.enterprise_head, self.enterprise_head)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.enterprise_head,
                status=Notification.STATUS_NEW,
                requires_action=True,
                action_url=schedule_planning_url(year, "final"),
                dedupe_key__startswith=f"{Notification.TYPE_SCHEDULE_REVIEW_REQUESTED}:enterprise:{schedule.id}:{self.enterprise_head.id}:submit:",
            ).exists()
        )

    def test_enterprise_final_submit_is_blocked_until_departments_approve(self):
        year = self._year()
        schedule = self._create_ready_draft_schedule(year)
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_department_review_start", args=[year]))

        response = self.client.post(
            reverse("schedule_final_review_submit", args=[year]),
            {"next": schedule_planning_url(year, "final")},
        )

        self.assertRedirects(response, schedule_planning_url(year, "final"))
        self.assertFalse(VacationScheduleEnterpriseApproval.objects.filter(schedule=schedule).exists())
        schedule.refresh_from_db()
        self.assertEqual(schedule.status, VacationSchedule.STATUS_DEPARTMENT_REVIEW)

    def test_enterprise_head_approves_final_schedule(self):
        year = self._year()
        schedule = self._approve_all_departments_for_final(year)
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_final_review_submit", args=[year]))
        approval = VacationScheduleEnterpriseApproval.objects.get(schedule=schedule)
        notification = Notification.objects.filter(
            recipient=self.enterprise_head,
            dedupe_key__startswith=f"{Notification.TYPE_SCHEDULE_REVIEW_REQUESTED}:enterprise:{schedule.id}:",
        ).latest("created_at")

        self.client.force_login(self.enterprise_head.user)
        response = self.client.post(
            reverse("schedule_final_review_approve", args=[year, approval.id]),
            {"next": schedule_planning_url(year, "final")},
        )

        self.assertRedirects(response, schedule_planning_url(year, "final"))
        approval.refresh_from_db()
        schedule.refresh_from_db()
        notification.refresh_from_db()
        self.assertEqual(approval.status, VacationScheduleEnterpriseApproval.STATUS_APPROVED)
        self.assertEqual(schedule.status, VacationSchedule.STATUS_APPROVED)
        self.assertEqual(schedule.approved_by, self.enterprise_head)
        self.assertIsNotNone(schedule.approved_at)
        self.assertFalse(schedule.items.filter(status=VacationScheduleItem.STATUS_PLANNED).exists())
        self.assertEqual(schedule.items.filter(status=VacationScheduleItem.STATUS_APPROVED).count(), schedule.items.count())
        self.assertEqual(notification.status, Notification.STATUS_DONE)
        expected_recipients = set(
            schedule.items.filter(
                status=VacationScheduleItem.STATUS_APPROVED,
                employee__is_active_employee=True,
            )
            .exclude(employee__role__in=Employees.SERVICE_ROLES)
            .values_list("employee_id", flat=True)
            .distinct()
        )
        approved_notifications = Notification.objects.filter(
            event_type=Notification.TYPE_SCHEDULE_APPROVED,
            dedupe_key__startswith=f"{Notification.TYPE_SCHEDULE_APPROVED}:{schedule.id}:",
        )
        self.assertEqual(approved_notifications.count(), len(expected_recipients))
        self.assertEqual(set(approved_notifications.values_list("recipient_id", flat=True)), expected_recipients)
        self.assertFalse(approved_notifications.filter(requires_action=True).exists())
        self.assertTrue(approved_notifications.filter(recipient=self.hr_employee).exists())
        self.assertTrue(approved_notifications.filter(recipient=self.department_head).exists())
        self.assertTrue(approved_notifications.filter(recipient=self.enterprise_head).exists())

    def test_approved_schedule_keeps_draft_stage_completed(self):
        year = self._year()
        schedule = self._approve_all_departments_for_final(year)
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_final_review_submit", args=[year]))
        approval = VacationScheduleEnterpriseApproval.objects.get(schedule=schedule)
        self.client.force_login(self.enterprise_head.user)
        self.client.post(reverse("schedule_final_review_approve", args=[year, approval.id]))

        self.client.force_login(self.hr_employee.user)
        response = self.client.get(reverse("schedule_planning", args=[year]), {"stage": "draft"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["draft_status"]["approved"])
        self.assertTrue(response.context["draft_approved"])
        self.assertGreater(response.context["draft_summary"]["placed"], 0)
        draft_stage = next(stage for stage in response.context["stages"] if stage["key"] == "draft")
        self.assertEqual(draft_stage["status"]["tone"], "ok")
        self.assertEqual(draft_stage["status"]["label"], "График утверждён")
        self.assertContains(response, "Этап черновика завершён")
        self.assertNotContains(response, "Черновик не создан")

    def test_non_enterprise_head_cannot_approve_final_schedule(self):
        year = self._year()
        schedule = self._approve_all_departments_for_final(year)
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_final_review_submit", args=[year]))
        approval = VacationScheduleEnterpriseApproval.objects.get(schedule=schedule)

        self.client.force_login(self.department_head.user)
        response = self.client.post(
            reverse("schedule_final_review_approve", args=[year, approval.id]),
            {"next": schedule_planning_url(year, "final")},
        )

        self.assertRedirects(response, schedule_planning_url(year, "final"))
        approval.refresh_from_db()
        schedule.refresh_from_db()
        self.assertEqual(approval.status, VacationScheduleEnterpriseApproval.STATUS_PENDING)
        self.assertEqual(schedule.status, VacationSchedule.STATUS_DEPARTMENT_REVIEW)

    def test_enterprise_return_allows_hr_department_rework_and_final_resubmit(self):
        year = self._year()
        schedule = self._approve_all_departments_for_final(year)
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_final_review_submit", args=[year]))
        enterprise_approval = VacationScheduleEnterpriseApproval.objects.get(schedule=schedule)

        self.client.force_login(self.enterprise_head.user)
        response = self.client.post(
            reverse("schedule_final_review_return", args=[year, enterprise_approval.id]),
            {
                "comment": "Проверьте даты в производстве.",
                "next": schedule_planning_url(year, "final"),
            },
        )

        self.assertRedirects(response, schedule_planning_url(year, "final"))
        enterprise_approval.refresh_from_db()
        self.assertEqual(enterprise_approval.status, VacationScheduleEnterpriseApproval.STATUS_REJECTED)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.hr_employee,
                status=Notification.STATUS_NEW,
                requires_action=True,
                title="График возвращён с финального согласования",
                action_url=schedule_planning_url(year, "final"),
            ).exists()
        )

        self.client.force_login(self.hr_employee.user)
        final_response = self.client.get(reverse("schedule_planning", args=[year]), {"stage": "final"})
        self.assertEqual(final_response.status_code, 200)
        self.assertContains(final_response, "Проверьте даты в производстве.")
        self.assertContains(final_response, "Доработать отдел")

        department_approval = VacationScheduleDepartmentApproval.objects.get(
            schedule=schedule,
            department=self.engineering,
        )
        response = self.client.post(
            reverse(
                "schedule_final_review_open_department_rework",
                args=[year, enterprise_approval.id, self.engineering.id],
            ),
            {"next": schedule_planning_url(year, "final")},
        )

        self.assertRedirects(response, reverse("schedule_department_review_rework", args=[year, department_approval.id]))
        department_approval.refresh_from_db()
        self.assertEqual(department_approval.status, VacationScheduleDepartmentApproval.STATUS_REJECTED)
        self.assertIn("Проверьте даты в производстве.", department_approval.comment)

        response = self.client.post(
            reverse("schedule_department_review_resubmit", args=[year, department_approval.id]),
            {"next": schedule_planning_url(year, "review")},
        )
        self.assertRedirects(response, schedule_planning_url(year, "review"))

        self.client.force_login(self.department_head.user)
        self.client.post(
            reverse("schedule_department_review_approve", args=[year, department_approval.id]),
            {"next": schedule_planning_url(year, "review")},
        )

        self.client.force_login(self.hr_employee.user)
        response = self.client.post(
            reverse("schedule_final_review_submit", args=[year]),
            {"next": schedule_planning_url(year, "final")},
        )

        self.assertRedirects(response, schedule_planning_url(year, "final"))
        enterprise_approval.refresh_from_db()
        self.assertEqual(enterprise_approval.status, VacationScheduleEnterpriseApproval.STATUS_PENDING)
        self.assertEqual(enterprise_approval.comment, "")

    def test_department_head_cannot_review_foreign_department(self):
        year = self._year()
        schedule = self._create_ready_draft_schedule(year)
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_department_review_start", args=[year]))
        approval = VacationScheduleDepartmentApproval.objects.get(schedule=schedule, department=self.engineering)
        self.client.force_login(self.foreign_department_head.user)

        response = self.client.post(reverse("schedule_department_review_approve", args=[year, approval.id]))

        self.assertEqual(response.status_code, 302)
        approval.refresh_from_db()
        self.assertEqual(approval.status, VacationScheduleDepartmentApproval.STATUS_PENDING)

    def test_full_calendar_opened_from_planning_keeps_planning_sidebar_active(self):
        year = self._year()
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("calendar"),
            {
                "view": "year",
                "year": year,
                "from": "schedule_planning",
                "back_url": reverse("schedule_planning", args=[year]),
                "back_label": "К планированию",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "schedule_planning")
        planning_link = self._sidebar_link_html(response, "schedule-planning")
        calendar_link = self._sidebar_link_html(response, "calendar")
        self.assertIn('aria-current="page"', planning_link)
        self.assertIn('data-sidebar-default-href=', planning_link)
        self.assertNotIn('aria-current="page"', calendar_link)

    def test_readiness_from_planning_preserves_planning_context_links(self):
        year = self._year()
        self._start_collection()
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("preference_collection_readiness", args=[year]),
            {
                "from": "schedule_planning",
                "back_url": f"{reverse('schedule_planning', args=[year])}?stage=collection",
                "back_label": "К планированию",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "schedule_planning")
        self.assertContains(response, "from=schedule_planning")
        self.assertContains(response, "back_url=")

    def test_schedule_planning_current_redirects_to_planning_year(self):
        year = self._year()
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("schedule_planning_current"))

        self.assertRedirects(response, reverse("schedule_planning", args=[year]))

    def test_schedule_planning_current_uses_active_planning_cycle(self):
        year = self._year()
        active_year = year + 1
        VacationPlanningCycle.objects.create(year=active_year, status=VacationPlanningCycle.STATUS_ACTIVE)
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("schedule_planning_current"))

        self.assertRedirects(response, reverse("schedule_planning", args=[active_year]))
        calendar_response = self.client.get(reverse("calendar"))
        planning_link = self._sidebar_link_html(calendar_response, "schedule-planning")
        self.assertIn(reverse("schedule_planning", args=[active_year]), planning_link)

    def test_hr_can_start_next_planning_cycle_after_current_schedule_is_approved(self):
        year = self._year()
        VacationPlanningCycle.objects.create(year=year, status=VacationPlanningCycle.STATUS_ACTIVE)
        VacationSchedule.objects.create(
            year=year,
            status=VacationSchedule.STATUS_APPROVED,
            created_by=self.hr_employee,
            approved_by=self.enterprise_head,
            approved_at=timezone.now(),
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(reverse("schedule_planning_start_next", args=[year]))

        self.assertRedirects(response, reverse("schedule_planning", args=[year + 1]))
        self.assertEqual(
            VacationPlanningCycle.objects.get(year=year).status,
            VacationPlanningCycle.STATUS_CLOSED,
        )
        self.assertEqual(
            VacationPlanningCycle.objects.get(year=year + 1).status,
            VacationPlanningCycle.STATUS_ACTIVE,
        )
        self.assertFalse(VacationPreferenceCollection.objects.filter(year=year + 1).exists())
        self.assertFalse(VacationSchedule.objects.filter(year=year + 1).exists())

    def test_cannot_start_next_planning_cycle_before_current_schedule_is_approved(self):
        year = self._year()
        VacationPlanningCycle.objects.create(year=year, status=VacationPlanningCycle.STATUS_ACTIVE)
        VacationSchedule.objects.create(year=year, status=VacationSchedule.STATUS_DRAFT, created_by=self.hr_employee)
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(reverse("schedule_planning_start_next", args=[year]))

        self.assertRedirects(response, reverse("schedule_planning", args=[year]))
        self.assertFalse(VacationPlanningCycle.objects.filter(year=year + 1).exists())
        self.assertEqual(
            VacationPlanningCycle.objects.get(year=year).status,
            VacationPlanningCycle.STATUS_ACTIVE,
        )

    def test_closed_planning_year_is_read_only_for_collection_actions(self):
        year = self._year()
        VacationPlanningCycle.objects.create(year=year, status=VacationPlanningCycle.STATUS_CLOSED, closed_at=timezone.now())
        VacationPlanningCycle.objects.create(year=year + 1, status=VacationPlanningCycle.STATUS_ACTIVE)
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(
            reverse("preferences_collection_start"),
            {
                "year": year,
                "deadline": self._deadline().isoformat(),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(VacationPreferenceCollection.objects.filter(year=year).exists())

    def test_regular_employee_cannot_access_schedule_planning_or_sidebar(self):
        year = self._year()
        self.client.force_login(self.employee.user)

        response = self.client.get(reverse("schedule_planning", args=[year]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("calendar"))

        calendar_response = self.client.get(reverse("calendar"))
        self.assertNotContains(calendar_response, 'data-sidebar-key="schedule-planning"')

    def test_department_head_opens_schedule_planning_for_pending_review(self):
        year = self._year()
        VacationPlanningCycle.objects.create(year=year + 1, status=VacationPlanningCycle.STATUS_ACTIVE)
        schedule = VacationSchedule.objects.create(
            year=year,
            status=VacationSchedule.STATUS_DEPARTMENT_REVIEW,
            created_by=self.hr_employee,
        )
        VacationScheduleDepartmentApproval.objects.create(
            schedule=schedule,
            department=self.engineering,
            department_head=self.department_head,
            status=VacationScheduleDepartmentApproval.STATUS_PENDING,
        )
        self.client.force_login(self.department_head.user)

        response = self.client.get(reverse("schedule_planning", args=[year]), {"stage": "review"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "schedule_planning")
        self.assertEqual(response.context["selected_stage"], "review")
        self.assertContains(response, "Проверка отделов")
        self.assertContains(response, self.engineering.name)
        self.assertContains(response, 'data-sidebar-key="schedule-planning"')

    def test_enterprise_head_can_view_readiness_without_finish_action(self):
        year = self._year()
        self._start_collection()
        self.client.force_login(self.enterprise_head.user)

        response = self.client.get(reverse("preference_collection_readiness", args=[year]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Готовность сбора")
        self.assertNotContains(response, "Завершить сбор")

    def test_non_hr_and_non_enterprise_head_cannot_view_readiness(self):
        year = self._year()
        self._start_collection()

        self.client.force_login(self.employee.user)
        response = self.client.get(reverse("preference_collection_readiness", args=[year]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("calendar"))

        self.client.force_login(self.authorized_person.user)
        response = self.client.get(reverse("preference_collection_readiness", args=[year]))
        self.assertRedirects(response, reverse("applications"))

    def test_readiness_filters_and_search_use_employee_preference_state(self):
        year = self._year()
        self._start_collection()
        VacationPreference.objects.filter(employee=self.employee, year=year).delete()
        VacationPreference.objects.bulk_create(
            [
                VacationPreference(
                    employee=self.employee,
                    year=year,
                    priority=VacationPreference.PRIORITY_PRIMARY,
                    start_date=date(year, 6, 1),
                    end_date=date(year, 6, 14),
                    status=VacationPreference.STATUS_FILLED,
                    comment="Хочу летом.",
                ),
                VacationPreference(
                    employee=self.employee,
                    year=year,
                    priority=VacationPreference.PRIORITY_BACKUP,
                    start_date=date(year, 9, 1),
                    end_date=date(year, 9, 14),
                    status=VacationPreference.STATUS_FILLED,
                    comment="Хочу летом.",
                ),
            ]
        )
        VacationPreference.objects.filter(employee=self.department_head, year=year).delete()
        VacationPreference.objects.bulk_create(
            [
                VacationPreference(
                    employee=self.department_head,
                    year=year,
                    priority=VacationPreference.PRIORITY_PRIMARY,
                    status=VacationPreference.STATUS_SKIPPED,
                    comment="Пожеланий нет.",
                ),
                VacationPreference(
                    employee=self.department_head,
                    year=year,
                    priority=VacationPreference.PRIORITY_BACKUP,
                    status=VacationPreference.STATUS_SKIPPED,
                    comment="Пожеланий нет.",
                ),
            ]
        )
        self.client.force_login(self.hr_employee.user)

        filled_response = self.client.get(
            reverse("preference_collection_readiness", args=[year]),
            {"status": VacationPreference.STATUS_FILLED},
        )
        filled_ids = [row["employee"].id for row in filled_response.context["rows"]]
        self.assertIn(self.employee.id, filled_ids)
        self.assertNotIn(self.department_head.id, filled_ids)
        filled_row = next(row for row in filled_response.context["rows"] if row["employee"].id == self.employee.id)
        self.assertEqual(filled_row["role_variant"], "employee")
        self.assertEqual(filled_row["role_icon"], "person")

        skipped_response = self.client.get(
            reverse("preference_collection_readiness", args=[year]),
            {"status": VacationPreference.STATUS_SKIPPED},
        )
        skipped_ids = [row["employee"].id for row in skipped_response.context["rows"]]
        self.assertIn(self.department_head.id, skipped_ids)
        self.assertNotIn(self.employee.id, skipped_ids)

        pending_response = self.client.get(
            reverse("preference_collection_readiness", args=[year]),
            {"status": VacationPreference.STATUS_PENDING},
        )
        pending_ids = [row["employee"].id for row in pending_response.context["rows"]]
        self.assertNotIn(self.employee.id, pending_ids)
        self.assertNotIn(self.department_head.id, pending_ids)

        search_response = self.client.get(
            reverse("preference_collection_readiness", args=[year]),
            {"q": self.employee.last_name},
        )
        search_ids = [row["employee"].id for row in search_response.context["rows"]]
        self.assertIn(self.employee.id, search_ids)

    def test_start_creates_pending_preferences_and_notifications(self):
        year = self._year()
        self._start_collection()

        self.assertTrue(
            VacationPreference.objects.filter(
                year=year,
                employee=self.employee,
                priority=VacationPreference.PRIORITY_PRIMARY,
                status=VacationPreference.STATUS_PENDING,
            ).exists()
        )
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.employee,
                event_type=Notification.TYPE_PREFERENCES_COLLECTION_STARTED,
                requires_action=True,
                status=Notification.STATUS_NEW,
                action_url=reverse("vacation_preferences", args=[year]),
            ).exists()
        )

    def test_demo_autofill_fills_majority_but_leaves_pending_tasks(self):
        year = self._year()
        demo_first_employee = Employees.objects.create(
            last_name="Первый",
            first_name="Сотрудник",
            middle_name="Демо",
            login="employ_1",
            position="Специалист",
            employee_position=self.engineering_position,
            department=self.engineering,
            date_joined=self.today - timedelta(days=420),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        self._start_collection(demo_autofill=True)

        eligible_count = Employees.objects.exclude(role__in=Employees.SERVICE_ROLES).count()
        filled_count = (
            VacationPreference.objects.filter(year=year, status=VacationPreference.STATUS_FILLED)
            .values("employee_id")
            .distinct()
            .count()
        )
        pending_count = (
            VacationPreference.objects.filter(year=year, status=VacationPreference.STATUS_PENDING)
            .values("employee_id")
            .distinct()
            .count()
        )
        skipped_count = (
            VacationPreference.objects.filter(year=year, status=VacationPreference.STATUS_SKIPPED)
            .values("employee_id")
            .distinct()
            .count()
        )

        self.assertGreaterEqual(filled_count, eligible_count // 2)
        self.assertGreater(pending_count, 0)
        filled_policies = set(
            VacationPreference.objects.filter(
                year=year,
                status=VacationPreference.STATUS_FILLED,
                priority=VacationPreference.PRIORITY_PRIMARY,
            ).values_list("remainder_policy", flat=True)
        )
        self.assertIn(VacationPreference.REMAINDER_DEFER, filled_policies)
        self.assertIn(VacationPreference.REMAINDER_APPROVAL, filled_policies)
        self.assertIn(VacationPreference.REMAINDER_AUTO, filled_policies)
        backup_preferences = VacationPreference.objects.filter(
            year=year,
            status=VacationPreference.STATUS_FILLED,
            priority=VacationPreference.PRIORITY_BACKUP,
            start_date__isnull=False,
            end_date__isnull=False,
        )
        self.assertTrue(backup_preferences.exists())
        for preference in backup_preferences:
            with self.subTest(employee=preference.employee_id):
                self.assertGreaterEqual((preference.end_date - preference.start_date).days + 1, 14)
        self.assertEqual(
            list(
                VacationPreference.objects.filter(employee=demo_first_employee, year=year)
                .order_by("priority")
                .values_list("status", flat=True)
            ),
            [VacationPreference.STATUS_PENDING, VacationPreference.STATUS_PENDING],
        )
        self.assertEqual(
            Notification.objects.filter(
                event_type=Notification.TYPE_PREFERENCES_COLLECTION_STARTED,
                status=Notification.STATUS_NEW,
            ).count(),
            pending_count,
        )
        self.assertEqual(
            Notification.objects.filter(
                event_type=Notification.TYPE_PREFERENCES_COLLECTION_STARTED,
            )
            .values("recipient_id")
            .distinct()
            .count(),
            eligible_count,
        )
        self.assertEqual(
            Notification.objects.filter(
                event_type=Notification.TYPE_PREFERENCES_COLLECTION_STARTED,
                status=Notification.STATUS_DONE,
            ).count(),
            filled_count + skipped_count,
        )

    def test_restarting_collection_refreshes_previous_preferences(self):
        year = self._year()
        self._start_collection()
        collection = VacationPreferenceCollection.objects.get(year=year)

        self.client.force_login(self.employee.user)
        self.client.post(
            reverse("vacation_preferences", args=[year]),
            {
                "primary_start_date": date(year, 7, 1).isoformat(),
                "primary_end_date": date(year, 7, 14).isoformat(),
                "backup_start_date": date(year, 9, 1).isoformat(),
                "backup_end_date": date(year, 9, 14).isoformat(),
                "comment": "Семейная поездка.",
            },
        )

        self.client.force_login(self.hr_employee.user)
        self.client.post(
            reverse("preferences_collection_start"),
            {
                "year": collection.year,
                "deadline": self._deadline().isoformat(),
                "demo_autofill": "on",
            },
        )

        primary = VacationPreference.objects.get(
            employee=self.employee,
            year=year,
            priority=VacationPreference.PRIORITY_PRIMARY,
        )
        self.assertNotEqual(primary.comment, "Семейная поездка.")
        self.assertIn(
            primary.status,
            {
                VacationPreference.STATUS_FILLED,
                VacationPreference.STATUS_PENDING,
                VacationPreference.STATUS_SKIPPED,
            },
        )
        self.assertEqual(
            VacationPreference.objects.filter(year=year).count(),
            Employees.objects.exclude(role__in=Employees.SERVICE_ROLES).count() * 2,
        )
        pending_count = (
            VacationPreference.objects.filter(year=year, status=VacationPreference.STATUS_PENDING)
            .values("employee_id")
            .distinct()
            .count()
        )
        self.assertEqual(
            Notification.objects.filter(
                event_type=Notification.TYPE_PREFERENCES_COLLECTION_STARTED,
                status=Notification.STATUS_NEW,
            ).count(),
            pending_count,
        )

    def test_start_without_demo_resets_old_seed_preferences_to_pending(self):
        year = self._year()
        VacationPreference.objects.create(
            employee=self.employee,
            year=year,
            priority=VacationPreference.PRIORITY_PRIMARY,
            start_date=date(year, 6, 1),
            end_date=date(year, 6, 14),
            status=VacationPreference.STATUS_FILLED,
            created_automatically=True,
        )
        VacationPreference.objects.create(
            employee=self.employee,
            year=year,
            priority=VacationPreference.PRIORITY_BACKUP,
            start_date=date(year, 8, 1),
            end_date=date(year, 8, 14),
            status=VacationPreference.STATUS_FILLED,
            created_automatically=True,
        )

        self._start_collection(demo_autofill=False)

        self.assertEqual(
            VacationPreference.objects.filter(
                employee=self.employee,
                year=year,
                status=VacationPreference.STATUS_PENDING,
            ).count(),
            2,
        )
        summary = build_preference_collection_summary(year)
        self.assertEqual(summary["ready"], 0)
        self.assertEqual(summary["answered"], 0)
        self.assertEqual(summary["pending"], summary["total"])
        self.assertEqual(summary["not_answered"], summary["total"])
        self.assertEqual(summary["no_preferences"], 0)

    def test_preference_page_hides_paid_leave_hint_after_waiting_period(self):
        year = self._year()
        self._start_collection()
        self.client.force_login(self.employee.user)

        response = self.client.get(reverse("vacation_preferences", args=[year]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Право на оплачиваемый отпуск")
        self.assertContains(response, 'data-preferences-form')
        self.assertContains(response, f'data-collection-year="{year}"')
        self.assertContains(response, 'data-preference-state="pending"')
        self.assertContains(response, 'data-calendar-return-link')
        self.assertContains(response, f"Сбор {year}-го открыт")
        self.assertContains(response, "Доступно к планированию")
        self.assertContains(response, "Обязательно закрыть")
        self.assertContains(response, "К планированию")
        self.assertContains(response, "vacation-preferences.js")
        self.assertEqual(response.context["sidebar_section"], "calendar")
        self.assertTrue(response.context["page_header_back_link"]["use_calendar_memory"])

    def test_preference_page_shows_paid_leave_hint_for_newcomer(self):
        year = self._year()
        self._start_collection()
        newcomer = Employees.objects.create(
            last_name="Новичков",
            first_name="Павел",
            middle_name="Игоревич",
            login="newcomer-preference-user",
            position="Специалист",
            employee_position=self.engineering_position,
            department=self.engineering,
            date_joined=timezone.localdate(),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )
        sync_employee_user(newcomer, raw_password="newcomer-pass")
        self.client.force_login(newcomer.user)

        response = self.client.get(reverse("vacation_preferences", args=[year]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Право на оплачиваемый отпуск")
        self.assertContains(response, add_months_safe(timezone.localdate(), 6).strftime("%d.%m.%Y"))

    def test_preference_ai_preview_compares_primary_and_backup(self):
        year = self._year()
        self._start_collection()
        self.client.force_login(self.employee.user)

        response = self.client.get(
            reverse("vacation_preferences_ai_preview", args=[year]),
            {
                "primary_start_date": date(year, 6, 1).isoformat(),
                "primary_end_date": date(year, 6, 14).isoformat(),
                "backup_start_date": date(year, 8, 1).isoformat(),
                "backup_end_date": date(year, 8, 14).isoformat(),
                "remainder_policy": VacationPreference.REMAINDER_AUTO,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn(payload["winner"], {"primary", "backup", "tie"})
        self.assertIn("module_score", payload["primary"])
        self.assertIn("module_recommendation", payload["primary"])
        self.assertIn("module_explanation", payload["primary"])
        self.assertIn("module_score", payload["backup"])
        self.assertIn("module_recommendation", payload["backup"])
        self.assertIn("module_explanation", payload["backup"])

    def test_preference_ai_preview_marks_invalid_period_unavailable(self):
        year = self._year()
        self._start_collection()
        self.client.force_login(self.employee.user)

        response = self.client.get(
            reverse("vacation_preferences_ai_preview", args=[year]),
            {
                "primary_start_date": date(year, 6, 1).isoformat(),
                "primary_end_date": date(year, 6, 14).isoformat(),
                "backup_start_date": date(year, 8, 20).isoformat(),
                "backup_end_date": date(year, 8, 1).isoformat(),
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["winner"], "unavailable")
        self.assertFalse(payload["backup"]["can_place"])
        self.assertEqual(payload["backup"]["module_recommendation"], "blocked")

    def test_employee_can_submit_or_skip_preferences_and_complete_notification(self):
        year = self._year()
        self._start_collection()
        self.client.force_login(self.employee.user)

        response = self.client.post(
            reverse("vacation_preferences", args=[year]),
            {
                "primary_start_date": date(year, 6, 1).isoformat(),
                "primary_end_date": date(year, 6, 14).isoformat(),
                "backup_start_date": date(year, 8, 1).isoformat(),
                "backup_end_date": date(year, 8, 14).isoformat(),
                "comment": "Хочу летом.",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            VacationPreference.objects.filter(
                employee=self.employee,
                year=year,
                status=VacationPreference.STATUS_FILLED,
            ).count(),
            2,
        )
        primary = VacationPreference.objects.get(
            employee=self.employee,
            year=year,
            priority=VacationPreference.PRIORITY_PRIMARY,
        )
        backup = VacationPreference.objects.get(
            employee=self.employee,
            year=year,
            priority=VacationPreference.PRIORITY_BACKUP,
        )
        self.assertEqual(primary.remainder_policy, VacationPreference.REMAINDER_AUTO)
        for preference in (primary, backup):
            self.assertIsNotNone(preference.ai_score)
            self.assertIsNotNone(preference.ai_confidence)
            self.assertTrue(preference.ai_model_version)
            self.assertTrue(preference.ai_recommendation)
            self.assertTrue(preference.ai_explanation)
            self.assertTrue(preference.ai_scorer_kind)
            self.assertIsNotNone(preference.ai_evaluated_at)
        notification = Notification.objects.get(
            dedupe_key=f"{Notification.TYPE_PREFERENCES_COLLECTION_STARTED}:{year}:{self.employee.id}"
        )
        self.assertEqual(notification.status, Notification.STATUS_DONE)

        saved_response = self.client.get(reverse("vacation_preferences", args=[year]))
        self.assertContains(saved_response, "Пожелания сохранены")
        self.assertContains(saved_response, "Оценка модуля")
        self.assertContains(saved_response, "Можно изменить ответ до закрытия сбора.")
        self.assertContains(saved_response, "Изменить")
        self.assertNotContains(saved_response, "data-preferences-form")

        accidental_response = self.client.post(
            reverse("vacation_preferences", args=[year]),
            {
                "no_preferences": "on",
                "comment": "Даты не принципиальны.",
            },
        )

        self.assertEqual(accidental_response.status_code, 302)
        self.assertEqual(
            VacationPreference.objects.filter(
                employee=self.employee,
                year=year,
                status=VacationPreference.STATUS_FILLED,
            ).count(),
            2,
        )

        edit_response = self.client.get(f"{reverse('vacation_preferences', args=[year])}?edit=1")
        self.assertContains(edit_response, "data-preferences-form")
        self.assertContains(edit_response, "Сохранить изменения")
        self.assertContains(edit_response, "Отменить")
        self.assertContains(edit_response, f'value="{date(year, 6, 1).isoformat()}"')
        self.assertContains(edit_response, f'value="{date(year, 6, 14).isoformat()}"')
        self.assertContains(edit_response, f'value="{date(year, 8, 1).isoformat()}"')
        self.assertContains(edit_response, f'value="{date(year, 8, 14).isoformat()}"')

        response = self.client.post(
            reverse("vacation_preferences", args=[year]),
            {
                "editing": "1",
                "no_preferences": "on",
                "comment": "Даты не принципиальны.",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            VacationPreference.objects.filter(
                employee=self.employee,
                year=year,
                status=VacationPreference.STATUS_SKIPPED,
            ).count(),
            2,
        )
        for preference in VacationPreference.objects.filter(
            employee=self.employee,
            year=year,
            status=VacationPreference.STATUS_SKIPPED,
        ):
            self.assertIsNone(preference.ai_score)
            self.assertIsNone(preference.ai_confidence)
            self.assertEqual(preference.ai_recommendation, "")
        summary = build_preference_collection_summary(year)
        self.assertGreaterEqual(summary["ready"], 1)
        self.assertEqual(summary["ready"], summary["total"] - summary["attention"])

    def test_employee_can_submit_long_preference_within_balance(self):
        year = self._year()
        self._start_collection()
        self.employee.date_joined = date(year - 1, 1, 1)
        self.employee.annual_paid_leave_days = 52
        self.employee.save(update_fields=["date_joined", "annual_paid_leave_days"])
        self.client.force_login(self.employee.user)

        response = self.client.post(
            reverse("vacation_preferences", args=[year]),
            {
                "primary_start_date": date(year, 6, 1).isoformat(),
                "primary_end_date": date(year, 7, 23).isoformat(),
                "backup_start_date": date(year, 8, 1).isoformat(),
                "backup_end_date": date(year, 9, 22).isoformat(),
                "remainder_policy": VacationPreference.REMAINDER_APPROVAL,
                "comment": "Хочу использовать длинный отпуск.",
            },
        )

        self.assertEqual(response.status_code, 302)
        primary = VacationPreference.objects.get(
            employee=self.employee,
            year=year,
            priority=VacationPreference.PRIORITY_PRIMARY,
        )
        self.assertEqual(primary.start_date, date(year, 6, 1))
        self.assertEqual(primary.end_date, date(year, 7, 23))
        self.assertEqual(primary.remainder_policy, VacationPreference.REMAINDER_APPROVAL)

    def test_employee_cannot_submit_short_preference_when_balance_allows_normal_part(self):
        year = self._year()
        self._start_collection()
        self.client.force_login(self.employee.user)

        response = self.client.post(
            reverse("vacation_preferences", args=[year]),
            {
                "primary_start_date": date(year, 6, 1).isoformat(),
                "primary_end_date": date(year, 6, 6).isoformat(),
                "backup_start_date": date(year, 8, 1).isoformat(),
                "backup_end_date": date(year, 8, 14).isoformat(),
                "comment": "Хочу коротко.",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Укажите не меньше 14 д.")
        self.assertFalse(
            VacationPreference.objects.filter(
                employee=self.employee,
                year=year,
                status=VacationPreference.STATUS_FILLED,
            ).exists()
        )

    def test_closed_collection_blocks_employee_edits_and_closes_notifications(self):
        year = self._year()
        self._start_collection()

        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("preferences_collection_finish", args=[year]))

        self.client.force_login(self.employee.user)
        response = self.client.post(
            reverse("vacation_preferences", args=[year]),
            {
                "primary_start_date": date(year, 6, 1).isoformat(),
                "primary_end_date": date(year, 6, 14).isoformat(),
                "backup_start_date": date(year, 8, 1).isoformat(),
                "backup_end_date": date(year, 8, 14).isoformat(),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            VacationPreference.objects.filter(
                employee=self.employee,
                year=year,
                status=VacationPreference.STATUS_FILLED,
            ).exists()
        )
        self.assertFalse(
            Notification.objects.filter(
                event_type=Notification.TYPE_PREFERENCES_COLLECTION_STARTED,
                status=Notification.STATUS_NEW,
            ).exists()
        )

    def test_non_planning_year_collection_is_read_only_even_if_open(self):
        current_year = timezone.localdate().year
        planning_year = self._year()
        VacationPreferenceCollection.objects.create(
            year=current_year,
            status=VacationPreferenceCollection.STATUS_OPEN,
            deadline=self._deadline(),
            started_by=self.hr_employee,
        )
        self.client.force_login(self.employee.user)

        get_response = self.client.get(reverse("vacation_preferences", args=[current_year]))
        self.assertContains(get_response, f"Сейчас пожелания собираются на {planning_year} год")
        self.assertContains(get_response, f'data-planning-year="{planning_year}"')

        response = self.client.post(
            reverse("vacation_preferences", args=[current_year]),
            {
                "primary_start_date": date(current_year, 6, 1).isoformat(),
                "primary_end_date": date(current_year, 6, 14).isoformat(),
                "backup_start_date": date(current_year, 8, 1).isoformat(),
                "backup_end_date": date(current_year, 8, 14).isoformat(),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            VacationPreference.objects.filter(
                employee=self.employee,
                year=current_year,
                status=VacationPreference.STATUS_FILLED,
            ).exists()
        )

    def test_new_employee_is_attached_to_open_collection(self):
        year = self._year()
        self._start_collection()
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(
            reverse("employees"),
            {
                "last_name": "Новый",
                "first_name": "Сотрудник",
                "middle_name": "Тестович",
                "login": "new-preference-user",
                "password": "1234",
                "employee_position": self.engineering_position.id,
                "department": self.engineering.id,
                "role": Employees.ROLE_EMPLOYEE,
                "date_joined": timezone.localdate().isoformat(),
            },
        )

        self.assertEqual(response.status_code, 302)
        employee = Employees.objects.get(login="new-preference-user")
        self.assertTrue(
            VacationPreference.objects.filter(
                employee=employee,
                year=year,
                status=VacationPreference.STATUS_PENDING,
            ).exists()
        )
        self.assertTrue(
            Notification.objects.filter(
                recipient=employee,
                event_type=Notification.TYPE_PREFERENCES_COLLECTION_STARTED,
                status=Notification.STATUS_NEW,
            ).exists()
        )
