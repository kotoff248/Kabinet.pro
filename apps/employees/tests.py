from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.services import sync_employee_user
from apps.employees.forms import EmployeeCreateForm
from apps.employees.models import Departments, Employees
from apps.leave.models import VacationRequest, VacationSchedule, VacationScheduleItem


class EmployeeManagementTests(TestCase):
    def setUp(self):
        self.engineering = Departments.objects.create(name="Engineering")
        self.hr_department = Departments.objects.create(name="HR")
        joined_date = timezone.localdate() - timedelta(days=900)

        self.hr_employee = Employees.objects.create(
            last_name="Кадрова",
            first_name="Анна",
            middle_name="Сергеевна",
            login="hr-login",
            position="HR",
            date_joined=joined_date,
            annual_paid_leave_days=52,
            department=self.hr_department,
            role=Employees.ROLE_HR,
        )
        sync_employee_user(self.hr_employee, raw_password="hr-pass")

        self.department_head = Employees.objects.create(
            last_name="Руководов",
            first_name="Павел",
            middle_name="Игоревич",
            login="dept-head-login",
            position="Руководитель отдела",
            date_joined=joined_date,
            annual_paid_leave_days=52,
            department=self.engineering,
            role=Employees.ROLE_DEPARTMENT_HEAD,
        )
        sync_employee_user(self.department_head, raw_password="dept-head-pass")

        self.available_department_head = Employees.objects.create(
            last_name="Запаснов",
            first_name="Алексей",
            middle_name="Сергеевич",
            login="available-head-login",
            position="Руководитель отдела",
            date_joined=joined_date,
            annual_paid_leave_days=52,
            role=Employees.ROLE_DEPARTMENT_HEAD,
        )
        sync_employee_user(self.available_department_head, raw_password="available-head-pass")

        self.enterprise_head = Employees.objects.create(
            last_name="Директорова",
            first_name="Мария",
            middle_name="Петровна",
            login="enterprise-head-login",
            position="Директор",
            date_joined=joined_date,
            annual_paid_leave_days=52,
            department=self.hr_department,
            role=Employees.ROLE_ENTERPRISE_HEAD,
        )
        sync_employee_user(self.enterprise_head, raw_password="enterprise-pass")

        self.authorized_person = Employees.objects.create(
            last_name="Админова",
            first_name="Инна",
            middle_name="Олеговна",
            login="authorized-login",
            position="Уполномоченное лицо",
            date_joined=joined_date,
            annual_paid_leave_days=52,
            role=Employees.ROLE_AUTHORIZED_PERSON,
        )
        sync_employee_user(self.authorized_person, raw_password="authorized-pass")

        self.employee = Employees.objects.create(
            last_name="Сотрудник",
            first_name="Иван",
            middle_name="Игоревич",
            login="employee-login",
            position="Специалист",
            date_joined=joined_date,
            annual_paid_leave_days=52,
            department=self.engineering,
            role=Employees.ROLE_EMPLOYEE,
        )
        sync_employee_user(self.employee, raw_password="employee-pass")

        self.outsider = Employees.objects.create(
            last_name="Чужой",
            first_name="Олег",
            middle_name="Петрович",
            login="outsider-login",
            position="Аналитик",
            date_joined=joined_date,
            annual_paid_leave_days=52,
            department=self.hr_department,
            role=Employees.ROLE_EMPLOYEE,
        )
        sync_employee_user(self.outsider, raw_password="outsider-pass")

    def test_duplicate_login_is_rejected(self):
        form = EmployeeCreateForm(
            data={
                "login": "employee-login",
                "last_name": "Новый",
                "first_name": "Сотрудник",
                "middle_name": "Андреевич",
                "position": "Аналитик",
                "date_joined": "2026-01-01",
                "annual_paid_leave_days": 52,
                "department": self.engineering.id,
                "role": Employees.ROLE_EMPLOYEE,
                "password": "new-user-pass",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("login", form.errors)

    def test_employee_full_name_property(self):
        self.assertEqual(self.employee.full_name, "Сотрудник Иван Игоревич")

    def test_department_head_is_linked_to_department(self):
        self.engineering.refresh_from_db()
        self.assertEqual(self.engineering.head, self.department_head)

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

    def test_hr_can_update_employee_profile(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(
            reverse("update_employee", args=[self.employee.id]),
            {
                "login": "employee-updated",
                "last_name": "Обновленный",
                "first_name": "Иван",
                "middle_name": "Игоревич",
                "position": "Ведущий специалист",
                "role": Employees.ROLE_EMPLOYEE,
                "date_joined": self.employee.date_joined.isoformat(),
                "annual_paid_leave_days": 28,
                "department": self.hr_department.id,
                "password": "",
                "next_path": reverse("main"),
            },
        )

        self.employee.refresh_from_db()

        self.assertRedirects(response, reverse("main"))
        self.assertEqual(self.employee.login, "employee-updated")
        self.assertEqual(self.employee.department, self.hr_department)
        self.assertEqual(self.employee.annual_paid_leave_days, 52)

    def test_department_head_cannot_update_employee_profile(self):
        self.client.force_login(self.department_head.user)

        response = self.client.post(
            reverse("update_employee", args=[self.employee.id]),
            {
                "login": "blocked-update",
                "last_name": self.employee.last_name,
                "first_name": self.employee.first_name,
                "middle_name": self.employee.middle_name,
                "position": self.employee.position,
                "role": Employees.ROLE_EMPLOYEE,
                "date_joined": self.employee.date_joined.isoformat(),
                "annual_paid_leave_days": 52,
                "department": self.engineering.id,
                "password": "",
                "next_path": reverse("main"),
            },
        )

        self.employee.refresh_from_db()

        self.assertRedirects(response, reverse("main"))
        self.assertEqual(self.employee.login, "employee-login")

    def test_enterprise_head_cannot_update_employee_profile(self):
        self.client.force_login(self.enterprise_head.user)

        response = self.client.post(
            reverse("update_employee", args=[self.employee.id]),
            {
                "login": "blocked-update",
                "last_name": self.employee.last_name,
                "first_name": self.employee.first_name,
                "middle_name": self.employee.middle_name,
                "position": self.employee.position,
                "role": Employees.ROLE_EMPLOYEE,
                "date_joined": self.employee.date_joined.isoformat(),
                "annual_paid_leave_days": 52,
                "department": self.engineering.id,
                "password": "",
                "next_path": reverse("main"),
            },
        )

        self.employee.refresh_from_db()

        self.assertRedirects(response, reverse("main"))
        self.assertEqual(self.employee.login, "employee-login")

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

    def test_hr_main_page_renders_with_edit_modal(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("main"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-modal-open="employee-edit-modal"')
        self.assertContains(response, 'id="employee-edit-modal"')
        self.assertContains(response, 'name="role"')
        self.assertContains(response, 'data-employee-form')
        self.assertContains(response, 'data-employee-submit')

    def test_employee_profile_renders_with_edit_modal_for_hr(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="employee-edit-modal"')
        self.assertContains(response, 'name="role"')
        self.assertContains(response, 'app-modal__dialog app-modal__dialog--employee')
        self.assertContains(response, "Можно запросить", count=2)
        self.assertContains(response, "Начислено по стажу")
        self.assertContains(response, 'data-modal-open="employee-delete-modal"')
        self.assertContains(response, 'id="employee-delete-modal"')
        self.assertContains(response, reverse("delete_employee", args=[self.employee.id]))

    def test_enterprise_head_can_view_profile_without_edit_modal(self):
        self.client.force_login(self.enterprise_head.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="employee-edit-modal"')
        self.assertNotContains(response, 'data-modal-open="employee-delete-modal"')

    def test_hr_can_soft_delete_employee_and_disable_login(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(reverse("delete_employee", args=[self.employee.id]))

        self.employee.refresh_from_db()
        self.engineering.refresh_from_db()
        self.assertRedirects(response, reverse("employees"))
        self.assertFalse(self.employee.is_active_employee)
        self.assertFalse(self.employee.user.is_active)

        employees_response = self.client.get(reverse("employees"))
        self.assertNotContains(employees_response, self.employee.full_name)

        departments_response = self.client.get(reverse("departments"))
        self.assertContains(departments_response, self.engineering.name)
        self.assertContains(departments_response, "1")

        self.client.logout()
        login_response = self.client.post(
            reverse("login"),
            {
                "username": "employee-login",
                "password": "employee-pass",
                "user_type": "employee",
            },
        )
        self.assertEqual(login_response.status_code, 200)
        self.assertTrue(login_response.context["error"])

    def test_hr_can_soft_delete_department_head_and_clear_department_head_link(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.post(reverse("delete_employee", args=[self.department_head.id]))

        self.department_head.refresh_from_db()
        self.engineering.refresh_from_db()
        self.assertRedirects(response, reverse("employees"))
        self.assertFalse(self.department_head.is_active_employee)
        self.assertIsNone(self.engineering.head)

    def test_hr_cannot_delete_enterprise_head_authorized_person_or_self(self):
        self.client.force_login(self.hr_employee.user)

        enterprise_response = self.client.post(reverse("delete_employee", args=[self.enterprise_head.id]))
        authorized_response = self.client.post(reverse("delete_employee", args=[self.authorized_person.id]))
        self_response = self.client.post(reverse("delete_employee", args=[self.hr_employee.id]))

        self.enterprise_head.refresh_from_db()
        self.authorized_person.refresh_from_db()
        self.hr_employee.refresh_from_db()
        self.assertRedirects(enterprise_response, reverse("employee_profile", args=[self.enterprise_head.id]))
        self.assertEqual(authorized_response.status_code, 404)
        self.assertRedirects(self_response, reverse("employee_profile", args=[self.hr_employee.id]))
        self.assertTrue(self.enterprise_head.is_active_employee)
        self.assertTrue(self.authorized_person.is_active_employee)
        self.assertTrue(self.hr_employee.is_active_employee)

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

    def test_employees_page_uses_shared_create_modal_shell_for_hr(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("employees"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="employee-create-modal"')
        self.assertContains(response, 'data-modal-open="employee-create-modal"')
        self.assertContains(response, 'class="department-summary-card department-summary-card--employees"')
        self.assertContains(response, 'id="employees-count"')
        self.assertContains(response, 'id="department"')
        self.assertContains(response, 'data-employee-submit disabled')
        self.assertContains(response, "js/employee-form.js")
        self.assertContains(response, "js/employees-page.js")
        self.assertContains(response, 'class="employee-card employee-row employee-row-clickable is-clickable"')
        self.assertNotContains(response, "<table", html=False)

    def test_department_head_sees_only_managed_department_on_employees_page(self):
        self.client.force_login(self.department_head.user)

        response = self.client.get(reverse("employees"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.employee.full_name)
        self.assertNotContains(response, self.outsider.full_name)
        self.assertNotContains(response, 'employee-create-modal')
        self.assertNotContains(response, 'id="department"')
        self.assertContains(response, 'class="department-summary-card department-summary-card--employees"')

    def test_employees_page_ajax_response_contains_card_fields(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("employees"),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["employees"])
        first_employee = payload["employees"][0]
        self.assertIn("department_name", first_employee)
        self.assertIn("status_label", first_employee)
        self.assertIn("profile_url", first_employee)

    def test_employees_search_filters_by_name_status_and_department(self):
        matching_outsider = Employees.objects.create(
            last_name=self.employee.last_name,
            first_name=self.employee.first_name,
            middle_name="Search",
            login="matching-outsider-login",
            position="Analyst",
            date_joined=timezone.localdate(),
            annual_paid_leave_days=52,
            department=self.hr_department,
            role=Employees.ROLE_EMPLOYEE,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("employees"),
            {
                "search": self.employee.first_name,
                "status": "True",
                "department": self.engineering.id,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        employee_ids = {employee["id"] for employee in response.json()["employees"]}
        self.assertIn(self.employee.id, employee_ids)
        self.assertNotIn(matching_outsider.id, employee_ids)
        self.assertNotIn(self.outsider.id, employee_ids)

    def test_employees_search_respects_department_head_scope(self):
        self.client.force_login(self.department_head.user)

        response = self.client.get(
            reverse("employees"),
            {"search": self.outsider.first_name},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.status_code, 200)
        employee_ids = {employee["id"] for employee in response.json()["employees"]}
        self.assertNotIn(self.outsider.id, employee_ids)

    def test_employees_page_uses_current_schedule_for_status(self):
        self.client.force_login(self.hr_employee.user)
        today = timezone.localdate()
        schedule = VacationSchedule.objects.create(
            year=today.year,
            status=VacationSchedule.STATUS_APPROVED,
            created_by=self.hr_employee,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=today,
            end_date=today,
            vacation_type="paid",
            chargeable_days=1,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        response = self.client.get(reverse("employees"))
        employee_row = next(employee for employee in response.context["employees"] if employee["id"] == self.employee.id)

        self.assertFalse(employee_row["is_working"])
        self.assertEqual(employee_row["status_label"], "В отпуске")
        self.assertContains(
            response,
            '<span class="employee-status-badge employee-status-badge--vacation">В отпуске</span>',
            html=True,
        )

    def test_employees_status_filter_uses_current_schedule(self):
        self.client.force_login(self.hr_employee.user)
        today = timezone.localdate()
        schedule = VacationSchedule.objects.create(
            year=today.year,
            status=VacationSchedule.STATUS_APPROVED,
            created_by=self.hr_employee,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=today,
            end_date=today,
            vacation_type="paid",
            chargeable_days=1,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        vacation_response = self.client.get(
            reverse("employees"),
            {"status": "False"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        working_response = self.client.get(
            reverse("employees"),
            {"status": "True"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        vacation_ids = {employee["id"] for employee in vacation_response.json()["employees"]}
        working_ids = {employee["id"] for employee in working_response.json()["employees"]}
        self.assertIn(self.employee.id, vacation_ids)
        self.assertNotIn(self.employee.id, working_ids)

    def test_authorized_person_is_hidden_from_employee_registry(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("employees"))
        ajax_response = self.client.get(reverse("employees"), HTTP_X_REQUESTED_WITH="XMLHttpRequest")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["employees_count"], 6)
        self.assertNotIn(self.authorized_person.id, [employee["id"] for employee in response.context["employees"]])
        self.assertNotIn(self.authorized_person.id, [employee["id"] for employee in ajax_response.json()["employees"]])

    def test_hr_cannot_view_or_update_authorized_person_profile(self):
        self.client.force_login(self.hr_employee.user)

        profile_response = self.client.get(reverse("employee_profile", args=[self.authorized_person.id]))
        update_response = self.client.post(
            reverse("update_employee", args=[self.authorized_person.id]),
            {
                "login": "admin_1",
                "last_name": "Hidden",
                "first_name": "Person",
                "middle_name": "Service",
                "position": "Service",
                "role": Employees.ROLE_AUTHORIZED_PERSON,
                "date_joined": self.authorized_person.date_joined.isoformat(),
                "annual_paid_leave_days": 52,
                "department": "",
                "password": "",
            },
        )

        self.authorized_person.refresh_from_db()
        self.assertEqual(profile_response.status_code, 404)
        self.assertEqual(update_response.status_code, 404)
        self.assertEqual(self.authorized_person.full_name, "")
        self.assertEqual(self.authorized_person.position, "")

    def test_employee_form_does_not_offer_authorized_person_role(self):
        form = EmployeeCreateForm()

        role_values = {value for value, _label in form.fields["role"].choices}

        self.assertNotIn(Employees.ROLE_AUTHORIZED_PERSON, role_values)

    def test_employees_page_uses_bulk_leave_summary_without_n_plus_one_queries(self):
        for index in range(12):
            extra_employee = Employees.objects.create(
                last_name=f"Сотрудник{index}",
                first_name="Тест",
                middle_name="Иванович",
                login=f"bulk-employee-{index}",
                position="Специалист",
                annual_paid_leave_days=52,
                department=self.engineering,
                role=Employees.ROLE_EMPLOYEE,
            )
            sync_employee_user(extra_employee, raw_password="bulk-pass")

        self.client.force_login(self.hr_employee.user)

        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("employees"))

        self.assertEqual(response.status_code, 200)
        self.assertLess(len(queries), 20)

    def test_departments_page_is_scoped_for_department_head(self):
        self.client.force_login(self.department_head.user)

        response = self.client.get(reverse("departments"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.engineering.name)
        self.assertContains(response, self.department_head.full_name)
        self.assertContains(response, "Численность")
        self.assertContains(response, "1")
        self.assertNotContains(response, self.hr_department.name)
        self.assertNotContains(response, "<table", html=False)
        self.assertNotContains(response, 'data-modal-open="department-create-modal"')

    def test_hr_and_enterprise_head_can_view_all_departments(self):
        for actor in (self.hr_employee, self.enterprise_head):
            self.client.force_login(actor.user)
            response = self.client.get(reverse("departments"))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, self.engineering.name)
            self.assertContains(response, self.hr_department.name)
            self.assertContains(response, 'class="department-card"')
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


class EmployeeNameMigrationTests(TransactionTestCase):
    migrate_from = ("employees", "0002_alter_departments_options_alter_employees_options_and_more")
    migrate_to = ("employees", "0003_split_employee_name")

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate([self.migrate_from])
        self.old_apps = self.executor.loader.project_state([self.migrate_from]).apps

    def migrate_forwards(self):
        self.executor.loader.build_graph()
        self.executor.migrate([self.migrate_to])
        return self.executor.loader.project_state([self.migrate_to]).apps

    def test_migration_splits_existing_name(self):
        Departments = self.old_apps.get_model("employees", "Departments")
        Employees = self.old_apps.get_model("employees", "Employees")

        department = Departments.objects.create(name="Migration Department")
        employee = Employees.objects.create(
            name="Иван Иванович Петров",
            login="migration-user",
            position="Аналитик",
            vacation_days=28,
            department=department,
        )

        new_apps = self.migrate_forwards()
        NewEmployees = new_apps.get_model("employees", "Employees")
        migrated_employee = NewEmployees.objects.get(pk=employee.pk)

        self.assertEqual(migrated_employee.last_name, "Иван")
        self.assertEqual(migrated_employee.first_name, "Иванович")
        self.assertEqual(migrated_employee.middle_name, "Петров")
