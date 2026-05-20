from pathlib import Path

from django.core.exceptions import ValidationError

from apps.leave.ml.package_runtime import load_package_ranker_model, reset_package_ranker_model_cache
from apps.leave.ml.package_training import (
    PackageTrainingDataError,
    PackageTrainingDependencyError,
    train_package_ranker_model,
)
from apps.leave.ml.runtime import load_candidate_mlp_model, reset_candidate_mlp_model_cache
from apps.leave.ml.training import (
    CandidateTrainingDataError,
    CandidateTrainingDependencyError,
    train_candidate_mlp_model,
)
from apps.leave.ml.training_sources import (
    build_training_source_summary,
    ensure_approved_schedule_training_feedback,
)
from apps.leave.models import VacationNeuralTrainingJob, VacationSchedule
from apps.leave.services.neural_training_jobs import update_neural_training_job_progress


class VacationNeuralRetrainingError(Exception):
    pass


def _update_job(job_id, **kwargs):
    if job_id:
        update_neural_training_job_progress(job_id, **kwargs)


def _result_payload(candidate_result, package_result, source_summary, *, feedback_created):
    return {
        "source": source_summary,
        "feedback_created": feedback_created,
        "candidate": {
            "version": candidate_result.model_artifact.get("version"),
            "examples_count": candidate_result.examples_count,
            "class_balance": candidate_result.class_balance,
            "years": candidate_result.metrics_artifact.get("years", []),
            "metrics": candidate_result.metrics,
            "model_path": str(candidate_result.model_path),
            "metrics_path": str(candidate_result.metrics_path),
        },
        "package": {
            "version": package_result.model_artifact.get("version"),
            "examples_count": package_result.examples_count,
            "class_balance": package_result.class_balance,
            "years": package_result.metrics_artifact.get("years", []),
            "metrics": package_result.metrics,
            "model_path": str(package_result.model_path),
            "metrics_path": str(package_result.metrics_path),
        },
    }


def train_vacation_neural_models_for_year(
    *,
    year,
    job_id=None,
    candidate_version="vacation-candidate-mlp-v2",
    package_version="vacation-package-ranker-v3",
    candidate_epochs=250,
    package_epochs=250,
    candidate_lr=0.01,
    package_lr=0.01,
    seed=42,
    candidate_min_examples=30,
    package_min_examples=20,
    output_dir=None,
):
    try:
        schedule = VacationSchedule.objects.filter(year=year).first()
        if schedule is None or schedule.status != VacationSchedule.STATUS_APPROVED:
            raise ValidationError("Сначала утвердите график.")

        _update_job(
            job_id,
            status=VacationNeuralTrainingJob.STATUS_RUNNING,
            progress_percent=5,
            stage_label="Проверяю обучающие данные",
            message="Готовлю утверждённый график как обучающий пример.",
            error_message="",
            started=True,
        )
        feedback_created = ensure_approved_schedule_training_feedback(schedule, actor=schedule.approved_by)
        source_summary = build_training_source_summary(max_schedule_year=year)

        _update_job(
            job_id,
            progress_percent=20,
            stage_label="Обучаю v2",
            message="Обучаю оценку отдельных периодов отпуска.",
            source_fingerprint=source_summary["source_fingerprint"],
        )
        candidate_result = train_candidate_mlp_model(
            output_version=candidate_version,
            output_dir=output_dir,
            epochs=candidate_epochs,
            lr=candidate_lr,
            seed=seed,
            min_examples=candidate_min_examples,
            max_schedule_year=year,
        )

        _update_job(
            job_id,
            progress_percent=62,
            stage_label="Обучаю v3",
            message="Обучаю ранжирование годовых пакетов отпусков.",
        )
        package_result = train_package_ranker_model(
            output_version=package_version,
            output_dir=output_dir,
            epochs=package_epochs,
            lr=package_lr,
            seed=seed,
            min_examples=package_min_examples,
            max_schedule_year=year,
        )

        _update_job(
            job_id,
            progress_percent=88,
            stage_label="Проверяю JSON-модели",
            message="Проверяю, что новые артефакты загружаются runtime-скорингом.",
        )
        reset_candidate_mlp_model_cache()
        reset_package_ranker_model_cache()
        load_candidate_mlp_model(candidate_version, allow_fallback=False)
        load_package_ranker_model(package_version, allow_fallback=False)

        metrics_payload = _result_payload(
            candidate_result,
            package_result,
            source_summary,
            feedback_created=feedback_created,
        )
        _update_job(
            job_id,
            status=VacationNeuralTrainingJob.STATUS_SUCCEEDED,
            progress_percent=100,
            stage_label="Переобучение завершено",
            message="Нейромодуль обучен на исторических годах и утверждённом графике.",
            metrics_payload=metrics_payload,
            source_fingerprint=source_summary["source_fingerprint"],
            finished=True,
        )
        return metrics_payload
    except (
        CandidateTrainingDataError,
        CandidateTrainingDependencyError,
        PackageTrainingDataError,
        PackageTrainingDependencyError,
        OSError,
        ValueError,
        ValidationError,
    ) as exc:
        _update_job(
            job_id,
            status=VacationNeuralTrainingJob.STATUS_FAILED,
            progress_percent=0,
            stage_label="Ошибка обучения",
            error_message=str(exc),
            finished=True,
        )
        raise VacationNeuralRetrainingError(str(exc)) from exc


def training_output_dir(value):
    return Path(value) if value else None
