import hashlib
import json
import math
import random
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from apps.leave.models import (
    VacationSchedule,
    VacationScheduleCandidateFeedback,
    VacationScheduleCandidatePackage,
)

from .package_runtime import (
    NEURAL_PACKAGE_RANKER_KIND,
    build_package_ranker_inputs,
    package_model_filename,
)
from .training_sources import build_training_source_summary
from .package_scoring import build_record_package_features


PACKAGE_TARGET_HEADS = ("score", "confidence", "prefer", "avoid")
PACKAGE_HIDDEN_NODE_NAMES = (
    "coverage_quality",
    "period_shape",
    "preference_alignment",
    "staffing_safety",
    "risk_control",
    "deadline_closure",
    "source_fit",
    "package_prior",
)


class PackageTrainingError(Exception):
    pass


class PackageTrainingDataError(PackageTrainingError):
    pass


class PackageTrainingDependencyError(PackageTrainingError):
    pass


@dataclass(frozen=True)
class PackageTrainingExample:
    package_id: int
    employee_id: int
    year: int
    decision: str
    label_bucket: str
    inputs: dict
    targets: dict
    feedback_decisions: tuple


@dataclass(frozen=True)
class PackageTrainingDataset:
    examples: list[PackageTrainingExample]
    input_names: tuple[str, ...]
    class_balance: dict

    @property
    def years(self):
        return sorted({example.year for example in self.examples})


@dataclass(frozen=True)
class PackageTrainingResult:
    model_path: Path
    metrics_path: Path
    examples_count: int
    class_balance: dict
    metrics: dict
    model_artifact: dict
    metrics_artifact: dict


def collect_package_training_dataset(*, current_year=None, max_schedule_year=None):
    max_schedule_year = int(max_schedule_year or current_year or timezone.localdate().year)
    queryset = (
        VacationScheduleCandidatePackage.objects.select_related("schedule", "employee")
        .prefetch_related("periods__candidate__feedback_entries")
        .filter(
            schedule__year__lte=max_schedule_year,
            schedule__status__in=[
                VacationSchedule.STATUS_ARCHIVED,
                VacationSchedule.STATUS_APPROVED,
            ],
            decision__in=[
                VacationScheduleCandidatePackage.DECISION_SELECTED,
                VacationScheduleCandidatePackage.DECISION_REJECTED,
                VacationScheduleCandidatePackage.DECISION_BLOCKED,
            ],
        )
        .order_by("schedule__year", "employee_id", "id")
    )

    examples = []
    input_names = None
    for package in queryset:
        if package.total_chargeable_days <= 0 and package.decision != VacationScheduleCandidatePackage.DECISION_BLOCKED:
            continue

        features = build_record_package_features(package)
        if int(features.get("feature_schema_version") or 0) != 1:
            continue

        label_bucket, targets = build_package_training_targets(package, features=features)
        inputs = build_package_ranker_inputs(features, passed_hard_rules=bool(package.passed_hard_rules))
        if input_names is None:
            input_names = tuple(sorted(inputs))

        examples.append(
            PackageTrainingExample(
                package_id=package.id,
                employee_id=package.employee_id,
                year=package.schedule.year,
                decision=package.decision,
                label_bucket=label_bucket,
                inputs={name: float(inputs.get(name, 0.0)) for name in input_names},
                targets=targets,
                feedback_decisions=_package_feedback_decisions(package),
            )
        )

    return PackageTrainingDataset(
        examples=examples,
        input_names=input_names or tuple(),
        class_balance=dict(Counter(example.label_bucket for example in examples)),
    )


def build_package_training_targets(package, *, features=None):
    features = features or build_record_package_features(package)
    quality_score = _package_quality_target(features)
    feedback_decisions = set(_package_feedback_decisions(package))

    if package.decision == VacationScheduleCandidatePackage.DECISION_BLOCKED or not package.passed_hard_rules:
        return "blocked", _target(score=0.00, confidence=0.96, prefer=0.00, avoid=1.00)

    if package.decision == VacationScheduleCandidatePackage.DECISION_REJECTED:
        score = _clamp(quality_score - 0.26, 0.18, 0.60)
        if features.get("has_preference_period"):
            score = _clamp(score + 0.08, 0.24, 0.66)
            return "rejected_preference_package", _target(
                score=score,
                confidence=0.74,
                prefer=_clamp(score - 0.04, 0.12, 0.62),
                avoid=0.70,
            )
        return "rejected_package", _target(
            score=score,
            confidence=0.76,
            prefer=max(score - 0.15, 0.06),
            avoid=0.82,
        )

    if package.decision != VacationScheduleCandidatePackage.DECISION_SELECTED:
        score = _clamp(quality_score - 0.18, 0.24, 0.58)
        return "ignored_package", _target(score=score, confidence=0.55, prefer=max(score - 0.18, 0.10), avoid=0.58)

    if VacationScheduleCandidateFeedback.DECISION_REJECT in feedback_decisions:
        score = _clamp(quality_score - 0.34, 0.18, 0.48)
        return "selected_package_reject", _target(score=score, confidence=0.84, prefer=max(score - 0.22, 0.04), avoid=0.86)

    if VacationScheduleCandidateFeedback.DECISION_NEEDS_CHANGE in feedback_decisions:
        score = _clamp(quality_score - 0.17, 0.43, 0.68)
        return "selected_package_needs_change", _target(
            score=score,
            confidence=0.74,
            prefer=max(score - 0.10, 0.25),
            avoid=0.52,
        )

    score = _clamp(quality_score, 0.58, 0.94)
    return "selected_package_agree", _target(
        score=score,
        confidence=0.90,
        prefer=_clamp(score + 0.06, 0.60, 0.97),
        avoid=0.05,
    )


def train_package_ranker_model(
    *,
    output_version="vacation-package-ranker-v3",
    output_dir=None,
    epochs=250,
    lr=0.01,
    seed=42,
    min_examples=20,
    current_year=None,
    max_schedule_year=None,
):
    max_schedule_year = int(max_schedule_year or current_year or timezone.localdate().year)
    dataset = collect_package_training_dataset(max_schedule_year=max_schedule_year)
    if not dataset.examples:
        raise PackageTrainingDataError(
            "Исторические пакеты кандидатов не найдены. Сначала запустите seed_vacation_requests "
            "--confirm-reset, а потом повторите обучение."
        )
    if len(dataset.examples) < int(min_examples):
        raise PackageTrainingDataError(
            f"Недостаточно исторических пакетов для обучения: {len(dataset.examples)} из {int(min_examples)}. "
            "Добавьте seed-историю или снизьте --min-examples."
        )

    torch = _import_torch()
    _seed_training(torch, int(seed))

    split = split_package_training_examples(dataset.examples, seed=seed)
    model, training_loss = _train_torch_model(
        torch,
        split["train"],
        input_names=dataset.input_names,
        epochs=int(epochs),
        lr=float(lr),
    )
    metrics = {
        name: _evaluate_torch_model(torch, model, examples, input_names=dataset.input_names)
        for name, examples in split.items()
    }
    metrics["training_loss"] = training_loss

    model_artifact = export_package_torch_model_to_json(
        model,
        input_names=dataset.input_names,
        output_version=output_version,
        examples_count=len(dataset.examples),
        class_balance=dataset.class_balance,
    )
    source_summary = build_training_source_summary(max_schedule_year=max_schedule_year)
    metrics_artifact = {
        "version": output_version,
        "kind": NEURAL_PACKAGE_RANKER_KIND,
        "feature_schema_version": 1,
        "trained_at": timezone.now().isoformat(),
        "examples_count": len(dataset.examples),
        "class_balance": dataset.class_balance,
        "years": dataset.years,
        "source_schedule_ids": source_summary["schedule_ids"],
        "source_fingerprint": source_summary["source_fingerprint"],
        "split_counts": {name: len(examples) for name, examples in split.items()},
        "input_names": list(dataset.input_names),
        "target_heads": list(PACKAGE_TARGET_HEADS),
        "training": {
            "epochs": int(epochs),
            "lr": float(lr),
            "seed": int(seed),
            "min_examples": int(min_examples),
        },
        "metrics": metrics,
    }

    output_dir = Path(
        output_dir
        or getattr(
            settings,
            "VACATION_PACKAGE_MODEL_DIR",
            getattr(settings, "VACATION_CANDIDATE_MODEL_DIR"),
        )
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    model_path = output_dir / package_model_filename(output_version)
    metrics_path = output_dir / package_model_filename(f"{output_version}-metrics")
    _write_json_atomic(model_path, model_artifact)
    _write_json_atomic(metrics_path, metrics_artifact)

    return PackageTrainingResult(
        model_path=model_path,
        metrics_path=metrics_path,
        examples_count=len(dataset.examples),
        class_balance=dataset.class_balance,
        metrics=metrics,
        model_artifact=model_artifact,
        metrics_artifact=metrics_artifact,
    )


def split_package_training_examples(examples, *, seed=42):
    groups = {"train": [], "val": [], "test": []}
    for example in examples:
        key = f"{seed}:{example.year}:{example.employee_id}:{example.package_id}"
        bucket = int(hashlib.sha1(key.encode("utf-8")).hexdigest(), 16) % 100
        if bucket < 70:
            groups["train"].append(example)
        elif bucket < 85:
            groups["val"].append(example)
        else:
            groups["test"].append(example)

    if len(examples) >= 3:
        for name in ("train", "val", "test"):
            if not groups[name]:
                _move_one_example_to_empty_split(groups, name)

    return groups


def export_package_torch_model_to_json(model, *, input_names, output_version, examples_count, class_balance):
    hidden_weight = model.hidden.weight.detach().cpu().tolist()
    hidden_bias = model.hidden.bias.detach().cpu().tolist()
    output_weight = model.output.weight.detach().cpu().tolist()
    output_bias = model.output.bias.detach().cpu().tolist()

    hidden_layer = []
    for hidden_index, node_name in enumerate(PACKAGE_HIDDEN_NODE_NAMES):
        hidden_layer.append(
            {
                "name": node_name,
                "bias": _round_weight(hidden_bias[hidden_index]),
                "weights": {
                    input_name: _round_weight(hidden_weight[hidden_index][input_index])
                    for input_index, input_name in enumerate(input_names)
                },
            }
        )

    heads = {}
    for head_index, head_name in enumerate(PACKAGE_TARGET_HEADS):
        heads[head_name] = {
            "bias": _round_weight(output_bias[head_index]),
            "weights": {
                hidden_name: _round_weight(output_weight[head_index][hidden_index])
                for hidden_index, hidden_name in enumerate(PACKAGE_HIDDEN_NODE_NAMES)
            },
        }

    return {
        "version": output_version,
        "kind": NEURAL_PACKAGE_RANKER_KIND,
        "feature_schema_version": 1,
        "description": "Trained tabular MLP ranker for full vacation schedule packages.",
        "hidden_activation": "tanh",
        "output_activation": "sigmoid",
        "input_names": list(input_names),
        "target_heads": list(PACKAGE_TARGET_HEADS),
        "training_summary": {
            "examples_count": int(examples_count),
            "class_balance": class_balance,
        },
        "hidden_layer": hidden_layer,
        "heads": heads,
    }


def _target(*, score, confidence, prefer, avoid):
    return {
        "score": float(score),
        "confidence": float(confidence),
        "prefer": float(prefer),
        "avoid": float(avoid),
    }


def _package_quality_target(features):
    score = 0.64
    score += _clamp(_float_feature(features, "package_coverage_ratio"), 0.0, 1.0) * 0.10
    score += _clamp(_float_feature(features, "avg_period_score") / 100.0, 0.0, 1.0) * 0.16
    score += _clamp(_float_feature(features, "min_period_score") / 100.0, 0.0, 1.0) * 0.14

    if features.get("package_closes_need"):
        score += 0.07
    if features.get("has_primary_preference_period"):
        score += 0.06
    elif features.get("has_backup_preference_period"):
        score += 0.04
    elif features.get("has_preference_period"):
        score += 0.02
    if features.get("has_required_continuous_part"):
        score += 0.04
    if _float_feature(features, "periods_count", 1) == 1:
        score += 0.03

    score -= _clamp(_float_feature(features, "max_risk_score") / 100.0, 0.0, 1.0) * 0.18
    score -= _clamp(_float_feature(features, "short_periods_count") / 3.0, 0.0, 1.0) * 0.05
    if _float_feature(features, "min_staff_margin") <= 0:
        score -= 0.12
    if features.get("has_risk_conflict"):
        score -= 0.36
    return _clamp(score, 0.0, 1.0)


def _package_feedback_decisions(package):
    decisions = set()
    for period in list(package.periods.all()):
        candidate = getattr(period, "candidate", None)
        if candidate is None:
            continue
        for feedback in list(candidate.feedback_entries.all()):
            if feedback.decision:
                decisions.add(feedback.decision)
    return tuple(sorted(decisions))


def _float_feature(features, key, default=0.0):
    try:
        return float(features.get(key, default) or default)
    except (TypeError, ValueError):
        return float(default)


def _clamp(value, lower=0.0, upper=1.0):
    return max(lower, min(upper, float(value)))


def _import_torch():
    try:
        import torch
    except ImportError as exc:
        raise PackageTrainingDependencyError(
            "PyTorch не установлен. Установите зависимости командой "
            ".\\.venv\\Scripts\\python.exe -m pip install -r requirements.txt "
            "и повторите обучение."
        ) from exc
    return torch


def _seed_training(torch, seed):
    random.seed(seed)
    torch.manual_seed(seed)
    if hasattr(torch, "set_num_threads"):
        torch.set_num_threads(1)


def _train_torch_model(torch, train_examples, *, input_names, epochs, lr):
    if not train_examples:
        raise PackageTrainingDataError("В train split не попало ни одного примера.")

    model = _build_torch_model(torch, input_dim=len(input_names), hidden_dim=len(PACKAGE_HIDDEN_NODE_NAMES))
    optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.0005)
    loss_fn = torch.nn.BCEWithLogitsLoss(reduction="none")
    inputs = _examples_tensor(torch, train_examples, input_names=input_names)
    targets = _targets_tensor(torch, train_examples)
    sample_weights = _sample_weights_tensor(torch, train_examples)

    losses = []
    for _ in range(max(int(epochs), 1)):
        optimizer.zero_grad()
        logits = model(inputs)
        raw_loss = loss_fn(logits, targets).mean(dim=1)
        loss = (raw_loss * sample_weights).sum() / sample_weights.sum()
        loss.backward()
        optimizer.step()
        losses.append(float(loss.detach().cpu().item()))

    return model, {
        "first": _round_metric(losses[0]),
        "last": _round_metric(losses[-1]),
        "best": _round_metric(min(losses)),
    }


def _build_torch_model(torch, *, input_dim, hidden_dim):
    class PackageMLP(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.hidden = torch.nn.Linear(input_dim, hidden_dim)
            self.output = torch.nn.Linear(hidden_dim, len(PACKAGE_TARGET_HEADS))

        def forward(self, inputs):
            return self.output(torch.tanh(self.hidden(inputs)))

    return PackageMLP()


def _evaluate_torch_model(torch, model, examples, *, input_names):
    if not examples:
        return {
            "count": 0,
            "score_mae": None,
            "score_rmse": None,
            "score_accuracy_0_5": None,
            "prefer_accuracy": None,
            "avoid_accuracy": None,
        }

    with torch.no_grad():
        logits = model(_examples_tensor(torch, examples, input_names=input_names))
        predictions = torch.sigmoid(logits).detach().cpu().tolist()

    target_rows = [[float(example.targets[head]) for head in PACKAGE_TARGET_HEADS] for example in examples]
    score_errors = [abs(prediction[0] - target[0]) for prediction, target in zip(predictions, target_rows)]
    score_sq_errors = [(prediction[0] - target[0]) ** 2 for prediction, target in zip(predictions, target_rows)]
    score_accuracy = [
        int((prediction[0] >= 0.5) == (target[0] >= 0.5))
        for prediction, target in zip(predictions, target_rows)
    ]
    prefer_accuracy = [
        int((prediction[2] >= 0.5) == (target[2] >= 0.5))
        for prediction, target in zip(predictions, target_rows)
    ]
    avoid_accuracy = [
        int((prediction[3] >= 0.5) == (target[3] >= 0.5))
        for prediction, target in zip(predictions, target_rows)
    ]

    return {
        "count": len(examples),
        "score_mae": _round_metric(sum(score_errors) / len(score_errors)),
        "score_rmse": _round_metric(math.sqrt(sum(score_sq_errors) / len(score_sq_errors))),
        "score_accuracy_0_5": _round_metric(sum(score_accuracy) / len(score_accuracy)),
        "prefer_accuracy": _round_metric(sum(prefer_accuracy) / len(prefer_accuracy)),
        "avoid_accuracy": _round_metric(sum(avoid_accuracy) / len(avoid_accuracy)),
    }


def _examples_tensor(torch, examples, *, input_names):
    return torch.tensor(
        [[float(example.inputs.get(name, 0.0)) for name in input_names] for example in examples],
        dtype=torch.float32,
    )


def _targets_tensor(torch, examples):
    return torch.tensor(
        [[float(example.targets[head]) for head in PACKAGE_TARGET_HEADS] for example in examples],
        dtype=torch.float32,
    )


def _sample_weights_tensor(torch, examples):
    weights = {
        "blocked": 0.45,
        "rejected_package": 1.35,
        "rejected_preference_package": 2.50,
        "ignored_package": 1.00,
        "selected_package_agree": 2.80,
        "selected_package_needs_change": 3.20,
        "selected_package_reject": 3.10,
    }
    return torch.tensor(
        [float(weights.get(example.label_bucket, 1.0)) for example in examples],
        dtype=torch.float32,
    )


def _move_one_example_to_empty_split(groups, target_name):
    donor_name = max(
        (name for name in groups if name != target_name),
        key=lambda name: len(groups[name]),
    )
    if len(groups[donor_name]) > 1:
        groups[target_name].append(groups[donor_name].pop(0))


def _round_weight(value):
    return round(float(value), 8)


def _round_metric(value):
    return round(float(value), 6)


def _json_dumps(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _write_json_atomic(path, payload):
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp_path.write_text(_json_dumps(payload), encoding="utf-8")
    try:
        tmp_path.replace(path)
    except PermissionError:
        try:
            if path.exists():
                path.unlink()
            tmp_path.replace(path)
        except PermissionError:
            path.write_text(_json_dumps(payload), encoding="utf-8")
            try:
                tmp_path.unlink(missing_ok=True)
            except PermissionError:
                pass
