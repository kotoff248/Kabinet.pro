import hashlib
import json
import math
import random
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from apps.leave.models import (
    VacationSchedule,
    VacationScheduleCandidate,
    VacationScheduleCandidateFeedback,
    VacationScheduleCandidatePackage,
)

from .candidate_neural import (
    NEURAL_CANDIDATE_SCORER_KIND,
    build_candidate_mlp_inputs,
    candidate_model_filename,
)


TARGET_HEADS = ("score", "confidence", "prefer", "avoid")
HIDDEN_NODE_NAMES = (
    "preference_alignment",
    "coverage_balance",
    "deadline_closure",
    "staffing_safety",
    "department_pressure",
    "calendar_quality",
    "manager_caution",
    "auto_fit",
)


class CandidateTrainingError(Exception):
    pass


class CandidateTrainingDataError(CandidateTrainingError):
    pass


class CandidateTrainingDependencyError(CandidateTrainingError):
    pass


@dataclass(frozen=True)
class CandidateTrainingExample:
    candidate_id: int
    employee_id: int
    year: int
    decision: str
    label_bucket: str
    inputs: dict
    targets: dict
    feedback_decisions: tuple
    package_decisions: tuple


@dataclass(frozen=True)
class CandidateTrainingDataset:
    examples: list[CandidateTrainingExample]
    input_names: tuple[str, ...]
    class_balance: dict

    @property
    def years(self):
        return sorted({example.year for example in self.examples})


@dataclass(frozen=True)
class CandidateTrainingResult:
    model_path: Path
    metrics_path: Path
    examples_count: int
    class_balance: dict
    metrics: dict
    model_artifact: dict
    metrics_artifact: dict


def collect_candidate_training_dataset(*, current_year=None):
    current_year = int(current_year or timezone.localdate().year)
    queryset = (
        VacationScheduleCandidate.objects.select_related("schedule", "employee")
        .prefetch_related("feedback_entries", "package_periods__candidate_package")
        .filter(
            schedule__year__lte=current_year,
            schedule__status__in=[
                VacationSchedule.STATUS_ARCHIVED,
                VacationSchedule.STATUS_APPROVED,
            ],
            decision__in=[
                VacationScheduleCandidate.DECISION_SELECTED,
                VacationScheduleCandidate.DECISION_REJECTED,
                VacationScheduleCandidate.DECISION_BLOCKED,
            ],
        )
        .order_by("schedule__year", "employee_id", "id")
    )

    examples = []
    input_names = None
    for candidate in queryset:
        features = candidate.features if isinstance(candidate.features, dict) else {}
        if int(features.get("feature_schema_version") or 0) != 1:
            continue
        if _candidate_has_no_trainable_period(candidate, features):
            continue

        label_bucket, targets = build_candidate_training_targets(candidate)
        inputs = build_candidate_mlp_inputs(
            features,
            passed_hard_rules=bool(candidate.passed_hard_rules),
        )
        if input_names is None:
            input_names = tuple(sorted(inputs))

        examples.append(
            CandidateTrainingExample(
                candidate_id=candidate.id,
                employee_id=candidate.employee_id,
                year=candidate.schedule.year,
                decision=candidate.decision,
                label_bucket=label_bucket,
                inputs={name: float(inputs.get(name, 0.0)) for name in input_names},
                targets=targets,
                feedback_decisions=_feedback_decisions(candidate),
                package_decisions=_package_decisions(candidate),
            )
        )

    return CandidateTrainingDataset(
        examples=examples,
        input_names=input_names or tuple(),
        class_balance=dict(Counter(example.label_bucket for example in examples)),
    )


def _candidate_has_no_trainable_period(candidate, features):
    chargeable_days = candidate.chargeable_days or _float_feature(features, "period_chargeable_days")
    return float(chargeable_days or 0) <= 0


def build_candidate_training_targets(candidate):
    decision = candidate.decision
    feedback_decisions = set(_feedback_decisions(candidate))
    features = candidate.features if isinstance(candidate.features, dict) else {}
    historical_outcome = features.get("historical_outcome") or ""
    package_decisions = set(_package_decisions(candidate))
    quality_score = _candidate_quality_target(features)

    if (
        decision == VacationScheduleCandidate.DECISION_BLOCKED
        or not candidate.passed_hard_rules
        or VacationScheduleCandidatePackage.DECISION_BLOCKED in package_decisions
    ):
        return "blocked", _target(score=0.00, confidence=0.96, prefer=0.00, avoid=1.00)

    if decision == VacationScheduleCandidate.DECISION_REJECTED:
        if _is_passed_preference_candidate(features):
            score = _clamp(quality_score - 0.03, 0.58, 0.86)
            return "rejected_preference", _target(
                score=score,
                confidence=0.76,
                prefer=_clamp(score + 0.02, 0.45, 0.86),
                avoid=0.30,
            )
        else:
            score = _clamp(quality_score - 0.32, 0.16, 0.56)
            avoid = 0.82
        return "rejected", _target(score=score, confidence=0.76, prefer=max(score - 0.16, 0.08), avoid=avoid)

    if decision != VacationScheduleCandidate.DECISION_SELECTED:
        score = _clamp(quality_score - 0.18, 0.24, 0.58)
        return "ignored", _target(score=score, confidence=0.55, prefer=max(score - 0.18, 0.12), avoid=0.58)

    if (
        VacationScheduleCandidateFeedback.DECISION_REJECT in feedback_decisions
        or historical_outcome == "later_transferred"
    ):
        score = _clamp(quality_score - 0.36, 0.18, 0.46)
        return "selected_reject", _target(score=score, confidence=0.84, prefer=max(score - 0.22, 0.04), avoid=0.86)

    if (
        VacationScheduleCandidateFeedback.DECISION_NEEDS_CHANGE in feedback_decisions
        or historical_outcome == "approved_high_risk"
    ):
        score = _clamp(quality_score - 0.18, 0.42, 0.66)
        return "selected_needs_change", _target(score=score, confidence=0.72, prefer=max(score - 0.10, 0.25), avoid=0.52)

    score = _clamp(quality_score, 0.50, 0.92)
    return "selected_agree", _target(score=score, confidence=0.88, prefer=_clamp(score + 0.06, 0.58, 0.96), avoid=0.05)


def train_candidate_mlp_model(
    *,
    output_version="vacation-candidate-mlp-v2",
    output_dir=None,
    epochs=250,
    lr=0.01,
    seed=42,
    min_examples=30,
    current_year=None,
):
    dataset = collect_candidate_training_dataset(current_year=current_year)
    if not dataset.examples:
        raise CandidateTrainingDataError(
            "Исторические ML-следы не найдены. Сначала запустите "
            "seed_vacation_requests --confirm-reset, а потом повторите обучение."
        )
    if len(dataset.examples) < int(min_examples):
        raise CandidateTrainingDataError(
            f"Недостаточно исторических примеров для обучения: {len(dataset.examples)} "
            f"из {int(min_examples)}. Добавьте seed-историю или снизьте --min-examples."
        )

    torch = _import_torch()
    _seed_training(torch, int(seed))

    split = split_training_examples(dataset.examples, seed=seed)
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

    model_artifact = export_torch_model_to_json(
        model,
        input_names=dataset.input_names,
        output_version=output_version,
        examples_count=len(dataset.examples),
        class_balance=dataset.class_balance,
    )
    metrics_artifact = {
        "version": output_version,
        "kind": NEURAL_CANDIDATE_SCORER_KIND,
        "feature_schema_version": 1,
        "examples_count": len(dataset.examples),
        "class_balance": dataset.class_balance,
        "years": dataset.years,
        "split_counts": {name: len(examples) for name, examples in split.items()},
        "input_names": list(dataset.input_names),
        "target_heads": list(TARGET_HEADS),
        "training": {
            "epochs": int(epochs),
            "lr": float(lr),
            "seed": int(seed),
            "min_examples": int(min_examples),
        },
        "metrics": metrics,
    }

    output_dir = Path(output_dir or getattr(settings, "VACATION_CANDIDATE_MODEL_DIR"))
    output_dir.mkdir(parents=True, exist_ok=True)
    model_path = output_dir / candidate_model_filename(output_version)
    metrics_path = output_dir / candidate_model_filename(f"{output_version}-metrics")
    model_path.write_text(_json_dumps(model_artifact), encoding="utf-8")
    metrics_path.write_text(_json_dumps(metrics_artifact), encoding="utf-8")

    return CandidateTrainingResult(
        model_path=model_path,
        metrics_path=metrics_path,
        examples_count=len(dataset.examples),
        class_balance=dataset.class_balance,
        metrics=metrics,
        model_artifact=model_artifact,
        metrics_artifact=metrics_artifact,
    )


def split_training_examples(examples, *, seed=42):
    groups = {"train": [], "val": [], "test": []}
    for example in examples:
        key = f"{seed}:{example.year}:{example.employee_id}:{example.candidate_id}"
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


def export_torch_model_to_json(model, *, input_names, output_version, examples_count, class_balance):
    hidden_weight = model.hidden.weight.detach().cpu().tolist()
    hidden_bias = model.hidden.bias.detach().cpu().tolist()
    output_weight = model.output.weight.detach().cpu().tolist()
    output_bias = model.output.bias.detach().cpu().tolist()

    hidden_layer = []
    for hidden_index, node_name in enumerate(HIDDEN_NODE_NAMES):
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
    for head_index, head_name in enumerate(TARGET_HEADS):
        heads[head_name] = {
            "bias": _round_weight(output_bias[head_index]),
            "weights": {
                hidden_name: _round_weight(output_weight[head_index][hidden_index])
                for hidden_index, hidden_name in enumerate(HIDDEN_NODE_NAMES)
            },
        }

    return {
        "version": output_version,
        "kind": NEURAL_CANDIDATE_SCORER_KIND,
        "feature_schema_version": 1,
        "description": "Trained tabular MLP scorer for vacation schedule candidate ranking.",
        "hidden_activation": "tanh",
        "output_activation": "sigmoid",
        "input_names": list(input_names),
        "target_heads": list(TARGET_HEADS),
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


def _candidate_quality_target(features):
    features = features if isinstance(features, dict) else {}
    priority = features.get("preference_priority") or ""
    exact_preference = _bool_feature(features, "preference_exact_period_match")
    has_preference = _bool_feature(features, "preference_has_preference")
    chargeable_days = _float_feature(features, "period_chargeable_days")
    calendar_days = _float_feature(features, "period_calendar_days")
    coverage_ratio = _float_feature(features, "planning_candidate_coverage_ratio")
    risk_score = _float_feature(features, "risk_score")
    load_level = _float_feature(features, "risk_department_load_level", 1.0)
    staff_margin = _float_feature(features, "risk_staff_margin")
    preference_period_ok = (
        exact_preference
        and priority in {"primary", "backup"}
        and (calendar_days >= 14.0 or chargeable_days >= 13.0)
    )

    score = 0.78
    if exact_preference and priority == "primary":
        score += 0.14
    elif exact_preference and priority == "backup":
        score += 0.08
    elif exact_preference:
        score += 0.03
    elif has_preference:
        score += 0.02
    else:
        score -= 0.02

    if preference_period_ok:
        score += 0.08
    elif chargeable_days >= 21:
        score += 0.04
    elif chargeable_days >= 13:
        score += 0.02
    else:
        score -= 0.06

    if coverage_ratio >= 0.90:
        score += 0.05
    elif 0.20 <= coverage_ratio < 0.75 and preference_period_ok:
        score += 0.03
    elif coverage_ratio <= 0.0:
        score -= 0.08

    if _bool_feature(features, "planning_ends_by_nearest_deadline"):
        score += 0.04
    if _bool_feature(features, "risk_is_conflict"):
        score -= 0.36

    score += min(max(staff_margin, 0.0), 4.0) * 0.010
    if staff_margin <= 0:
        score -= 0.16
    elif staff_margin == 1:
        score -= 0.04

    score -= _clamp(risk_score / 100.0, 0.0, 1.0) * 0.22
    score -= max(load_level - 1.0, 0.0) * 0.040

    outcome = features.get("historical_outcome") or ""
    if outcome == "transfer_replacement":
        score += 0.03
    elif outcome == "manual_approved":
        score -= 0.02

    return _clamp(score, 0.0, 1.0)


def _is_passed_preference_candidate(features):
    kind = features.get("candidate_kind") or ""
    priority = features.get("preference_priority") or ""
    return (
        kind in {"primary_preference", "backup_preference"}
        or priority in {"primary", "backup"}
        or bool(features.get("preference_exact_period_match"))
    )


def _float_feature(features, key, default=0.0):
    try:
        return float(features.get(key, default) or default)
    except (TypeError, ValueError):
        return float(default)


def _bool_feature(features, key):
    return 1.0 if bool(features.get(key)) else 0.0


def _clamp(value, lower=0.0, upper=1.0):
    return max(lower, min(upper, float(value)))


def _feedback_decisions(candidate):
    return tuple(
        sorted(
            {
                feedback.decision
                for feedback in list(candidate.feedback_entries.all())
                if feedback.decision
            }
        )
    )


def _package_decisions(candidate):
    return tuple(
        sorted(
            {
                period.candidate_package.decision
                for period in list(candidate.package_periods.all())
                if period.candidate_package_id and period.candidate_package.decision
            }
        )
    )


def _move_one_example_to_empty_split(groups, target_name):
    donor_name = max(
        (name for name in groups if name != target_name),
        key=lambda name: len(groups[name]),
    )
    if len(groups[donor_name]) > 1:
        groups[target_name].append(groups[donor_name].pop(0))


def _import_torch():
    try:
        import torch
    except ImportError as exc:
        raise CandidateTrainingDependencyError(
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
        raise CandidateTrainingDataError("В train split не попало ни одного примера.")

    model = _build_torch_model(torch, input_dim=len(input_names), hidden_dim=len(HIDDEN_NODE_NAMES))
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
    class CandidateMLP(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.hidden = torch.nn.Linear(input_dim, hidden_dim)
            self.output = torch.nn.Linear(hidden_dim, len(TARGET_HEADS))

        def forward(self, inputs):
            return self.output(torch.tanh(self.hidden(inputs)))

    return CandidateMLP()


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

    target_rows = [[float(example.targets[head]) for head in TARGET_HEADS] for example in examples]
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
        [[float(example.targets[head]) for head in TARGET_HEADS] for example in examples],
        dtype=torch.float32,
    )


def _sample_weights_tensor(torch, examples):
    weights = {
        "blocked": 0.35,
        "rejected": 1.30,
        "rejected_preference": 4.00,
        "ignored": 1.00,
        "selected_agree": 2.60,
        "selected_needs_change": 3.20,
        "selected_reject": 3.00,
    }
    return torch.tensor(
        [float(weights.get(example.label_bucket, 1.0)) for example in examples],
        dtype=torch.float32,
    )


def _round_weight(value):
    return round(float(value), 8)


def _round_metric(value):
    return round(float(value), 6)


def _json_dumps(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
