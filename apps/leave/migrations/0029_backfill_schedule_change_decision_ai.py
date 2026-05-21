from decimal import Decimal, ROUND_HALF_UP

from django.db import migrations


RECOMMENDATION_LABELS = {
    "prefer": "удачный период",
    "normal": "можно одобрять",
    "avoid": "лучше проверить",
    "blocked": "есть ограничения",
}


def _decimal_percent(value):
    try:
        percent = Decimal(str(value or 0))
    except Exception:
        percent = Decimal("0")
    percent = max(Decimal("0"), min(Decimal("100"), percent))
    return percent.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _recommendation_for(risk_level, risk_score, score):
    if risk_level == "high" or risk_score >= 75:
        return "blocked"
    if risk_level == "medium" or risk_score >= 45:
        return "avoid"
    if score >= Decimal("80.00"):
        return "prefer"
    return "normal"


def backfill_schedule_change_decision_ai(apps, schema_editor):
    VacationScheduleChangeRequest = apps.get_model("leave", "VacationScheduleChangeRequest")
    reviewed_changes = VacationScheduleChangeRequest.objects.exclude(status="pending").filter(
        decision_ai_score__isnull=True,
    )
    for change_request in reviewed_changes.iterator():
        risk_score = int(change_request.risk_score or 0)
        score = _decimal_percent(Decimal("100") - Decimal(risk_score))
        confidence = _decimal_percent(Decimal("80") - (Decimal(risk_score) / Decimal("4")))
        recommendation = _recommendation_for(change_request.risk_level, risk_score, score)
        recommendation_label = RECOMMENDATION_LABELS.get(recommendation, RECOMMENDATION_LABELS["normal"])

        change_request.decision_ai_score = score
        change_request.decision_ai_confidence = confidence
        change_request.decision_ai_model_version = "schedule-change-decision-backfill-v1"
        change_request.decision_ai_recommendation = recommendation
        change_request.decision_ai_explanation = (
            "Оценка на момент решения восстановлена по сохраненному расчету переноса: "
            f"риск {risk_score}%, рекомендация — {recommendation_label}."
        )
        change_request.decision_ai_scorer_kind = "backfill"
        change_request.decision_ai_evaluated_at = change_request.reviewed_at or change_request.created_at
        change_request.save(
            update_fields=[
                "decision_ai_score",
                "decision_ai_confidence",
                "decision_ai_model_version",
                "decision_ai_recommendation",
                "decision_ai_explanation",
                "decision_ai_scorer_kind",
                "decision_ai_evaluated_at",
            ]
        )


class Migration(migrations.Migration):
    dependencies = [
        ("leave", "0028_schedule_change_decision_ai_support"),
    ]

    operations = [
        migrations.RunPython(backfill_schedule_change_decision_ai, migrations.RunPython.noop),
    ]
