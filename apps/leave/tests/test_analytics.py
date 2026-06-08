from datetime import date

from django.urls import reverse

from apps.leave.models import DepartmentWorkload, VacationPreference, VacationRequest, VacationSchedule, VacationScheduleItem
from apps.leave.services import analytics as analytics_service
from apps.leave.services.analytics import build_analytics_payload

from .base import LeaveTestCase


class LeaveAnalyticsTests(LeaveTestCase):
    def test_department_head_analytics_are_limited_to_own_department(self):
        VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 1, 30),
            end_date=date(2026, 2, 2),
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )
        VacationRequest.objects.create(
            employee=self.outsider,
            start_date=date(2026, 1, 10),
            end_date=date(2026, 1, 12),
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )
        self.client.force_login(self.department_head.user)

        response = self.client.get(reverse("analytics"))

        self.assertEqual(response.status_code, 200)
        row_employee_ids = {row["employee_id"] for row in response.context["rows"]}
        self.assertIn(self.employee.id, row_employee_ids)
        self.assertNotIn(self.outsider.id, row_employee_ids)

    def test_analytics_split_duration_by_month_overlap(self):
        VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 1, 30),
            end_date=date(2026, 2, 2),
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )

        payload = build_analytics_payload()

        self.assertEqual(payload["values1"][0], 1)
        self.assertEqual(payload["values1"][1], 1)
        self.assertEqual(payload["values2"][0], 2)
        self.assertEqual(payload["values2"][1], 2)
        self.assertEqual(payload["values3"][0], 2)
        self.assertEqual(payload["values3"][1], 2)

    def test_analytics_department_filter_scopes_dashboard(self):
        VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 4, 10),
            end_date=date(2026, 4, 14),
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )
        VacationRequest.objects.create(
            employee=self.outsider,
            start_date=date(2026, 4, 10),
            end_date=date(2026, 4, 14),
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )
        self.client.force_login(self.enterprise_head.user)

        response = self.client.get(
            reverse("analytics"),
            {"department": self.engineering.id, "year": 2026},
        )

        self.assertEqual(response.status_code, 200)
        row_employee_ids = {row["employee_id"] for row in response.context["rows"]}
        heatmap_departments = {row["department_name"] for row in response.context["department_heatmap"]}
        self.assertIn(self.employee.id, row_employee_ids)
        self.assertNotIn(self.outsider.id, row_employee_ids)
        self.assertEqual(response.context["analytics_filters"]["selected_department"], str(self.engineering.id))
        self.assertEqual(heatmap_departments, {"Engineering"})
        self.assertContains(response, "data-schedule-status-tooltip")
        self.assertContains(response, 'data-tooltip-title="Норма"')
        self.assertContains(response, 'data-tooltip-title="Доступно"')
        self.assertContains(response, 'data-tooltip-title="Готовность предпочтений"')

    def test_hr_can_open_analytics_with_module_summary(self):
        VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 5, 10),
            end_date=date(2026, 5, 14),
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
            risk_level=VacationRequest.RISK_HIGH,
            risk_score=90,
            ai_score=42,
            ai_recommendation="avoid",
            ai_explanation="Лучше проверить месяц.",
        )
        self.client.force_login(self.hr_employee.user)

        response = self.client.get(reverse("analytics"), {"year": 2026})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Сводка модуля")
        self.assertContains(response, "Модуль 42,00%")
        self.assertContains(response, "лучше проверить")

    def test_employee_attention_item_uses_calendar_role_icon(self):
        rows = [
            {
                "has_conflict": True,
                "has_high_risk": False,
                "role_icon": "♛",
                "role_icon_type": "symbol",
                "role_variant": "enterprise-head",
                "employee_name": "Руководитель Предприятия",
                "issue_description": "Руководитель предприятия и заместитель будут отсутствовать одновременно.",
                "profile_url": "/employee/1/?from=calendar",
            }
        ]

        items = analytics_service._build_attention_items(
            [],
            {"low_balance_count": 0},
            {"attention_count": 0},
            {"total_pending": 0},
            rows,
            2026,
        )

        self.assertEqual(items[0]["icon"], "♛")
        self.assertEqual(items[0]["icon_type"], "symbol")
        self.assertEqual(items[0]["icon_role_variant"], "enterprise-head")
        self.assertEqual(items[0]["url"], "/employee/1/?from=analytics")

    def test_department_head_module_summary_is_limited_to_own_department(self):
        VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 6, 10),
            end_date=date(2026, 6, 14),
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
            risk_level=VacationRequest.RISK_HIGH,
            risk_score=90,
            ai_score=48,
            ai_recommendation="avoid",
        )
        VacationRequest.objects.create(
            employee=self.outsider,
            start_date=date(2026, 7, 10),
            end_date=date(2026, 7, 14),
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
            ai_score=12,
            ai_recommendation="blocked",
        )
        self.client.force_login(self.department_head.user)

        response = self.client.get(reverse("analytics"), {"year": 2026})

        top_departments = {item["department_name"] for item in response.context["module_summary"]["top_cells"]}
        self.assertIn("Engineering", top_departments)
        self.assertNotIn("HR", top_departments)

    def test_low_saved_module_score_gets_into_top_cells(self):
        VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 8, 10),
            end_date=date(2026, 8, 14),
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
            risk_level=VacationRequest.RISK_HIGH,
            risk_score=92,
            ai_score=42,
            ai_recommendation="avoid",
        )

        payload = build_analytics_payload(employee_ids=[self.employee.id, self.department_head.id], year=2026)

        top_cell = payload["module_summary"]["top_cells"][0]
        self.assertEqual(top_cell["department_name"], "Engineering")
        self.assertEqual(top_cell["month_number"], 8)
        self.assertEqual(top_cell["score_label"], "42,00%")
        self.assertEqual(top_cell["recommendation_label"], "лучше проверить")

    def test_low_saved_module_score_without_calendar_risk_does_not_take_over_month(self):
        VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 2, 23),
            end_date=date(2026, 2, 24),
            vacation_type="paid",
            status=VacationRequest.STATUS_PENDING,
            ai_score=42,
            ai_recommendation="avoid",
        )

        payload = build_analytics_payload(employee_ids=[self.employee.id, self.department_head.id], year=2026)

        february = payload["module_summary"]["monthly_rollup"][1]
        self.assertEqual(payload["module_summary"]["top_cells"], [])
        self.assertEqual(february["score_label"], "80,21%")
        self.assertEqual(february["variant"], "planned")

    def test_department_staffing_pressure_does_not_become_module_conflict(self):
        department = {
            "department_id": self.engineering.id,
            "department_name": self.engineering.name,
        }
        month = {
            "month_number": 9,
            "month_short": "Сен",
            "month_name": "Сентябрь",
            "status": "conflict",
            "absent_count": 11,
            "busy_days": 120,
            "remaining_staff": 14,
            "min_staff_required": 14,
            "max_absent": 8,
            "load_level": 5,
            "risk_count": 0,
            "conflict_count": 0,
            "near_limit": True,
            "breaks_min_staff": False,
            "exceeds_absent_limit": True,
        }

        cell = analytics_service._module_payload_for_department_month(department, month, [], 2026)

        self.assertEqual(cell["variant"], "info")
        self.assertEqual(cell["score_label"], "72,20%")
        self.assertEqual(cell["recommendation_label"], "под контролем")
        self.assertIn("ориентира отдела", cell["reason"])

    def test_department_staffing_pressure_is_heatmap_load_not_conflict(self):
        DepartmentWorkload.objects.create(
            department=self.engineering,
            year=2026,
            month=6,
            load_level=4,
            min_staff_required=1,
            max_absent=1,
        )
        VacationRequest.objects.create(
            employee=self.employee,
            start_date=date(2026, 6, 10),
            end_date=date(2026, 6, 12),
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )
        VacationRequest.objects.create(
            employee=self.department_head,
            start_date=date(2026, 6, 15),
            end_date=date(2026, 6, 17),
            vacation_type="paid",
            status=VacationRequest.STATUS_APPROVED,
        )

        payload = build_analytics_payload(employee_ids=[self.employee.id, self.department_head.id], year=2026)

        month = payload["department_heatmap"][0]["months"][5]
        self.assertEqual(month["absent_count"], 2)
        self.assertTrue(month["exceeds_absent_limit"])
        self.assertEqual(month["status"], "watch")
        self.assertEqual(month["conflict_count"], 0)

    def test_schedule_snapshot_alone_does_not_make_calm_month_risky(self):
        department = {
            "department_id": self.engineering.id,
            "department_name": self.engineering.name,
        }
        month = {
            "month_number": 6,
            "month_short": "Июн",
            "month_name": "Июнь",
            "status": "stable",
            "absent_count": 4,
            "busy_days": 38,
            "remaining_staff": 20,
            "min_staff_required": 7,
            "max_absent": 5,
            "load_level": 2,
            "risk_count": 0,
            "conflict_count": 0,
            "near_limit": False,
            "breaks_min_staff": False,
            "exceeds_absent_limit": False,
        }
        snapshots = [
            {
                "department_id": self.engineering.id,
                "start_date": date(2026, 6, 3),
                "end_date": date(2026, 6, 12),
                "score": 43,
                "recommendation": "avoid",
                "source_kind": "schedule",
            }
        ]

        cell = analytics_service._module_payload_for_department_month(department, month, snapshots, 2026)

        self.assertEqual(cell["variant"], "planned")
        self.assertEqual(cell["score_label"], "88,99%")
        self.assertFalse(cell["has_attention"])
        self.assertIn("Учтены сохраненные ML-снимки", cell["reason"])

    def test_normal_saved_module_score_does_not_take_over_month_rollup(self):
        department = {
            "department_id": self.engineering.id,
            "department_name": self.engineering.name,
        }
        month = {
            "month_number": 6,
            "month_short": "Июн",
            "month_name": "Июнь",
            "status": "stable",
            "absent_count": 1,
            "busy_days": 2,
            "remaining_staff": 9,
            "min_staff_required": 5,
            "max_absent": 5,
            "load_level": 1,
            "risk_count": 0,
            "conflict_count": 0,
            "near_limit": False,
            "breaks_min_staff": False,
            "exceeds_absent_limit": False,
        }
        snapshots = [
            {
                "department_id": self.engineering.id,
                "start_date": date(2026, 6, 24),
                "end_date": date(2026, 6, 25),
                "score": 61,
                "recommendation": "normal",
                "source_kind": "request",
            }
        ]

        cell = analytics_service._module_payload_for_department_month(department, month, snapshots, 2026)
        rollup = analytics_service._build_module_monthly_rollup([cell], 2026)

        self.assertEqual(cell["variant"], "planned")
        self.assertFalse(cell["has_attention"])
        self.assertNotIn("ниже комфортного уровня", cell["reason"])
        self.assertEqual(rollup[5]["score_label"], "93,15%")
        self.assertEqual(rollup[5]["variant"], "planned")
        self.assertFalse(rollup[5]["has_attention"])

    def test_module_month_rollup_uses_smooth_scores_for_calm_months(self):
        department = {
            "department_id": self.engineering.id,
            "department_name": self.engineering.name,
            "employees_count": 10,
        }
        quiet_month = {
            "month_number": 2,
            "month_short": "Фев",
            "month_name": "Февраль",
            "status": "stable",
            "absent_count": 1,
            "busy_days": 2,
            "remaining_staff": 9,
            "min_staff_required": 5,
            "max_absent": 5,
            "load_level": 1,
            "risk_count": 0,
            "conflict_count": 0,
            "near_limit": False,
            "breaks_min_staff": False,
            "exceeds_absent_limit": False,
        }
        busier_month = {
            **quiet_month,
            "month_number": 6,
            "month_short": "Июн",
            "month_name": "Июнь",
            "absent_count": 4,
            "busy_days": 38,
            "remaining_staff": 6,
            "load_level": 4,
            "near_limit": True,
        }

        quiet_cell = analytics_service._module_payload_for_department_month(department, quiet_month, [], 2026)
        busier_cell = analytics_service._module_payload_for_department_month(department, busier_month, [], 2026)
        rollup = analytics_service._build_module_monthly_rollup([quiet_cell, busier_cell], 2026)

        self.assertNotEqual(quiet_cell["score_label"], busier_cell["score_label"])
        self.assertEqual(rollup[1]["score_label"], quiet_cell["score_label"])
        self.assertEqual(rollup[5]["score_label"], busier_cell["score_label"])

    def test_medium_risks_softly_lower_module_score_without_calendar_risk_filter(self):
        department = {
            "department_id": self.engineering.id,
            "department_name": self.engineering.name,
            "employees_count": 10,
        }
        month = {
            "month_number": 9,
            "month_short": "Сен",
            "month_name": "Сентябрь",
            "status": "stable",
            "absent_count": 8,
            "busy_days": 120,
            "remaining_staff": 2,
            "min_staff_required": 2,
            "max_absent": 8,
            "load_level": 5,
            "medium_risk_count": 6,
            "risk_count": 0,
            "conflict_count": 0,
            "near_limit": True,
            "breaks_min_staff": False,
            "exceeds_absent_limit": False,
        }

        cell = analytics_service._module_payload_for_department_month(department, month, [], 2026)

        self.assertEqual(cell["variant"], "risk")
        self.assertEqual(cell["module_status"], "watch")
        self.assertEqual(cell["recommendation_label"], "лучше проверить")
        self.assertIn("средние предупреждения", cell["reason"])
        self.assertNotIn("issue=risk", cell["calendar_url"])
        self.assertNotIn("issue=conflict", cell["calendar_url"])

    def test_module_summary_falls_back_without_saved_snapshots(self):
        payload = build_analytics_payload(employee_ids=[self.employee.id], year=2026)

        self.assertIn("module_summary", payload)
        self.assertEqual(payload["module_summary"]["top_cells"], [])
        self.assertEqual(len(payload["module_summary"]["monthly_rollup"]), 12)

    def test_module_summary_does_not_mark_empty_understaffed_month_as_conflict(self):
        DepartmentWorkload.objects.create(
            department=self.engineering,
            year=2026,
            month=10,
            load_level=1,
            min_staff_required=5,
            max_absent=5,
        )

        payload = build_analytics_payload(employee_ids=[self.employee.id, self.department_head.id], year=2026)

        october = payload["module_summary"]["monthly_rollup"][9]
        self.assertEqual(payload["module_summary"]["top_cells"], [])
        self.assertEqual(october["score_label"], "91,50%")
        self.assertEqual(october["variant"], "planned")

    def test_analytics_payload_contains_planning_dashboard_sections(self):
        schedule = VacationSchedule.objects.create(
            year=2026,
            status=VacationSchedule.STATUS_APPROVED,
            created_by=self.enterprise_head,
            approved_by=self.enterprise_head,
        )
        VacationScheduleItem.objects.create(
            schedule=schedule,
            employee=self.employee,
            start_date=date(2026, 3, 3),
            end_date=date(2026, 3, 10),
            vacation_type="paid",
            chargeable_days=8,
            status=VacationScheduleItem.STATUS_APPROVED,
            risk_level=VacationScheduleItem.RISK_HIGH,
            risk_score=82,
        )
        DepartmentWorkload.objects.create(
            department=self.engineering,
            year=2026,
            month=3,
            load_level=5,
            min_staff_required=1,
            max_absent=1,
        )
        VacationPreference.objects.create(
            employee=self.employee,
            year=2026,
            start_date=date(2026, 3, 3),
            end_date=date(2026, 3, 10),
            status=VacationPreference.STATUS_FILLED,
        )

        payload = build_analytics_payload(employee_ids=[self.employee.id, self.department_head.id], year=2026)

        self.assertIn("planning_kpis", payload)
        self.assertIn("department_heatmap", payload)
        self.assertIn("analytics_chart_payload", payload)
        self.assertEqual(len(payload["monthly_metrics"]), 12)
        self.assertEqual(payload["monthly_metrics"][2]["schedule_days"], 8)
        self.assertEqual(payload["planned_employee_count"], 1)
        self.assertEqual(payload["preference_summary"]["ready_count"], 1)
        self.assertEqual(payload["analytics_chart_payload"]["sources"]["schedule"][2], 8)
