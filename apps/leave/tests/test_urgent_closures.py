from datetime import date
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.core.models import Notification
from apps.leave.models import VacationSchedule, VacationScheduleItem, VacationUrgentClosureRequest
from apps.leave.services.urgent_closures import (
    accept_urgent_closure_by_employee,
    approve_urgent_closure_by_manager,
    build_urgent_closure_options,
    create_urgent_closure_request,
    finalize_urgent_closure,
    propose_urgent_closure_period_by_employee,
)

from .base import LeaveTestCase


class UrgentClosureWorkflowTests(LeaveTestCase):
    def _create_closure(self):
        return create_urgent_closure_request(
            employee=self.employee,
            planning_year=2027,
            required_days=3,
            deadline=date(2027, 1, 3),
            start_date=date(2026, 12, 29),
            end_date=date(2026, 12, 31),
            actor=self.hr_employee,
            reason="Остаток нельзя закрыть в графике 2027 года.",
        )

    def test_urgent_closure_options_close_january_deadline_before_planning_year(self):
        options = build_urgent_closure_options(
            self.employee,
            planning_year=2027,
            required_days=3,
            deadline=date(2027, 1, 3),
        )

        self.assertTrue(options)
        first_option = options[0]
        self.assertLess(first_option["end_date"], date(2027, 1, 1))
        self.assertEqual(first_option["chargeable_days"], 3)
        self.assertTrue(first_option["can_submit"])

    def test_urgent_closure_routes_through_manager_employee_and_hr(self):
        closure_request = self._create_closure()
        detail_url = reverse("urgent_closure_detail", args=[closure_request.id])

        self.assertEqual(closure_request.status, VacationUrgentClosureRequest.STATUS_DEPARTMENT_REVIEW)
        manager_task = Notification.objects.get(
            recipient=self.department_head,
            event_type=Notification.TYPE_URGENT_CLOSURE_DEPARTMENT_REVIEW,
        )
        self.assertEqual(manager_task.action_url, detail_url)
        self.assertTrue(manager_task.requires_action)

        approve_urgent_closure_by_manager(
            closure_request.id,
            reviewer=self.department_head,
            comment="По составу отдела подходит.",
        )
        closure_request.refresh_from_db()
        manager_task.refresh_from_db()

        self.assertEqual(closure_request.status, VacationUrgentClosureRequest.STATUS_EMPLOYEE_REVIEW)
        self.assertEqual(manager_task.status, Notification.STATUS_DONE)
        employee_task = Notification.objects.get(
            recipient=self.employee,
            event_type=Notification.TYPE_URGENT_CLOSURE_EMPLOYEE_REVIEW,
        )
        self.assertEqual(employee_task.action_url, detail_url)

        accept_urgent_closure_by_employee(
            closure_request.id,
            employee=self.employee,
            comment="Период подходит.",
        )
        closure_request.refresh_from_db()
        employee_task.refresh_from_db()

        self.assertEqual(closure_request.status, VacationUrgentClosureRequest.STATUS_HR_FINALIZATION)
        self.assertEqual(employee_task.status, Notification.STATUS_DONE)
        hr_task = Notification.objects.get(
            recipient=self.hr_employee,
            event_type=Notification.TYPE_URGENT_CLOSURE_HR_FINALIZATION,
        )
        self.assertEqual(hr_task.action_url, detail_url)

        schedule_item = finalize_urgent_closure(
            closure_request.id,
            actor=self.hr_employee,
            comment="Согласовано всеми участниками.",
        )
        closure_request.refresh_from_db()
        hr_task.refresh_from_db()

        self.assertEqual(closure_request.status, VacationUrgentClosureRequest.STATUS_COMPLETED)
        self.assertEqual(hr_task.status, Notification.STATUS_DONE)
        self.assertEqual(schedule_item.schedule.year, 2026)
        self.assertEqual(schedule_item.status, VacationScheduleItem.STATUS_APPROVED)
        self.assertEqual(schedule_item.source, VacationScheduleItem.SOURCE_MANUAL)
        self.assertEqual(schedule_item.chargeable_days, 3)
        self.assertEqual(closure_request.created_schedule_item_id, schedule_item.id)

    def test_employee_can_propose_another_period_back_to_manager(self):
        closure_request = self._create_closure()
        approve_urgent_closure_by_manager(closure_request.id, reviewer=self.department_head)

        propose_urgent_closure_period_by_employee(
            closure_request.id,
            employee=self.employee,
            start_date=date(2026, 11, 25),
            end_date=date(2026, 11, 27),
            comment="Так удобнее.",
        )
        closure_request.refresh_from_db()

        self.assertEqual(closure_request.status, VacationUrgentClosureRequest.STATUS_DEPARTMENT_REVIEW)
        self.assertEqual(closure_request.proposed_start_date, date(2026, 11, 25))
        self.assertEqual(closure_request.proposed_end_date, date(2026, 11, 27))
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.department_head,
                event_type=Notification.TYPE_URGENT_CLOSURE_DEPARTMENT_REVIEW,
                action_url=reverse("urgent_closure_detail", args=[closure_request.id]),
            ).exists()
        )

    def test_foreign_user_cannot_view_urgent_closure_detail(self):
        closure_request = self._create_closure()

        self.client.force_login(self.foreign_department_head.user)
        response = self.client.get(reverse("urgent_closure_detail", args=[closure_request.id]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("main"))

    def test_department_head_sees_urgent_closure_detail_actions(self):
        closure_request = self._create_closure()

        self.client.force_login(self.department_head.user)
        response = self.client.get(reverse("urgent_closure_detail", args=[closure_request.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Закрытие остатка отпуска")
        self.assertContains(response, "Отправить сотруднику")
        self.assertContains(response, "29.12.2026 - 31.12.2026")

    def test_hr_cannot_create_duplicate_active_closure(self):
        self._create_closure()

        with self.assertRaises(ValidationError):
            self._create_closure()

        self.assertEqual(VacationUrgentClosureRequest.objects.count(), 1)

    def test_finalized_closure_creates_or_reuses_approved_previous_year_schedule(self):
        schedule = VacationSchedule.objects.create(year=2026, status=VacationSchedule.STATUS_DRAFT)
        closure_request = self._create_closure()
        approve_urgent_closure_by_manager(closure_request.id, reviewer=self.department_head)
        accept_urgent_closure_by_employee(closure_request.id, employee=self.employee)

        schedule_item = finalize_urgent_closure(closure_request.id, actor=self.hr_employee)
        schedule.refresh_from_db()

        self.assertEqual(schedule.status, VacationSchedule.STATUS_APPROVED)
        self.assertEqual(schedule_item.schedule_id, schedule.id)

    def test_hr_can_preview_valid_urgent_closure_period(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("urgent_closure_preview", args=[2027, self.employee.id]),
            {
                "required_days": "3",
                "deadline": "2027-01-03",
                "start_date": "2026-12-29",
                "end_date": "2026-12-31",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["can_submit"])
        self.assertEqual(payload["chargeable_days"], 3)
        self.assertIn("29.12.2026", payload["period_label"])
        self.assertIn("risk_label", payload)

    def test_urgent_closure_preview_rejects_reversed_dates(self):
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("urgent_closure_preview", args=[2027, self.employee.id]),
            {
                "required_days": "3",
                "deadline": "2027-01-03",
                "start_date": "2026-06-12",
                "end_date": "2026-06-05",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["can_submit"])
        self.assertEqual(payload["message"], "Дата окончания не может быть раньше даты начала.")

    def test_urgent_closure_preview_rejects_employee_overlap(self):
        schedule = VacationSchedule.objects.create(year=2026, status=VacationSchedule.STATUS_DRAFT)
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 12, 29),
            end_date=date(2026, 12, 31),
            chargeable_days=3,
            status=VacationScheduleItem.STATUS_PLANNED,
            source=VacationScheduleItem.SOURCE_MANUAL,
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(
            reverse("urgent_closure_preview", args=[2027, self.employee.id]),
            {
                "required_days": "3",
                "deadline": "2027-01-03",
                "start_date": "2026-12-29",
                "end_date": "2026-12-31",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["can_submit"])
        self.assertEqual(payload["message"], "У сотрудника уже есть отпуск или активная заявка на эти даты.")

    def test_urgent_closure_preview_keeps_conflict_visible_without_blocking(self):
        self.client.force_login(self.hr_employee.user)

        with (
            patch(
                "apps.leave.services.urgent_closures.calculate_vacation_request_risk",
                return_value={
                    "risk_score": 92,
                    "risk_level": "high",
                    "department_load_level": 5,
                    "overlapping_absences_count": 4,
                    "remaining_staff_count": 1,
                    "min_staff_required": 3,
                    "balance_after_request": 40,
                },
            ),
            patch(
                "apps.leave.services.urgent_closures.build_vacation_request_risk_explanation",
                return_value={
                    "short_reason": "Не хватает людей в группе.",
                    "recommended_action": "Проверьте замену.",
                    "is_conflict": True,
                },
            ),
        ):
            response = self.client.get(
                reverse("urgent_closure_preview", args=[2027, self.employee.id]),
                {
                    "required_days": "3",
                    "deadline": "2027-01-03",
                    "start_date": "2026-12-29",
                    "end_date": "2026-12-31",
                },
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["can_submit"])
        self.assertTrue(payload["risk_is_conflict"])
        self.assertEqual(payload["risk_score"], 92)
        self.assertEqual(payload["risk_short_reason"], "Не хватает людей в группе.")
