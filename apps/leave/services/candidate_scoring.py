from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from apps.leave.models import VacationScheduleCandidate

from .candidate_neural import (
    NEURAL_CANDIDATE_SCORER_VERSION,
    get_active_candidate_scorer_version,
    score_candidate_features_neural,
)

BASELINE_CANDIDATE_SCORER_VERSION = "candidate-scorer-baseline-v1"
ACTIVE_CANDIDATE_SCORER_VERSION = NEURAL_CANDIDATE_SCORER_VERSION
NEURAL_FALLBACK_SCORER_VERSION = f"{NEURAL_CANDIDATE_SCORER_VERSION}+fallback-{BASELINE_CANDIDATE_SCORER_VERSION}"


def get_neural_fallback_scorer_version():
    return f"{get_active_candidate_scorer_version()}+fallback-{BASELINE_CANDIDATE_SCORER_VERSION}"


@dataclass(frozen=True)
class CandidateScoringResult:
    score: Decimal
    confidence: Decimal
    recommendation: str
    explanation: str
    model_version: str = ACTIVE_CANDIDATE_SCORER_VERSION
    scorer_kind: str = "neural"


def _percent(value):
    value = max(Decimal("0.00"), min(Decimal("100.00"), Decimal(str(value))))
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _feature_decimal(features, key, default=0):
    try:
        return Decimal(str(features.get(key, default) or default))
    except Exception:
        return Decimal(str(default))


def _feature_bool(features, key):
    return bool(features.get(key))


def _risk_penalty(features):
    risk_score = _feature_decimal(features, "risk_score")
    risk_level_weight = _feature_decimal(features, "risk_level_weight")
    conflict_penalty = Decimal("45.00") if _feature_bool(features, "risk_is_conflict") else Decimal("0.00")
    return (risk_score * Decimal("0.35")) + (risk_level_weight * Decimal("2.50")) + conflict_penalty


def _coverage_bonus(features):
    ratio = _feature_decimal(features, "planning_candidate_coverage_ratio")
    if ratio <= 0:
        return Decimal("-15.00")
    if Decimal("0.90") <= ratio <= Decimal("1.10"):
        return Decimal("14.00")
    if Decimal("0.50") <= ratio < Decimal("0.90"):
        return Decimal("6.00")
    if Decimal("1.10") < ratio <= Decimal("1.30"):
        return Decimal("3.00")
    return Decimal("-8.00")


def _preference_bonus(features):
    if not _feature_bool(features, "preference_has_preference"):
        return Decimal("0.00")
    if _feature_bool(features, "preference_exact_period_match"):
        priority = features.get("preference_priority")
        if priority == "primary":
            return Decimal("16.00")
        if priority == "backup":
            return Decimal("10.00")
        return Decimal("8.00")
    return Decimal("3.00")


def _deadline_bonus(features):
    if not _feature_bool(features, "planning_has_blocker"):
        return Decimal("0.00")
    if _feature_bool(features, "planning_ends_by_nearest_deadline"):
        return Decimal("12.00")
    return Decimal("-18.00")


def _staffing_bonus(features):
    staff_margin = _feature_decimal(features, "risk_staff_margin")
    if staff_margin >= 3:
        return Decimal("5.00")
    if staff_margin >= 1:
        return Decimal("2.00")
    if staff_margin == 0:
        return Decimal("-4.00")
    return Decimal("-12.00")


def _confidence(features, *, passed_hard_rules):
    confidence = Decimal("45.00")
    if passed_hard_rules:
        confidence += Decimal("18.00")
    if features.get("feature_schema_version"):
        confidence += Decimal("8.00")
    if _feature_decimal(features, "period_chargeable_days") > 0:
        confidence += Decimal("7.00")
    if _feature_decimal(features, "planning_open_required_days") > 0:
        confidence += Decimal("7.00")
    if "risk_score" in features and "risk_staff_margin" in features:
        confidence += Decimal("8.00")
    if _feature_bool(features, "preference_has_preference"):
        confidence += Decimal("5.00")
    if _feature_bool(features, "risk_is_conflict"):
        confidence -= Decimal("12.00")
    return _percent(confidence)


def _recommendation(score, features, *, passed_hard_rules):
    if not passed_hard_rules:
        return "blocked"
    if _feature_bool(features, "risk_is_conflict"):
        return "avoid"
    if score >= Decimal("80.00"):
        return "prefer"
    if score >= Decimal("55.00"):
        return "normal"
    return "avoid"


def _explanation(recommendation, score, confidence, features, *, passed_hard_rules):
    if not passed_hard_rules:
        reason = features.get("candidate_block_reason_key") or "hard_rule"
        return f"Кандидат заблокирован жесткими правилами: {reason}. Оценка {score}%, уверенность {confidence}%."

    factors = []
    if _feature_bool(features, "preference_exact_period_match"):
        factors.append("совпадает с пожеланием сотрудника")
    if _feature_bool(features, "planning_ends_by_nearest_deadline"):
        factors.append("закрывает срочный остаток до дедлайна")
    if _feature_decimal(features, "planning_candidate_coverage_ratio") >= Decimal("0.90"):
        factors.append("хорошо закрывает плановую потребность")
    if _feature_decimal(features, "risk_score") <= Decimal("35.00"):
        factors.append("имеет низкий расчетный риск")
    if _feature_decimal(features, "risk_staff_margin") > 0:
        factors.append("оставляет запас по составу отдела")
    if not factors:
        factors.append("прошел жесткие правила, но требует управленческой проверки")

    recommendation_label = {
        "prefer": "предпочтительный вариант",
        "normal": "допустимый вариант",
        "avoid": "нежелательный вариант",
    }.get(recommendation, "допустимый вариант")
    return f"{recommendation_label}: {', '.join(factors)}. Оценка {score}%, уверенность {confidence}%."


def score_candidate_features_baseline(features, *, passed_hard_rules=True, model_version=BASELINE_CANDIDATE_SCORER_VERSION):
    features = features or {}
    if not passed_hard_rules:
        score = Decimal("0.00")
    else:
        score = (
            Decimal("58.00")
            + _coverage_bonus(features)
            + _preference_bonus(features)
            + _deadline_bonus(features)
            + _staffing_bonus(features)
            - _risk_penalty(features)
        )
    score = _percent(score)
    confidence = _confidence(features, passed_hard_rules=passed_hard_rules)
    recommendation = _recommendation(score, features, passed_hard_rules=passed_hard_rules)
    return CandidateScoringResult(
        score=score,
        confidence=confidence,
        recommendation=recommendation,
        explanation=_explanation(
            recommendation,
            score,
            confidence,
            features,
            passed_hard_rules=passed_hard_rules,
        ),
        model_version=model_version,
        scorer_kind="baseline",
    )


def score_candidate_features(features, *, passed_hard_rules=True, use_neural=True):
    if use_neural:
        try:
            neural_result = score_candidate_features_neural(
                features,
                passed_hard_rules=passed_hard_rules,
            )
            return CandidateScoringResult(
                score=neural_result.score,
                confidence=neural_result.confidence,
                recommendation=neural_result.recommendation,
                explanation=neural_result.explanation,
                model_version=neural_result.model_version,
                scorer_kind=neural_result.scorer_kind,
            )
        except Exception:
            fallback_model_version = get_neural_fallback_scorer_version()
            fallback = score_candidate_features_baseline(
                features,
                passed_hard_rules=passed_hard_rules,
                model_version=fallback_model_version,
            )
            return CandidateScoringResult(
                score=fallback.score,
                confidence=fallback.confidence,
                recommendation=fallback.recommendation,
                explanation=(
                    "Нейромодуль временно недоступен, применена безопасная базовая оценка. "
                    f"{fallback.explanation}"
                ),
                model_version=fallback_model_version,
                scorer_kind="baseline_fallback",
            )
    return score_candidate_features_baseline(features, passed_hard_rules=passed_hard_rules)


def score_schedule_candidate(candidate):
    return score_candidate_features(
        candidate.features,
        passed_hard_rules=candidate.passed_hard_rules,
    )


def apply_schedule_candidate_score(candidate):
    result = score_schedule_candidate(candidate)
    features = dict(candidate.features or {})
    features["scoring_recommendation"] = result.recommendation
    features["scoring_scorer_kind"] = result.scorer_kind
    candidate.score = result.score
    candidate.confidence = result.confidence
    candidate.model_version = result.model_version
    candidate.explanation = result.explanation
    candidate.features = features
    candidate.save(update_fields=["score", "confidence", "model_version", "explanation", "features"])
    return candidate
