from datetime import date

from django.urls import reverse
from django.utils import timezone

from apps.leave.models import (
    VacationRequest,
    VacationSchedule,
    VacationScheduleChangeRequest,
    VacationScheduleItem,
)
from apps.leave.services.schedule_changes import create_schedule_change_request

from .base import LeaveTestCase


class LeaveAccessTests(LeaveTestCase):
    def test_employee_cannot_open_management_sections(self):
        self.client.force_login(self.employee.user)

        applications_response = self.client.get(reverse("applications"))
        analytics_response = self.client.get(reverse("analytics"))

        self.assertRedirects(applications_response, reverse("main"))
        self.assertRedirects(analytics_response, reverse("main"))

    def test_hr_can_view_all_applications_but_cannot_approve(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-12-01",
            end_date="2026-12-02",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        self.client.force_login(self.hr_employee.user)

        applications_response = self.client.get(reverse("applications"))
        approve_response = self.client.post(reverse("approve_vacation", args=[request_obj.id]))
        request_obj.refresh_from_db()

        self.assertEqual(applications_response.status_code, 200)
        self.assertContains(applications_response, self.employee.full_name)
        self.assertRedirects(approve_response, reverse("vacation_detail", args=[request_obj.id]))
        self.assertEqual(request_obj.status, VacationRequest.STATUS_PENDING)

    def test_enterprise_head_can_view_all_applications_but_approve_only_management(self):
        department_head_request = VacationRequest.objects.create(
            employee=self.department_head,
            start_date="2026-12-01",
            end_date="2026-12-02",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-12-05",
            end_date="2026-12-06",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        self.client.force_login(self.enterprise_head.user)

        applications_response = self.client.get(reverse("applications"))
        approve_department_head_response = self.client.post(reverse("approve_vacation", args=[department_head_request.id]))
        approve_regular_employee_response = self.client.post(reverse("approve_vacation", args=[request_obj.id]))

        department_head_request.refresh_from_db()
        request_obj.refresh_from_db()

        self.assertEqual(applications_response.status_code, 200)
        self.assertContains(applications_response, self.department_head.full_name)
        self.assertContains(applications_response, self.employee.full_name)
        self.assertEqual(applications_response.context["pending_requests_count"], 2)
        self.assertRedirects(approve_department_head_response, reverse("applications"))
        self.assertEqual(department_head_request.status, VacationRequest.STATUS_APPROVED)
        self.assertRedirects(approve_regular_employee_response, reverse("vacation_detail", args=[request_obj.id]))
        self.assertEqual(request_obj.status, VacationRequest.STATUS_PENDING)

    def test_enterprise_head_can_view_regular_employee_transfer_requests_without_approving(self):
        future_year = timezone.localdate().year + 1
        schedule = VacationSchedule.objects.create(
            year=future_year,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(future_year, 7, 1),
            end_date=date(future_year, 7, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.employee,
            new_start_date=date(future_year, 8, 1),
            new_end_date=date(future_year, 8, 14),
            reason="Семейные обстоятельства.",
        )
        self.client.force_login(self.enterprise_head.user)

        response = self.client.get(reverse("applications"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.employee.full_name)
        self.assertContains(response, "Недоступно")
        self.assertContains(response, "lock")
        self.assertEqual(response.context["pending_requests_count"], 1)
        change_request.refresh_from_db()
        self.assertFalse(response.context["change_requests"][0].can_approve)
        self.assertTrue(response.context["change_requests"][0].decision_locked)
        self.assertEqual(change_request.status, VacationScheduleChangeRequest.STATUS_PENDING)

    def test_department_head_can_approve_only_own_department_requests(self):
        own_request = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-12-10",
            end_date="2026-12-12",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        foreign_request = VacationRequest.objects.create(
            employee=self.outsider,
            start_date="2026-12-15",
            end_date="2026-12-17",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        foreign_head_request = VacationRequest.objects.create(
            employee=self.foreign_department_head,
            start_date="2026-12-20",
            end_date="2026-12-21",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        self.client.force_login(self.department_head.user)

        approve_own_response = self.client.post(reverse("approve_vacation", args=[own_request.id]))
        approve_foreign_response = self.client.post(reverse("approve_vacation", args=[foreign_request.id]))
        approve_foreign_head_response = self.client.post(reverse("approve_vacation", args=[foreign_head_request.id]))

        own_request.refresh_from_db()
        foreign_request.refresh_from_db()
        foreign_head_request.refresh_from_db()

        self.assertRedirects(approve_own_response, reverse("applications"))
        self.assertEqual(own_request.status, VacationRequest.STATUS_APPROVED)
        self.assertEqual(approve_foreign_response.status_code, 302)
        self.assertEqual(approve_foreign_response.url, reverse("vacation_detail", args=[foreign_request.id]))
        self.assertEqual(approve_foreign_head_response.status_code, 302)
        self.assertEqual(approve_foreign_head_response.url, reverse("vacation_detail", args=[foreign_head_request.id]))
        self.assertEqual(foreign_request.status, VacationRequest.STATUS_PENDING)
        self.assertEqual(foreign_head_request.status, VacationRequest.STATUS_PENDING)

    def test_authorized_person_can_approve_only_enterprise_head_requests(self):
        enterprise_request = VacationRequest.objects.create(
            employee=self.enterprise_head,
            start_date="2026-12-10",
            end_date="2026-12-12",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        department_head_request = VacationRequest.objects.create(
            employee=self.department_head,
            start_date="2026-12-15",
            end_date="2026-12-17",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        self.client.force_login(self.authorized_person.user)

        applications_response = self.client.get(reverse("applications"))
        approve_enterprise_response = self.client.post(reverse("approve_vacation", args=[enterprise_request.id]))
        approve_department_head_response = self.client.post(reverse("approve_vacation", args=[department_head_request.id]))

        enterprise_request.refresh_from_db()
        department_head_request.refresh_from_db()

        self.assertEqual(applications_response.status_code, 200)
        self.assertContains(applications_response, self.enterprise_head.full_name)
        self.assertNotContains(applications_response, self.department_head.full_name)
        self.assertRedirects(approve_enterprise_response, reverse("applications"))
        self.assertEqual(enterprise_request.status, VacationRequest.STATUS_APPROVED)
        self.assertEqual(approve_department_head_response.status_code, 302)
        self.assertEqual(
            approve_department_head_response.url,
            reverse("vacation_detail", args=[department_head_request.id]),
        )
        self.assertEqual(department_head_request.status, VacationRequest.STATUS_PENDING)

    def test_enterprise_head_cannot_approve_own_request(self):
        own_request = VacationRequest.objects.create(
            employee=self.enterprise_head,
            start_date="2026-12-22",
            end_date="2026-12-23",
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        self.client.force_login(self.enterprise_head.user)

        response = self.client.post(reverse("approve_vacation", args=[own_request.id]))
        own_request.refresh_from_db()

        self.assertRedirects(response, reverse("vacation_detail", args=[own_request.id]))
        self.assertEqual(own_request.status, VacationRequest.STATUS_PENDING)

    def test_vacation_detail_renders_role_based_action_forms(self):
        request_obj = VacationRequest.objects.create(
            employee=self.employee,
            start_date="2026-12-15",
            end_date="2026-12-17",
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
        )

        self.client.force_login(self.department_head.user)
        manager_response = self.client.get(reverse("vacation_detail", args=[request_obj.id]))

        self.client.force_login(self.enterprise_head.user)
        enterprise_response = self.client.get(reverse("vacation_detail", args=[request_obj.id]))

        self.assertEqual(manager_response.status_code, 200)
        self.assertContains(manager_response, reverse("approve_vacation", args=[request_obj.id]))
        self.assertContains(manager_response, reverse("reject_vacation", args=[request_obj.id]))
        self.assertContains(manager_response, reverse("delete_vacation", args=[request_obj.id]))
        self.assertContains(manager_response, reverse("employee_profile", args=[self.employee.id]))
        self.assertContains(manager_response, "Открыть профиль")
        self.assertContains(manager_response, "vacation-decision-summary")
        self.assertContains(manager_response, "vacation-decision-panel")
        self.assertContains(manager_response, "vacation-action-button--approve")
        self.assertContains(manager_response, "На сегодня")
        self.assertContains(manager_response, "Доступно на дату начала")
        self.assertContains(manager_response, "Останется после заявки")
        self.assertContains(manager_response, "Источник дней")
        self.assertContains(manager_response, "Дни будут списаны из рабочего года")
        self.assertContains(manager_response, "Списывается:")
        self.assertContains(manager_response, "Текущий оплачиваемый баланс")
        self.assertNotContains(manager_response, "Начислено к началу отпуска")
        self.assertNotContains(manager_response, "Баланс к началу отпуска")
        self.assertContains(manager_response, "Маршрут")
        self.assertContains(manager_response, "История заявки")
        self.assertContains(manager_response, "Рекомендация системы будет доступна после подключения аналитического модуля")
        self.assertContains(manager_response, "Руководитель отдела")

        self.assertEqual(enterprise_response.status_code, 200)
        self.assertNotContains(enterprise_response, reverse("approve_vacation", args=[request_obj.id]))
        self.assertNotContains(enterprise_response, reverse("reject_vacation", args=[request_obj.id]))

    def test_vacation_detail_hides_paid_balance_for_non_paid_requests(self):
        unpaid_request = VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 3),
            vacation_type="unpaid",
            status=VacationRequest.STATUS_PENDING,
        )
        study_request = VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 5),
            vacation_type="study",
            status=VacationRequest.STATUS_PENDING,
        )

        self.client.force_login(self.department_head.user)
        unpaid_response = self.client.get(reverse("vacation_detail", args=[unpaid_request.id]))
        study_response = self.client.get(reverse("vacation_detail", args=[study_request.id]))

        self.assertEqual(unpaid_response.status_code, 200)
        self.assertContains(unpaid_response, "Неоплачиваемый отпуск оформляется без сохранения заработной платы")
        self.assertContains(unpaid_response, "Не списывается")
        self.assertContains(unpaid_response, "Текущий оплачиваемый баланс")
        self.assertNotContains(unpaid_response, "На сегодня")
        self.assertNotContains(unpaid_response, "После рассмотрения")
        self.assertNotContains(unpaid_response, "Останется после заявки")
        self.assertNotContains(unpaid_response, "Начислено к началу отпуска")
        self.assertNotContains(unpaid_response, "Баланс по рабочим годам")

        self.assertEqual(study_response.status_code, 200)
        self.assertContains(study_response, "Учебный отпуск не уменьшает остаток ежегодного оплачиваемого отпуска")
        self.assertContains(study_response, "Не списывается")
        self.assertContains(study_response, "Текущий оплачиваемый баланс")
        self.assertNotContains(study_response, "На сегодня")
        self.assertNotContains(study_response, "После рассмотрения")
        self.assertNotContains(study_response, "Останется после заявки")
        self.assertNotContains(study_response, "Начислено к началу отпуска")
        self.assertNotContains(study_response, "Баланс по рабочим годам")

    def test_vacation_detail_redirects_when_request_was_deleted(self):
        deleted_request_id = 987654
        self.client.force_login(self.department_head.user)

        response = self.client.get(reverse("vacation_detail", args=[deleted_request_id]))

        self.assertRedirects(response, reverse("applications"))

    def test_authorized_person_sees_action_forms_for_enterprise_head_request(self):
        request_obj = VacationRequest.objects.create(
            employee=self.enterprise_head,
            start_date="2026-12-20",
            end_date="2026-12-22",
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
        )
        self.client.force_login(self.authorized_person.user)

        response = self.client.get(reverse("vacation_detail", args=[request_obj.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("approve_vacation", args=[request_obj.id]))
        self.assertContains(response, reverse("reject_vacation", args=[request_obj.id]))

    def test_authorized_person_has_only_applications_access(self):
        self.client.force_login(self.authorized_person.user)

        main_response = self.client.get(reverse("main"))
        applications_response = self.client.get(reverse("applications"))
        employees_response = self.client.get(reverse("employees"))
        calendar_response = self.client.get(reverse("calendar"))
        profile_response = self.client.get(reverse("employee_profile", args=[self.authorized_person.id]))

        self.assertRedirects(main_response, reverse("applications"))
        self.assertEqual(applications_response.status_code, 200)
        self.assertNotContains(applications_response, "Профиль")
        self.assertContains(applications_response, "Служебный доступ")
        self.assertRedirects(employees_response, reverse("applications"))
        self.assertRedirects(calendar_response, reverse("applications"))
        self.assertRedirects(profile_response, reverse("applications"))
