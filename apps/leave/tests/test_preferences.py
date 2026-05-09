from datetime import date, timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from apps.accounts.services import sync_employee_user
from apps.core.models import Notification
from apps.employees.models import Employees
from apps.leave.models import (
    VacationPreference,
    VacationPreferenceCollection,
    VacationSchedule,
    VacationScheduleDepartmentApproval,
    VacationScheduleItem,
)
from apps.leave.services.dates import add_months_safe, get_chargeable_leave_days
from apps.leave.services.preferences import (
    build_preference_collection_summary,
    get_employee_preference_pair_map,
    get_employee_preference_state_map,
    preference_readiness_url,
)
from apps.leave.services.schedule_drafts import (
    _build_employee_schedule_planning_need_from_rows,
    auto_place_remaining_schedule_draft,
)
from apps.leave.tests.base import LeaveTestCase


class VacationPreferenceCollectionTests(LeaveTestCase):
    def _year(self):
        return timezone.localdate().year + 1

    def _deadline(self):
        return timezone.localdate() + timedelta(days=14)

    def _start_collection(self, *, demo_autofill=False):
        self.client.force_login(self.hr_employee.user)
        payload = {
            "year": self._year(),
            "deadline": self._deadline().isoformat(),
        }
        if demo_autofill:
            payload["demo_autofill"] = "on"
        return self.client.post(reverse("preferences_collection_start"), payload)

    def _set_filled_preferences(
        self,
        employee,
        *,
        primary_start,
        primary_end,
        backup_start,
        backup_end,
        comment="",
        remainder_policy=VacationPreference.REMAINDER_AUTO,
    ):
        year = self._year()
        VacationPreference.objects.filter(employee=employee, year=year).delete()
        VacationPreference.objects.bulk_create(
            [
                VacationPreference(
                    employee=employee,
                    year=year,
                    priority=VacationPreference.PRIORITY_PRIMARY,
                    start_date=primary_start,
                    end_date=primary_end,
                    status=VacationPreference.STATUS_FILLED,
                    remainder_policy=remainder_policy,
                    comment=comment,
                ),
                VacationPreference(
                    employee=employee,
                    year=year,
                    priority=VacationPreference.PRIORITY_BACKUP,
                    start_date=backup_start,
                    end_date=backup_end,
                    status=VacationPreference.STATUS_FILLED,
                    remainder_policy=remainder_policy,
                    comment=comment,
                ),
            ]
        )

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

    def _set_skipped_preferences(self, employee, *, comment="Без пожеланий."):
        year = self._year()
        VacationPreference.objects.filter(employee=employee, year=year).delete()
        VacationPreference.objects.bulk_create(
            [
                VacationPreference(
                    employee=employee,
                    year=year,
                    priority=VacationPreference.PRIORITY_PRIMARY,
                    status=VacationPreference.STATUS_SKIPPED,
                    comment=comment,
                ),
                VacationPreference(
                    employee=employee,
                    year=year,
                    priority=VacationPreference.PRIORITY_BACKUP,
                    status=VacationPreference.STATUS_SKIPPED,
                    comment=comment,
                ),
            ]
        )

    def _paid_period_for_chargeable_days(self, start_date, chargeable_days):
        end_date = start_date
        while get_chargeable_leave_days(start_date, end_date, "paid") < chargeable_days:
            end_date += timedelta(days=1)
        return end_date

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
        self.assertContains(response, "js/preference-readiness.js")
        self.assertEqual(response.context["summary"]["not_answered"], response.context["summary"]["total"])

        response = self.client.post(
            reverse("preferences_collection_finish", args=[year]),
            {"next": reverse("preference_collection_readiness", args=[year])},
        )

        self.assertRedirects(response, reverse("preference_collection_readiness", args=[year]))
        collection = VacationPreferenceCollection.objects.get(year=year)
        self.assertEqual(collection.status, VacationPreferenceCollection.STATUS_FINISHED)

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
        for label in ["График", "Сбор", "Черновик", "Проверка", "Финал"]:
            self.assertContains(response, label)

    def test_schedule_planning_current_redirects_to_planning_year(self):
        year = self._year()
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("schedule_planning_current"))

        self.assertRedirects(response, reverse("schedule_planning", args=[year]))

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

    def test_hr_creates_schedule_draft_from_finished_collection(self):
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

        response = self.client.post(reverse("schedule_draft_create", args=[year]))

        self.assertRedirects(response, reverse("schedule_draft_detail", args=[year]))
        schedule = VacationSchedule.objects.get(year=year)
        self.assertEqual(schedule.status, VacationSchedule.STATUS_DRAFT)
        item = VacationScheduleItem.objects.get(schedule=schedule, employee=self.employee)
        self.assertEqual(item.status, VacationScheduleItem.STATUS_DRAFT)
        self.assertEqual(item.source, VacationScheduleItem.SOURCE_GENERATED)
        self.assertEqual(item.start_date, date(year, 6, 1))
        self.assertFalse(item.generated_by_ai)

        readiness_response = self.client.get(reverse("preference_collection_readiness", args=[year]))
        self.assertContains(readiness_response, "Открыть черновик")

    def test_schedule_draft_creation_is_idempotent(self):
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
        self.client.post(reverse("schedule_draft_create", args=[year]))

        self.assertEqual(VacationSchedule.objects.filter(year=year).count(), 1)
        self.assertEqual(VacationScheduleItem.objects.filter(schedule__year=year, employee=self.employee).count(), 1)

    def test_hr_auto_places_remaining_schedule_draft_items(self):
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
        schedule = VacationSchedule.objects.get(year=year)
        before_count = VacationScheduleItem.objects.filter(schedule=schedule).count()
        draft_response = self.client.get(reverse("schedule_draft_detail", args=[year]))
        self.assertContains(draft_response, "Автоматически распределить", status_code=200)
        self.assertContains(draft_response, "data-draft-manual-open")
        self.assertContains(draft_response, "schedule-draft-placement-form")
        self.assertNotContains(draft_response, "schedule-draft-manual-form")

        response = self.client.post(reverse("schedule_draft_auto_place", args=[year]))

        self.assertRedirects(response, reverse("schedule_draft_detail", args=[year]))
        after_count = VacationScheduleItem.objects.filter(schedule=schedule).count()
        self.assertGreater(after_count, before_count)
        self.assertTrue(
            VacationScheduleItem.objects.filter(
                schedule=schedule,
                source=VacationScheduleItem.SOURCE_GENERATED,
                manager_comment__contains="Автоматически распределено",
            ).exists()
        )

    def test_auto_place_prefers_whole_long_leave_before_splitting(self):
        year = self._year()
        Employees.objects.exclude(id=self.employee.id).update(is_active_employee=False)
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
        )
        VacationPreference.objects.create(
            employee=self.employee,
            year=year,
            priority=VacationPreference.PRIORITY_BACKUP,
            start_date=date(year, 8, 1),
            end_date=date(year, 9, 22),
            status=VacationPreference.STATUS_FILLED,
        )
        schedule = VacationSchedule.objects.create(
            year=year,
            status=VacationSchedule.STATUS_DRAFT,
            created_by=self.hr_employee,
        )

        result = auto_place_remaining_schedule_draft(year=year, actor=self.hr_employee)

        items = list(VacationScheduleItem.objects.filter(schedule=schedule, employee=self.employee))
        self.assertGreater(result["placed_count"], 0)
        self.assertTrue(any(item.chargeable_days >= Decimal("52.00") for item in items))
        self.assertFalse(any(item.chargeable_days == Decimal("28.00") for item in items))

    def test_auto_place_keeps_annual_plan_when_previous_year_closure_is_needed(self):
        year = self._year()
        Employees.objects.exclude(id=self.employee.id).update(is_active_employee=False)
        self.employee.date_joined = date(year - 2, 1, 4)
        self.employee.annual_paid_leave_days = 52
        self.employee.save(update_fields=["date_joined", "annual_paid_leave_days"])
        VacationPreference.objects.filter(employee=self.employee, year=year).delete()
        schedule = VacationSchedule.objects.create(
            year=year,
            status=VacationSchedule.STATUS_DRAFT,
            created_by=self.hr_employee,
        )

        result = auto_place_remaining_schedule_draft(year=year, actor=self.hr_employee)

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

    def test_auto_place_does_not_create_short_topup_without_employee_consent(self):
        year = self._year()
        Employees.objects.exclude(id=self.employee.id).update(is_active_employee=False)
        self.employee.date_joined = date(year - 1, 7, 1)
        self.employee.annual_paid_leave_days = 52
        self.employee.save(update_fields=["date_joined", "annual_paid_leave_days"])
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

        result = auto_place_remaining_schedule_draft(year=year, actor=self.hr_employee)

        self.assertEqual(result["placed_count"], 0)
        self.assertEqual(VacationScheduleItem.objects.filter(schedule=schedule, employee=self.employee).count(), 1)

    def test_remainder_policy_approval_blocks_automatic_extra_days(self):
        year = self._year()
        Employees.objects.exclude(id=self.employee.id).update(is_active_employee=False)
        self.employee.date_joined = date(year - 1, 7, 1)
        self.employee.annual_paid_leave_days = 52
        self.employee.save(update_fields=["date_joined", "annual_paid_leave_days"])
        self._start_collection()
        self._set_filled_preferences(
            self.employee,
            primary_start=date(year, 6, 1),
            primary_end=date(year, 6, 21),
            backup_start=date(year, 9, 1),
            backup_end=date(year, 9, 21),
            remainder_policy=VacationPreference.REMAINDER_APPROVAL,
        )
        VacationPreferenceCollection.objects.filter(year=year).update(
            status=VacationPreferenceCollection.STATUS_FINISHED,
            finished_by=self.hr_employee,
            finished_at=timezone.now(),
        )
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_draft_create", args=[year]))
        schedule = VacationSchedule.objects.get(year=year)
        before_count = VacationScheduleItem.objects.filter(schedule=schedule, employee=self.employee).count()

        result = auto_place_remaining_schedule_draft(year=year, actor=self.hr_employee)

        self.assertEqual(result["placed_count"], 0)
        self.assertEqual(VacationScheduleItem.objects.filter(schedule=schedule, employee=self.employee).count(), before_count)
        response = self.client.get(reverse("schedule_draft_detail", args=[year]))
        planning_need = response.context["planning_need_by_employee"][self.employee.id]
        self.assertFalse(planning_need["needs_manual_attention"])
        self.assertGreater(planning_need["remainder_approval_days"], Decimal("0.00"))

    def test_hr_can_manually_place_schedule_draft_item(self):
        year = self._year()
        self._start_collection()
        VacationPreferenceCollection.objects.filter(year=year).update(
            status=VacationPreferenceCollection.STATUS_FINISHED,
            finished_by=self.hr_employee,
            finished_at=timezone.now(),
        )
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_draft_create", args=[year]))

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

    def test_manual_draft_placement_merges_adjacent_parts(self):
        year = self._year()
        self.employee.date_joined = date(year - 2, 1, 1)
        self.employee.save(update_fields=["date_joined"])
        self._start_collection()
        VacationPreferenceCollection.objects.filter(year=year).update(
            status=VacationPreferenceCollection.STATUS_FINISHED,
            finished_by=self.hr_employee,
            finished_at=timezone.now(),
        )
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_draft_create", args=[year]))
        schedule = VacationSchedule.objects.get(year=year)
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
        self.employee.date_joined = date(year - 2, 1, 1)
        self.employee.save(update_fields=["date_joined"])
        self._start_collection()
        VacationPreferenceCollection.objects.filter(year=year).update(
            status=VacationPreferenceCollection.STATUS_FINISHED,
            finished_by=self.hr_employee,
            finished_at=timezone.now(),
        )
        self.client.force_login(self.hr_employee.user)
        self.client.post(reverse("schedule_draft_create", args=[year]))
        schedule = VacationSchedule.objects.get(year=year)
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

    def test_schedule_draft_tries_backup_when_primary_has_staffing_conflict(self):
        year = self._year()
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
            backup_start=date(year, 10, 1),
            backup_end=date(year, 10, 14),
        )
        VacationPreferenceCollection.objects.filter(year=year).update(
            status=VacationPreferenceCollection.STATUS_FINISHED,
            finished_by=self.hr_employee,
            finished_at=timezone.now(),
        )
        self.client.force_login(self.hr_employee.user)

        self.client.post(reverse("schedule_draft_create", args=[year]))

        schedule = VacationSchedule.objects.get(year=year)
        employee_item = VacationScheduleItem.objects.get(schedule=schedule, employee=self.employee)
        head_item = VacationScheduleItem.objects.get(schedule=schedule, employee=self.department_head)
        self.assertEqual(employee_item.start_date, date(year, 6, 1))
        self.assertEqual(head_item.start_date, date(year, 10, 1))

        response = self.client.get(reverse("schedule_draft_detail", args=[year]))
        self.assertContains(response, "schedule-draft-card__profile schedule-draft-card__profile--employee")
        self.assertContains(response, "schedule-draft-card__profile schedule-draft-card__profile--department-head")
        self.assertContains(
            response,
            "schedule-draft-card__management-badge schedule-draft-card__management-badge--department-head",
        )
        self.assertContains(response, "Руководитель отдела")
        self.assertNotContains(response, "schedule-draft-card--risk")

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
        self.assertContains(response, "Блокирует согласование")
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
            preference_state=VacationPreference.STATUS_FILLED,
        )

        self.assertEqual(planning_need["target_days"], Decimal("54.00"))
        self.assertEqual(planning_need["placed_days"], Decimal("23.00"))
        self.assertEqual(planning_need["open_required_days"], Decimal("31.00"))
        self.assertEqual(planning_need["future_available_days"], Decimal("52.00"))
        self.assertIn("годового плана", planning_need["action_text"])
        self.assertNotIn("83", planning_need["action_text"])

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
        notification = Notification.objects.get(
            dedupe_key=f"{Notification.TYPE_PREFERENCES_COLLECTION_STARTED}:{year}:{self.employee.id}"
        )
        self.assertEqual(notification.status, Notification.STATUS_DONE)

        saved_response = self.client.get(reverse("vacation_preferences", args=[year]))
        self.assertContains(saved_response, "Пожелания сохранены")
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
