import json
import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from django.conf import settings


DEFAULT_NEURAL_PACKAGE_RANKER_VERSION = "vacation-package-ranker-v3"
NEURAL_PACKAGE_RANKER_KIND = "package_tabular_mlp"


@dataclass(frozen=True)
class NeuralPackageScoringResult:
    score: Decimal
    confidence: Decimal
    recommendation: str
    explanation: str
    model_version: str = DEFAULT_NEURAL_PACKAGE_RANKER_VERSION
    scorer_kind: str = NEURAL_PACKAGE_RANKER_KIND


_PACKAGE_MODEL_CACHE = {}


def get_active_package_ranker_version():
    return getattr(
        settings,
        "VACATION_PACKAGE_RANKER_VERSION",
        DEFAULT_NEURAL_PACKAGE_RANKER_VERSION,
    )


def package_model_filename(version):
    return f"{str(version).replace('-', '_')}.json"


def _model_dir():
    return Path(
        getattr(
            settings,
            "VACATION_PACKAGE_MODEL_DIR",
            getattr(
                settings,
                "VACATION_CANDIDATE_MODEL_DIR",
                Path(__file__).resolve().parent / "artifacts",
            ),
        )
    )


def package_model_path(version=None):
    return _model_dir() / package_model_filename(version or get_active_package_ranker_version())


def reset_package_ranker_model_cache():
    global _PACKAGE_MODEL_CACHE
    _PACKAGE_MODEL_CACHE = {}


def _read_package_ranker_model(version):
    model = json.loads(package_model_path(version).read_text(encoding="utf-8"))
    if model.get("version") != version:
        raise ValueError("Unexpected package ranker artifact version.")
    if model.get("kind") != NEURAL_PACKAGE_RANKER_KIND:
        raise ValueError("Unexpected package ranker artifact kind.")
    if not model.get("hidden_layer") or not model.get("heads"):
        raise ValueError("Package ranker artifact is incomplete.")
    return model


def load_package_ranker_model(version=None, *, allow_fallback=False):
    requested_version = version or get_active_package_ranker_version()
    candidate_versions = [requested_version]
    if allow_fallback and requested_version != DEFAULT_NEURAL_PACKAGE_RANKER_VERSION:
        candidate_versions.append(DEFAULT_NEURAL_PACKAGE_RANKER_VERSION)

    errors = []
    for current_version in candidate_versions:
        path = package_model_path(current_version)
        try:
            cache_key = (str(path), current_version, path.stat().st_mtime_ns)
            if cache_key not in _PACKAGE_MODEL_CACHE:
                _PACKAGE_MODEL_CACHE[cache_key] = _read_package_ranker_model(current_version)
            return _PACKAGE_MODEL_CACHE[cache_key]
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            errors.append(exc)

    if errors:
        raise errors[-1]
    raise FileNotFoundError(package_model_path(requested_version))


def _percent(value):
    value = max(Decimal("0.00"), min(Decimal("100.00"), Decimal(str(value))))
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _float_feature(features, key, default=0.0):
    try:
        return float(features.get(key, default) or default)
    except (TypeError, ValueError):
        return float(default)


def _bool_feature(features, key):
    return 1.0 if bool(features.get(key)) else 0.0


def _clamp(value, lower=0.0, upper=1.0):
    return max(lower, min(upper, float(value)))


def _sigmoid(value):
    value = _clamp(value, -40.0, 40.0)
    return 1.0 / (1.0 + math.exp(-value))


def _activation(value, name):
    if name == "relu":
        return max(0.0, value)
    if name == "sigmoid":
        return _sigmoid(value)
    return math.tanh(value)


def _linear(node, inputs, hidden_values=None):
    hidden_values = hidden_values or {}
    value = float(node.get("bias", 0.0))
    for key, weight in (node.get("weights") or {}).items():
        value += float(weight) * float(hidden_values.get(key, inputs.get(key, 0.0)))
    for key, weight in (node.get("input_weights") or {}).items():
        value += float(weight) * float(inputs.get(key, 0.0))
    return value


def build_package_ranker_inputs(features, *, passed_hard_rules=True):
    features = features or {}
    total_days = max(_float_feature(features, "total_chargeable_days"), 0.0)
    target_days = _float_feature(features, "package_target_days", total_days)
    if target_days <= 0:
        target_days = max(total_days, 1.0)
    coverage_ratio = _float_feature(features, "package_coverage_ratio", total_days / target_days)
    period_count = max(_float_feature(features, "periods_count"), 1.0)
    longest_days = _float_feature(features, "longest_period_chargeable_days")
    shortest_days = _float_feature(features, "shortest_period_chargeable_days")
    avg_period_score = _float_feature(features, "avg_period_score", 50.0)
    min_period_score = _float_feature(features, "min_period_score", avg_period_score)
    avg_confidence = _float_feature(features, "avg_period_confidence", 65.0)
    max_risk = _float_feature(features, "max_risk_score")
    avg_risk = _float_feature(features, "avg_risk_score")
    min_staff_margin = _float_feature(features, "min_staff_margin")
    avg_load = _float_feature(features, "avg_department_load_level", 1.0)
    short_periods = _float_feature(features, "short_periods_count")
    preference_ratio = _float_feature(features, "preference_periods_ratio")
    low_score_ratio = _float_feature(features, "low_score_periods_ratio")

    coverage_fit = 1.0 - min(abs(coverage_ratio - 1.0), 1.0)
    period_count_fit = 1.0 - min(max(period_count - 1.0, 0.0) / 4.0, 1.0)
    shape_quality = _clamp((longest_days / 28.0) * 0.55 + period_count_fit * 0.45)
    if shortest_days and shortest_days < 7.0:
        shape_quality -= 0.10
    if _bool_feature(features, "has_required_continuous_part"):
        shape_quality += 0.10

    risk_pressure = _clamp((max_risk / 100.0) * 0.70 + (avg_risk / 100.0) * 0.30)
    staff_safety = _clamp((min_staff_margin + 2.0) / 7.0)
    load_pressure = _clamp((avg_load - 1.0) / 4.0)
    quality_prior = _package_quality_prior(
        features,
        coverage_fit=coverage_fit,
        shape_quality=shape_quality,
        risk_pressure=risk_pressure,
        staff_safety=staff_safety,
        load_pressure=load_pressure,
    )

    return {
        "schema_match": 1.0 if int(_float_feature(features, "feature_schema_version")) == 1 else 0.0,
        "passed_hard_rules": 1.0 if passed_hard_rules else 0.0,
        "coverage_fit": _clamp(coverage_fit),
        "coverage_complete": _clamp(coverage_ratio / 1.12),
        "coverage_over_plan": _clamp(max(coverage_ratio - 1.05, 0.0)),
        "closes_need": _bool_feature(features, "package_closes_need"),
        "deadline_fit": _bool_feature(features, "package_ends_by_nearest_deadline"),
        "deadline_pressure": _bool_feature(features, "planning_has_blocker"),
        "period_count_fit": _clamp(period_count_fit),
        "single_period": 1.0 if period_count == 1.0 else 0.0,
        "two_periods": 1.0 if period_count == 2.0 else 0.0,
        "three_or_more_periods": 1.0 if period_count >= 3.0 else 0.0,
        "short_period_pressure": _clamp(short_periods / max(period_count, 1.0)),
        "long_part_ok": _bool_feature(features, "has_required_continuous_part"),
        "longest_period_fit": _clamp(longest_days / 28.0),
        "shape_quality": _clamp(shape_quality),
        "period_score_avg": _clamp(avg_period_score / 100.0),
        "period_score_min": _clamp(min_period_score / 100.0),
        "period_confidence": _clamp(avg_confidence / 100.0),
        "low_score_pressure": _clamp(low_score_ratio),
        "risk_safety": _clamp(1.0 - risk_pressure),
        "risk_pressure": risk_pressure,
        "risk_conflict": _bool_feature(features, "has_risk_conflict"),
        "staff_safety": staff_safety,
        "staff_shortage": 1.0 if min_staff_margin <= 0.0 else 0.0,
        "load_pressure": load_pressure,
        "preference_any": _bool_feature(features, "has_preference_period"),
        "primary_preference": _bool_feature(features, "has_primary_preference_period"),
        "backup_preference": _bool_feature(features, "has_backup_preference_period"),
        "preference_ratio": _clamp(preference_ratio),
        "extends_existing": _bool_feature(features, "extends_existing_item"),
        "manual_source": 1.0 if features.get("package_source") == "manual" else 0.0,
        "auto_source": 1.0 if features.get("package_source") == "generated" else 0.0,
        "quality_prior": quality_prior,
    }


def _package_quality_prior(
    features,
    *,
    coverage_fit,
    shape_quality,
    risk_pressure,
    staff_safety,
    load_pressure,
):
    score = 0.50
    score += coverage_fit * 0.18
    score += shape_quality * 0.14
    score += _clamp(_float_feature(features, "avg_period_score", 50.0) / 100.0) * 0.16
    score += _clamp(_float_feature(features, "min_period_score", 50.0) / 100.0) * 0.12
    score += staff_safety * 0.08
    score -= risk_pressure * 0.18
    score -= load_pressure * 0.06
    score -= _clamp(_float_feature(features, "short_periods_count") / 3.0) * 0.05

    if _bool_feature(features, "package_closes_need"):
        score += 0.07
    if _bool_feature(features, "has_primary_preference_period"):
        score += 0.07
    elif _bool_feature(features, "has_backup_preference_period"):
        score += 0.04
    elif _bool_feature(features, "has_preference_period"):
        score += 0.02
    if _bool_feature(features, "package_ends_by_nearest_deadline"):
        score += 0.05
    if _bool_feature(features, "has_risk_conflict"):
        score -= 0.35
    return _clamp(score)


def _hidden_values(model, inputs):
    activation_name = model.get("hidden_activation", "tanh")
    values = {}
    for node in model.get("hidden_layer", []):
        values[node["name"]] = _activation(_linear(node, inputs, values), activation_name)
    return values


def _recommendation(score, heads, inputs, *, passed_hard_rules):
    if not passed_hard_rules:
        return "blocked"
    if inputs.get("risk_conflict", 0.0) >= 1.0:
        return "avoid"
    prefer_probability = _sigmoid(heads.get("prefer", 0.0))
    avoid_probability = _sigmoid(heads.get("avoid", 0.0))
    if score >= Decimal("80.00") and prefer_probability >= avoid_probability:
        return "prefer"
    if score < Decimal("55.00") or avoid_probability > prefer_probability + 0.18:
        return "avoid"
    return "normal"


def _explanation(recommendation, score, confidence, inputs, hidden, *, passed_hard_rules, features, model_version=None):
    model_version = model_version or get_active_package_ranker_version()
    if not passed_hard_rules:
        reason = features.get("block_reason_key") or "hard_rule"
        return (
            f"Пакет не передавался в нейромодуль: он заблокирован жесткими правилами ({reason}). "
            f"Оценка {score}%, уверенность {confidence}%."
        )

    factors = []
    if hidden.get("coverage_quality", 0.0) > 0.35:
        factors.append("пакет закрывает нужный объём дней")
    if hidden.get("period_shape", 0.0) > 0.30:
        factors.append("структура периодов выглядит устойчивой")
    if hidden.get("preference_alignment", 0.0) > 0.32:
        factors.append("учтены пожелания сотрудника")
    if hidden.get("staffing_safety", 0.0) > 0.30:
        factors.append("сохраняется запас состава отдела")
    if hidden.get("risk_control", 0.0) < -0.35 or inputs.get("risk_pressure", 0.0) > 0.55:
        factors.append("есть повышенный риск по периоду или нагрузке")
    if not factors:
        factors.append("пакет прошел правила и сравнен с альтернативами")

    recommendation_label = {
        "prefer": "предпочтительный пакет",
        "normal": "допустимый пакет",
        "avoid": "нежелательный пакет",
    }.get(recommendation, "допустимый пакет")
    return (
        f"Нейромодуль {model_version} оценил {recommendation_label}: "
        f"{', '.join(factors[:3])}. Оценка {score}%, уверенность {confidence}%."
    )


def score_package_features_neural(features, *, passed_hard_rules=True):
    features = features or {}
    if not passed_hard_rules:
        score = Decimal("0.00")
        confidence = Decimal("94.00")
        return NeuralPackageScoringResult(
            score=score,
            confidence=confidence,
            recommendation="blocked",
            explanation=_explanation(
                "blocked",
                score,
                confidence,
                {},
                {},
                passed_hard_rules=False,
                features=features,
                model_version=get_active_package_ranker_version(),
            ),
            model_version=get_active_package_ranker_version(),
        )

    model = load_package_ranker_model()
    model_version = model.get("version") or get_active_package_ranker_version()
    inputs = build_package_ranker_inputs(features, passed_hard_rules=passed_hard_rules)
    hidden = _hidden_values(model, inputs)
    heads = {
        name: _linear(head, inputs, hidden)
        for name, head in (model.get("heads") or {}).items()
    }
    score = _percent(_sigmoid(heads["score"]) * 100.0)
    confidence = _percent(_sigmoid(heads["confidence"]) * 100.0)
    recommendation = _recommendation(score, heads, inputs, passed_hard_rules=passed_hard_rules)
    return NeuralPackageScoringResult(
        score=score,
        confidence=confidence,
        recommendation=recommendation,
        explanation=_explanation(
            recommendation,
            score,
            confidence,
            inputs,
            hidden,
            passed_hard_rules=passed_hard_rules,
            features=features,
            model_version=model_version,
        ),
        model_version=model_version,
    )
