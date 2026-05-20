import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts.services import is_enterprise_head_employee, is_hr_employee
from apps.leave.ml.package_runtime import package_model_path
from apps.leave.ml.runtime import candidate_model_path
from apps.leave.ml.training_sources import build_training_source_summary
from apps.leave.models import VacationNeuralTrainingJob, VacationSchedule


NEURAL_TRAINING_JOB_TOKEN_BYTES = 32
DEFAULT_TRAINING_CANDIDATE_VERSION = "vacation-candidate-mlp-v2"
DEFAULT_TRAINING_PACKAGE_VERSION = "vacation-package-ranker-v3"
ACTIVE_NEURAL_TRAINING_STATUSES = (
    VacationNeuralTrainingJob.STATUS_QUEUED,
    VacationNeuralTrainingJob.STATUS_RUNNING,
)


def can_manage_neural_training(actor):
    return is_hr_employee(actor) or is_enterprise_head_employee(actor)


def get_active_neural_training_job(*, year):
    return (
        VacationNeuralTrainingJob.objects.filter(year=year, status__in=ACTIVE_NEURAL_TRAINING_STATUSES)
        .order_by("-created_at", "-id")
        .first()
    )


def neural_training_job_status_url(job):
    return f"{reverse('schedule_neural_training_status', args=[job.year, job.id])}?token={job.token}"


def get_neural_training_start_state(year, actor):
    if not can_manage_neural_training(actor):
        return {"can_start": False, "reason": "Переобучать нейромодуль может только HR или руководитель предприятия."}
    schedule = VacationSchedule.objects.filter(year=year).first()
    if schedule is None:
        return {"can_start": False, "reason": "Сначала создайте и утвердите график за этот год."}
    if schedule.status != VacationSchedule.STATUS_APPROVED:
        return {"can_start": False, "reason": "Сначала утвердите график."}
    if get_active_neural_training_job(year=year) is not None:
        return {"can_start": False, "reason": "Переобучение уже выполняется."}
    return {"can_start": True, "reason": "", "schedule": schedule}


def get_or_create_neural_training_job(*, year, actor, candidate_version=None, package_version=None):
    state = get_neural_training_start_state(year, actor)
    active_job = get_active_neural_training_job(year=year)
    if active_job is not None:
        return active_job, False
    if not state.get("can_start"):
        raise ValidationError(state.get("reason") or "Нельзя запустить переобучение нейромодуля.")

    source_summary = build_training_source_summary(max_schedule_year=year)
    defaults = {
        "token": secrets.token_urlsafe(NEURAL_TRAINING_JOB_TOKEN_BYTES),
        "year": year,
        "started_by": actor,
        "status": VacationNeuralTrainingJob.STATUS_QUEUED,
        "progress_percent": 0,
        "stage_label": "Ожидает запуска",
        "message": "Подготовка переобучения нейромодуля.",
        "source_fingerprint": source_summary["source_fingerprint"],
    }
    if candidate_version:
        defaults["candidate_version"] = candidate_version
    if package_version:
        defaults["package_version"] = package_version
    return VacationNeuralTrainingJob.objects.create(**defaults), True


def start_neural_training_process(job):
    if job.status in ACTIVE_NEURAL_TRAINING_STATUSES and job.process_id:
        return None

    command = [
        sys.executable,
        str(settings.BASE_DIR / "manage.py"),
        "train_vacation_neural_models",
        "--year",
        str(job.year),
        "--job-id",
        str(job.id),
        "--candidate-version",
        job.candidate_version,
        "--package-version",
        job.package_version,
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
        update_neural_training_job_progress(
            job.id,
            status=VacationNeuralTrainingJob.STATUS_FAILED,
            progress_percent=0,
            stage_label="Не удалось запустить обучение",
            error_message=str(exc),
            finished=True,
        )
        raise

    VacationNeuralTrainingJob.objects.filter(id=job.id).update(process_id=process.pid, updated_at=timezone.now())
    job.process_id = process.pid
    return process


def update_neural_training_job_progress(
    job_id,
    *,
    status=None,
    progress_percent=None,
    stage_label=None,
    message=None,
    error_message=None,
    metrics_payload=None,
    source_fingerprint=None,
    process_id=None,
    started=False,
    finished=False,
):
    updates = {"updated_at": timezone.now()}
    if status is not None:
        updates["status"] = status
    if progress_percent is not None:
        updates["progress_percent"] = max(0, min(100, int(progress_percent)))
    if stage_label is not None:
        updates["stage_label"] = stage_label
    if message is not None:
        updates["message"] = message
    if error_message is not None:
        updates["error_message"] = error_message
    if metrics_payload is not None:
        updates["metrics_payload"] = metrics_payload
    if source_fingerprint is not None:
        updates["source_fingerprint"] = source_fingerprint
    if process_id is not None:
        updates["process_id"] = process_id
    if started:
        updates["started_at"] = timezone.now()
    if finished:
        updates["finished_at"] = timezone.now()
    VacationNeuralTrainingJob.objects.filter(id=job_id).update(**updates)


def neural_training_job_payload(job):
    return {
        "ok": True,
        "job_id": job.id,
        "status": job.status,
        "year": job.year,
        "progress_percent": int(job.progress_percent or 0),
        "stage_label": job.stage_label,
        "message": job.message,
        "error_message": job.error_message,
        "candidate_version": job.candidate_version,
        "package_version": job.package_version,
        "metrics_payload": job.metrics_payload or {},
        "source_fingerprint": job.source_fingerprint,
        "process_id": job.process_id,
        "created_at": job.created_at.isoformat() if job.created_at else "",
        "updated_at": job.updated_at.isoformat() if job.updated_at else "",
        "started_at": job.started_at.isoformat() if job.started_at else "",
        "finished_at": job.finished_at.isoformat() if job.finished_at else "",
    }


def neural_training_job_page_payload(job):
    payload = neural_training_job_payload(job)
    payload["status_url"] = neural_training_job_status_url(job)
    return payload


def _read_metrics(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _metrics_summary(candidate_version, package_version):
    candidate_metrics = _read_metrics(candidate_model_path(f"{candidate_version}-metrics"))
    package_metrics = _read_metrics(package_model_path(f"{package_version}-metrics"))
    candidate_fingerprint = candidate_metrics.get("source_fingerprint") or ""
    package_fingerprint = package_metrics.get("source_fingerprint") or ""
    return {
        "candidate": candidate_metrics,
        "package": package_metrics,
        "candidate_fingerprint": candidate_fingerprint,
        "package_fingerprint": package_fingerprint,
        "source_fingerprint": candidate_fingerprint if candidate_fingerprint == package_fingerprint else "",
    }


def build_neural_training_context(year, actor, *, schedule=None):
    if not can_manage_neural_training(actor):
        return None

    schedule = schedule or VacationSchedule.objects.filter(year=year).first()
    active_job = get_active_neural_training_job(year=year)
    latest_job = (
        VacationNeuralTrainingJob.objects.filter(year=year)
        .order_by("-created_at", "-id")
        .first()
    )
    candidate_version = getattr(settings, "VACATION_CANDIDATE_SCORER_VERSION", DEFAULT_TRAINING_CANDIDATE_VERSION)
    if candidate_version == "vacation-candidate-mlp-v1":
        candidate_version = DEFAULT_TRAINING_CANDIDATE_VERSION
    package_version = getattr(settings, "VACATION_PACKAGE_RANKER_VERSION", DEFAULT_TRAINING_PACKAGE_VERSION)
    metrics = _metrics_summary(candidate_version, package_version)
    current_source = (
        build_training_source_summary(max_schedule_year=year)
        if schedule is not None and schedule.status == VacationSchedule.STATUS_APPROVED
        else {}
    )
    current_fingerprint = current_source.get("source_fingerprint") or ""
    is_current = bool(current_fingerprint and metrics["source_fingerprint"] == current_fingerprint)
    start_state = get_neural_training_start_state(year, actor)

    status_key = "not_ready"
    status_label = "Сначала утвердите график"
    status_tone = "muted"
    if active_job is not None:
        status_key = "running"
        status_label = active_job.get_status_display()
        status_tone = "info"
    elif schedule is not None and schedule.status == VacationSchedule.STATUS_APPROVED:
        if is_current:
            status_key = "current"
            status_label = "Модель обучена на текущих данных"
            status_tone = "ok"
        else:
            status_key = "needs_retrain"
            status_label = "Нужно переобучить под текущие данные"
            status_tone = "warning"

    return {
        "can_view": True,
        "can_start": start_state.get("can_start", False),
        "block_reason": start_state.get("reason", ""),
        "start_url": reverse("schedule_neural_training_start", args=[year]),
        "status": {
            "key": status_key,
            "label": status_label,
            "tone": status_tone,
        },
        "active_job": neural_training_job_page_payload(active_job) if active_job else None,
        "latest_job": neural_training_job_page_payload(latest_job) if latest_job else None,
        "candidate_version": candidate_version,
        "package_version": package_version,
        "metrics": metrics,
        "current_source": current_source,
        "is_current": is_current,
    }
