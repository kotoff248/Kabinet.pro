from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from .package_runtime import (
    DEFAULT_NEURAL_PACKAGE_RANKER_VERSION,
    get_active_package_ranker_version,
    score_package_features_neural,
)


BASELINE_PACKAGE_RANKER_VERSION = "package-ranker-baseline-v1"


@dataclass(frozen=True)
class PackageScoringResult:
    score: Decimal
    confidence: Decimal
    recommendation: str
    explanation: str
    model_version: str
    scorer_kind: str


def get_neural_package_fallback_version():
    return f"{get_active_package_ranker_version()}+fallback-{BASELINE_PACKAGE_RANKER_VERSION}"


def _percent(value):
    value = max(Decimal("0.00"), min(Decimal("100.00"), Decimal(str(value))))
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _decimal_feature(features, key, default=0):
    try:
        return Decimal(str(features.get(key, default) or default))
    except Exception:
        return Decimal(str(default))


def _bool_feature(features, key):
    return bool(features.get(key))


def _ratio(numerator, denominator):
    denominator = _decimal_feature({"value": denominator}, "value")
    if denominator <= 0:
        return Decimal("0.00")
    return _decimal_feature({"value": numerator}, "value") / denominator


def _clamp(value, lower=Decimal("0.00"), upper=Decimal("1.00")):
    value = Decimal(str(value))
    return max(lower, min(upper, value))


def _period_rows_from_generation_package(package):
    rows = []
    for candidate in getattr(package, "candidates", []) or []:
        metadata = getattr(candidate, "metadata", {}) or {}
        assessment = getattr(candidate, "assessment", {}) or {}
        risk_payload = assessment.get("risk_payload") or {}
        risk_explanation = risk_payload.get("risk_explanation") or {}
        staff_margin = (
            risk_payload.get("remaining_staff_count", 0)
            or risk_explanation.get("remaining_staff", 0)
            or 0
        )
        required_staff = (
            risk_payload.get("min_staff_required", 0)
            or risk_explanation.get("required_staff", 0)
            or 0
        )
        rows.append(
            {
                "start_date": getattr(candidate, "start_date", None),
                "end_date": getattr(candidate, "end_date", None),
                "chargeable_days": metadata.get("chargeable_days") or 0,
                "score": metadata.get("scoring_score") or 0,
                "confidence": metadata.get("scoring_confidence") or 0,
                "risk_score": metadata.get("risk_score") or risk_payload.get("risk_score") or 0,
                "risk_level": metadata.get("risk_level") or risk_payload.get("risk_level") or "",
                "risk_is_conflict": bool(risk_explanation.get("is_conflict")),
                "staff_margin": Decimal(str(staff_margin or 0)) - Decimal(str(required_staff or 0)),
                "department_load_level": risk_payload.get("department_load_level") or 1,
                "passed_hard_rules": bool(metadata.get("passed_hard_rules", True)),
                "preference_match": metadata.get("preference_match") or "",
                "is_preference_candidate": bool(metadata.get("is_preference_candidate")),
                "extends_existing_item": bool(metadata.get("extends_existing_item")),
                "planning_ends_by_nearest_deadline": bool(metadata.get("planning_ends_by_nearest_deadline")),
            }
        )
    return rows


def _period_rows_from_record_features(features):
    rows = []
    for row in features.get("periods") or []:
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "start_date": row.get("start_date"),
                "end_date": row.get("end_date"),
                "chargeable_days": row.get("chargeable_days") or row.get("period_chargeable_days") or 0,
                "score": row.get("score") or row.get("scoring_score") or 0,
                "confidence": row.get("confidence") or row.get("scoring_confidence") or 0,
                "risk_score": row.get("risk_score") or 0,
                "risk_level": row.get("risk_level") or "",
                "risk_is_conflict": bool(row.get("risk_is_conflict")),
                "staff_margin": row.get("risk_staff_margin") or row.get("staff_margin") or 0,
                "department_load_level": row.get("risk_department_load_level") or row.get("department_load_level") or 1,
                "passed_hard_rules": bool(row.get("passed_hard_rules", True)),
                "preference_match": row.get("preference_match") or "",
                "is_preference_candidate": bool(row.get("is_preference_candidate")),
                "extends_existing_item": bool(row.get("extends_existing_item")),
                "planning_ends_by_nearest_deadline": bool(row.get("planning_ends_by_nearest_deadline")),
            }
        )
    return rows


def build_generation_package_features(package):
    metadata = dict(getattr(package, "metadata", {}) or {})
    rows = _period_rows_from_generation_package(package)
    return build_package_features(metadata, rows, package_source=getattr(package, "source", ""))


def build_package_features(metadata, period_rows, *, package_source=""):
    metadata = dict(metadata or {})
    period_rows = list(period_rows or [])
    total_days = _decimal_feature(metadata, "total_chargeable_days")
    if total_days <= 0:
        total_days = sum((_decimal_feature(row, "chargeable_days") for row in period_rows), Decimal("0.00"))
    target_days = (
        _decimal_feature(metadata, "auto_place_target_days")
        or _decimal_feature(metadata, "package_target_days")
        or _decimal_feature(metadata, "target_days")
        or total_days
    )
    remaining_days = _decimal_feature(metadata, "remaining_after_package")
    if target_days > 0 and "remaining_after_package" not in metadata:
        remaining_days = max(target_days - total_days, Decimal("0.00"))

    scores = [_decimal_feature(row, "score") for row in period_rows if _decimal_feature(row, "score") > 0]
    confidences = [
        _decimal_feature(row, "confidence")
        for row in period_rows
        if _decimal_feature(row, "confidence") > 0
    ]
    risks = [_decimal_feature(row, "risk_score") for row in period_rows]
    staff_margins = [_decimal_feature(row, "staff_margin") for row in period_rows]
    loads = [_decimal_feature(row, "department_load_level", 1) for row in period_rows]
    chargeable_days = [_decimal_feature(row, "chargeable_days") for row in period_rows]
    preference_rows = [
        row
        for row in period_rows
        if row.get("is_preference_candidate") or row.get("preference_match")
    ]

    periods_count = len(period_rows) or int(_decimal_feature(metadata, "periods_count") or 0)
    longest_days = max(chargeable_days, default=Decimal("0.00"))
    shortest_days = min([value for value in chargeable_days if value > 0], default=Decimal("0.00"))
    short_periods = sum(1 for value in chargeable_days if Decimal("0.00") < value < Decimal("14.00"))

    features = {
        **metadata,
        "feature_schema_version": 1,
        "package_source": package_source or metadata.get("package_source", ""),
        "periods_count": periods_count,
        "total_chargeable_days": float(total_days),
        "package_target_days": float(target_days),
        "remaining_after_package": float(remaining_days),
        "package_coverage_ratio": float(_ratio(total_days, target_days)),
        "package_closes_need": bool(metadata.get("package_closes_need")) or (target_days > 0 and remaining_days <= 0),
        "planning_has_blocker": bool(metadata.get("has_blocker") or metadata.get("planning_has_blocker")),
        "package_ends_by_nearest_deadline": any(row.get("planning_ends_by_nearest_deadline") for row in period_rows),
        "avg_period_score": float(sum(scores, Decimal("0.00")) / Decimal(len(scores))) if scores else 0.0,
        "min_period_score": float(min(scores)) if scores else 0.0,
        "low_score_periods_ratio": float(
            _ratio(sum(1 for score in scores if score < Decimal("55.00")), len(scores))
        )
        if scores
        else 0.0,
        "avg_period_confidence": float(sum(confidences, Decimal("0.00")) / Decimal(len(confidences)))
        if confidences
        else 0.0,
        "max_risk_score": float(max(risks, default=Decimal("0.00"))),
        "avg_risk_score": float(sum(risks, Decimal("0.00")) / Decimal(len(risks))) if risks else 0.0,
        "has_risk_conflict": any(bool(row.get("risk_is_conflict")) or not bool(row.get("passed_hard_rules", True)) for row in period_rows),
        "min_staff_margin": float(min(staff_margins, default=Decimal("0.00"))),
        "avg_staff_margin": float(sum(staff_margins, Decimal("0.00")) / Decimal(len(staff_margins)))
        if staff_margins
        else 0.0,
        "avg_department_load_level": float(sum(loads, Decimal("0.00")) / Decimal(len(loads))) if loads else 1.0,
        "short_periods_count": short_periods,
        "longest_period_chargeable_days": float(longest_days),
        "shortest_period_chargeable_days": float(shortest_days),
        "has_required_continuous_part": bool(longest_days >= Decimal("14.00")),
        "has_preference_period": bool(preference_rows),
        "has_primary_preference_period": any(row.get("preference_match") == "primary" for row in preference_rows),
        "has_backup_preference_period": any(row.get("preference_match") == "backup" for row in preference_rows),
        "preference_periods_ratio": float(_ratio(len(preference_rows), periods_count or 1)),
        "extends_existing_item": any(bool(row.get("extends_existing_item")) for row in period_rows),
        "periods": period_rows,
    }
    return features


def build_record_package_features(package):
    features = dict(package.features or {})
    rows = []
    if hasattr(package, "periods"):
        for period in list(package.periods.all()):
            candidate = getattr(period, "candidate", None)
            candidate_features = dict(getattr(candidate, "features", {}) or {})
            period_features = period.features if isinstance(period.features, dict) else {}
            rows.append(
                {
                    **candidate_features,
                    "start_date": period.start_date,
                    "end_date": period.end_date,
                    "chargeable_days": period.chargeable_days,
                    "score": getattr(candidate, "score", None) or period_features.get("score") or 0,
                    "confidence": getattr(candidate, "confidence", None) or 0,
                    "risk_score": period.risk_score,
                    "risk_level": period.risk_level,
                    "passed_hard_rules": period.passed_hard_rules,
                }
            )
    if not rows:
        rows = _period_rows_from_record_features(features)
    return build_package_features(
        {
            **features,
            "total_chargeable_days": package.total_chargeable_days,
            "periods_count": package.periods_count,
            "package_source": package.source,
        },
        rows,
        package_source=package.source,
    )


def score_package_features_baseline(features, *, passed_hard_rules=True, model_version=BASELINE_PACKAGE_RANKER_VERSION):
    features = features or {}
    if not passed_hard_rules:
        score = Decimal("0.00")
    else:
        coverage_ratio = _decimal_feature(features, "package_coverage_ratio")
        coverage_fit = Decimal("1.00") - min(abs(coverage_ratio - Decimal("1.00")), Decimal("1.00"))
        avg_score = _decimal_feature(features, "avg_period_score", 50)
        min_score = _decimal_feature(features, "min_period_score", avg_score)
        max_risk = _decimal_feature(features, "max_risk_score")
        periods_count = max(_decimal_feature(features, "periods_count", 1), Decimal("1.00"))
        min_staff_margin = _decimal_feature(features, "min_staff_margin")

        score = Decimal("42.00")
        score += coverage_fit * Decimal("18.00")
        score += avg_score * Decimal("0.30")
        score += min_score * Decimal("0.18")
        if _bool_feature(features, "package_closes_need"):
            score += Decimal("6.00")
        if _bool_feature(features, "has_primary_preference_period"):
            score += Decimal("5.00")
        elif _bool_feature(features, "has_backup_preference_period"):
            score += Decimal("3.00")
        if _bool_feature(features, "has_required_continuous_part"):
            score += Decimal("3.00")
        if periods_count == 1:
            score += Decimal("2.50")
        elif periods_count >= 3:
            score -= Decimal("1.00") * min(periods_count - Decimal("2.00"), Decimal("3.00"))
        score -= _decimal_feature(features, "short_periods_count") * Decimal("0.80")
        score -= max_risk * Decimal("0.12")
        if min_staff_margin <= 0:
            score -= Decimal("7.50")
        elif min_staff_margin >= 3:
            score += Decimal("2.50")
        if _bool_feature(features, "has_risk_conflict"):
            score -= Decimal("35.00")

    score = _percent(score)
    confidence = _percent(
        Decimal("58.00")
        + (Decimal("12.00") if _bool_feature(features, "package_closes_need") else Decimal("0.00"))
        + min(_decimal_feature(features, "periods_count", 1) * Decimal("3.00"), Decimal("12.00"))
        + (Decimal("8.00") if _decimal_feature(features, "avg_period_score") > 0 else Decimal("0.00"))
        - (Decimal("12.00") if _bool_feature(features, "has_risk_conflict") else Decimal("0.00"))
    )
    if not passed_hard_rules:
        recommendation = "blocked"
    elif score >= Decimal("80.00"):
        recommendation = "prefer"
    elif score >= Decimal("55.00"):
        recommendation = "normal"
    else:
        recommendation = "avoid"

    return PackageScoringResult(
        score=score,
        confidence=confidence,
        recommendation=recommendation,
        explanation=_baseline_explanation(recommendation, score, confidence, features, passed_hard_rules=passed_hard_rules),
        model_version=model_version,
        scorer_kind="package_baseline",
    )


def _baseline_explanation(recommendation, score, confidence, features, *, passed_hard_rules):
    if not passed_hard_rules:
        return f"Пакет заблокирован жесткими правилами. Оценка {score}%, уверенность {confidence}%."
    factors = []
    if _bool_feature(features, "package_closes_need"):
        factors.append("закрывает нужные дни")
    if _bool_feature(features, "has_preference_period"):
        factors.append("учитывает пожелания")
    if _bool_feature(features, "has_required_continuous_part"):
        factors.append("есть непрерывная часть от 14 дней")
    if _decimal_feature(features, "max_risk_score") <= 35:
        factors.append("низкий максимальный риск")
    if not factors:
        factors.append("прошел жесткие правила")
    label = {
        "prefer": "предпочтительный пакет",
        "normal": "допустимый пакет",
        "avoid": "нежелательный пакет",
    }.get(recommendation, "допустимый пакет")
    return f"{label}: {', '.join(factors)}. Оценка {score}%, уверенность {confidence}%."


def score_package_features(features, *, passed_hard_rules=True, use_neural=True):
    if use_neural:
        try:
            neural = score_package_features_neural(features, passed_hard_rules=passed_hard_rules)
            return PackageScoringResult(
                score=neural.score,
                confidence=neural.confidence,
                recommendation=neural.recommendation,
                explanation=neural.explanation,
                model_version=neural.model_version,
                scorer_kind=neural.scorer_kind,
            )
        except Exception:
            fallback = score_package_features_baseline(
                features,
                passed_hard_rules=passed_hard_rules,
                model_version=get_neural_package_fallback_version(),
            )
            return PackageScoringResult(
                score=fallback.score,
                confidence=fallback.confidence,
                recommendation=fallback.recommendation,
                explanation=(
                    "Нейромодуль пакетов временно недоступен, применена безопасная пакетная оценка. "
                    f"{fallback.explanation}"
                ),
                model_version=fallback.model_version,
                scorer_kind="package_baseline_fallback",
            )
    return score_package_features_baseline(features, passed_hard_rules=passed_hard_rules)


def score_generation_package(package, *, use_neural=True):
    features = build_generation_package_features(package)
    passed_hard_rules = all(bool(row.get("passed_hard_rules", True)) for row in features.get("periods") or [])
    result = score_package_features(features, passed_hard_rules=passed_hard_rules, use_neural=use_neural)
    package.metadata.update(
        {
            "package_score": result.score,
            "package_confidence": result.confidence,
            "package_model_version": result.model_version,
            "package_scorer_kind": result.scorer_kind,
            "package_recommendation": result.recommendation,
            "package_scoring_explanation": result.explanation,
        }
    )
    return result
