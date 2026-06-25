from time import monotonic

from django.core.management.base import BaseCommand, CommandError

from apps.core.models import DemoDataResetJob
from apps.core.services.demo_baseline import (
    DemoBaselineMissingError,
    DemoBaselineResetInProgressError,
    reset_demo_to_baseline,
)
from apps.core.services.demo_reset_jobs import update_demo_data_reset_job_progress
from apps.core.services.demo_seed import DemoVacationSeedRunner
from apps.core.services.demo_seed.constants import (
    DEFAULT_SCHEDULE_HISTORY_YEARS,
    DEMO_SEED_PRESET_FAST,
    DEMO_SEED_PRESET_FULL,
    DEMO_SEED_PRESET_STANDARD,
    DEMO_SEED_PRESETS,
)


class Command(BaseCommand):
    help = "Reset demo enterprise data and create realistic departments, employees, logins, and vacation history"

    def execute(self, *args, **options):
        try:
            return super().execute(*args, **options)
        except Exception as exc:
            progress_job_id = options.get("progress_job_id") or getattr(self, "progress_job_id", None)
            if progress_job_id:
                runner = getattr(self, "_seed_runner", None)
                update_demo_data_reset_job_progress(
                    progress_job_id,
                    status=DemoDataResetJob.STATUS_FAILED,
                    progress_percent=getattr(runner, "_last_progress_percent", 0),
                    stage_label="Ошибка пересоздания",
                    error_message=str(exc),
                    finished=True,
                )
            raise

    def add_arguments(self, parser):
        parser.add_argument("--seed-value", type=int, default=42)
        parser.add_argument(
            "--preset",
            choices=[DEMO_SEED_PRESET_STANDARD, DEMO_SEED_PRESET_FAST, DEMO_SEED_PRESET_FULL],
            default=DEMO_SEED_PRESET_STANDARD,
            help="Preset size for demo data: standard, fast, or full research dataset.",
        )
        parser.add_argument(
            "--history-years",
            type=int,
            default=None,
            help="How many full years before the current year to include in vacation history.",
        )
        parser.add_argument(
            "--fast",
            action="store_true",
            help="Create a smaller but structurally complete dataset for tests and quick checks.",
        )
        parser.add_argument(
            "--force-rebuild",
            action="store_true",
            help="Rebuild demo data from scratch even when a fast baseline snapshot is available.",
        )
        parser.add_argument(
            "--confirm-reset",
            action="store_true",
            help="Confirm deleting existing demo data before rebuilding the demo enterprise dataset.",
        )
        parser.add_argument(
            "--progress-job-id",
            type=int,
            default=None,
            help="DemoDataResetJob id for background progress updates.",
        )

    def handle(self, *args, **options):
        if not options["confirm_reset"]:
            raise CommandError(
                "seed_vacation_requests deletes existing demo employees, departments, vacation requests, "
                "schedules, and linked users. Run again with --confirm-reset to rebuild demo data."
            )

        self.progress_job_id = options.get("progress_job_id")
        preset = options["preset"]
        if options["fast"]:
            preset = DEMO_SEED_PRESET_FAST
        preset_spec = DEMO_SEED_PRESETS[preset]
        history_years = options["history_years"]
        if history_years is None:
            history_years = preset_spec.get("history_years", DEFAULT_SCHEDULE_HISTORY_YEARS)
        if preset == DEMO_SEED_PRESET_FAST and not options["force_rebuild"]:
            try:
                self._restore_fast_baseline(progress_job_id=options.get("progress_job_id"))
                return
            except DemoBaselineMissingError:
                self.stdout.write(
                    self.style.WARNING(
                        "Быстрый снимок демо-данных не найден. Запускается первичное пересоздание demo-БД."
                    )
                )
            except DemoBaselineResetInProgressError as exc:
                raise CommandError(
                    "Быстрый сброс демо-данных уже выполняется. Дождитесь завершения текущей операции."
                ) from exc
        self._seed_runner = DemoVacationSeedRunner(stdout=self.stdout, style=self.style)
        return self._seed_runner.run(
            seed_value=options["seed_value"],
            history_years=history_years,
            fast=preset == DEMO_SEED_PRESET_FAST,
            progress_job_id=options.get("progress_job_id"),
            preset=preset,
            employee_counts=preset_spec.get("employee_counts"),
        )

    def _restore_fast_baseline(self, *, progress_job_id=None):
        started_at = monotonic()
        if progress_job_id:
            update_demo_data_reset_job_progress(
                progress_job_id,
                status=DemoDataResetJob.STATUS_RUNNING,
                progress_percent=5,
                stage_label="Быстрый снимок",
                message="Восстанавливается уже подготовленная демо-база.",
                started=True,
            )

        result = reset_demo_to_baseline(ignore_reset_job_id=progress_job_id)
        elapsed_seconds = max(0.0, monotonic() - started_at)
        message = (
            f"Быстрая демо-база восстановлена из снимка за {elapsed_seconds:.1f} сек. "
            "Можно войти заново с паролем 1234."
        )

        if progress_job_id:
            update_demo_data_reset_job_progress(
                progress_job_id,
                status=DemoDataResetJob.STATUS_SUCCEEDED,
                progress_percent=100,
                stage_label="Готово",
                message=message,
                finished=True,
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"{message} planning_year={result['planning_year']}, departments={result['departments']}."
            )
        )
