import random
from collections import Counter
from datetime import date

from django.db import transaction
from django.utils import timezone

from apps.core.models import DemoDataResetJob
from apps.core.services.demo_baseline import capture_demo_baseline_snapshot
from apps.core.services.demo_reset_jobs import update_demo_data_reset_job_progress
from apps.core.services.demo_seed.audit import DemoSeedAuditMixin
from apps.core.services.demo_seed.constants import (
    DEFAULT_SCHEDULE_HISTORY_YEARS,
    DEMO_SEED_PRESET_FULL,
    ENTERPRISE_HEAD_COUNT,
    FAST_SCHEDULE_HISTORY_YEARS,
)
from apps.core.services.demo_seed.context import DemoSeedContext, NameFactory
from apps.core.services.demo_seed.enterprise import DemoSeedEnterpriseMixin
from apps.core.services.demo_seed.manual_cases import create_manual_draft_cases
from apps.core.services.demo_seed.reset import DemoSeedResetMixin
from apps.core.services.demo_seed.vacations import DemoSeedVacationMixin
from apps.leave.ml.traces import create_historical_schedule_ml_traces
from apps.leave.models import VacationRequest
from apps.leave.services.metrics import set_vacation_metric_sync_enabled
from apps.leave.services.notifications import backfill_notifications_from_history
from apps.leave.services.planning_cycles import ensure_active_planning_cycle


class DemoVacationSeedRunner(
    DemoSeedAuditMixin,
    DemoSeedResetMixin,
    DemoSeedEnterpriseMixin,
    DemoSeedVacationMixin,
):
    def __init__(self, *, stdout, style):
        self.stdout = stdout
        self.style = style
        self._last_progress_percent = 0

    @transaction.atomic
    def run(
        self,
        *,
        seed_value,
        history_years=DEFAULT_SCHEDULE_HISTORY_YEARS,
        fast=False,
        progress_job_id=None,
        preset="",
        employee_counts=None,
    ):
        self.rng = random.Random(seed_value)
        self.today = timezone.localdate()
        self.fast_mode = fast
        if self.fast_mode:
            history_years = min(max(1, history_years), FAST_SCHEDULE_HISTORY_YEARS)
        else:
            history_years = max(1, history_years)
        self.demo_preset = preset or ("fast" if self.fast_mode else "full")
        self.demo_employee_counts = list(employee_counts) if employee_counts else None
        self.context = DemoSeedContext(
            seed_value=seed_value,
            history_years=history_years,
            fast_mode=self.fast_mode,
            progress_job_id=progress_job_id,
            today=self.today,
            rng=self.rng,
        )
        self.schedule_end_year = self.today.year
        self.schedule_start_year = self.schedule_end_year - history_years
        enterprise_history_years = (
            DEFAULT_SCHEDULE_HISTORY_YEARS
            if self.demo_preset == DEMO_SEED_PRESET_FULL
            else history_years
        )
        self.enterprise_start_year = self.schedule_end_year - enterprise_history_years
        self.schedule_approval_cutoff = date(self.schedule_end_year - 1, 12, 31)
        self.department_specs = self._build_department_specs()
        self.department_spec_by_name = {spec["name"]: spec for spec in self.department_specs}
        self.total_employee_count = sum(spec["employee_count"] for spec in self.department_specs)
        self.name_factory = NameFactory(self.rng)
        self.status_counts = Counter()
        self.schedule_item_counts = Counter()
        self.transfer_counts = Counter()
        self.schedule_by_year = {}
        self.department_workload = {}
        self.staffing_rules = {}
        self.position_by_department_title = {}
        self.group_by_department_name = {}
        self._paid_source_signature_by_employee = {}
        self._employees_with_saved_allocations = set()
        self._calendar_year_paid_days_cache = {}
        self._active_request_periods_by_employee = {}
        self._active_schedule_item_periods_by_employee = {}
        self._department_active_periods_by_year = {}
        self.progress_job_id = progress_job_id

        previous_sync_state = set_vacation_metric_sync_enabled(False)
        try:
            self._progress(
                DemoDataResetJob.STATUS_RUNNING,
                1,
                "Подготовка",
                "Запуск полного пересоздания демо-данных.",
                started=True,
            )
            self._progress(
                DemoDataResetJob.STATUS_RUNNING,
                5,
                "Очистка демо-БД",
                "Удаляются старые демо-сотрудники, графики, заявки и связанные данные.",
            )
            self._reset_demo_data()
            self._progress(
                DemoDataResetJob.STATUS_RUNNING,
                12,
                "Структура предприятия",
                "Создаются отделы, сотрудники, пользователи и правила состава.",
            )
            departments = self._create_departments()
            self._create_staffing_reference_data(departments)
            enterprise_head = self._create_enterprise_head()
            authorized_person = self._create_authorized_person()
            hr_team = self._create_hr_team(departments[-1])
            department_heads = self._create_department_heads(departments)
            employees = self._create_department_employees(departments)
            self._assign_department_deputies(departments)
            self._assign_enterprise_deputy(hr_team)
            self._create_staffing_rules(departments)
            self._create_department_workload(departments)
            self._progress(
                DemoDataResetJob.STATUS_RUNNING,
                24,
                "Исторические графики",
                "Создаются архивные и текущие графики отпусков.",
            )
            self._create_historical_schedules(hr_team[0], enterprise_head, authorized_person, departments)

            everyone = [enterprise_head, *hr_team, *department_heads, *employees]
            self._progress(
                DemoDataResetJob.STATUS_RUNNING,
                34,
                "Пожелания и отпуска",
                "Формируются исторические пожелания и отпуска сотрудников.",
            )
            self._create_vacation_preferences(everyone)
            for index, employee in enumerate(everyone, start=1):
                self._seed_employee_vacations(employee)
                if index % 20 == 0 or index == len(everyone):
                    self._progress(
                        DemoDataResetJob.STATUS_RUNNING,
                        min(49, 34 + int(index / max(len(everyone), 1) * 15)),
                        "Пожелания и отпуска",
                        f"Обработано сотрудников: {index} из {len(everyone)}.",
                    )

            self._progress(
                DemoDataResetJob.STATUS_RUNNING,
                52,
                "Нормализация истории",
                "Выравниваются балансы, переносы и календарная история отпусков.",
            )
            self._cancel_unallocatable_paid_sources(everyone)
            self._normalize_calendar_year_leave_history(everyone)
            self._cancel_unallocatable_paid_sources(everyone)
            self._create_balanced_special_request_history(everyone)
            self._cancel_unallocatable_paid_sources(everyone)
            self._normalize_calendar_year_leave_history(everyone)
            self._cancel_unallocatable_paid_sources(everyone)
            self._stabilize_current_calendar_year_leave(everyone)
            self._normalize_planning_year_carryover(everyone)
            self._cancel_unallocatable_paid_sources(everyone)
            self._stabilize_current_calendar_year_leave(everyone)
            self._normalize_short_paid_leave_fragments(everyone)
            self._cleanup_tiny_generated_calendar_year_leaves(everyone)
            self._progress(
                DemoDataResetJob.STATUS_RUNNING,
                68,
                "Переносы и ручные кейсы",
                "Создаются исторические переносы и демо-кейсы для черновика.",
            )
            self._create_historical_manager_initiated_transfers()
            self._create_pending_current_year_transfers()
            self._normalize_historical_schedule_risk_levels()
            self._normalize_demo_historical_staffing_pressure(everyone)
            self._normalize_pre_planning_deadline_leftovers(everyone)
            if not self._historical_manager_transfer_exists(status="approved"):
                self._create_required_approved_manager_transfer(department_heads)
            self.manual_draft_case_stats = create_manual_draft_cases(
                planning_year=self.schedule_end_year + 1,
                employees=everyone,
            )
            self._progress(
                DemoDataResetJob.STATUS_RUNNING,
                78,
                "ML-следы",
                "Создаются generation runs, candidates, packages и feedback для обучения.",
            )
            self.historical_ml_trace_stats = create_historical_schedule_ml_traces(
                self.rng,
                hr_team[0],
                self.schedule_end_year,
            )
            self._progress(
                DemoDataResetJob.STATUS_RUNNING,
                88,
                "Уведомления",
                "Восстанавливаются уведомления из истории заявок и графиков.",
            )
            self.notification_stats = backfill_notifications_from_history(as_of_date=self.today)
        finally:
            set_vacation_metric_sync_enabled(previous_sync_state)

        self._progress(
            DemoDataResetJob.STATUS_RUNNING,
            94,
            "Отпускные балансы",
            "Пересчитываются отпускные балансы сотрудников.",
        )
        for index, employee in enumerate(everyone, start=1):
            self._rebuild_employee_leave_ledger(employee)
            if index % 20 == 0 or index == len(everyone):
                self._progress(
                    DemoDataResetJob.STATUS_RUNNING,
                    min(99, 94 + int(index / max(len(everyone), 1) * 5)),
                    "Отпускные балансы",
                    f"Пересчитано сотрудников: {index} из {len(everyone)}.",
                )

        self._write_calendar_leave_audit(everyone)
        self._progress(
            DemoDataResetJob.STATUS_RUNNING,
            99,
            "Начальная точка",
            "Сохраняется снимок для быстрого сброса демо-состояния.",
        )
        self.demo_baseline_snapshot = capture_demo_baseline_snapshot(
            planning_year=self.schedule_end_year + 1,
            seed_value=seed_value,
        )
        ensure_active_planning_cycle(self.schedule_end_year + 1, actor=hr_team[0])
        self._progress(
            DemoDataResetJob.STATUS_SUCCEEDED,
            100,
            "Готово",
            "Демо-данные пересозданы. Можно войти заново с паролем 1234.",
            finished=True,
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Демо-база предприятия создана: "
                f"отделы={len(departments)}, "
                f"руководители_отделов={len(department_heads)}, "
                f"hr={len(hr_team)}, "
                f"директора={ENTERPRISE_HEAD_COUNT}, "
                f"сотрудники={len(employees)}"
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Созданы заявки: "
                f"approved={self.status_counts[VacationRequest.STATUS_APPROVED]}, "
                f"pending={self.status_counts[VacationRequest.STATUS_PENDING]}, "
                f"rejected={self.status_counts[VacationRequest.STATUS_REJECTED]}"
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Созданы переносы: "
                f"employee_history_approved={self.transfer_counts['employee_historical_approved']}, "
                f"employee_history_rejected={self.transfer_counts['employee_historical_rejected']}, "
                f"employee_current_pending={self.transfer_counts['employee_current_pending']}, "
                f"manager_history_approved={self.transfer_counts['manager_historical_approved']}, "
                f"manager_history_rejected={self.transfer_counts['manager_historical_rejected']}, "
                f"manager_current_pending={self.transfer_counts['manager_current_pending']}"
            )
        )
        if getattr(self, "manual_draft_case_stats", None):
            self.stdout.write(
                self.style.SUCCESS(
                    "Ручные кейсы черновика: "
                    f"urgent_closures={self.manual_draft_case_stats['urgent_closures']}, "
                    f"days={self.manual_draft_case_stats['days']}, "
                    f"employee_review={self.manual_draft_case_stats['employee_review']}"
                )
            )
        self.stdout.write(
            self.style.SUCCESS(
                "Созданы уведомления: "
                f"created={self.notification_stats['notifications_created']}, "
                f"updated={self.notification_stats['notifications_updated']}"
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                "Сохранён начальный снимок демо-состояния: "
                f"planning_year={self.demo_baseline_snapshot.planning_year}, "
                f"seed={self.demo_baseline_snapshot.seed_value}"
            )
        )
    def _progress(self, status, progress_percent, stage_label, message, *, started=False, finished=False):
        if not getattr(self, "progress_job_id", None):
            return
        self._last_progress_percent = progress_percent
        update_demo_data_reset_job_progress(
            self.progress_job_id,
            status=status,
            progress_percent=progress_percent,
            stage_label=stage_label,
            message=message,
            error_message="" if status != DemoDataResetJob.STATUS_FAILED else None,
            started=started,
            finished=finished,
        )
