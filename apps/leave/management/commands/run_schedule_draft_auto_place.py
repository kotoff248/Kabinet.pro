from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError

from apps.employees.models import Employees
from apps.leave.models import VacationScheduleAutoPlaceJob
from apps.leave.services.schedule_auto_place_jobs import update_schedule_auto_place_job_progress
from apps.leave.services.schedule_drafts.auto_place import auto_place_remaining_schedule_draft


class Command(BaseCommand):
    help = "Run smart vacation schedule auto placement in a background process."

    def add_arguments(self, parser):
        parser.add_argument("--job-id", type=int, required=True)
        parser.add_argument("--year", type=int, required=True)
        parser.add_argument("--actor-id", type=int, required=True)

    def handle(self, *args, **options):
        job_id = options["job_id"]
        year = options["year"]
        actor_id = options["actor_id"]

        try:
            VacationScheduleAutoPlaceJob.objects.get(id=job_id)
        except VacationScheduleAutoPlaceJob.DoesNotExist as exc:
            raise CommandError(f"Auto-place job {job_id} not found.") from exc

        actor = Employees.objects.filter(id=actor_id).first()
        if actor is None:
            update_schedule_auto_place_job_progress(
                job_id,
                status=VacationScheduleAutoPlaceJob.STATUS_FAILED,
                progress_percent=0,
                stage_label="Не найден инициатор",
                error_message="HR-сотрудник, запустивший действие «Добрать незакрытые дни», не найден.",
                finished=True,
            )
            raise CommandError("Actor not found.")

        update_schedule_auto_place_job_progress(
            job_id,
            status=VacationScheduleAutoPlaceJob.STATUS_RUNNING,
            progress_percent=1,
            stage_label="Запуск: добрать незакрытые дни",
            message="Готовлю черновик и список сотрудников.",
            started=True,
        )

        def progress_callback(payload):
            total = max(int(payload.get("total") or 0), 1)
            processed = int(payload.get("processed") or 0)
            percent = 5 + int(min(90, (processed / total) * 90))
            employee_name = payload.get("employee_name") or "сотрудник"
            stage_label = payload.get("stage_label") or f"Добрать незакрытые дни: {processed} из {total}"
            message = payload.get("message") or f"Подбираю лучший пакет для: {employee_name}."
            update_schedule_auto_place_job_progress(
                job_id,
                status=VacationScheduleAutoPlaceJob.STATUS_RUNNING,
                progress_percent=percent,
                stage_label=stage_label,
                message=message,
                placed_count=payload.get("placed_count"),
                unresolved_count=payload.get("unresolved_count"),
                processed_employees=processed,
                total_employees=total,
            )

        try:
            result = auto_place_remaining_schedule_draft(
                year=year,
                actor=actor,
                progress_callback=progress_callback,
                use_package_selection=True,
            )
        except ValidationError as exc:
            message = "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)
            update_schedule_auto_place_job_progress(
                job_id,
                status=VacationScheduleAutoPlaceJob.STATUS_FAILED,
                progress_percent=100,
                stage_label="Добрать незакрытые дни не выполнено",
                error_message=message,
                finished=True,
            )
            raise CommandError(message) from exc
        except Exception as exc:
            update_schedule_auto_place_job_progress(
                job_id,
                status=VacationScheduleAutoPlaceJob.STATUS_FAILED,
                progress_percent=100,
                stage_label="Ошибка: добрать незакрытые дни",
                error_message=str(exc),
                finished=True,
            )
            raise

        update_schedule_auto_place_job_progress(
            job_id,
            status=VacationScheduleAutoPlaceJob.STATUS_SUCCEEDED,
            progress_percent=100,
            stage_label="Добрать незакрытые дни завершено",
            message=(
                f"Добавлено {result['placed_count']} пунктов. "
                f"Осталось вручную: {result['unresolved_count']}."
            ),
            placed_count=result["placed_count"],
            unresolved_count=result["unresolved_count"],
            finished=True,
        )
        self.stdout.write(self.style.SUCCESS("Schedule draft auto placement completed."))
