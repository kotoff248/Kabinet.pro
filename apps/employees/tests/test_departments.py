from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from apps.employees.models import DepartmentCoverageRule, Departments, ProductionGroupSubstitutionRule
from apps.leave.models import DepartmentStaffingRule, VacationRequest, VacationSchedule, VacationScheduleItem
from apps.leave.services.staffing import (
    build_department_group_staffing_forecast_map,
    build_department_staffing_forecast_map,
)

from .base import EmployeeTestCase


class DepartmentPageTests(EmployeeTestCase):
    def _future_period(self, offset=5, days=4):
        start_date = timezone.localdate() + timedelta(days=offset)
        return start_date, start_date + timedelta(days=days - 1)

    def _create_staffing_rule(self, department=None, min_staff_required=0, max_absent=10):
        return DepartmentStaffingRule.objects.create(
            department=department or self.engineering,
            min_staff_required=min_staff_required,
            max_absent=max_absent,
            criticality_level=3,
        )

    def _create_active_request(self, employee=None, status=VacationRequest.STATUS_APPROVED):
        start_date, end_date = self._future_period()
        return VacationRequest.objects.create(
            employee=employee or self.employee,
            start_date=start_date,
            end_date=end_date,
            vacation_type="unpaid",
            status=status,
        )

    def test_departments_page_is_scoped_for_department_head(self):
        self.client.force_login(self.department_head.user)

        response = self.client.get(reverse("departments"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.engineering.name)
        self.assertContains(response, self.department_head.full_name)
        self.assertContains(response, "Сотрудников")
        self.assertContains(response, "1")
        self.assertContains(response, "Риск состава")
        self.assertContains(response, 'data-tooltip-title="Риск состава"')
        self.assertContains(response, 'data-tooltip-title="Нагрузка отдела"')
        self.assertContains(response, 'class="department-current-card department-current-card--list department-current-card--risk-')
        self.assertContains(response, 'class="department-current-card__since"')
        self.assertContains(response, "Отдел с")
        self.assertNotContains(response, "department-card__cell--date")
        self.assertNotContains(response, "department-card--risk-")
        self.assertNotContains(response, self.hr_department.name)
        self.assertNotContains(response, "<table", html=False)
        self.assertNotContains(response, 'data-modal-open="department-create-modal"')
        self.assertContains(response, f'data-href="{reverse("department_detail", args=[self.engineering.id])}"')
        self.assertContains(response, f'data-department-id="{self.engineering.id}"')
        self.assertContains(response, "js/departments-page.js")

    def test_department_detail_shows_department_groups_and_employees(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("department_detail", args=[self.engineering.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "departments")
        self.assertContains(response, "Отделы")
        self.assertContains(response, self.engineering.name)
        self.assertContains(response, 'class="department-current-card department-current-card--risk-')
        self.assertNotContains(response, "department-card--current")
        self.assertContains(response, 'class="department-current-card__metric"', count=3)
        self.assertContains(response, 'class="department-current-card__metric department-current-card__metric--workload')
        self.assertContains(response, 'class="department-current-card__risk"')
        self.assertContains(response, "Руководитель")
        self.assertContains(response, "Отдел с")
        self.assertNotContains(response, "department-card__cell--date")
        self.assertContains(response, "Группы отдела")
        self.assertContains(response, self.engineering_group.name)
        self.assertContains(response, self.engineering_leadership_group.name)
        self.assertContains(response, 'class="page-hero__back-link"')
        self.assertContains(response, 'data-section-back-link="departments"')
        self.assertContains(response, "data-department-group-filter")
        self.assertContains(response, "data-department-detail-switch")
        self.assertContains(response, "employee-select--toolbar")
        self.assertContains(response, f'value="{reverse("department_detail", args=[self.hr_department.id])}"')
        self.assertContains(response, self.employee.full_name)
        self.assertContains(response, f'data-href="{reverse("employee_profile", args=[self.employee.id])}?from=departments"')
        self.assertContains(response, self.department_head.full_name)
        self.assertNotContains(response, self.outsider.full_name)
        self.assertNotContains(response, self.hr_group.name)
        self.assertContains(response, "js/employee-form.js")
        self.assertContains(response, "js/departments-page.js")

    def test_department_detail_group_filter_limits_employee_list(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("department_detail", args=[self.engineering.id]),
            {"group": self.engineering_leadership_group.id},
        )

        self.assertEqual(response.status_code, 200)
        employee_ids = {employee["id"] for employee in response.context["employees"]}
        self.assertIn(self.department_head.id, employee_ids)
        self.assertNotIn(self.employee.id, employee_ids)
        self.assertContains(response, f"группа {self.engineering_leadership_group.name}")

    def test_department_head_cannot_open_foreign_department_detail(self):
        self.client.force_login(self.department_head.user)

        own_response = self.client.get(reverse("department_detail", args=[self.engineering.id]))
        foreign_response = self.client.get(reverse("department_detail", args=[self.hr_department.id]))

        self.assertEqual(own_response.status_code, 200)
        self.assertRedirects(foreign_response, reverse("departments"))

    def test_department_detail_group_cards_show_staffing_metrics(self):
        DepartmentCoverageRule.objects.create(
            department=self.engineering,
            production_group=self.engineering_group,
            min_staff_required=1,
            max_absent=10,
            criticality_level=5,
        )
        self._create_active_request()
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("department_detail", args=[self.engineering.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Скоро конфликт")
        self.assertContains(response, "Инженеры ниже минимума")
        self.assertContains(response, "Сейчас отсутствует")
        self.assertContains(response, "Минимум")
        self.assertContains(response, "Резерв")
        self.assertContains(response, "Нагрузка")
        self.assertContains(response, 'data-tooltip-title="Риск состава"')
        self.assertContains(response, 'data-tooltip-title="Минимум"')
        self.assertContains(response, 'data-tooltip-title="Резерв"')

    def test_departments_page_shows_upcoming_department_conflict(self):
        self._create_staffing_rule(min_staff_required=2, max_absent=10)
        self._create_active_request()
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("departments"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Скоро конфликт")
        self.assertContains(response, "отдел ниже минимума")
        self.assertContains(response, "Макс. отсутствует")
        self.assertContains(response, "Резерв")

    def test_departments_page_shows_minimum_staffing_warning(self):
        self._create_staffing_rule(min_staff_required=1, max_absent=10)
        self._create_active_request()
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("departments"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "На минимуме")
        self.assertContains(response, "отдел на минимуме")

    def test_departments_page_shows_stable_staffing_forecast(self):
        self._create_staffing_rule(min_staff_required=0, max_absent=10)
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("departments"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Состав стабилен")
        self.assertContains(response, "30 дней · критичных рисков нет")

    def test_departments_page_shows_group_staffing_reason(self):
        self._create_staffing_rule(min_staff_required=0, max_absent=10)
        DepartmentCoverageRule.objects.create(
            department=self.engineering,
            production_group=self.engineering_group,
            min_staff_required=1,
            max_absent=10,
            criticality_level=5,
        )
        self._create_active_request()
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("departments"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Скоро конфликт")
        self.assertContains(response, "Инженеры ниже минимума")

    def test_departments_page_shows_reserve_needed_when_substitution_covers_risk(self):
        self._create_staffing_rule(min_staff_required=0, max_absent=10)
        DepartmentCoverageRule.objects.create(
            department=self.engineering,
            production_group=self.engineering_group,
            min_staff_required=1,
            max_absent=10,
            criticality_level=5,
        )
        ProductionGroupSubstitutionRule.objects.create(
            department=self.engineering,
            source_group=self.engineering_group,
            substitute_group=self.engineering_leadership_group,
            max_covered_absences=1,
        )
        self._create_active_request()
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("departments"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Нужен резерв")
        self.assertContains(response, "Инженеры: нужен резерв")

    def test_converted_paid_request_counts_once_in_department_forecast(self):
        self._create_staffing_rule(min_staff_required=0, max_absent=10)
        start_date, end_date = self._future_period()
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date=start_date,
            end_date=end_date,
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )
        schedule = VacationSchedule.objects.create(
            year=start_date.year,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=start_date,
            end_date=end_date,
            vacation_type="paid",
            chargeable_days=4,
            status=VacationScheduleItem.STATUS_APPROVED,
            created_from_vacation_request=request_obj,
        )

        forecast = build_department_staffing_forecast_map([self.engineering])[self.engineering.id]

        self.assertEqual(forecast["peak_absent_count"], 1)

    def test_converted_paid_request_counts_once_in_group_forecast(self):
        start_date, end_date = self._future_period()
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date=start_date,
            end_date=end_date,
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )
        schedule = VacationSchedule.objects.create(
            year=start_date.year,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=start_date,
            end_date=end_date,
            vacation_type="paid",
            chargeable_days=4,
            status=VacationScheduleItem.STATUS_APPROVED,
            created_from_vacation_request=request_obj,
        )

        forecast = build_department_group_staffing_forecast_map(
            self.engineering,
            groups=[self.engineering_group],
        )[self.engineering_group.id]

        self.assertEqual(forecast["peak_absent_count"], 1)

    def test_hr_and_enterprise_head_can_view_all_departments(self):
        for actor in (self.hr_employee, self.enterprise_head):
            self.client.force_login(actor.user)
            response = self.client.get(reverse("departments"))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, self.engineering.name)
            self.assertContains(response, self.hr_department.name)
            self.assertContains(response, 'class="department-current-card department-current-card--list department-current-card--risk-')
            self.assertContains(response, f'data-href="{reverse("department_detail", args=[self.engineering.id])}"')
            self.assertNotContains(response, "<thead>", html=False)

    def test_only_hr_sees_department_create_controls(self):
        self.client.force_login(self.hr_employee.user)
        hr_response = self.client.get(reverse("departments"))

        self.assertContains(hr_response, 'data-modal-open="department-create-modal"')
        self.assertContains(hr_response, 'id="department-create-modal"')
        self.assertContains(hr_response, 'name="head"')
        self.assertContains(hr_response, self.available_department_head.full_name)
        self.assertNotContains(hr_response, f'data-value="{self.department_head.id}"')
        self.assertContains(hr_response, 'data-employee-form')
        self.assertContains(hr_response, 'data-employee-submit disabled')

        for actor in (self.department_head, self.enterprise_head):
            self.client.force_login(actor.user)
            response = self.client.get(reverse("departments"))
            self.assertNotContains(response, 'data-modal-open="department-create-modal"')
            self.assertNotContains(response, 'id="department-create-modal"')

    def test_hr_can_create_department_without_head(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(
            reverse("departments"),
            {
                "name": "Новый отдел",
                "head": "",
            },
        )

        self.assertRedirects(response, reverse("departments"))
        self.assertTrue(Departments.objects.filter(name="Новый отдел", head__isnull=True).exists())

    def test_hr_can_create_department_with_head_and_relink_employee(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(
            reverse("departments"),
            {
                "name": "Новый производственный блок",
                "head": self.available_department_head.id,
            },
        )

        self.assertRedirects(response, reverse("departments"))
        created_department = Departments.objects.get(name="Новый производственный блок")
        self.available_department_head.refresh_from_db()

        self.assertEqual(created_department.head, self.available_department_head)
        self.assertEqual(self.available_department_head.department, created_department)

    def test_hr_cannot_create_department_with_duplicate_name(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(
            reverse("departments"),
            {
                "name": self.engineering.name.lower(),
                "head": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Отдел с таким названием уже существует.")
        self.assertContains(response, 'id="department-create-modal"')
        self.assertContains(response, 'class="app-modal is-open"')

    def test_hr_cannot_assign_department_head_linked_elsewhere(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(
            reverse("departments"),
            {
                "name": "Отдел с конфликтом",
                "head": self.department_head.id,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Выберите корректный вариант.")
        self.assertFalse(Departments.objects.filter(name="Отдел с конфликтом").exists())

    def test_non_hr_cannot_create_department(self):
        self.client.force_login(self.enterprise_head.user)

        response = self.client.post(
            reverse("departments"),
            {
                "name": "Закрытый отдел",
                "head": "",
            },
        )

        self.assertRedirects(response, reverse("departments"))
        self.assertFalse(Departments.objects.filter(name="Закрытый отдел").exists())
