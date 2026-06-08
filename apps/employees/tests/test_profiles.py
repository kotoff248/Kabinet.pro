from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urlsplit

from django.urls import reverse
from django.utils import timezone

from apps.leave.models import VacationRequest, VacationSchedule, VacationScheduleItem
from apps.leave.services.schedule_changes import approve_schedule_change_request, create_schedule_change_request

from .base import EmployeeTestCase
from ..page_contexts import _format_vacation_count_label


class EmployeeProfileTests(EmployeeTestCase):
    def test_planned_vacation_count_label_uses_russian_plural_forms(self):
        cases = (
            (0, "0 отпусков"),
            (1, "1 отпуск"),
            (2, "2 отпуска"),
            (4, "4 отпуска"),
            (5, "5 отпусков"),
            (11, "11 отпусков"),
            (21, "21 отпуск"),
            (24, "24 отпуска"),
        )

        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(_format_vacation_count_label(value), expected)

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

    def test_employee_profile_from_applications_keeps_applications_navigation(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]), {"from": "applications"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "applications")
        self.assertContains(response, "К заявкам")
        self.assertContains(response, "page-hero__center")
        self.assertContains(response, "page-hero__back-link")
        self.assertContains(response, 'data-section-back-link="applications"')
        self.assertContains(response, f'href="{reverse("applications")}"')
        self.assertContains(
            response,
            'data-sidebar-key="applications" aria-label="Заявки" title="Заявки" aria-current="page"',
        )
        self.assertNotContains(
            response,
            'data-sidebar-key="employees" aria-label="Сотрудники" title="Сотрудники" aria-current="page"',
        )

    def test_employee_profile_from_employees_keeps_employees_navigation(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]), {"from": "employees"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "employees")
        self.assertContains(response, "К сотрудникам")
        self.assertContains(response, "page-hero__center")
        self.assertContains(response, "page-hero__back-link")
        self.assertContains(response, 'data-section-back-link="employees"')
        self.assertContains(response, f'href="{reverse("employees")}"')
        self.assertContains(
            response,
            'data-sidebar-key="employees" aria-label="Сотрудники" title="Сотрудники" aria-current="page"',
        )

    def test_employee_profile_without_source_does_not_show_section_back_link(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "employees")
        self.assertNotContains(response, "К заявкам")
        self.assertNotContains(response, "К сотрудникам")
        self.assertNotContains(response, "data-section-back-link")

    def test_employee_profile_from_calendar_keeps_calendar_navigation(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]), {"from": "calendar"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "calendar")
        self.assertContains(response, "К графику")
        self.assertNotContains(response, "data-section-back-link")

    def test_employee_profile_from_preference_readiness_uses_explicit_back_link(self):
        self.client.force_login(self.hr_employee.user)
        readiness_url = "/preferences/2027/readiness/?status=all"

        response = self.client.get(
            reverse("employee_profile", args=[self.employee.id]),
            {
                "from": "preferences",
                "back_url": readiness_url,
                "back_label": "К сбору",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "calendar")
        self.assertContains(response, f'href="{readiness_url}"')
        self.assertContains(response, "К сбору")

    def test_employee_profile_ignores_unknown_source(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]), {"from": "unknown"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "employees")
        self.assertNotContains(response, "data-section-back-link")

    def test_employee_profile_from_departments_keeps_departments_navigation(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]), {"from": "departments"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "departments")
        self.assertContains(response, "К отделам")
        self.assertContains(response, 'data-section-back-link="departments"')
        self.assertContains(
            response,
            'data-sidebar-key="departments" aria-label="Отделы" title="Отделы" aria-current="page"',
        )
        self.assertNotContains(
            response,
            'data-sidebar-key="employees" aria-label="Сотрудники" title="Сотрудники" aria-current="page"',
        )

    def test_employee_profile_from_analytics_keeps_analytics_navigation(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]), {"from": "analytics"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "analytics")
        self.assertContains(response, "К аналитике")
        self.assertContains(
            response,
            'data-sidebar-key="analytics" aria-label="Аналитика" title="Аналитика" aria-current="page"',
        )
        self.assertNotContains(
            response,
            'data-sidebar-key="calendar" aria-label="График" title="График" aria-current="page"',
        )

    def test_employee_profile_uses_explicit_back_link_to_department_group(self):
        department_group_url = f"{reverse('department_detail', args=[self.engineering.id])}?group={self.engineering_group.id}"
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("employee_profile", args=[self.employee.id]),
            {
                "from": "departments",
                "back_url": department_group_url,
                "back_label": "К группам",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "departments")
        self.assertContains(response, "К группам")
        self.assertContains(response, f'href="{department_group_url}"')
        self.assertNotContains(response, 'data-section-back-link="departments"')

    def test_employee_profile_from_vacation_detail_returns_to_that_request(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-12-15",
            end_date="2026-12-17",
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("employee_profile", args=[self.employee.id]),
            {"from": "applications", "return_to": "vacation", "vacation_id": request_obj.id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "applications")
        self.assertContains(response, "К заявке")
        self.assertContains(response, f'href="{reverse("vacation_detail", args=[request_obj.id])}?from=applications"')
        self.assertNotContains(response, 'data-section-back-link="applications"')

    def test_employee_profile_from_transfer_detail_returns_to_that_transfer(self):
        schedule = VacationSchedule.objects.create(
            year=2027,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 7, 1),
            end_date=date(2027, 7, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.employee,
            new_start_date=date(2027, 8, 1),
            new_end_date=date(2027, 8, 14),
            reason="Прошу перенести отпуск.",
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("employee_profile", args=[self.employee.id]),
            {"from": "applications", "return_to": "transfer", "transfer_id": change_request.id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "applications")
        self.assertContains(response, "К переносу")
        self.assertContains(
            response,
            f'href="{reverse("schedule_change_detail", args=[change_request.id])}?from=applications"',
        )
        self.assertNotContains(response, 'data-section-back-link="applications"')

    def test_employee_profile_ignores_applications_source_without_access(self):
        self.client.force_login(self.employee.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]), {"from": "applications"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["sidebar_section"], "profile")
        self.assertNotContains(response, "К заявкам")
        self.assertNotContains(response, "data-section-back-link")

    def test_main_page_uses_dedicated_template_for_regular_employee(self):
        self.client.force_login(self.employee.user)

        response = self.client.get(reverse("main"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "main.html")
        self.assertNotContains(response, 'data-modal-open="employee-edit-modal"')
        self.assertNotContains(response, 'id="employee-edit-modal"')
        self.assertContains(response, "js/components/employee-form.js")
        self.assertNotContains(response, "js/pages/employees.js")
        self.assertNotContains(response, "page-hero__chip")
        self.assertContains(response, "Сводка сотрудника")
        self.assertContains(response, "employee-summary__identity")
        self.assertContains(response, "employee-summary__details")
        self.assertContains(response, "employee-summary__joined-row")
        self.assertContains(response, 'class="employee-summary__detail-card ', count=6)
        self.assertContains(response, "employee-summary__detail-card--position")
        self.assertContains(response, "employee-summary__detail-card--department")
        self.assertContains(response, "employee-summary__detail-card--group")
        self.assertContains(response, "employee-summary__detail-card--login")
        self.assertContains(response, "employee-summary__detail-card--schedule")
        self.assertContains(response, "employee-summary__detail-card--status")
        self.assertNotContains(response, "employee-summary__meta")
        self.assertNotContains(response, "employee-summary__status")
        self.assertNotContains(response, "employee-summary__markers")
        self.assertContains(response, self.employee.position)
        self.assertContains(response, self.engineering.name)
        self.assertContains(response, self.engineering_group.name)
        self.assertContains(response, self.employee.login)
        self.assertContains(response, "Группа отдела")
        self.assertContains(response, "График")
        self.assertContains(response, "Нет отпуска")
        self.assertContains(response, "calendar_modal=employee_detail")
        self.assertContains(response, "data-schedule-status-tooltip")
        self.assertContains(response, 'data-tooltip-title="Нет отпуска"')
        self.assertContains(response, "Доступно сейчас")
        self.assertContains(response, "Ближайший отпуск")
        self.assertContains(response, "Занято графиком")
        self.assertContains(response, "В ожидании заявок")
        self.assertContains(response, "event_upcoming")
        self.assertContains(response, "Дата начала работы")
        self.assertContains(response, "Баланс по рабочим годам")
        self.assertContains(response, "Рабочий год")
        self.assertContains(response, "Право")
        self.assertContains(response, "Использовано")
        self.assertContains(response, "В резерве")
        self.assertContains(response, "Остаток")
        self.assertContains(response, "Использовать до")
        self.assertNotContains(response, "Показать расшифровку баланса")
        self.assertNotContains(response, 'id="employee-balance-modal"')
        self.assertContains(response, 'data-profile-section="schedule"')
        self.assertNotContains(response, "Начислено по стажу")
        self.assertNotContains(response, "Факт. баланс")
        self.assertNotContains(response, "Авансом")
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
        self.assertContains(response, "employee-summary__marker--hr")
        self.assertContains(response, "employee-summary__detail-card--login")
        content = response.content.decode(response.charset or "utf-8")
        self.assertLess(
            content.index("employee-summary__marker--hr"),
            content.index("Дата начала работы"),
        )

    def test_employee_profile_renders_with_edit_modal_for_hr(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="employee-edit-modal"')
        self.assertContains(response, 'name="role"')
        self.assertContains(response, 'app-modal__dialog app-modal__dialog--employee')
        self.assertNotContains(response, 'id="edit_employee_annual_paid_leave_days"')
        self.assertContains(response, "Сводка сотрудника")
        self.assertContains(response, "employee-summary__identity")
        self.assertContains(response, "employee-summary__details")
        self.assertContains(response, 'class="employee-summary__detail-card ', count=6)
        self.assertContains(response, "employee-summary__detail-card--position")
        self.assertContains(response, "employee-summary__detail-card--department")
        self.assertContains(response, "employee-summary__detail-card--group")
        self.assertContains(response, "employee-summary__detail-card--login")
        self.assertContains(response, "employee-summary__detail-card--schedule")
        self.assertContains(response, "employee-summary__detail-card--status")
        self.assertNotContains(response, "employee-summary__meta")
        self.assertNotContains(response, "employee-summary__status")
        self.assertNotContains(response, "employee-summary__markers")
        self.assertContains(response, self.employee.position)
        self.assertContains(response, self.engineering.name)
        self.assertContains(response, self.engineering_group.name)
        self.assertContains(response, self.employee.login)
        self.assertContains(response, "Группа отдела")
        self.assertContains(response, "График")
        self.assertContains(response, "Нет отпуска")
        self.assertContains(response, "calendar_modal=employee_detail")
        self.assertContains(response, "data-schedule-status-tooltip")
        self.assertContains(response, 'data-tooltip-title="Нет отпуска"')
        self.assertContains(response, "Доступно сейчас")
        self.assertContains(response, "Ближайший отпуск")
        self.assertContains(response, "Занято графиком")
        self.assertContains(response, "В ожидании заявок")
        self.assertContains(response, "event_upcoming")
        self.assertContains(response, "Баланс по рабочим годам")
        self.assertContains(response, "Рабочий год")
        self.assertContains(response, "Право")
        self.assertContains(response, "Использовано")
        self.assertContains(response, "В резерве")
        self.assertContains(response, "Остаток")
        self.assertContains(response, "Использовать до")
        self.assertNotContains(response, "Показать расшифровку баланса")
        self.assertNotContains(response, 'id="employee-balance-modal"')
        self.assertNotContains(response, "Начислено по стажу")
        self.assertContains(response, "Статус")
        self.assertContains(response, "Работает")
        self.assertContains(response, "employee-status-badge employee-status-badge--working")
        self.assertContains(response, 'data-modal-open="employee-delete-modal"')
        self.assertContains(response, 'id="employee-delete-modal"')
        self.assertContains(response, reverse("delete_employee", args=[self.employee.id]))

    def test_employee_profile_summary_shows_schedule_status(self):
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
            start_date=today.replace(month=1, day=10),
            end_date=today.replace(month=1, day=23),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["profile_summary"]["schedule_status"]["key"], "planned")
        self.assertContains(response, "employee-summary__detail-card--schedule-planned")
        self.assertContains(response, "График есть")
        self.assertContains(response, f"calendar_employee={self.employee.id}")
        self.assertContains(response, "data-app-link")
        self.assertContains(response, "data-schedule-status-tooltip")
        self.assertContains(response, 'data-tooltip-title="График есть"')

    def test_main_profile_calendar_links_return_to_profile_section(self):
        self.client.force_login(self.employee.user)
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
            start_date=today.replace(month=4, day=1),
            end_date=today.replace(month=4, day=14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        response = self.client.get(reverse("main"))
        entry = response.context["planned_vacations"]["entries"][0]
        schedule_status = response.context["profile_summary"]["schedule_status"]

        entry_query = parse_qs(urlsplit(entry["calendar_url"]).query)
        status_query = parse_qs(urlsplit(schedule_status["calendar_url"]).query)
        self.assertEqual(entry_query["from"], ["profile"])
        self.assertEqual(entry_query["back_url"], [reverse("main")])
        self.assertEqual(entry_query["back_label"], ["К профилю"])
        self.assertEqual(status_query["from"], ["profile"])
        self.assertEqual(status_query["back_url"], [reverse("main")])
        self.assertEqual(status_query["back_label"], ["К профилю"])

    def test_profile_balance_summary_counts_schedule_and_pending_paid_requests(self):
        year = timezone.localdate().year
        schedule = VacationSchedule.objects.create(
            year=year,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(year, 8, 1),
            end_date=date(year, 8, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(year, 2, 2),
            end_date=date(year, 2, 6),
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
            reason="Нужно использовать остаток.",
        )
        self.client.force_login(self.employee.user)

        response = self.client.get(reverse("main"))

        self.assertEqual(response.status_code, 200)
        summary = response.context["profile_summary"]
        self.assertEqual(summary["scheduled_paid_days"], Decimal("14.00"))
        self.assertEqual(summary["pending_paid_request_days"], Decimal("5.00"))
        self.assertContains(response, "Занято графиком")
        self.assertContains(response, "14 д.")
        self.assertContains(response, "В ожидании заявок")
        self.assertContains(response, "5 д.")
        self.assertNotContains(response, "Свободно вне графика")
        self.assertNotContains(response, "Нужно использовать остаток.")

    def test_employee_profile_highlights_department_deputy(self):
        self.engineering.deputy = self.employee
        self.engineering.save(update_fields=["deputy"])
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "employee-summary__role--department-deputy")
        self.assertContains(response, "employee-summary__marker--department-deputy")
        self.assertContains(response, "employee-summary__joined-row")
        self.assertContains(response, "supervisor_account")
        self.assertContains(response, "Заместитель отдела")
        content = response.content.decode(response.charset or "utf-8")
        self.assertLess(
            content.index("employee-summary__marker--department-deputy"),
            content.index("Дата начала работы"),
        )

    def test_employee_profile_marks_new_hire_near_name(self):
        self.employee.date_joined = timezone.localdate()
        self.employee.save(update_fields=["date_joined"])
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="new-hire-badge"')
        self.assertContains(response, "person_add")
        self.assertContains(response, "Работает меньше 6 месяцев")
        content = response.content.decode(response.charset or "utf-8")
        self.assertLess(content.index("new-hire-badge"), content.index("Дата начала работы"))

    def test_employee_profile_places_management_role_before_joined_date(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("employee_profile", args=[self.department_head.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "employee-summary__role--department-head")
        self.assertContains(response, "employee-summary__marker--department-head")
        self.assertContains(response, "Руководитель отдела")
        content = response.content.decode(response.charset or "utf-8")
        self.assertLess(
            content.index("employee-summary__marker--department-head"),
            content.index("Дата начала работы"),
        )

    def test_employee_profile_shows_schedule_filters_and_confirmed_vacations(self):
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
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(year, 1, 10),
            end_date=date(year, 1, 15),
            vacation_type="paid",
            chargeable_days=6,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        archive_schedule = VacationSchedule.objects.create(
            year=year - 1,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=archive_schedule,
            employee=self.employee,
            start_date=date(year - 1, 9, 1),
            end_date=date(year - 1, 9, 7),
            vacation_type="paid",
            chargeable_days=7,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]))

        self.assertEqual(response.status_code, 200)
        html = response.content.decode("utf-8")
        self.assertContains(response, "Отпуска и график")
        self.assertContains(response, 'aria-label="Год отображения отпусков"')
        self.assertContains(response, 'aria-label="Тип отпуска"')
        self.assertNotContains(response, 'data-profile-schedule-period')
        self.assertNotContains(response, "profile-schedule-filter__label")
        self.assertContains(response, "Все")
        self.assertContains(response, "Оплачиваемые")
        self.assertContains(response, "Неоплачиваемые")
        self.assertContains(response, "Учебные")
        self.assertContains(response, "profile-schedule-calendar-select")
        self.assertContains(response, 'data-profile-schedule-year')
        self.assertContains(response, '<option value="all">Все</option>', html=False)
        self.assertContains(response, f'<option value="{year}" selected>{year}</option>', html=False)
        self.assertContains(response, f'<option value="{year - 1}"', html=False)
        self.assertContains(response, f"Отпуска на {year} год")
        self.assertContains(response, "Занято графиком")
        self.assertContains(response, "20 д.")
        self.assertContains(response, "01.10.{} - 14.10.{}".format(year, year))
        self.assertContains(response, 'data-schedule-entry-card')
        self.assertContains(response, 'data-vacation-type="paid"')
        self.assertContains(response, 'data-source-kind="schedule"')
        self.assertContains(response, "Источник")
        self.assertContains(response, "Годовой график")
        self.assertNotContains(response, "Переносы отпусков")
        self.assertLess(
            html.index("01.10.{} - 14.10.{}".format(year, year)),
            html.index("10.01.{} - 15.01.{}".format(year, year)),
        )
        self.assertLess(
            html.index("10.01.{} - 15.01.{}".format(year, year)),
            html.index("01.09.{} - 07.09.{}".format(year - 1, year - 1)),
        )

    def test_employee_profile_schedule_contains_only_confirmed_absences(self):
        VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-05-01",
            end_date="2026-05-03",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_APPROVED,
        )
        VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-06-01",
            end_date="2026-06-05",
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
        )
        VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-07-01",
            end_date="2026-07-05",
            vacation_type="study",
            status=VacationRequest.STATUS_REJECTED,
        )
        self.client.force_login(self.employee.user)

        response = self.client.get(reverse("main"))
        html = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "01.05.2026 - 03.05.2026")
        self.assertContains(response, 'data-vacation-type="unpaid"')
        self.assertContains(response, 'data-source-kind="request"')
        self.assertContains(response, "Одобренная заявка")
        self.assertNotIn('data-status="request-pending"', html)
        self.assertNotIn('data-status="request-rejected"', html)
        self.assertContains(response, "01.06.2026 - 05.06.2026")
        self.assertContains(response, "01.07.2026 - 05.07.2026")
        self.assertContains(response, "Заявки на отпуск")
        self.assertContains(response, "Заявки на отпуск из свободного остатка, неоплачиваемые и учебные отпуска.")

    def test_employee_profile_shows_schedule_changes_only_when_present(self):
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.employee,
            new_start_date=date(2026, 8, 1),
            new_end_date=date(2026, 8, 14),
            reason="Нужно перенести отпуск.",
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Переносы отпусков")
        self.assertContains(response, "profile-schedule-card--transfer is-clickable")
        self.assertContains(response, "data-schedule-transfer-card")
        self.assertContains(response, 'data-years="2026"')
        self.assertContains(response, "01.07.2026 - 14.07.2026")
        self.assertContains(response, "01.08.2026 - 14.08.2026")
        self.assertContains(response, "В ожидании")
        self.assertContains(response, "Нужно перенести отпуск.")
        self.assertContains(
            response,
            f'data-href="{reverse("schedule_change_detail", args=[change_request.id])}?from=employees',
        )
        self.assertContains(response, "back_url=/employee/")
        self.assertContains(response, "back_label=%D0%9A%20%D0%BF%D1%80%D0%BE%D1%84%D0%B8%D0%BB%D1%8E")

    def test_employee_profile_schedule_item_created_from_request_links_to_request_detail(self):
        schedule = VacationSchedule.objects.create(
            year=2027,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2027, 9, 1),
            end_date=date(2027, 9, 14),
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=request_obj.start_date,
            end_date=request_obj.end_date,
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
            source=VacationScheduleItem.SOURCE_MANUAL,
            created_from_vacation_request=request_obj,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]))
        entry = next(
            row
            for row in response.context["planned_vacations"]["entries"]
            if row["id"] == f"schedule-{schedule_item.id}"
        )

        self.assertEqual(entry["detail_url"], reverse("vacation_detail", args=[request_obj.id]))
        self.assertEqual(entry["detail_label"], "Открыть заявку")
        self.assertContains(response, "Открыть заявку")
        self.assertContains(
            response,
            f'href="{reverse("vacation_detail", args=[request_obj.id])}?from=employees',
        )

    def test_employee_profile_schedule_item_calendar_url_focuses_period(self):
        schedule = VacationSchedule.objects.create(
            year=2027,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 7, 3),
            end_date=date(2027, 7, 10),
            vacation_type="paid",
            chargeable_days=8,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]))
        entry = next(
            row
            for row in response.context["planned_vacations"]["entries"]
            if row["id"] == f"schedule-{schedule_item.id}"
        )
        query = parse_qs(urlsplit(entry["calendar_url"]).query)

        self.assertEqual(query["view"], ["month"])
        self.assertEqual(query["year"], ["2027"])
        self.assertEqual(query["month"], ["7"])
        self.assertEqual(query["employee"], [str(self.employee.id)])
        self.assertEqual(query["calendar_focus_employee"], [str(self.employee.id)])
        self.assertEqual(query["calendar_focus_start"], ["2027-07-03"])
        self.assertEqual(query["calendar_focus_end"], ["2027-07-10"])
        self.assertEqual(query["from"], ["employees"])
        self.assertEqual(
            query["back_url"],
            [f"{reverse('employee_profile', args=[self.employee.id])}?from=employees"],
        )
        self.assertEqual(query["back_label"], ["К сотруднику"])

    def test_employee_profile_approved_request_calendar_url_focuses_period(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2027, 8, 4),
            end_date=date(2027, 8, 6),
            vacation_type="unpaid",
            status=VacationRequest.STATUS_APPROVED,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]))
        entry = next(
            row
            for row in response.context["planned_vacations"]["entries"]
            if row["id"] == f"request-{request_obj.id}"
        )
        query = parse_qs(urlsplit(entry["calendar_url"]).query)

        self.assertEqual(query["view"], ["month"])
        self.assertEqual(query["year"], ["2027"])
        self.assertEqual(query["month"], ["8"])
        self.assertEqual(query["employee"], [str(self.employee.id)])
        self.assertEqual(query["calendar_focus_employee"], [str(self.employee.id)])
        self.assertEqual(query["calendar_focus_start"], ["2027-08-04"])
        self.assertEqual(query["calendar_focus_end"], ["2027-08-06"])
        self.assertEqual(query["from"], ["employees"])
        self.assertEqual(
            query["back_url"],
            [f"{reverse('employee_profile', args=[self.employee.id])}?from=employees"],
        )
        self.assertEqual(query["back_label"], ["К сотруднику"])

    def test_employee_profile_from_calendar_keeps_calendar_context_in_schedule_links(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2027, 8, 4),
            end_date=date(2027, 8, 6),
            vacation_type="unpaid",
            status=VacationRequest.STATUS_APPROVED,
        )
        self.client.force_login(self.hr_employee.user)
        calendar_url = f"{reverse('calendar')}?view=month&year=2027&month=8"

        response = self.client.get(
            reverse("employee_profile", args=[self.employee.id]),
            {
                "from": "calendar",
                "back_url": calendar_url,
                "back_label": "К графику",
            },
        )
        entry = next(
            row
            for row in response.context["planned_vacations"]["entries"]
            if row["id"] == f"request-{request_obj.id}"
        )
        calendar_query = parse_qs(urlsplit(entry["calendar_url"]).query)

        self.assertEqual(response.context["sidebar_section"], "calendar")
        self.assertEqual(calendar_query["from"], ["calendar"])
        self.assertEqual(
            calendar_query["back_url"],
            [
                f"{reverse('employee_profile', args=[self.employee.id])}?"
                f"{urlencode({'from': 'calendar', 'back_url': calendar_url, 'back_label': 'К графику'})}"
            ],
        )
        self.assertContains(
            response,
            f'href="{reverse("vacation_detail", args=[request_obj.id])}?from=calendar',
        )

    def test_employee_profile_schedule_item_created_from_transfer_links_to_transfer_detail(self):
        schedule = VacationSchedule.objects.create(
            year=2027,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 7, 1),
            end_date=date(2027, 7, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.employee,
            new_start_date=date(2027, 8, 1),
            new_end_date=date(2027, 8, 14),
            reason="Прошу перенести отпуск.",
        )
        replacement_item = approve_schedule_change_request(change_request.id, reviewer=self.department_head)
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]))
        entries = response.context["planned_vacations"]["entries"]
        replacement_entry = next(row for row in entries if row["id"] == f"schedule-{replacement_item.id}")

        self.assertEqual(replacement_entry["detail_url"], reverse("schedule_change_detail", args=[change_request.id]))
        self.assertEqual(replacement_entry["detail_label"], "Открыть перенос")
        self.assertFalse(any(row["id"] == f"schedule-{schedule_item.id}" for row in entries))
        self.assertContains(response, "Открыть перенос")
        self.assertContains(
            response,
            f'href="{reverse("schedule_change_detail", args=[change_request.id])}?from=employees',
        )

    def test_main_profile_schedule_cards_expose_employee_transfer_action(self):
        schedule = VacationSchedule.objects.create(
            year=2027,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 7, 1),
            end_date=date(2027, 7, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        self.client.force_login(self.employee.user)

        response = self.client.get(reverse("main"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'data-transfer-url="{reverse("schedule_change_request_create", args=[schedule_item.id])}"')
        self.assertContains(response, f'data-transfer-preview-url="{reverse("schedule_change_request_preview", args=[schedule_item.id])}"')
        self.assertContains(response, 'data-transfer-submit-label="Запросить перенос"')
        self.assertContains(response, 'data-transfer-modal-title="Запросить перенос отпуска"')
        self.assertContains(
            response,
            'data-transfer-modal-subtitle="Выберите новые даты и укажите причину. Запрос уйдёт руководителю на согласование."',
        )
        self.assertContains(response, 'data-transfer-next-url="/main/"')

    def test_employee_profile_schedule_cards_expose_manager_transfer_action(self):
        schedule = VacationSchedule.objects.create(
            year=2027,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2027, 8, 1),
            end_date=date(2027, 8, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        self.client.force_login(self.department_head.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'data-transfer-url="{reverse("schedule_change_request_create", args=[schedule_item.id])}"')
        self.assertContains(response, f'data-transfer-preview-url="{reverse("schedule_change_request_preview", args=[schedule_item.id])}"')
        self.assertContains(response, 'data-transfer-submit-label="Отправить предложение"')
        self.assertContains(response, 'data-transfer-modal-title="Предложить перенос отпуска"')
        self.assertContains(
            response,
            'data-transfer-modal-subtitle="Выберите новые даты и укажите причину. Предложение уйдёт сотруднику."',
        )
        self.assertContains(response, 'data-transfer-hint="Сотрудник получит уведомление и сможет принять или отклонить перенос."')
        self.assertContains(response, "Предложить перенос")

    def test_enterprise_head_can_view_profile_without_edit_modal(self):
        self.client.force_login(self.enterprise_head.user)

        response = self.client.get(reverse("employee_profile", args=[self.employee.id]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="employee-edit-modal"')
        self.assertNotContains(response, 'data-modal-open="employee-delete-modal"')

    def test_main_page_renders_requests_as_cards_without_table_header(self):
        VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-09-04",
            end_date="2026-09-12",
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
            reason="Причина заявки в профиле.",
        )
        self.client.force_login(self.employee.user)

        response = self.client.get(reverse("main"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="vacation-request-card is-clickable"')
        self.assertContains(response, "Период")
        self.assertContains(response, "04.09.2026 - 12.09.2026")
        self.assertContains(response, "Тип")
        self.assertContains(response, "Статус")
        self.assertNotContains(response, "Причина заявки в профиле.")
        self.assertNotContains(response, "vacation-request-card__reason")
        self.assertNotContains(response, ">Дата начала<", html=False)
        self.assertNotContains(response, ">Дата окончания<", html=False)
        self.assertNotContains(response, ">Тип отпуска<", html=False)
        self.assertNotContains(response, "<thead>", html=False)

    def test_main_page_empty_requests_state_does_not_render_table_markup(self):
        self.client.force_login(self.employee.user)

        response = self.client.get(reverse("main"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Заявок на отпуск пока нет.")
        self.assertNotContains(response, "<table", html=False)
