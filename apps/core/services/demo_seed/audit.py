from datetime import date

from apps.core.services.demo_seed.constants import ENTERPRISE_HEAD_COUNT
from apps.leave.models import VacationRequest


class DemoSeedAuditMixin:
    def _write_calendar_leave_audit(self, employees):
        rows = self._calendar_leave_audit_rows(employees)
        adjustment_bits = []
        if getattr(self, "calendar_leave_adjustments", None):
            adjustment_bits = [
                f"добрано_дней={self.calendar_leave_adjustments['top_up_days']}",
                f"добрано_периодов={self.calendar_leave_adjustments['top_up_items']}",
                f"снято_лишних_дней={self.calendar_leave_adjustments['trimmed_days']}",
                f"снято_периодов={self.calendar_leave_adjustments['trimmed_items']}",
                f"отменено_микро={self.calendar_leave_adjustments['cancelled_tiny_items']}",
                f"отменено_коротких={self.calendar_leave_adjustments['cancelled_short_items']}",
            ]
        if getattr(self, "carryover_adjustments", None):
            adjustment_bits.extend(
                [
                    f"перенос_добрано_дней={self.carryover_adjustments['top_up_days']}",
                    f"перенос_добрано_периодов={self.carryover_adjustments['top_up_items']}",
                    f"перенос_не_размещено={self.carryover_adjustments['unplaced_employees']}",
                ]
            )
        if getattr(self, "stale_deadline_adjustments", None):
            adjustment_bits.extend(
                [
                    f"старые_сроки_добрано_дней={self.stale_deadline_adjustments['top_up_days']}",
                    f"старые_сроки_периодов={self.stale_deadline_adjustments['top_up_items']}",
                    f"старые_сроки_не_размещено={self.stale_deadline_adjustments['unplaced_employees']}",
                ]
            )
        self.stdout.write("Аудит календарных отпусков:")
        if adjustment_bits:
            self.stdout.write("  нормализация: " + ", ".join(adjustment_bits))
        if getattr(self, "hr_policy_selection_stats", None):
            stats = self.hr_policy_selection_stats
            events = max(stats.get("events", 0), 1)
            top_selected = stats.get("top_selected", 0)
            close_selected = stats.get("close_alternative_selected", 0)
            near_selected = stats.get("near_alternative_selected", 0)
            self.stdout.write(
                "  HR-policy: "
                f"событий={stats.get('events', 0)}, "
                f"лучший={top_selected} ({top_selected / events * 100:.1f}%), "
                f"близкая_альтернатива={close_selected + near_selected} "
                f"({(close_selected + near_selected) / events * 100:.1f}%)"
            )
        for row in rows:
            self.stdout.write(
                "  "
                f"{row['year']}: "
                f"сотрудников={row['employees']}, "
                f"0={row['zero']}, "
                f"1-13={row['small_1_13']}, "
                f"<28={row['under_28']}, "
                f">=52={row['gte_52']}, "
                f">70={row['gt_70']}, "
                f"среднее={row['avg']:.1f}, "
                f"максимум={row['max']}"
            )

    def _calendar_leave_audit_rows(self, employees):
        eligible_employees = [employee for employee in employees if not employee.is_service_account]
        rows = []
        for year in range(self.schedule_start_year, self.schedule_end_year + 1):
            year_end = date(year, 12, 31)
            totals = [
                float(self._calendar_year_paid_schedule_days(employee, year))
                for employee in eligible_employees
                if employee.date_joined <= year_end
            ]
            if not totals:
                continue
            rows.append(
                {
                    "year": year,
                    "employees": len(totals),
                    "zero": sum(1 for total in totals if total == 0),
                    "small_1_13": sum(1 for total in totals if 0 < total < 14),
                    "under_28": sum(1 for total in totals if 0 < total < 28),
                    "gte_52": sum(1 for total in totals if total >= 52),
                    "gt_70": sum(1 for total in totals if total > 70),
                    "avg": sum(totals) / len(totals),
                    "max": max(totals),
                }
            )
        return rows
