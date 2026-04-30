from django.urls import reverse

from apps.employees.models import Departments

from .base import EmployeeTestCase


class DepartmentPageTests(EmployeeTestCase):
    def test_departments_page_is_scoped_for_department_head(self):
        self.client.force_login(self.department_head.user)

        response = self.client.get(reverse("departments"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.engineering.name)
        self.assertContains(response, self.department_head.full_name)
        self.assertContains(response, "Сотрудников")
        self.assertContains(response, "1")
        self.assertNotContains(response, self.hr_department.name)
        self.assertNotContains(response, "<table", html=False)
        self.assertNotContains(response, 'data-modal-open="department-create-modal"')
        self.assertContains(response, f'data-href="{reverse("employees")}?department={self.engineering.id}"')

    def test_hr_and_enterprise_head_can_view_all_departments(self):
        for actor in (self.hr_employee, self.enterprise_head):
            self.client.force_login(actor.user)
            response = self.client.get(reverse("departments"))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, self.engineering.name)
            self.assertContains(response, self.hr_department.name)
            self.assertContains(response, 'class="department-card"')
            self.assertContains(response, f'data-href="{reverse("employees")}?department={self.engineering.id}"')
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
