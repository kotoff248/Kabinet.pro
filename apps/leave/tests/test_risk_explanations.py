from datetime import date

from apps.employees.models import DepartmentCoverageRule, EmployeePosition, Employees, ProductionGroup, ProductionGroupSubstitutionRule
from apps.leave.models import DepartmentStaffingRule, VacationRequest, VacationSchedule, VacationScheduleItem
from apps.leave.services.page_contexts import _build_leave_decision_context
from apps.leave.services.risk import build_vacation_request_risk_explanation, calculate_vacation_request_risk

from .base import LeaveTestCase


class RiskExplanationTests(LeaveTestCase):
    def _detail_kinds(self, explanation):
        return {detail["kind"] for detail in explanation["details"]}

    def _decision_context(self, explanation):
        return _build_leave_decision_context(
            explanation,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 7),
            employee_id=self.employee.id,
            calendar_action_label="Открыть период в графике",
        )

    def test_calm_request_has_low_risk_explanation(self):
        DepartmentStaffingRule.objects.create(
            department=self.engineering,
            min_staff_required=0,
            max_absent=10,
            criticality_level=3,
        )
        DepartmentCoverageRule.objects.create(
            department=self.engineering,
            production_group=self.engineering_group,
            min_staff_required=0,
            max_absent=10,
            criticality_level=3,
        )

        explanation = build_vacation_request_risk_explanation(
            self.employee,
            date(2026, 6, 1),
            date(2026, 6, 7),
            "paid",
        )

        self.assertEqual(explanation["level"], VacationRequest.RISK_LOW)
        self.assertFalse(explanation["is_conflict"])
        self.assertFalse(explanation["substitution_used"])
        self.assertEqual(explanation["short_reason"], "Критичных пересечений не найдено.")
        self.assertEqual(explanation["details"], [])

    def test_group_shortage_returns_conflict_explanation(self):
        DepartmentStaffingRule.objects.create(
            department=self.engineering,
            min_staff_required=0,
            max_absent=10,
            criticality_level=3,
        )
        DepartmentCoverageRule.objects.create(
            department=self.engineering,
            production_group=self.engineering_group,
            min_staff_required=2,
            max_absent=5,
            criticality_level=5,
        )
        Employees.objects.create(
            last_name="Рисков",
            first_name="Артем",
            middle_name="Иванович",
            login="risk-explanation-group-coworker",
            position="Инженер",
            employee_position=self.engineering_engineer_position,
            department=self.engineering,
            date_joined=date(2024, 1, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )

        risk_payload = calculate_vacation_request_risk(
            self.employee,
            date(2026, 8, 1),
            date(2026, 8, 7),
            "unpaid",
        )
        explanation = build_vacation_request_risk_explanation(
            self.employee,
            date(2026, 8, 1),
            date(2026, 8, 7),
            "unpaid",
        )

        self.assertEqual(risk_payload["risk_level"], VacationRequest.RISK_HIGH)
        self.assertTrue(explanation["is_conflict"])
        self.assertIn("group_shortage", self._detail_kinds(explanation))
        self.assertIn("Инженеры", explanation["short_reason"])

    def test_substitution_is_high_risk_without_conflict(self):
        DepartmentStaffingRule.objects.create(
            department=self.engineering,
            min_staff_required=0,
            max_absent=10,
            criticality_level=3,
        )
        substitute_group = ProductionGroup.objects.create(department=self.engineering, name="Замещающая группа")
        substitute_position = EmployeePosition.objects.create(
            department=self.engineering,
            production_group=substitute_group,
            title="Сменный специалист",
        )
        DepartmentCoverageRule.objects.create(
            department=self.engineering,
            production_group=self.engineering_group,
            min_staff_required=1,
            max_absent=5,
            criticality_level=5,
        )
        ProductionGroupSubstitutionRule.objects.create(
            department=self.engineering,
            source_group=self.engineering_group,
            substitute_group=substitute_group,
            max_covered_absences=1,
        )
        Employees.objects.create(
            last_name="Замещающий",
            first_name="Петр",
            middle_name="Иванович",
            login="risk-explanation-substitute",
            position="Сменный специалист",
            employee_position=substitute_position,
            department=self.engineering,
            date_joined=date(2024, 1, 10),
            annual_paid_leave_days=52,
            role=Employees.ROLE_EMPLOYEE,
        )

        risk_payload = calculate_vacation_request_risk(
            self.employee,
            date(2026, 10, 1),
            date(2026, 10, 7),
            "unpaid",
        )
        explanation = build_vacation_request_risk_explanation(
            self.employee,
            date(2026, 10, 1),
            date(2026, 10, 7),
            "unpaid",
        )

        self.assertEqual(risk_payload["risk_level"], VacationRequest.RISK_HIGH)
        self.assertFalse(explanation["is_conflict"])
        self.assertTrue(explanation["substitution_used"])
        self.assertIn("substitution_used", self._detail_kinds(explanation))
        self.assertIn("замещением", explanation["short_reason"])

    def test_department_head_and_deputy_absence_returns_conflict_explanation(self):
        self.engineering.deputy = self.employee
        self.engineering.save(update_fields=["deputy"])
        DepartmentStaffingRule.objects.create(
            department=self.engineering,
            min_staff_required=0,
            max_absent=10,
            criticality_level=3,
        )
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.department_head,
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 7),
            vacation_type="paid",
            chargeable_days=7,
            status=VacationScheduleItem.STATUS_APPROVED,
        )

        explanation = build_vacation_request_risk_explanation(
            self.employee,
            date(2026, 11, 1),
            date(2026, 11, 7),
            "unpaid",
        )

        self.assertEqual(explanation["level"], VacationRequest.RISK_HIGH)
        self.assertTrue(explanation["is_conflict"])
        self.assertIn("department_leadership_pair", self._detail_kinds(explanation))
        self.assertIn("Руководитель отдела и заместитель", explanation["short_reason"])

    def test_decision_context_moves_load_and_overlaps_to_metrics(self):
        explanation = {
            "level": VacationRequest.RISK_HIGH,
            "label": "Высокий",
            "score": 72,
            "is_conflict": False,
            "short_reason": "Нагрузка отдела на период оценивается как 5/5.",
            "recommended_action": "Лучше отдельно подтвердить решение у руководителя.",
            "affected_department": self.engineering.name,
            "affected_group": self.engineering_group.name,
            "remaining_staff": 8,
            "required_staff": 3,
            "department_load_level": 5,
            "overlapping_absences_count": 2,
            "overlapping_employee_label": "Планова Мария Игоревна, Директоров Олег Игоревич",
            "details": [
                {
                    "kind": "department_load",
                    "severity": "medium",
                    "title": "Повышенная загрузка",
                    "text": "Нагрузка отдела на период оценивается как 5/5.",
                    "affected_department": self.engineering.name,
                    "department_load_level": 5,
                },
                {
                    "kind": "overlapping_absences",
                    "severity": "info",
                    "title": "Есть пересечения",
                    "text": "В этот период уже отсутствуют 2 сотрудника.",
                    "affected_department": self.engineering.name,
                    "overlapping_absences_count": 2,
                },
            ],
        }

        context = self._decision_context(explanation)

        self.assertEqual([metric["label"] for metric in context["metrics"]], ["Состав", "Одновременно отсутствуют", "Нагрузка", "Замещение"])
        self.assertEqual(context["metrics"][1]["value"], "2 сотрудника")
        self.assertEqual(context["metrics"][2]["value"], "5/5 · пиковая")
        self.assertEqual(context["rule_cards"], [])

    def test_decision_context_keeps_real_negative_balance_card(self):
        explanation = {
            "level": VacationRequest.RISK_HIGH,
            "label": "Высокий",
            "score": 82,
            "is_conflict": False,
            "short_reason": "После заявки оплачиваемый баланс уйдет в отрицательное значение.",
            "recommended_action": "Лучше подобрать другой период.",
            "affected_department": self.engineering.name,
            "affected_group": self.engineering_group.name,
            "remaining_staff": 8,
            "required_staff": 3,
            "department_load_level": 1,
            "overlapping_absences_count": 0,
            "details": [
                {
                    "kind": "negative_balance",
                    "severity": "high",
                    "title": "Недостаточно дней",
                    "text": "После заявки оплачиваемый баланс уйдет в отрицательное значение.",
                    "balance_after_request": -7,
                }
            ],
        }

        context = self._decision_context(explanation)

        self.assertEqual(len(context["rule_cards"]), 1)
        self.assertEqual(context["rule_cards"][0]["title"], "Недостаточно дней")

    def test_decision_context_combines_staffing_conflict_cards(self):
        explanation = {
            "level": VacationRequest.RISK_HIGH,
            "label": "Высокий",
            "score": 90,
            "is_conflict": True,
            "short_reason": "Группа «Инженеры» не проходит по составу.",
            "recommended_action": "Сначала перенесите период.",
            "affected_department": self.engineering.name,
            "affected_group": self.engineering_group.name,
            "remaining_staff": 1,
            "required_staff": 2,
            "department_load_level": 3,
            "overlapping_absences_count": 1,
            "details": [
                {
                    "kind": "group_shortage",
                    "severity": "conflict",
                    "title": "Недостаток состава группы",
                    "text": "В группе останется 1 сотрудник при минимуме 2 сотрудника.",
                    "affected_department": self.engineering.name,
                    "affected_group": self.engineering_group.name,
                    "affected_employee_label": "Календарев Иван Петрович",
                    "remaining_staff": 1,
                    "required_staff": 2,
                    "missing_staff": 1,
                },
                {
                    "kind": "group_absence_limit",
                    "severity": "conflict",
                    "title": "Превышен лимит отсутствующих",
                    "text": "В группе отсутствуют 2 сотрудника при лимите 1 сотрудник.",
                    "affected_department": self.engineering.name,
                    "affected_group": self.engineering_group.name,
                    "affected_employee_label": "Календарев Иван Петрович",
                    "absent_staff": 2,
                    "max_absent": 1,
                },
            ],
        }

        context = self._decision_context(explanation)

        self.assertEqual(len(context["rule_cards"]), 1)
        self.assertEqual(context["rule_cards"][0]["title"], "Группа не проходит по составу")
        self.assertIn("Календарев Иван Петрович", context["rule_cards"][0]["people_label"])
