from datetime import date, timedelta
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

from apps.leave.models import (
    VacationSchedule,
    VacationScheduleCandidatePackage,
    VacationScheduleCandidatePackagePeriod,
    VacationScheduleDepartmentApproval,
    VacationScheduleEnterpriseApproval,
    VacationScheduleGenerationRun,
    VacationScheduleItem,
)
from apps.leave.tests.base import LeaveTestCase


class SchedulePackageReportTests(LeaveTestCase):
    def _create_schedule_with_run(self, *, status=VacationSchedule.STATUS_DRAFT):
        year = self._year()
        schedule = VacationSchedule.objects.create(year=year, status=status, created_by=self.hr_employee)
        generation_run = VacationScheduleGenerationRun.objects.create(
            schedule=schedule,
            year=year,
            mode=VacationScheduleGenerationRun.MODE_HYBRID,
            status=VacationScheduleGenerationRun.STATUS_COMPLETED,
            actor=self.hr_employee,
            model_version="vacation-candidate-mlp-v2",
        )
        return year, schedule, generation_run

    def _create_packaged_item(
        self,
        schedule,
        generation_run,
        employee,
        *,
        start_date,
        score=Decimal("86.00"),
        risk_score=12,
        risk_level=VacationScheduleItem.RISK_LOW,
    ):
        end_date = start_date + timedelta(days=13)
        item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=employee,
            start_date=start_date,
            end_date=end_date,
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_DRAFT
            if schedule.status == VacationSchedule.STATUS_DRAFT
            else VacationScheduleItem.STATUS_PLANNED,
            source=VacationScheduleItem.SOURCE_GENERATED,
            risk_score=risk_score,
            risk_level=risk_level,
            generated_by_ai=True,
            generation_run=generation_run,
            ai_score=score,
            ai_confidence=Decimal("91.00"),
            ai_model_version="vacation-candidate-mlp-v2",
            ai_explanation="Период прошел нейронную проверку.",
        )
        selected_package = VacationScheduleCandidatePackage.objects.create(
            generation_run=generation_run,
            schedule=schedule,
            employee=employee,
            periods_count=1,
            total_chargeable_days=14,
            source=VacationScheduleItem.SOURCE_GENERATED,
            passed_hard_rules=True,
            risk_score=risk_score,
            risk_level=risk_level,
            features={
                "package_recommendation": "prefer",
                "package_closes_need": True,
                "has_primary_preference_period": True,
            },
            score=score,
            confidence=Decimal("90.00"),
            model_version="vacation-package-ranker-v3",
            explanation="Нейромодуль выбрал пакет: закрывает нужные дни и сохраняет состав.",
            decision=VacationScheduleCandidatePackage.DECISION_SELECTED,
            decision_rank=1,
            selected_at=timezone.now(),
        )
        VacationScheduleCandidatePackagePeriod.objects.create(
            candidate_package=selected_package,
            schedule_item=item,
            start_date=start_date,
            end_date=end_date,
            chargeable_days=14,
            passed_hard_rules=True,
            risk_score=risk_score,
            risk_level=risk_level,
            features={"risk_is_conflict": False},
            order=1,
        )
        alternative_package = VacationScheduleCandidatePackage.objects.create(
            generation_run=generation_run,
            schedule=schedule,
            employee=employee,
            periods_count=1,
            total_chargeable_days=14,
            source=VacationScheduleItem.SOURCE_GENERATED,
            passed_hard_rules=True,
            risk_score=45,
            risk_level=VacationScheduleItem.RISK_MEDIUM,
            features={"package_recommendation": "normal", "package_closes_need": True},
            score=Decimal("63.00"),
            confidence=Decimal("74.00"),
            model_version="vacation-package-ranker-v3",
            explanation="Альтернатива хуже из-за большей нагрузки отдела.",
            decision=VacationScheduleCandidatePackage.DECISION_REJECTED,
            decision_rank=2,
        )
        VacationScheduleCandidatePackagePeriod.objects.create(
            candidate_package=alternative_package,
            start_date=start_date + timedelta(days=40),
            end_date=end_date + timedelta(days=40),
            chargeable_days=14,
            passed_hard_rules=True,
            risk_score=45,
            risk_level=VacationScheduleItem.RISK_MEDIUM,
            features={"risk_is_conflict": False},
            order=1,
        )
        return item, selected_package, alternative_package

    def test_review_modal_shows_selected_package_report_and_alternatives(self):
        year, schedule, generation_run = self._create_schedule_with_run()
        item, _, _ = self._create_packaged_item(
            schedule,
            generation_run,
            self.employee,
            start_date=date(year, 7, 1),
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("schedule_draft_item_review", args=[year, item.id]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("Почему выбран этот пакет", payload["html"])
        self.assertIn("vacation-package-ranker-v3", payload["html"])
        self.assertIn("Альтернатива хуже", payload["html"])

    def test_review_modal_keeps_existing_view_without_package_data(self):
        year, schedule, generation_run = self._create_schedule_with_run()
        item = VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(year, 8, 1),
            end_date=date(year, 8, 14),
            vacation_type="paid",
            chargeable_days=14,
            status=VacationScheduleItem.STATUS_DRAFT,
            source=VacationScheduleItem.SOURCE_GENERATED,
            generated_by_ai=True,
            generation_run=generation_run,
            ai_score=Decimal("70.00"),
            ai_confidence=Decimal("80.00"),
            ai_model_version="vacation-candidate-mlp-v2",
            ai_explanation="Есть только объяснение периода.",
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("schedule_draft_item_review", args=[year, item.id]))

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("Есть только объяснение периода", payload["html"])
        self.assertNotIn("Почему выбран этот пакет", payload["html"])

    def test_department_head_sees_package_summary_only_for_own_department(self):
        year, schedule, generation_run = self._create_schedule_with_run(status=VacationSchedule.STATUS_DEPARTMENT_REVIEW)
        self._create_packaged_item(
            schedule,
            generation_run,
            self.employee,
            start_date=date(year, 7, 1),
        )
        self._create_packaged_item(
            schedule,
            generation_run,
            self.outsider,
            start_date=date(year, 8, 1),
            score=Decimal("58.00"),
            risk_score=75,
            risk_level=VacationScheduleItem.RISK_HIGH,
        )
        VacationScheduleDepartmentApproval.objects.create(
            schedule=schedule,
            department=self.engineering,
            department_head=self.department_head,
            status=VacationScheduleDepartmentApproval.STATUS_PENDING,
        )
        VacationScheduleDepartmentApproval.objects.create(
            schedule=schedule,
            department=self.hr_department,
            department_head=self.foreign_department_head,
            status=VacationScheduleDepartmentApproval.STATUS_PENDING,
        )
        self.client.force_login(self.department_head.user)

        response = self.client.get(reverse("schedule_planning", args=[year]), {"stage": "review"})

        self.assertEqual(response.status_code, 200)
        rows = response.context["review_summary"]["rows"]
        self.assertEqual([row["department"].id for row in rows], [self.engineering.id])
        self.assertEqual(rows[0]["ml_summary"]["employees_count"], 1)
        self.assertContains(response, "Пакетная оценка")
        self.assertContains(response, "Пакеты безопасны")

    def test_enterprise_head_sees_final_package_ml_summary(self):
        year, schedule, generation_run = self._create_schedule_with_run(status=VacationSchedule.STATUS_DEPARTMENT_REVIEW)
        self._create_packaged_item(
            schedule,
            generation_run,
            self.employee,
            start_date=date(year, 7, 1),
        )
        self._create_packaged_item(
            schedule,
            generation_run,
            self.outsider,
            start_date=date(year, 8, 1),
            score=Decimal("58.00"),
            risk_score=75,
            risk_level=VacationScheduleItem.RISK_HIGH,
        )
        VacationScheduleDepartmentApproval.objects.create(
            schedule=schedule,
            department=self.engineering,
            department_head=self.department_head,
            status=VacationScheduleDepartmentApproval.STATUS_APPROVED,
            approved_at=timezone.now(),
        )
        VacationScheduleDepartmentApproval.objects.create(
            schedule=schedule,
            department=self.hr_department,
            department_head=self.foreign_department_head,
            status=VacationScheduleDepartmentApproval.STATUS_APPROVED,
            approved_at=timezone.now(),
        )
        VacationScheduleEnterpriseApproval.objects.create(
            schedule=schedule,
            enterprise_head=self.enterprise_head,
            status=VacationScheduleEnterpriseApproval.STATUS_PENDING,
        )
        self.client.force_login(self.enterprise_head.user)

        response = self.client.get(reverse("schedule_planning", args=[year]), {"stage": "final"})

        self.assertEqual(response.status_code, 200)
        ml_summary = response.context["final_summary"]["ml_summary"]
        self.assertTrue(ml_summary["has_data"])
        self.assertEqual(ml_summary["employees_count"], 2)
        self.assertEqual(ml_summary["high_risk_count"], 1)
        self.assertContains(response, "Итог пакетной оценки")
        self.assertContains(response, "vacation-package-ranker-v3")
