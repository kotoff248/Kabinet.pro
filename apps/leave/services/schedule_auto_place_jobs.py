import os
import secrets
import subprocess
import sys

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.urls import reverse
from django.utils import timezone

from apps.leave.models import VacationSchedule, VacationScheduleAutoPlaceJob


AUTO_PLACE_JOB_TOKEN_BYTES = 32
ACTIVE_AUTO_PLACE_JOB_STATUSES = (
    VacationScheduleAutoPlaceJob.STATUS_QUEUED,
    VacationScheduleAutoPlaceJob.STATUS_RUNNING,
)


def create_schedule_auto_place_job(*, year, actor):
    schedule = VacationSchedule.objects.filter(year=year, status=VacationSchedule.STATUS_DRAFT).first()
    if schedule is None:
        raise ValidationError("Черновик графика за этот год не найден.")
    return VacationScheduleAutoPlaceJob.objects.create(
        token=secrets.token_urlsafe(AUTO_PLACE_JOB_TOKEN_BYTES),
        year=year,
        schedule=schedule,
        actor=actor,
        progress_percent=0,
        stage_label="Ожидает запуска",
        message="Подготовка действия «Добрать незакрытые дни».",
    )


def get_or_create_schedule_auto_place_job(*, year, actor):
    schedule = VacationSchedule.objects.filter(year=year, status=VacationSchedule.STATUS_DRAFT).first()
    if schedule is None:
        raise ValidationError("Черновик графика за этот год не найден.")

    active_job = (
        VacationScheduleAutoPlaceJob.objects.filter(
            year=year,
            schedule=schedule,
            status__in=ACTIVE_AUTO_PLACE_JOB_STATUSES,
        )
        .order_by("-created_at", "-id")
        .first()
    )
    if active_job is not None:
        return active_job, False

    return create_schedule_auto_place_job(year=year, actor=actor), True


def start_schedule_auto_place_process(job):
    if job.status in ACTIVE_AUTO_PLACE_JOB_STATUSES and job.process_id:
        return None

    command = [
        sys.executable,
        str(settings.BASE_DIR / "manage.py"),
        "run_schedule_draft_auto_place",
        "--job-id",
        str(job.id),
        "--year",
        str(job.year),
        "--actor-id",
        str(job.actor_id or 0),
    ]
    creationflags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW

    try:
        process = subprocess.Popen(
            command,
            cwd=str(settings.BASE_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=False if os.name == "nt" else True,
            creationflags=creationflags,
        )
    except Exception as exc:
        update_schedule_auto_place_job_progress(
            job.id,
            status=VacationScheduleAutoPlaceJob.STATUS_FAILED,
            progress_percent=0,
            stage_label="Не удалось запустить добор",
            error_message=str(exc),
            finished=True,
        )
        raise

    VacationScheduleAutoPlaceJob.objects.filter(id=job.id).update(process_id=process.pid, updated_at=timezone.now())
    job.process_id = process.pid
    return process


def update_schedule_auto_place_job_progress(
    job_id,
    *,
    status=None,
    progress_percent=None,
    stage_label=None,
    message=None,
    error_message=None,
    placed_count=None,
    unresolved_count=None,
    processed_employees=None,
    total_employees=None,
    process_id=None,
    started=False,
    finished=False,
):
    now = timezone.now()
    updates = ["updated_at = %s"]
    params = [now]

    if status is not None:
        updates.append("status = %s")
        params.append(status)
    if progress_percent is not None:
        updates.append("progress_percent = %s")
        params.append(max(0, min(100, int(progress_percent))))
    if stage_label is not None:
        updates.append("stage_label = %s")
        params.append(stage_label)
    if message is not None:
        updates.append("message = %s")
        params.append(message)
    if error_message is not None:
        updates.append("error_message = %s")
        params.append(error_message)
    if placed_count is not None:
        updates.append("placed_count = %s")
        params.append(max(0, int(placed_count)))
    if unresolved_count is not None:
        updates.append("unresolved_count = %s")
        params.append(max(0, int(unresolved_count)))
    if processed_employees is not None:
        updates.append("processed_employees = %s")
        params.append(max(0, int(processed_employees)))
    if total_employees is not None:
        updates.append("total_employees = %s")
        params.append(max(0, int(total_employees)))
    if process_id is not None:
        updates.append("process_id = %s")
        params.append(process_id)
    if started:
        updates.append("started_at = COALESCE(started_at, %s)")
        params.append(now)
    if finished:
        updates.append("finished_at = %s")
        params.append(now)

    params.append(job_id)
    sql = f"UPDATE {VacationScheduleAutoPlaceJob._meta.db_table} SET {', '.join(updates)} WHERE id = %s"

    if connection.in_atomic_block:
        progress_connection = connection.copy()
        try:
            progress_connection.set_autocommit(True)
            with progress_connection.cursor() as cursor:
                cursor.execute(sql, params)
                updated_rows = cursor.rowcount
        finally:
            progress_connection.close()
        if updated_rows:
            return
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
        return

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(sql, params)


def schedule_auto_place_job_payload(job):
    return {
        "ok": True,
        "job_id": job.id,
        "status": job.status,
        "year": job.year,
        "progress_percent": int(job.progress_percent or 0),
        "stage_label": job.stage_label,
        "message": job.message,
        "error_message": job.error_message,
        "placed_count": int(job.placed_count or 0),
        "unresolved_count": int(job.unresolved_count or 0),
        "processed_employees": int(job.processed_employees or 0),
        "total_employees": int(job.total_employees or 0),
        "process_id": job.process_id,
        "created_at": job.created_at.isoformat() if job.created_at else "",
        "updated_at": job.updated_at.isoformat() if job.updated_at else "",
        "started_at": job.started_at.isoformat() if job.started_at else "",
        "finished_at": job.finished_at.isoformat() if job.finished_at else "",
        "detail_url": reverse("schedule_draft_detail", args=[job.year]),
    }
