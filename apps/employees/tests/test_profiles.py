from datetime import date

from django.urls import reverse
from django.utils import timezone

from apps.leave.models import VacationRequest, VacationSchedule, VacationScheduleItem

from .base import EmployeeTestCase


class EmployeeProfileTests(EmployeeTestCase):
    def test_employee_cannot_view_another_profile(self):
        self.client.force_login(self.employee.user)

        response = self.client.get(reverse("employee_profile", args=[self.department_head.id]))

        self.assertRedirects(response, reverse("main"))

    def test_department_head_can_view_only_own_department_profile(self):
        self.client.force_login(self.department_head.user)

        own_department_response = self.client.get(reverse("employee_profile", args=[self.employee.id]))
        foreign_department_response = self.client.get(reverse("employee_profile", args=[self.outsider.id]))

        self.assertEqual(own_department_response.status_code, 200)
        self.assertRedirects(foreign_department_response, reverse("main"))

    def test_main_page_uses_dedicated_template_for_regular_employee(self):
        self.client.force_login(self.employee.user)

        response = self.client.get(reverse("main"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "main.html")
        self.assertNotContains(response, 'data-modal-open="employee-edit-modal"')
        self.assertNotContains(response, 'id="employee-edit-modal"')
        self.assertContains(response, "js/employee-form.js")
        self.assertNotContains(response, "js/employees-page.js")
        self.assertContains(response, "Можно запросить", count=2)
        self.assertContains(response, "Можно запланировать сейчас")
        self.assertNotContains(response, "page-hero__chip")
        self.assertContains(response, "Начислено по стажу")
        self.assertContains(response, "Можно запланировать сейчас")
        self.assertContains(response, "Статус")
        self.assertContains(response, "Работает")
        self.assertContains(response, "employee-status-badge employee-status-badge--working")

    def test_hr_main_page_renders_with_edit_modal(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("main"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-modal-open="employee-edit-modal"')
        self.assertContains(response, 'id="employee-edit-modal"')
        self.assertContains(response, 'name="role"')
        self.assertContains(response, 'data-employee-form')
        self.assertContains(response, 'data-employee-submit')
        self.assertNotContains(response, 'id="edit_employee_annual_paid_leave_days"')

    def test_employee_profile_renders_with_edit_modal_for_hr(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="employee-edit-modal"')
        self.assertContains(response, 'name="role"')
        self.assertContains(response, 'app-modal__dialog app-modal__dialog--employee')
        self.assertNotContains(response, 'id="edit_employee_annual_paid_leave_days"')
        self.assertContains(response, "Можно запросить", count=2)
        self.assertContains(response, "Начислено по стажу")
        self.assertContains(response, "Статус")
        self.assertContains(response, "Работает")
        self.assertContains(response, "employee-status-badge employee-status-badge--working")
        self.assertContains(response, 'data-modal-open="employee-delete-modal"')
        self.assertContains(response, 'id="employee-delete-modal"')
        self.assertContains(response, reverse("delete_employee", args=[self.employee.id]))

    def test_employee_profile_shows_planned_vacations_with_calendar_link(self):
        year = timezone.localdate().year
        schedule = VacationSchedule.objects.create(
            year=year,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(year, 10, 1),
            end_date=date(year, 10, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Запланированные отпуска")
        self.assertContains(response, "Годовой график")
        self.assertContains(response, "Открыть в графике")
        self.assertContains(
            response,
            f'{reverse("calendar")}?view=month&amp;year={year}&amp;month=10&amp;employee={self.employee.id}',
        )

    def test_enterprise_head_can_view_profile_without_edit_modal(self):
        self.client.force_login(self.enterprise_head.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="employee-edit-modal"')
        self.assertNotContains(response, 'data-modal-open="employee-delete-modal"')

    def test_main_page_renders_requests_as_cards_without_table_header(self):
        VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-08-10",
            end_date="2026-08-15",
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
        )
        self.client.force_login(self.employee.user)

        response = self.client.get(reverse("main"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="vacation-request-card is-clickable"')
        self.assertContains(response, "Дата начала")
        self.assertContains(response, "Дата окончания")
        self.assertContains(response, "Тип отпуска")
        self.assertContains(response, "Статус")
        self.assertNotContains(response, "<thead>", html=False)

    def test_main_page_empty_requests_state_does_not_render_table_markup(self):
        self.client.force_login(self.employee.user)

        response = self.client.get(reverse("main"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Заявок на отпуск пока нет.")
        self.assertNotContains(response, "<table", html=False)
