from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import SESSION_KEY
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.models import DemoBaselineSnapshot, DemoDataResetJob, Notification
from apps.core.services.demo_baseline import capture_demo_baseline_snapshot
from apps.employees.models import (
    DepartmentCoverageRule,
    Departments,
    EmployeePosition,
    ProductionGroup,
    ProductionGroupSubstitutionRule,
)
from apps.leave.models import (
    DepartmentStaffingRule,
    DepartmentWorkload,
    VacationPlanningCycle,
    VacationPreference,
    VacationPreferenceCollection,
    VacationSchedule,
    VacationScheduleCandidate,
    VacationScheduleCandidateFeedback,
    VacationScheduleDepartmentApproval,
    VacationScheduleGenerationRun,
    VacationScheduleItem,
)

from .base import EmployeeTestCase


class StaffingRulesPageTests(EmployeeTestCase):
    def _workload_payload(self, action="save_workload_year", year=2026):
        payload = {
            "action": action,
            "department_id": self.engineering.id,
            "year": year,
        }
        for month in range(1, 13):
            payload[f"load_level_{month}"] = 2
            payload[f"min_staff_required_{month}"] = 1
            payload[f"max_absent_{month}"] = 1
        return payload

    def test_hr_can_open_and_edit_staffing_rules(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("staffing_rules"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Правила состава")
        self.assertTrue(response.context["can_edit_staffing"])
        self.assertContains(response, self.engineering.name)
        self.assertContains(response, self.hr_department.name)
        self.assertContains(response, 'data-modal-open="staffing-delete-department-')
        self.assertContains(response, "Действительно ли вы хотите удалить отдел?")
        self.assertContains(response, 'name="action" value="delete_department"')
        self.assertContains(response, "Месячные лимиты")
        self.assertContains(response, 'name="action" value="save_workload_year"')
        self.assertNotContains(response, 'value="fill_workload_year"')
        self.assertContains(response, "data-staffing-workload-year-select")
        self.assertContains(response, "Лимит отсутствий")
        self.assertNotContains(response, ">Нагр.<")
        self.assertNotContains(response, ">Мин.<")
        self.assertNotContains(response, ">Отс.<")
        self.assertNotContains(response, ">Показать<")

        post_response = self.client.post(
            reverse("staffing_rules"),
            {
                "action": "create_group",
                "department_id": self.engineering.id,
                "name": "Диспетчеры",
                "code": "dispatch",
            },
        )

        self.assertRedirects(post_response, reverse("staffing_rules"))
        self.assertTrue(ProductionGroup.objects.filter(department=self.engineering, name="Диспетчеры").exists())

    def test_staffing_page_explains_rule_quality(self):
        self.client.force_login(self.hr_employee.user)
        empty_substitute_group = ProductionGroup.objects.create(department=self.engineering, name="Пустой резерв")
        DepartmentCoverageRule.objects.create(
            department=self.engineering,
            production_group=self.engineering_group,
            min_staff_required=2,
            max_absent=3,
            criticality_level=5,
        )
        ProductionGroupSubstitutionRule.objects.create(
            department=self.engineering,
            source_group=self.engineering_group,
            substitute_group=empty_substitute_group,
            max_covered_absences=2,
        )

        response = self.client.get(reverse("staffing_rules"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Качество правил отдела")
        self.assertContains(response, "Риск состава:")
        self.assertContains(response, "Слишком жёстко")
        self.assertContains(response, "Нет замещающих")
        self.assertContains(response, "Резерв:")
        self.assertContains(response, "staffing-diagnostic-chip")
        self.assertContains(response, "staffing-workload-month__quality")
        self.assertContains(response, "data-schedule-status-tooltip")
        self.assertContains(response, 'data-tooltip-title="Риск состава"')
        self.assertContains(response, 'data-tooltip-title="Критичность"')
        self.assertContains(response, 'data-tooltip-title="Нагрузка месяца"')
        self.assertContains(response, "staffing-workload-month__label")
        self.assertContains(response, "beach_access")

    def test_staffing_deputy_picker_marks_new_hires(self):
        self.employee.date_joined = timezone.localdate()
        self.employee.save(update_fields=["date_joined"])
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("staffing_rules"))

        self.assertEqual(response.status_code, 200)
        active_employees = {employee.id: employee for employee in response.context["enterprise_deputy_candidates"]}
        self.assertEqual(active_employees[self.employee.id].new_hire_badge["label"], "Новичок")
        self.assertContains(response, 'class="new-hire-badge"')
        self.assertContains(response, "person_add")
        self.assertContains(response, "Работает меньше 6 месяцев")

    @override_settings(DEBUG=True)
    def test_enterprise_head_sees_demo_reset_button_in_debug(self):
        self.client.force_login(self.enterprise_head.user)

        response = self.client.get(reverse("staffing_rules"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_reset_demo_data"])
        self.assertContains(response, "Пересоздать демо-данные")
        self.assertContains(response, "Сбросить до начальных настроек")
        self.assertContains(response, 'data-modal-open="staffing-demo-reset-modal"')
        self.assertContains(response, 'data-modal-open="staffing-demo-restore-modal"')
        self.assertContains(response, "data-demo-reset-form")
        self.assertContains(response, 'name="demo_mode"')
        self.assertContains(response, "Обычная демо-база")
        self.assertContains(response, "Быстрая демо-база")
        self.assertContains(response, "data-demo-reset-progress")
        self.assertContains(response, reverse("reset_demo_data"))
        self.assertContains(response, reverse("restore_demo_initial_state"))

    @override_settings(DEBUG=True)
    def test_hr_sees_demo_reset_button_in_debug(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("staffing_rules"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_reset_demo_data"])
        self.assertContains(response, "Пересоздать демо-данные")
        self.assertContains(response, "Сбросить до начальных настроек")
        self.assertContains(response, 'data-modal-open="staffing-demo-reset-modal"')
        self.assertContains(response, 'data-modal-open="staffing-demo-restore-modal"')

    @override_settings(DEBUG=True)
    def test_demo_reset_button_is_hidden_from_department_head_and_employee(self):
        self.client.force_login(self.department_head.user)
        head_response = self.client.get(reverse("staffing_rules"))
        self.assertFalse(head_response.context["can_reset_demo_data"])
        self.assertNotContains(head_response, 'data-modal-open="staffing-demo-reset-modal"')
        self.assertNotContains(head_response, 'data-modal-open="staffing-demo-restore-modal"')

        self.client.force_login(self.employee.user)
        employee_response = self.client.get(reverse("staffing_rules"))
        self.assertEqual(employee_response.status_code, 302)

    @override_settings(DEBUG=False, DEMO_DATA_TOOLS_ENABLED=False)
    def test_demo_reset_button_is_hidden_outside_debug(self):
        self.client.force_login(self.enterprise_head.user)

        response = self.client.get(reverse("staffing_rules"))

        self.assertFalse(response.context["can_reset_demo_data"])
        self.assertNotContains(response, 'data-modal-open="staffing-demo-reset-modal"')
        self.assertNotContains(response, 'data-modal-open="staffing-demo-restore-modal"')

    @override_settings(DEBUG=False, DEMO_DATA_TOOLS_ENABLED=True)
    def test_demo_reset_button_can_be_enabled_without_debug(self):
        self.client.force_login(self.enterprise_head.user)

        response = self.client.get(reverse("staffing_rules"))

        self.assertTrue(response.context["can_reset_demo_data"])
        self.assertContains(response, 'data-modal-open="staffing-demo-reset-modal"')
        self.assertContains(response, 'data-modal-open="staffing-demo-restore-modal"')

    @override_settings(DEBUG=True)
    @patch("apps.employees.views.start_demo_data_reset_process")
    @patch("apps.employees.views.secrets.randbelow", return_value=123455)
    def test_enterprise_head_can_reset_demo_data_and_is_logged_out(self, randbelow_mock, start_process_mock):
        self.client.force_login(self.enterprise_head.user)

        response = self.client.post(reverse("reset_demo_data"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], DemoDataResetJob.STATUS_QUEUED)
        self.assertEqual(payload["seed_value"], 123456)
        self.assertIn("status_url", payload)
        self.assertIn("token=", payload["status_url"])
        job = DemoDataResetJob.objects.get(id=payload["job_id"])
        self.assertEqual(job.seed_value, 123456)
        self.assertEqual(job.token, payload["token"])
        self.assertEqual(job.preset, DemoDataResetJob.PRESET_STANDARD)
        self.assertEqual(job.employee_count, 50)
        self.assertEqual(job.history_years, 2)
        self.assertEqual(payload["preset"], DemoDataResetJob.PRESET_STANDARD)
        self.assertEqual(payload["employee_count"], 50)
        self.assertEqual(payload["calendar_years"], 3)
        start_process_mock.assert_called_once_with(job)
        self.assertNotIn(SESSION_KEY, self.client.session)
        randbelow_mock.assert_called_once()

    @override_settings(DEBUG=True)
    @patch("apps.employees.views.start_demo_data_reset_process")
    @patch("apps.employees.views.secrets.randbelow", return_value=222)
    def test_demo_reset_can_start_fast_preset(self, randbelow_mock, start_process_mock):
        self.client.force_login(self.enterprise_head.user)

        response = self.client.post(reverse("reset_demo_data"), {"demo_mode": "fast"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        job = DemoDataResetJob.objects.get(id=payload["job_id"])
        self.assertEqual(job.preset, DemoDataResetJob.PRESET_FAST)
        self.assertEqual(job.employee_count, 25)
        self.assertEqual(job.history_years, 2)
        self.assertEqual(payload["preset"], DemoDataResetJob.PRESET_FAST)
        self.assertEqual(payload["employee_count"], 25)
        self.assertEqual(payload["calendar_years"], 3)
        start_process_mock.assert_called_once_with(job)
        randbelow_mock.assert_called_once()

    @override_settings(DEBUG=True)
    @patch("apps.employees.views.start_demo_data_reset_process")
    @patch("apps.employees.views.secrets.randbelow", return_value=55)
    def test_demo_reset_reuses_running_job_on_repeat_click(self, randbelow_mock, start_process_mock):
        running_job = DemoDataResetJob.objects.create(
            token="running-reset-token",
            seed_value=42,
            status=DemoDataResetJob.STATUS_RUNNING,
            progress_percent=30,
            stage_label="Исторические графики",
            process_id=12345,
        )
        self.client.force_login(self.enterprise_head.user)

        response = self.client.post(reverse("reset_demo_data"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["job_id"], running_job.id)
        self.assertEqual(payload["token"], running_job.token)
        self.assertIn("уже выполняется", payload["message"])
        self.assertEqual(DemoDataResetJob.objects.count(), 1)
        start_process_mock.assert_not_called()
        randbelow_mock.assert_called_once()
        self.assertNotIn(SESSION_KEY, self.client.session)

    @override_settings(DEBUG=True)
    @patch("apps.employees.views.start_demo_data_reset_process")
    def test_demo_reset_post_is_denied_for_other_roles(self, start_process_mock):
        self.client.force_login(self.department_head.user)
        head_response = self.client.post(reverse("reset_demo_data"))
        self.assertRedirects(head_response, reverse("staffing_rules"))

        self.client.force_login(self.employee.user)
        employee_response = self.client.post(reverse("reset_demo_data"))
        self.assertRedirects(employee_response, reverse("main"))
        start_process_mock.assert_not_called()

    @override_settings(DEBUG=True)
    @patch("apps.employees.views.start_demo_data_reset_process")
    @patch("apps.employees.views.secrets.randbelow", return_value=777)
    def test_hr_can_reset_demo_data_and_is_logged_out(self, randbelow_mock, start_process_mock):
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(reverse("reset_demo_data"))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], DemoDataResetJob.STATUS_QUEUED)
        self.assertEqual(payload["seed_value"], 778)
        self.assertIn("status_url", payload)
        job = DemoDataResetJob.objects.get(id=payload["job_id"])
        self.assertEqual(job.token, payload["token"])
        start_process_mock.assert_called_once_with(job)
        self.assertNotIn(SESSION_KEY, self.client.session)
        randbelow_mock.assert_called_once()

    @override_settings(DEBUG=True)
    def test_demo_reset_status_requires_valid_token(self):
        job = DemoDataResetJob.objects.create(
            token="correct-token",
            seed_value=42,
            status=DemoDataResetJob.STATUS_RUNNING,
            progress_percent=37,
            stage_label="Исторические графики",
            message="Создаются архивные графики.",
        )

        bad_response = self.client.get(reverse("reset_demo_data_status", args=[job.id]), {"token": "wrong"})
        self.assertEqual(bad_response.status_code, 403)

        good_response = self.client.get(reverse("reset_demo_data_status", args=[job.id]), {"token": "correct-token"})
        self.assertEqual(good_response.status_code, 200)
        payload = good_response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], DemoDataResetJob.STATUS_RUNNING)
        self.assertEqual(payload["progress_percent"], 37)
        self.assertEqual(payload["stage_label"], "Исторические графики")
        self.assertEqual(payload["login_url"], reverse("login"))

    @override_settings(DEBUG=False, DEMO_DATA_TOOLS_ENABLED=True)
    def test_demo_reset_status_can_be_enabled_without_debug(self):
        job = DemoDataResetJob.objects.create(
            token="enabled-token",
            seed_value=42,
            status=DemoDataResetJob.STATUS_RUNNING,
            progress_percent=55,
        )

        response = self.client.get(reverse("reset_demo_data_status", args=[job.id]), {"token": "enabled-token"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["progress_percent"], 55)

    @override_settings(DEBUG=False, DEMO_DATA_TOOLS_ENABLED=False)
    def test_demo_reset_status_is_hidden_when_demo_tools_disabled(self):
        job = DemoDataResetJob.objects.create(
            token="disabled-token",
            seed_value=42,
            status=DemoDataResetJob.STATUS_RUNNING,
        )

        response = self.client.get(reverse("reset_demo_data_status", args=[job.id]), {"token": "disabled-token"})

        self.assertEqual(response.status_code, 404)

    @override_settings(DEBUG=True)
    def test_demo_restore_without_snapshot_shows_error_and_keeps_session(self):
        self.client.force_login(self.enterprise_head.user)

        response = self.client.post(reverse("restore_demo_initial_state"), follow=True)

        self.assertRedirects(response, reverse("staffing_rules"))
        self.assertIn(SESSION_KEY, self.client.session)
        self.assertContains(response, "Быстрый сброс пока недоступен")

    @override_settings(DEBUG=True)
    def test_demo_restore_is_blocked_while_full_reset_is_running(self):
        DemoDataResetJob.objects.create(
            token="running-reset-token",
            seed_value=42,
            status=DemoDataResetJob.STATUS_RUNNING,
            progress_percent=30,
            stage_label="Исторические графики",
            process_id=12345,
        )
        self.client.force_login(self.enterprise_head.user)

        response = self.client.post(reverse("restore_demo_initial_state"), follow=True)

        self.assertRedirects(response, reverse("staffing_rules"))
        self.assertContains(response, "Сброс демо-данных уже выполняется")
        self.assertIn(SESSION_KEY, self.client.session)

    @override_settings(DEBUG=True)
    @patch("apps.employees.views.reset_demo_to_baseline")
    def test_demo_restore_post_is_denied_for_other_roles(self, reset_demo_mock):
        self.client.force_login(self.department_head.user)
        head_response = self.client.post(reverse("restore_demo_initial_state"))
        self.assertRedirects(head_response, reverse("staffing_rules"))

        self.client.force_login(self.employee.user)
        employee_response = self.client.post(reverse("restore_demo_initial_state"))
        self.assertRedirects(employee_response, reverse("main"))
        reset_demo_mock.assert_not_called()

    @override_settings(DEBUG=True)
    @patch("apps.employees.views.reset_demo_to_baseline", return_value={"planning_year": 2027})
    def test_hr_can_restore_demo_initial_state_without_logout(self, reset_demo_mock):
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(reverse("restore_demo_initial_state"), follow=True)

        self.assertRedirects(response, reverse("staffing_rules"))
        self.assertContains(response, "Демо-состояние сброшено")
        self.assertIn(SESSION_KEY, self.client.session)
        reset_demo_mock.assert_called_once_with(actor=self.hr_employee)

    @override_settings(DEBUG=True)
    @patch("apps.employees.views.start_demo_data_reset_process")
    def test_enterprise_head_can_restore_demo_initial_state_without_full_seed(self, start_process_mock):
        planning_year = 2027
        self.engineering.head = self.department_head
        self.engineering.deputy = self.employee
        self.engineering.save(update_fields=["head", "deputy"])
        self.employee.is_enterprise_deputy = True
        self.employee.save(update_fields=["is_enterprise_deputy"])
        DepartmentStaffingRule.objects.create(
            department=self.engineering,
            min_staff_required=4,
            max_absent=2,
            criticality_level=3,
            substitution_group="engineering",
        )
        DepartmentWorkload.objects.create(
            department=self.engineering,
            year=planning_year,
            month=1,
            load_level=5,
            min_staff_required=4,
            max_absent=2,
        )
        capture_demo_baseline_snapshot(planning_year=planning_year, seed_value=77)
        VacationPlanningCycle.objects.create(year=planning_year, status=VacationPlanningCycle.STATUS_ACTIVE)
        VacationPlanningCycle.objects.create(year=planning_year + 1, status=VacationPlanningCycle.STATUS_CLOSED)

        self.engineering.name = "Changed Engineering"
        self.engineering.head = None
        self.engineering.deputy = None
        self.engineering.save(update_fields=["name", "head", "deputy"])
        self.employee.department = self.hr_department
        self.employee.employee_position = self.hr_position
        self.employee.position = self.hr_position.title
        self.employee.is_enterprise_deputy = False
        self.employee.save(update_fields=["department", "employee_position", "position", "is_enterprise_deputy"])
        self.engineering_group.name = "Changed group"
        self.engineering_group.save(update_fields=["name"])
        ProductionGroup.objects.create(department=self.engineering, name="Temporary group")
        DepartmentWorkload.objects.filter(department=self.engineering, year=planning_year, month=1).update(
            load_level=1,
            min_staff_required=1,
            max_absent=9,
        )

        collection = VacationPreferenceCollection.objects.create(
            year=planning_year,
            status=VacationPreferenceCollection.STATUS_FINISHED,
            deadline=date(2026, 12, 1),
            started_by=self.hr_employee,
            finished_by=self.hr_employee,
            finished_at=timezone.now(),
        )
        VacationPreference.objects.create(
            employee=self.employee,
            year=planning_year,
            start_date=date(planning_year, 7, 1),
            end_date=date(planning_year, 7, 14),
            priority=VacationPreference.PRIORITY_PRIMARY,
            status=VacationPreference.STATUS_FILLED,
        )
        schedule = VacationSchedule.objects.create(
            year=planning_year,
            status=VacationSchedule.STATUS_DRAFT,
            created_by=self.hr_employee,
        )
        run = VacationScheduleGenerationRun.objects.create(
            schedule=schedule,
            year=planning_year,
            status=VacationScheduleGenerationRun.STATUS_COMPLETED,
            actor=self.hr_employee,
        )
        item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(planning_year, 7, 1),
            end_date=date(planning_year, 7, 14),
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_DRAFT,
            generated_by_ai=True,
            generation_run=run,
            ai_score=Decimal("72.50"),
            ai_confidence=Decimal("84.20"),
            ai_model_version="vacation-candidate-mlp-v2",
        )
        candidate = VacationScheduleCandidate.objects.create(
            generation_run=run,
            schedule=schedule,
            employee=self.employee,
            start_date=item.start_date,
            end_date=item.end_date,
            chargeable_days=item.chargeable_days,
            kind=VacationScheduleCandidate.KIND_AUTO,
            source=VacationScheduleItem.SOURCE_GENERATED,
            passed_hard_rules=True,
            features={"feature_schema_version": 1},
            score=Decimal("72.50"),
            confidence=Decimal("84.20"),
            model_version="vacation-candidate-mlp-v2",
            decision=VacationScheduleCandidate.DECISION_SELECTED,
        )
        item.selected_candidate = candidate
        item.save(update_fields=["selected_candidate"])
        VacationScheduleCandidateFeedback.objects.create(
            schedule_item=item,
            candidate=candidate,
            generation_run=run,
            reviewer=self.hr_employee,
            reviewer_role=VacationScheduleCandidateFeedback.ROLE_HR,
            decision=VacationScheduleCandidateFeedback.DECISION_AGREE,
        )
        VacationScheduleDepartmentApproval.objects.create(
            schedule=schedule,
            department=self.engineering,
            department_head=self.department_head,
        )
        Notification.objects.create(
            recipient=self.employee,
            actor=self.hr_employee,
            event_type=Notification.TYPE_PREFERENCES_COLLECTION_STARTED,
            title="Сбор пожеланий",
            message="Заполните пожелания.",
            dedupe_key=f"{Notification.TYPE_PREFERENCES_COLLECTION_STARTED}:{planning_year}:{self.employee.id}",
        )
        future_schedule = VacationSchedule.objects.create(
            year=planning_year + 1,
            status=VacationSchedule.STATUS_DRAFT,
            created_by=self.hr_employee,
        )
        self.client.force_login(self.enterprise_head.user)

        response = self.client.post(reverse("restore_demo_initial_state"), follow=True)

        self.assertRedirects(response, reverse("staffing_rules"))
        self.assertContains(response, "Демо-состояние сброшено")
        self.assertIn(SESSION_KEY, self.client.session)
        start_process_mock.assert_not_called()
        self.assertTrue(DemoBaselineSnapshot.objects.filter(key="initial_demo_state").exists())

        self.engineering.refresh_from_db()
        self.assertEqual(self.engineering.name, "Engineering")
        self.assertEqual(self.engineering.head_id, self.department_head.id)
        self.assertEqual(self.engineering.deputy_id, self.employee.id)
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.department_id, self.engineering.id)
        self.assertEqual(self.employee.employee_position_id, self.engineering_position.id)
        self.assertTrue(self.employee.is_enterprise_deputy)
        self.assertEqual(ProductionGroup.objects.get(id=self.engineering_group.id).name, "Инженеры")
        self.assertFalse(ProductionGroup.objects.filter(name="Temporary group").exists())
        workload = DepartmentWorkload.objects.get(department=self.engineering, year=planning_year, month=1)
        self.assertEqual(workload.load_level, 5)
        self.assertEqual(workload.min_staff_required, 4)
        self.assertEqual(workload.max_absent, 2)
        self.assertFalse(VacationPreferenceCollection.objects.filter(id=collection.id).exists())
        self.assertFalse(VacationPreference.objects.filter(year=planning_year).exists())
        self.assertFalse(VacationSchedule.objects.filter(year=planning_year).exists())
        self.assertFalse(VacationSchedule.objects.filter(id=future_schedule.id).exists())
        self.assertFalse(VacationPlanningCycle.objects.filter(year=planning_year + 1).exists())
        self.assertEqual(
            VacationPlanningCycle.objects.get(year=planning_year).status,
            VacationPlanningCycle.STATUS_ACTIVE,
        )
        self.assertFalse(VacationScheduleGenerationRun.objects.filter(id=run.id).exists())
        self.assertFalse(VacationScheduleCandidateFeedback.objects.filter(schedule_item_id=item.id).exists())
        self.assertFalse(
            Notification.objects.filter(
                dedupe_key=f"{Notification.TYPE_PREFERENCES_COLLECTION_STARTED}:{planning_year}:{self.employee.id}"
            ).exists()
        )

    def test_hr_sees_staffing_item_edit_and_delete_modals(self):
        self.client.force_login(self.hr_employee.user)
        coverage = DepartmentCoverageRule.objects.create(
            department=self.engineering,
            production_group=self.engineering_group,
            min_staff_required=1,
            max_absent=1,
            criticality_level=3,
        )
        substitution = ProductionGroupSubstitutionRule.objects.create(
            department=self.engineering,
            source_group=self.engineering_group,
            substitute_group=self.engineering_leadership_group,
            max_covered_absences=1,
        )

        response = self.client.get(reverse("staffing_rules"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'data-modal-open="staffing-edit-group-{self.engineering_group.id}-modal"')
        self.assertContains(response, f'data-modal-open="staffing-delete-group-{self.engineering_group.id}-modal"')
        self.assertContains(response, f'data-modal-open="staffing-edit-position-{self.engineering_position.id}-modal"')
        self.assertContains(response, f'data-modal-open="staffing-delete-position-{self.engineering_position.id}-modal"')
        self.assertContains(response, f'data-modal-open="staffing-edit-coverage-{coverage.id}-modal"')
        self.assertContains(response, f'data-modal-open="staffing-delete-coverage-{coverage.id}-modal"')
        self.assertContains(response, f'data-modal-open="staffing-edit-substitution-{substitution.id}-modal"')
        self.assertContains(response, f'data-modal-open="staffing-delete-substitution-{substitution.id}-modal"')
        self.assertContains(response, 'name="action" value="update_group"')
        self.assertContains(response, 'name="action" value="delete_group"')
        self.assertContains(response, 'name="action" value="update_position"')
        self.assertContains(response, 'name="action" value="delete_position"')
        self.assertContains(response, 'name="action" value="update_coverage"')
        self.assertContains(response, 'name="action" value="delete_coverage"')
        self.assertContains(response, "Удалить правило замещения?")

    def test_department_head_sees_only_own_department_without_editing(self):
        self.client.force_login(self.department_head.user)

        response = self.client.get(reverse("staffing_rules"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_edit_staffing"])
        self.assertContains(response, self.engineering.name)
        self.assertNotContains(response, self.hr_department.name)
        self.assertNotContains(response, 'name="action" value="create_group"')
        self.assertNotContains(response, 'name="action" value="delete_department"')
        self.assertNotContains(response, 'name="action" value="update_group"')
        self.assertNotContains(response, 'name="action" value="delete_group"')
        self.assertNotContains(response, 'name="action" value="update_position"')
        self.assertNotContains(response, 'name="action" value="delete_position"')
        self.assertContains(response, "Месячные лимиты")
        self.assertNotContains(response, 'name="action" value="save_workload_year"')

        post_response = self.client.post(
            reverse("staffing_rules"),
            {
                "action": "delete_department",
                "department_id": self.engineering.id,
            },
        )

        self.assertRedirects(post_response, reverse("staffing_rules"))
        self.assertTrue(Departments.objects.filter(id=self.engineering.id).exists())

        group_update_response = self.client.post(
            reverse("staffing_rules"),
            {
                "action": "update_group",
                "department_id": self.engineering.id,
                "group_id": self.engineering_group.id,
                "name": "Нельзя менять",
                "code": "blocked",
            },
        )

        self.assertRedirects(group_update_response, reverse("staffing_rules"))
        self.engineering_group.refresh_from_db()
        self.assertEqual(self.engineering_group.name, "Инженеры")

    def test_staffing_page_saves_department_workload_year(self):
        self.client.force_login(self.hr_employee.user)
        payload = self._workload_payload(year=2026)
        payload.update(
            {
                "load_level_3": 5,
                "min_staff_required_3": 7,
                "max_absent_3": 2,
            }
        )

        response = self.client.post(reverse("staffing_rules"), payload)

        self.assertRedirects(response, f"{reverse('staffing_rules')}?year=2026")
        self.assertEqual(DepartmentWorkload.objects.filter(department=self.engineering, year=2026).count(), 12)
        march_workload = DepartmentWorkload.objects.get(department=self.engineering, year=2026, month=3)
        self.assertEqual(march_workload.load_level, 5)
        self.assertEqual(march_workload.min_staff_required, 7)
        self.assertEqual(march_workload.max_absent, 2)

    def test_staffing_page_inherits_workload_for_year_without_records(self):
        self.client.force_login(self.hr_employee.user)
        DepartmentWorkload.objects.create(
            department=self.engineering,
            year=2026,
            month=1,
            load_level=4,
            min_staff_required=3,
            max_absent=2,
        )

        response = self.client.get(reverse("staffing_rules"), {"year": 2027})

        self.assertEqual(response.status_code, 200)
        department = next(
            department
            for department in response.context["staffing_departments"]
            if department.id == self.engineering.id
        )
        january = department.staffing_workload_months[0]
        self.assertEqual(january["load_level"], 4)
        self.assertEqual(january["min_staff_required"], 3)
        self.assertEqual(january["max_absent"], 2)
        self.assertTrue(january["is_inherited"])
        self.assertFalse(january["is_configured"])

    def test_staffing_page_fills_workload_year_from_department_rule(self):
        self.client.force_login(self.hr_employee.user)
        DepartmentStaffingRule.objects.create(
            department=self.engineering,
            min_staff_required=4,
            max_absent=2,
            criticality_level=3,
        )

        response = self.client.post(
            reverse("staffing_rules"),
            {
                "action": "fill_workload_year",
                "department_id": self.engineering.id,
                "year": 2027,
            },
        )

        self.assertRedirects(response, f"{reverse('staffing_rules')}?year=2027")
        self.assertEqual(DepartmentWorkload.objects.filter(department=self.engineering, year=2027).count(), 12)
        january_workload = DepartmentWorkload.objects.get(department=self.engineering, year=2027, month=1)
        self.assertEqual(january_workload.load_level, 3)
        self.assertEqual(january_workload.min_staff_required, 4)
        self.assertEqual(january_workload.max_absent, 2)

    def test_department_head_cannot_edit_department_workload(self):
        self.client.force_login(self.department_head.user)

        response = self.client.post(reverse("staffing_rules"), self._workload_payload(year=2026))

        self.assertRedirects(response, f"{reverse('staffing_rules')}?year=2026")
        self.assertFalse(DepartmentWorkload.objects.filter(department=self.engineering, year=2026).exists())

    def test_staffing_page_deletes_department_and_unlinks_staff(self):
        self.client.force_login(self.hr_employee.user)
        department_id = self.engineering.id

        delete_response = self.client.post(
            reverse("staffing_rules"),
            {
                "action": "delete_department",
                "department_id": department_id,
            },
        )

        self.assertRedirects(delete_response, reverse("staffing_rules"))
        self.assertFalse(Departments.objects.filter(id=department_id).exists())
        self.assertFalse(ProductionGroup.objects.filter(department_id=department_id).exists())
        self.assertFalse(EmployeePosition.objects.filter(department_id=department_id).exists())

        self.employee.refresh_from_db()
        self.department_head.refresh_from_db()
        self.assertIsNone(self.employee.department_id)
        self.assertIsNone(self.employee.employee_position_id)
        self.assertIsNone(self.department_head.department_id)
        self.assertIsNone(self.department_head.employee_position_id)

    def test_staffing_page_saves_position_and_coverage_rule(self):
        self.client.force_login(self.hr_employee.user)
        group = ProductionGroup.objects.create(department=self.engineering, name="Контроль качества")

        position_response = self.client.post(
            reverse("staffing_rules"),
            {
                "action": "create_position",
                "department_id": self.engineering.id,
                "production_group_id": group.id,
                "title": "Контролер качества",
            },
        )
        self.assertRedirects(position_response, reverse("staffing_rules"))
        self.assertTrue(
            EmployeePosition.objects.filter(
                department=self.engineering,
                production_group=group,
                title="Контролер качества",
            ).exists()
        )

        rule_response = self.client.post(
            reverse("staffing_rules"),
            {
                "action": "save_coverage",
                "department_id": self.engineering.id,
                "production_group_id": group.id,
                "min_staff_required": 2,
                "max_absent": 1,
                "criticality_level": 5,
            },
        )
        self.assertRedirects(rule_response, reverse("staffing_rules"))
        rule = DepartmentCoverageRule.objects.get(department=self.engineering, production_group=group)
        self.assertEqual(rule.min_staff_required, 2)
        self.assertEqual(rule.max_absent, 1)
        self.assertEqual(rule.criticality_level, 5)

    def test_staffing_page_updates_group_position_coverage_and_substitution(self):
        self.client.force_login(self.hr_employee.user)
        target_group = ProductionGroup.objects.create(department=self.engineering, name="Резервная группа")
        coverage = DepartmentCoverageRule.objects.create(
            department=self.engineering,
            production_group=self.engineering_group,
            min_staff_required=1,
            max_absent=1,
            criticality_level=2,
        )
        substitution = ProductionGroupSubstitutionRule.objects.create(
            department=self.engineering,
            source_group=self.engineering_group,
            substitute_group=self.engineering_leadership_group,
            max_covered_absences=1,
        )

        group_response = self.client.post(
            reverse("staffing_rules"),
            {
                "action": "update_group",
                "department_id": self.engineering.id,
                "group_id": self.engineering_group.id,
                "name": "Инженерный резерв",
                "code": "eng-reserve",
            },
        )
        self.assertRedirects(group_response, reverse("staffing_rules"))
        self.engineering_group.refresh_from_db()
        self.assertEqual(self.engineering_group.name, "Инженерный резерв")
        self.assertEqual(self.engineering_group.code, "eng-reserve")

        position_response = self.client.post(
            reverse("staffing_rules"),
            {
                "action": "update_position",
                "department_id": self.engineering.id,
                "position_id": self.engineering_position.id,
                "production_group_id": target_group.id,
                "title": "Старший специалист",
            },
        )
        self.assertRedirects(position_response, reverse("staffing_rules"))
        self.engineering_position.refresh_from_db()
        self.assertEqual(self.engineering_position.title, "Старший специалист")
        self.assertEqual(self.engineering_position.production_group_id, target_group.id)

        coverage_response = self.client.post(
            reverse("staffing_rules"),
            {
                "action": "update_coverage",
                "department_id": self.engineering.id,
                "coverage_id": coverage.id,
                "production_group_id": target_group.id,
                "min_staff_required": 2,
                "max_absent": 1,
                "criticality_level": 5,
            },
        )
        self.assertRedirects(coverage_response, reverse("staffing_rules"))
        coverage.refresh_from_db()
        self.assertEqual(coverage.production_group_id, target_group.id)
        self.assertEqual(coverage.min_staff_required, 2)
        self.assertEqual(coverage.max_absent, 1)
        self.assertEqual(coverage.criticality_level, 5)

        substitution_response = self.client.post(
            reverse("staffing_rules"),
            {
                "action": "update_substitution",
                "department_id": self.engineering.id,
                "substitution_id": substitution.id,
                "source_group_id": self.engineering_leadership_group.id,
                "substitute_group_id": target_group.id,
                "max_covered_absences": 3,
            },
        )
        self.assertRedirects(substitution_response, reverse("staffing_rules"))
        substitution.refresh_from_db()
        self.assertEqual(substitution.source_group_id, self.engineering_leadership_group.id)
        self.assertEqual(substitution.substitute_group_id, target_group.id)
        self.assertEqual(substitution.max_covered_absences, 3)

    def test_staffing_page_deletes_free_position_coverage_and_substitution(self):
        self.client.force_login(self.hr_employee.user)
        source_group = ProductionGroup.objects.create(department=self.engineering, name="Свободный источник")
        substitute_group = ProductionGroup.objects.create(department=self.engineering, name="Свободный резерв")
        position = EmployeePosition.objects.create(
            department=self.engineering,
            production_group=source_group,
            title="Свободная должность",
        )
        coverage = DepartmentCoverageRule.objects.create(
            department=self.engineering,
            production_group=source_group,
            min_staff_required=1,
            max_absent=1,
            criticality_level=3,
        )
        substitution = ProductionGroupSubstitutionRule.objects.create(
            department=self.engineering,
            source_group=source_group,
            substitute_group=substitute_group,
            max_covered_absences=1,
        )

        position_response = self.client.post(
            reverse("staffing_rules"),
            {
                "action": "delete_position",
                "department_id": self.engineering.id,
                "position_id": position.id,
            },
        )
        self.assertRedirects(position_response, reverse("staffing_rules"))
        self.assertFalse(EmployeePosition.objects.filter(id=position.id).exists())

        coverage_response = self.client.post(
            reverse("staffing_rules"),
            {
                "action": "delete_coverage",
                "department_id": self.engineering.id,
                "coverage_id": coverage.id,
            },
        )
        self.assertRedirects(coverage_response, reverse("staffing_rules"))
        self.assertFalse(DepartmentCoverageRule.objects.filter(id=coverage.id).exists())

        substitution_response = self.client.post(
            reverse("staffing_rules"),
            {
                "action": "delete_substitution",
                "department_id": self.engineering.id,
                "substitution_id": substitution.id,
            },
        )
        self.assertRedirects(substitution_response, reverse("staffing_rules"))
        self.assertFalse(ProductionGroupSubstitutionRule.objects.filter(id=substitution.id).exists())

    def test_staffing_page_blocks_deleting_used_group_and_position(self):
        self.client.force_login(self.hr_employee.user)

        group_response = self.client.post(
            reverse("staffing_rules"),
            {
                "action": "delete_group",
                "department_id": self.engineering.id,
                "group_id": self.engineering_group.id,
            },
        )
        self.assertRedirects(group_response, reverse("staffing_rules"))
        self.assertTrue(ProductionGroup.objects.filter(id=self.engineering_group.id).exists())

        position_response = self.client.post(
            reverse("staffing_rules"),
            {
                "action": "delete_position",
                "department_id": self.engineering.id,
                "position_id": self.engineering_position.id,
            },
        )
        self.assertRedirects(position_response, reverse("staffing_rules"))
        self.assertTrue(EmployeePosition.objects.filter(id=self.engineering_position.id).exists())

    def test_staffing_page_saves_and_deletes_substitution_capacity(self):
        self.client.force_login(self.hr_employee.user)
        source_group = ProductionGroup.objects.create(department=self.engineering, name="Диспетчеры")
        substitute_group = ProductionGroup.objects.create(department=self.engineering, name="Старшие логисты")

        create_response = self.client.post(
            reverse("staffing_rules"),
            {
                "action": "create_substitution",
                "department_id": self.engineering.id,
                "source_group_id": source_group.id,
                "substitute_group_id": substitute_group.id,
                "max_covered_absences": 2,
            },
        )

        self.assertRedirects(create_response, reverse("staffing_rules"))
        substitution = ProductionGroupSubstitutionRule.objects.get(
            department=self.engineering,
            source_group=source_group,
            substitute_group=substitute_group,
        )
        self.assertEqual(substitution.max_covered_absences, 2)

        update_response = self.client.post(
            reverse("staffing_rules"),
            {
                "action": "update_substitution",
                "department_id": self.engineering.id,
                "substitution_id": substitution.id,
                "max_covered_absences": 1,
            },
        )

        self.assertRedirects(update_response, reverse("staffing_rules"))
        substitution.refresh_from_db()
        self.assertEqual(substitution.max_covered_absences, 1)

        delete_response = self.client.post(
            reverse("staffing_rules"),
            {
                "action": "delete_substitution",
                "department_id": self.engineering.id,
                "substitution_id": substitution.id,
            },
        )

        self.assertRedirects(delete_response, reverse("staffing_rules"))
        self.assertFalse(ProductionGroupSubstitutionRule.objects.filter(id=substitution.id).exists())
