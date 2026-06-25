import os
import secrets
import subprocess
import sys

from django.conf import settings
from django.db import connection, transaction
from django.urls import reverse
from django.utils import timezone

from apps.core.models import DemoDataResetJob
from apps.core.services.demo_locks import try_demo_data_mutation_lock


RESET_JOB_TOKEN_BYTES = 32
DEMO_SEED_PRESET_STANDARD = "standard"
DEMO_SEED_PRESET_FAST = "fast"
DEMO_SEED_PRESET_FULL = "full"
DEMO_RESET_PRESETS = {
    DEMO_SEED_PRESET_STANDARD: {
        "label": "Обычная демо-база",
        "history_years": 2,
        "employee_count": 50,
    },
    DEMO_SEED_PRESET_FAST: {
        "label": "Быстрая демо-база",
        "history_years": 2,
        "employee_count": 25,
    },
    DEMO_SEED_PRESET_FULL: {
        "label": "Полная исследовательская база",
        "history_years": 5,
        "employee_count": 100,
    },
}
ACTIVE_DEMO_RESET_JOB_STATUSES = (
    DemoDataResetJob.STATUS_QUEUED,
    DemoDataResetJob.STATUS_RUNNING,
)


class DemoDataResetInProgressError(Exception):
    pass


def _demo_data_reset_preset_meta(preset):
    preset = preset if preset in DEMO_RESET_PRESETS else DEMO_SEED_PRESET_STANDARD
    spec = DEMO_RESET_PRESETS[preset]
    return {
        "preset": preset,
        "label": spec["label"],
        "history_years": int(spec["history_years"]),
        "calendar_years": int(spec["history_years"]) + 1,
        "employee_count": int(spec["employee_count"]),
    }


def demo_data_reset_preset_options():
    return [
        _demo_data_reset_preset_meta(DEMO_SEED_PRESET_STANDARD),
        _demo_data_reset_preset_meta(DEMO_SEED_PRESET_FAST),
    ]


def normalize_demo_data_reset_preset(value):
    if value == DEMO_SEED_PRESET_FAST:
        return DEMO_SEED_PRESET_FAST
    if value == DEMO_SEED_PRESET_FULL:
        return DEMO_SEED_PRESET_FULL
    return DEMO_SEED_PRESET_STANDARD


def create_demo_data_reset_job(*, seed_value, preset=DEMO_SEED_PRESET_STANDARD):
    meta = _demo_data_reset_preset_meta(preset)
    return DemoDataResetJob.objects.create(
        token=secrets.token_urlsafe(RESET_JOB_TOKEN_BYTES),
        seed_value=seed_value,
        preset=meta["preset"],
        history_years=meta["history_years"],
        employee_count=meta["employee_count"],
        progress_percent=0,
        stage_label="Ожидает запуска",
        message=f"Подготовка фонового пересоздания: {meta['label']}.",
    )


@transaction.atomic
def get_or_create_demo_data_reset_job(*, seed_value, preset=DEMO_SEED_PRESET_STANDARD):
    if not try_demo_data_mutation_lock():
        raise DemoDataResetInProgressError

    active_job = (
        DemoDataResetJob.objects.filter(status__in=ACTIVE_DEMO_RESET_JOB_STATUSES)
        .order_by("-created_at", "-id")
        .first()
    )
    if active_job is not None:
        return active_job, False

    return create_demo_data_reset_job(seed_value=seed_value, preset=preset), True


def start_demo_data_reset_process(job):
    if job.status in ACTIVE_DEMO_RESET_JOB_STATUSES and job.process_id:
        return None

    command = [
        sys.executable,
        str(settings.BASE_DIR / "manage.py"),
        "seed_vacation_requests",
        "--confirm-reset",
        "--seed-value",
        str(job.seed_value),
        "--preset",
        job.preset,
        "--history-years",
        str(job.history_years),
        "--progress-job-id",
        str(job.id),
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
        update_demo_data_reset_job_progress(
            job.id,
            status=DemoDataResetJob.STATUS_FAILED,
            progress_percent=0,
            stage_label="Не удалось запустить seed",
            error_message=str(exc),
            finished=True,
        )
        raise

    DemoDataResetJob.objects.filter(id=job.id).update(process_id=process.pid, updated_at=timezone.now())
    job.process_id = process.pid
    return process


def update_demo_data_reset_job_progress(
    job_id,
    *,
    status=None,
    progress_percent=None,
    stage_label=None,
    message=None,
    error_message=None,
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
    sql = f"UPDATE {DemoDataResetJob._meta.db_table} SET {', '.join(updates)} WHERE id = %s"

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

        # In Django TestCase the job row can still be inside the current
        # uncommitted test transaction, so a separate connection cannot see it.
        # The real background process uses a committed row and keeps the live
        # progress path above.
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
        return

    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(sql, params)


def _job_timing_payload(job):
    now = timezone.now()
    started_at = job.started_at
    finished_at = job.finished_at
    elapsed_seconds = None
    estimated_total_seconds = None
    estimated_remaining_seconds = None

    if started_at:
        end_at = finished_at or now
        elapsed_seconds = max(0, int((end_at - started_at).total_seconds()))
        progress = int(job.progress_percent or 0)
        if job.status == DemoDataResetJob.STATUS_RUNNING and 1 <= progress < 100:
            estimated_total_seconds = max(elapsed_seconds, int(elapsed_seconds * 100 / progress))
            estimated_remaining_seconds = max(0, estimated_total_seconds - elapsed_seconds)
        elif job.status == DemoDataResetJob.STATUS_SUCCEEDED:
            estimated_total_seconds = elapsed_seconds
            estimated_remaining_seconds = 0

    return {
        "elapsed_seconds": elapsed_seconds,
        "estimated_total_seconds": estimated_total_seconds,
        "estimated_remaining_seconds": estimated_remaining_seconds,
    }


def demo_data_reset_job_payload(job):
    preset_meta = _demo_data_reset_preset_meta(job.preset)
    status_url = f"{reverse('reset_demo_data_status', args=[job.id])}?token={job.token}"
    return {
        "ok": True,
        "job_id": job.id,
        "token": job.token,
        "status_url": status_url,
        "status": job.status,
        "seed_value": job.seed_value,
        "preset": job.preset,
        "preset_label": preset_meta["label"],
        "history_years": int(job.history_years or preset_meta["history_years"]),
        "calendar_years": int(job.history_years or preset_meta["history_years"]) + 1,
        "employee_count": int(job.employee_count or preset_meta["employee_count"]),
        "progress_percent": int(job.progress_percent or 0),
        "stage_label": job.stage_label,
        "message": job.message,
        "error_message": job.error_message,
        "process_id": job.process_id,
        "created_at": job.created_at.isoformat() if job.created_at else "",
        "updated_at": job.updated_at.isoformat() if job.updated_at else "",
        "started_at": job.started_at.isoformat() if job.started_at else "",
        "finished_at": job.finished_at.isoformat() if job.finished_at else "",
        "login_url": reverse("login"),
        **_job_timing_payload(job),
    }
