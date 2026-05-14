import json
import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from django.conf import settings

DEFAULT_NEURAL_CANDIDATE_SCORER_VERSION = "vacation-candidate-mlp-v1"
NEURAL_CANDIDATE_SCORER_VERSION = getattr(
    settings,
    "VACATION_CANDIDATE_SCORER_VERSION",
    DEFAULT_NEURAL_CANDIDATE_SCORER_VERSION,
)
NEURAL_CANDIDATE_SCORER_KIND = "tabular_mlp"


@dataclass(frozen=True)
class NeuralCandidateScoringResult:
    score: Decimal
    confidence: Decimal
    recommendation: str
    explanation: str
    model_version: str = NEURAL_CANDIDATE_SCORER_VERSION
    scorer_kind: str = NEURAL_CANDIDATE_SCORER_KIND


_MODEL_CACHE = {}


def get_active_candidate_scorer_version():
    return getattr(
        settings,
        "VACATION_CANDIDATE_SCORER_VERSION",
        DEFAULT_NEURAL_CANDIDATE_SCORER_VERSION,
    )


def candidate_model_filename(version):
    return f"{str(version).replace('-', '_')}.json"


def _model_dir():
    return Path(
        getattr(
            settings,
            "VACATION_CANDIDATE_MODEL_DIR",
            Path(__file__).resolve().parent.parent / "ml_models",
        )
    )


def candidate_model_path(version=None):
    return _model_dir() / candidate_model_filename(version or get_active_candidate_scorer_version())


def reset_candidate_mlp_model_cache():
    global _MODEL_CACHE
    _MODEL_CACHE = {}


def _read_candidate_mlp_model(version):
    model = json.loads(candidate_model_path(version).read_text(encoding="utf-8"))
    if model.get("version") != version:
        raise ValueError("Unexpected neural scorer artifact version.")
    if model.get("kind") != NEURAL_CANDIDATE_SCORER_KIND:
        raise ValueError("Unexpected neural scorer artifact kind.")
    if not model.get("hidden_layer") or not model.get("heads"):
        raise ValueError("Neural scorer artifact is incomplete.")
    return model


def load_candidate_mlp_model(version=None, *, allow_fallback=True):
    requested_version = version or get_active_candidate_scorer_version()
    candidate_versions = [requested_version]
    if allow_fallback and requested_version != DEFAULT_NEURAL_CANDIDATE_SCORER_VERSION:
        candidate_versions.append(DEFAULT_NEURAL_CANDIDATE_SCORER_VERSION)

    errors = []
    for current_version in candidate_versions:
        path = candidate_model_path(current_version)
        cache_key = (str(path), current_version)
        try:
            if cache_key not in _MODEL_CACHE:
                _MODEL_CACHE[cache_key] = _read_candidate_mlp_model(current_version)
            return _MODEL_CACHE[cache_key]
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            errors.append(exc)

    if errors:
        raise errors[-1]
    raise FileNotFoundError(candidate_model_path(requested_version))


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


def build_candidate_mlp_inputs(features, *, passed_hard_rules=True):
    features = features or {}
    coverage_ratio = _float_feature(features, "planning_candidate_coverage_ratio")
    calendar_days = max(_float_feature(features, "period_calendar_days"), 1.0)
    chargeable_days = _float_feature(features, "period_chargeable_days")
    summer_overlap_days = _float_feature(features, "period_summer_overlap_days")
    if summer_overlap_days <= 0.0 and _bool_feature(features, "period_overlaps_summer"):
        summer_overlap_days = calendar_days
    risk_score = _float_feature(features, "risk_score")
    risk_level_weight = _float_feature(features, "risk_level_weight", 1.0)
    staff_margin = _float_feature(features, "risk_staff_margin")
    load_level = _float_feature(features, "risk_department_load_level", 1.0)
    priority = features.get("preference_priority") or ""
    candidate_kind = features.get("candidate_kind") or ""
    tenure_days = _float_feature(features, "employee_tenure_days_at_year_end")
    preference_exact = _bool_feature(features, "preference_exact_period_match")
    preference_period_ok = (
        preference_exact
        and priority in {"primary", "backup"}
        and (calendar_days >= 14.0 or chargeable_days >= 13.0)
    )
    preference_partial_remainder = preference_period_ok and 0.0 < coverage_ratio < 0.75

    coverage_fit = 1.0 - min(abs(coverage_ratio - 1.0), 1.0)
    over_plan_pressure = max(coverage_ratio - 1.1, 0.0)
    risk_pressure = _clamp((risk_score / 100.0) + (risk_level_weight - 1.0) * 0.12, 0.0, 1.0)
    period_length_fit = 1.0 - min(abs(chargeable_days - 28.0) / 28.0, 1.0)
    planning_open_days = _float_feature(features, "planning_open_required_days")
    planning_blocking_days = _float_feature(features, "planning_blocking_days")
    if preference_period_ok:
        # A normal requested 14-day first period is allowed to leave a yearly
        # remainder for top-up; that remainder should not make the period look
        # like a near-zero quality candidate.
        coverage_fit = max(coverage_fit, _clamp(0.64 + period_length_fit * 0.30))
        coverage_complete = max(_clamp(coverage_ratio / 1.15), _clamp(0.52 + period_length_fit * 0.34))
    else:
        coverage_complete = _clamp(coverage_ratio / 1.15)
    quality_prior = _candidate_quality_prior(
        features,
        priority=priority,
        preference_exact=preference_exact,
        preference_period_ok=preference_period_ok,
        coverage_ratio=coverage_ratio,
        calendar_days=calendar_days,
        chargeable_days=chargeable_days,
        risk_score=risk_score,
        staff_margin=staff_margin,
        load_level=load_level,
    )

    return {
        "schema_match": 1.0 if int(_float_feature(features, "feature_schema_version")) == 1 else 0.0,
        "passed_hard_rules": 1.0 if passed_hard_rules else 0.0,
        "preference_exact": preference_exact,
        "preference_primary": 1.0 if priority == "primary" else 0.0,
        "preference_backup": 1.0 if priority == "backup" else 0.0,
        "preference_missing": 0.0 if _bool_feature(features, "preference_has_preference") else 1.0,
        "preference_period_ok": 1.0 if preference_period_ok else 0.0,
        "preference_partial_remainder": 1.0 if preference_partial_remainder else 0.0,
        "auto_candidate": 1.0 if str(candidate_kind).startswith("auto") else 0.0,
        "manual_candidate": 1.0 if candidate_kind == "manual" else 0.0,
        "coverage_fit": _clamp(coverage_fit),
        "coverage_complete": coverage_complete,
        "quality_prior": quality_prior,
        "risk_quality_prior": _clamp(1.0 - (risk_score / 100.0) - max(load_level - 1.0, 0.0) * 0.08),
        "over_plan_pressure": _clamp(over_plan_pressure),
        "period_length_fit": _clamp(period_length_fit),
        "deadline_fit": (
            1.0
            if _bool_feature(features, "planning_has_blocker")
            and _bool_feature(features, "planning_ends_by_nearest_deadline")
            else 0.0
        ),
        "deadline_miss": (
            1.0
            if _bool_feature(features, "planning_has_blocker")
            and not _bool_feature(features, "planning_ends_by_nearest_deadline")
            else 0.0
        ),
        "planning_blocker_pressure": _clamp(planning_blocking_days / 28.0),
        "planning_open_pressure": _clamp(planning_open_days / 52.0),
        "risk_safety": _clamp(1.0 - risk_pressure),
        "risk_pressure": risk_pressure,
        "risk_conflict": _bool_feature(features, "risk_is_conflict"),
        "staff_margin_positive": _clamp(staff_margin / 5.0),
        "staff_shortage": 1.0 if staff_margin <= 0.0 else 0.0,
        "load_pressure": _clamp((load_level - 1.0) / 4.0),
        "substitution_available": (
            _bool_feature(features, "risk_has_substitution_capacity")
            or _bool_feature(features, "risk_substitution_used")
        ),
        "summer_overlap_ratio": _clamp(summer_overlap_days / calendar_days),
        "cross_month": _bool_feature(features, "period_cross_month") or _bool_feature(features, "period_crosses_month"),
        "tenure_maturity": _clamp(tenure_days / 730.0),
        "manager_role": _bool_feature(features, "employee_is_manager") or _bool_feature(features, "employee_is_management"),
        "has_risk_payload": 1.0 if "risk_staff_margin" in features and "risk_score" in features else 0.0,
    }


def _candidate_quality_prior(
    features,
    *,
    priority,
    preference_exact,
    preference_period_ok,
    coverage_ratio,
    calendar_days,
    chargeable_days,
    risk_score,
    staff_margin,
    load_level,
):
    score = 0.52
    if preference_exact and priority == "primary":
        score += 0.24
    elif preference_exact and priority == "backup":
        score += 0.17
    elif preference_exact:
        score += 0.08
    elif _bool_feature(features, "preference_has_preference"):
        score += 0.02
    else:
        score -= 0.03

    if preference_period_ok:
        score += 0.05
    length_anchor = 21.0 if chargeable_days <= 35.0 else 28.0
    period_length_fit = 1.0 - min(abs(chargeable_days - length_anchor) / max(length_anchor, 1.0), 1.0)
    score += _clamp(period_length_fit) * 0.08

    if coverage_ratio >= 0.90:
        score += 0.05
    elif preference_period_ok and 0.18 <= coverage_ratio < 0.75:
        score += 0.04
    elif coverage_ratio <= 0.0:
        score -= 0.08

    score += min(max(staff_margin, 0.0), 6.0) * 0.010
    if staff_margin <= 0.0:
        score -= 0.18
    elif staff_margin == 1.0:
        score -= 0.04

    score -= _clamp(risk_score / 100.0) * 0.20
    score -= max(load_level - 1.0, 0.0) * 0.045
    if _bool_feature(features, "risk_is_conflict"):
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
    model_version = model_version or get_active_candidate_scorer_version()
    if not passed_hard_rules:
        reason = features.get("candidate_block_reason_key") or "hard_rule"
        return (
            f"Кандидат не передавался в нейромодуль: он заблокирован жесткими правилами ({reason}). "
            f"Оценка {score}%, уверенность {confidence}%."
        )

    factors = []
    if hidden.get("preference_alignment", 0.0) > 0.45:
        factors.append("период хорошо совпадает с пожеланием")
    if inputs.get("preference_period_ok", 0.0) >= 1.0 and inputs.get("planning_open_pressure", 0.0) > 0.0:
        factors.append("оставшийся отпуск можно добрать отдельными периодами")
    if hidden.get("coverage_balance", 0.0) > 0.38:
        factors.append("закрывает плановую потребность без сильного перекоса")
    if hidden.get("deadline_closure", 0.0) > 0.32:
        factors.append("помогает закрыть срочный остаток")
    if hidden.get("staffing_safety", 0.0) > 0.34:
        factors.append("оставляет приемлемый запас состава отдела")
    if hidden.get("department_pressure", 0.0) > 0.55:
        factors.append("модель видит повышенную нагрузку отдела")
    if hidden.get("manager_caution", 0.0) > 0.45:
        factors.append("для управленческой роли нужен дополнительный контроль")
    if not factors:
        factors.append("вариант прошел проверку, но требует управленческой оценки")

    recommendation_label = {
        "prefer": "предпочтительный вариант",
        "normal": "допустимый вариант",
        "avoid": "нежелательный вариант",
    }.get(recommendation, "допустимый вариант")
    return (
        f"Нейромодуль {model_version} выбрал {recommendation_label}: "
        f"{', '.join(factors[:3])}. Оценка {score}%, уверенность {confidence}%."
    )


def score_candidate_features_neural(features, *, passed_hard_rules=True):
    features = features or {}
    if not passed_hard_rules:
        score = Decimal("0.00")
        confidence = Decimal("94.00")
        return NeuralCandidateScoringResult(
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
                model_version=get_active_candidate_scorer_version(),
            ),
            model_version=get_active_candidate_scorer_version(),
        )

    model = load_candidate_mlp_model()
    model_version = model.get("version") or get_active_candidate_scorer_version()
    inputs = build_candidate_mlp_inputs(features, passed_hard_rules=passed_hard_rules)
    hidden = _hidden_values(model, inputs)
    heads = {
        name: _linear(head, inputs, hidden)
        for name, head in (model.get("heads") or {}).items()
    }
    score = _percent(_sigmoid(heads["score"]) * 100.0)
    confidence = _percent(_sigmoid(heads["confidence"]) * 100.0)
    recommendation = _recommendation(score, heads, inputs, passed_hard_rules=passed_hard_rules)
    return NeuralCandidateScoringResult(
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
