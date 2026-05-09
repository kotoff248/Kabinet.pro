from datetime import date

from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.accounts.services import can_initiate_schedule_change_for_item, can_review_schedule_change_request
from apps.leave.models import DepartmentStaffingRule, VacationSchedule, VacationScheduleChangeRequest, VacationScheduleItem
from apps.leave.services.schedule_changes import (
    approve_schedule_change_request,
    build_schedule_change_transfer_action,
    create_schedule_change_request,
    reject_schedule_change_request,
)

from .base import LeaveTestCase


class ScheduleChangeRequestTests(LeaveTestCase):
    def _create_schedule_item(self, employee=None, *, start_date=date(2027, 7, 1), end_date=date(2027, 7, 14)):
        schedule, _ = VacationSchedule.objects.get_or_create(
            year=start_date.year,
            defaults={
                "status": VacationSchedule.STATUS_APPROVED,
                "approved_by": self.enterprise_head,
            },
        )
        return VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=employee or self.employee,
            start_date=start_date,
            end_date=end_date,
            vacation_type="paid",
            chargeable_days=(end_date - start_date).days + 1,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

    def _preview_transfer(self, schedule_item, actor, start_date, end_date):
        self.client.force_login(actor.user)
        return self.client.get(
            reverse("schedule_change_request_preview", args=[schedule_item.id]),
            {
                "new_start_date": start_date.isoformat(),
                "new_end_date": end_date.isoformat(),
            },
        )

    def test_schedule_change_initiation_permission_matrix(self):
        employee_item = self._create_schedule_item()
        hr_item = self._create_schedule_item(self.hr_employee, start_date=date(2027, 9, 1), end_date=date(2027, 9, 14))
        department_head_item = self._create_schedule_item(
            self.department_head,
            start_date=date(2027, 10, 1),
            end_date=date(2027, 10, 14),
        )
        enterprise_head_item = self._create_schedule_item(
            self.enterprise_head,
            start_date=date(2027, 11, 1),
            end_date=date(2027, 11, 14),
        )

        self.assertTrue(can_initiate_schedule_change_for_item(self.employee, employee_item))
        self.assertTrue(can_initiate_schedule_change_for_item(self.department_head, employee_item))
        self.assertFalse(can_initiate_schedule_change_for_item(self.foreign_department_head, employee_item))
        self.assertFalse(can_initiate_schedule_change_for_item(self.enterprise_head, employee_item))
        self.assertFalse(can_initiate_schedule_change_for_item(self.hr_employee, employee_item))
        self.assertTrue(can_initiate_schedule_change_for_item(self.enterprise_head, hr_item))
        self.assertTrue(can_initiate_schedule_change_for_item(self.enterprise_head, department_head_item))
        self.assertFalse(can_initiate_schedule_change_for_item(self.authorized_person, enterprise_head_item))

    def test_schedule_change_request_approves_by_replacing_old_item(self):
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
            reason="Нужно перенести по семейным обстоятельствам.",
        )
        schedule_item.refresh_from_db()

        self.assertEqual(change_request.status, VacationScheduleChangeRequest.STATUS_PENDING)
        self.assertEqual(schedule_item.status, VacationScheduleItem.STATUS_APPROVED)

        replacement = approve_schedule_change_request(change_request.id, reviewer=self.department_head)
        schedule_item.refresh_from_db()
        change_request.refresh_from_db()

        self.assertEqual(schedule_item.status, VacationScheduleItem.STATUS_TRANSFERRED)
        self.assertEqual(change_request.status, VacationScheduleChangeRequest.STATUS_APPROVED)
        self.assertEqual(replacement.previous_item_id, schedule_item.id)
        self.assertEqual(replacement.created_from_change_request_id, change_request.id)
        self.assertEqual(replacement.source, VacationScheduleItem.SOURCE_TRANSFER)
        self.assertEqual(replacement.chargeable_days, 14)

    def test_rejected_schedule_change_does_not_modify_schedule_item(self):
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
            reason="Нужно перенести.",
        )

        reject_schedule_change_request(change_request.id, reviewer=self.department_head)
        schedule_item.refresh_from_db()
        change_request.refresh_from_db()

        self.assertEqual(schedule_item.status, VacationScheduleItem.STATUS_APPROVED)
        self.assertEqual(change_request.status, VacationScheduleChangeRequest.STATUS_REJECTED)
        self.assertFalse(VacationScheduleItem.objects.filter(previous_item=schedule_item).exists())

    def test_schedule_change_approve_requires_valid_reviewer(self):
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
        )

        for reviewer in (None, self.employee, self.foreign_department_head, self.hr_employee):
            with self.subTest(reviewer=reviewer):
                with self.assertRaises(ValidationError):
                    approve_schedule_change_request(change_request.id, reviewer=reviewer)

        change_request.refresh_from_db()
        schedule_item.refresh_from_db()
        self.assertEqual(change_request.status, VacationScheduleChangeRequest.STATUS_PENDING)
        self.assertEqual(schedule_item.status, VacationScheduleItem.STATUS_APPROVED)

    def test_schedule_change_reject_requires_valid_reviewer(self):
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        schedule_item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 9, 1),
            end_date=date(2026, 9, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_APPROVED,
        )
        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.employee,
            new_start_date=date(2026, 10, 1),
            new_end_date=date(2026, 10, 14),
        )

        with self.assertRaises(ValidationError):
            reject_schedule_change_request(change_request.id, reviewer=self.employee)
        with self.assertRaises(ValidationError):
            reject_schedule_change_request(change_request.id, reviewer=None)

        change_request.refresh_from_db()
        self.assertEqual(change_request.status, VacationScheduleChangeRequest.STATUS_PENDING)

    def test_department_head_can_propose_transfer_and_employee_accepts(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 8, 1), end_date=date(2027, 8, 14))

        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.department_head,
            new_start_date=date(2027, 9, 1),
            new_end_date=date(2027, 9, 14),
            reason="Нужно сохранить покрытие отдела.",
        )

        self.assertNotEqual(change_request.requested_by_id, change_request.employee_id)
        self.assertTrue(can_review_schedule_change_request(self.employee, change_request))
        self.assertFalse(can_review_schedule_change_request(self.department_head, change_request))

        replacement = approve_schedule_change_request(change_request.id, reviewer=self.employee)
        schedule_item.refresh_from_db()
        change_request.refresh_from_db()

        self.assertEqual(schedule_item.status, VacationScheduleItem.STATUS_TRANSFERRED)
        self.assertEqual(change_request.status, VacationScheduleChangeRequest.STATUS_APPROVED)
        self.assertEqual(change_request.reviewed_by_id, self.employee.id)
        self.assertEqual(replacement.created_from_change_request_id, change_request.id)

    def test_transfer_action_is_hidden_when_schedule_item_has_pending_change(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 8, 1), end_date=date(2027, 8, 14))
        create_schedule_change_request(
            schedule_item.id,
            requested_by=self.employee,
            new_start_date=date(2027, 9, 1),
            new_end_date=date(2027, 9, 14),
        )

        action = build_schedule_change_transfer_action(
            actor=self.employee,
            employee=self.employee,
            schedule_item_id=schedule_item.id,
            start_date=schedule_item.start_date,
            end_date=schedule_item.end_date,
            vacation_type_label=schedule_item.get_vacation_type_display(),
            schedule_status=schedule_item.status,
            today=date(2027, 1, 1),
        )

        self.assertFalse(action["can_request_transfer"])
        self.assertEqual(action["transfer_url"], "")

    def test_transfer_action_uses_prefetched_pending_flag_without_query(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 8, 1), end_date=date(2027, 8, 14))

        with self.assertNumQueries(0):
            action = build_schedule_change_transfer_action(
                actor=self.employee,
                employee=self.employee,
                schedule_item_id=schedule_item.id,
                start_date=schedule_item.start_date,
                end_date=schedule_item.end_date,
                vacation_type_label=schedule_item.get_vacation_type_display(),
                schedule_status=schedule_item.status,
                today=date(2027, 1, 1),
                pending_change_exists=True,
            )

        self.assertFalse(action["can_request_transfer"])
        self.assertEqual(action["transfer_url"], "")

    def test_manager_initiated_transfer_can_be_rejected_only_by_employee(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 8, 15), end_date=date(2027, 8, 28))
        change_request = create_schedule_change_request(
            schedule_item.id,
            requested_by=self.department_head,
            new_start_date=date(2027, 10, 1),
            new_end_date=date(2027, 10, 14),
        )

        with self.assertRaises(ValidationError):
            approve_schedule_change_request(change_request.id, reviewer=self.department_head)

        reject_schedule_change_request(change_request.id, reviewer=self.employee)
        schedule_item.refresh_from_db()
        change_request.refresh_from_db()

        self.assertEqual(schedule_item.status, VacationScheduleItem.STATUS_APPROVED)
        self.assertEqual(change_request.status, VacationScheduleChangeRequest.STATUS_REJECTED)

    def test_enterprise_head_proposes_only_for_hr_and_department_heads(self):
        employee_item = self._create_schedule_item(start_date=date(2027, 9, 15), end_date=date(2027, 9, 28))
        hr_item = self._create_schedule_item(self.hr_employee, start_date=date(2027, 10, 15), end_date=date(2027, 10, 28))

        with self.assertRaises(ValidationError):
            create_schedule_change_request(
                employee_item.id,
                requested_by=self.enterprise_head,
                new_start_date=date(2027, 11, 1),
                new_end_date=date(2027, 11, 14),
            )

        change_request = create_schedule_change_request(
            hr_item.id,
            requested_by=self.enterprise_head,
            new_start_date=date(2027, 12, 1),
            new_end_date=date(2027, 12, 14),
        )

        self.assertEqual(change_request.requested_by_id, self.enterprise_head.id)
        self.assertTrue(can_review_schedule_change_request(self.hr_employee, change_request))

    def test_schedule_change_preview_allows_employee_and_returns_delta_payload(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 7, 1), end_date=date(2027, 7, 14))

        response = self._preview_transfer(schedule_item, self.employee, date(2027, 8, 1), date(2027, 8, 14))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["can_submit"])
        self.assertEqual(payload["old_calendar_days"], 14)
        self.assertEqual(payload["new_calendar_days"], 14)
        self.assertEqual(payload["old_chargeable_days"], 14)
        self.assertEqual(payload["new_chargeable_days"], 14)
        self.assertEqual(payload["chargeable_days_delta"], 0)
        self.assertEqual(payload["chargeable_days_delta_label"], "Без изменения")
        self.assertGreaterEqual(payload["balance_after_change"], 0)
        self.assertIn("risk_explanation", payload)
        self.assertIn("risk_short_reason", payload)
        self.assertIn("risk_recommended_action", payload)

    def test_schedule_change_preview_allows_manager_proposal(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 8, 1), end_date=date(2027, 8, 14))

        response = self._preview_transfer(schedule_item, self.department_head, date(2027, 9, 1), date(2027, 9, 14))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["can_submit"])

    def test_schedule_change_preview_forbids_unavailable_actor(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 8, 1), end_date=date(2027, 8, 14))

        response = self._preview_transfer(schedule_item, self.foreign_department_head, date(2027, 9, 1), date(2027, 9, 14))

        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json()["can_submit"])

    def test_schedule_change_preview_blocks_when_no_fourteen_day_part_remains(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 8, 1), end_date=date(2027, 8, 14))
        self._create_schedule_item(start_date=date(2027, 10, 1), end_date=date(2027, 10, 7))

        response = self._preview_transfer(schedule_item, self.employee, date(2027, 9, 1), date(2027, 9, 7))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["can_submit"])
        self.assertIn("не меньше 14 дней", payload["message"])
        self.assertEqual(payload["new_chargeable_days"], 7)
        self.assertEqual(payload["chargeable_days_delta_label"], "Освободится 7 д.")

    def test_schedule_change_preview_allows_shortening_when_another_fourteen_day_part_exists(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 8, 1), end_date=date(2027, 8, 14))
        self._create_schedule_item(start_date=date(2027, 10, 1), end_date=date(2027, 10, 14))

        response = self._preview_transfer(schedule_item, self.employee, date(2027, 9, 1), date(2027, 9, 7))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["can_submit"])
        self.assertEqual(payload["old_chargeable_days"], 14)
        self.assertEqual(payload["new_chargeable_days"], 7)
        self.assertEqual(payload["chargeable_days_delta"], -7)
        self.assertEqual(payload["chargeable_days_delta_label"], "Освободится 7 д.")

    def test_schedule_change_preview_allows_shortening_initial_short_leave_when_long_part_exists(self):
        schedule_item = self._create_schedule_item(start_date=date(2027, 7, 1), end_date=date(2027, 7, 7))
        schedule_item.chargeable_days = 7
        schedule_item.save(update_fields=["chargeable_days"])
        self._create_schedule_item(start_date=date(2027, 10, 1), end_date=date(2027, 10, 14))

        response = self._preview_transfer(schedule_item, self.employee, date(2027, 7, 15), date(2027, 7, 19))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["can_submit"])
        self.assertEqual(payload["old_chargeable_days"], 7)
        self.assertEqual(payload["new_chargeable_days"], 5)
        self.assertEqual(payload["chargeable_days_delta"], -2)
        self.assertEqual(payload["chargeable_days_delta_label"], "Освободится 2 д.")

    def test_schedule_change_preview_warns_about_staffing_conflict_without_blocking(self):
        DepartmentStaffingRule.objects.create(
            department=self.engineering,
            min_staff_required=10,
            max_absent=10,
            criticality_level=5,
        )
        schedule_item = self._create_schedule_item(start_date=date(2027, 10, 1), end_date=date(2027, 10, 14))

        response = self._preview_transfer(schedule_item, self.employee, date(2027, 9, 1), date(2027, 9, 14))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["can_submit"])
        self.assertTrue(payload["risk_is_conflict"])
        self.assertGreaterEqual(payload["risk_score"], 70)
        self.assertIn("конфликт", payload["message"].lower())
