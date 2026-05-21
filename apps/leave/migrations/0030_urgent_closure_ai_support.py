from decimal import Decimal, ROUND_HALF_UP

from django.db import migrations, models


ACTIVE_STATUSES = ("department_review", "employee_review", "hr_finalization")
TERMINAL_STATUSES = ("completed", "rejected")
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


def _snapshot_from_saved_risk(closure_request, *, model_version):
    risk_score = int(closure_request.risk_score or 0)
    score = _decimal_percent(Decimal("100") - Decimal(risk_score))
    confidence = _decimal_percent(Decimal("80") - (Decimal(risk_score) / Decimal("4")))
    recommendation = _recommendation_for(closure_request.risk_level, risk_score, score)
    recommendation_label = RECOMMENDATION_LABELS.get(recommendation, RECOMMENDATION_LABELS["normal"])
    explanation = (
        "Оценка срочного закрытия восстановлена по сохраненному расчету: "
        f"риск {risk_score}%, рекомендация — {recommendation_label}."
    )
    return {
        "score": score,
        "confidence": confidence,
        "model_version": model_version,
        "recommendation": recommendation,
        "explanation": explanation,
        "scorer_kind": "backfill",
    }


def _apply_active_snapshot(closure_request, snapshot):
    closure_request.ai_score = snapshot["score"]
    closure_request.ai_confidence = snapshot["confidence"]
    closure_request.ai_model_version = snapshot["model_version"]
    closure_request.ai_recommendation = snapshot["recommendation"]
    closure_request.ai_explanation = snapshot["explanation"]
    closure_request.ai_scorer_kind = snapshot["scorer_kind"]
    closure_request.ai_evaluated_at = closure_request.updated_at or closure_request.created_at


def _apply_decision_snapshot(closure_request, snapshot):
    closure_request.decision_ai_score = snapshot["score"]
    closure_request.decision_ai_confidence = snapshot["confidence"]
    closure_request.decision_ai_model_version = snapshot["model_version"]
    closure_request.decision_ai_recommendation = snapshot["recommendation"]
    closure_request.decision_ai_explanation = snapshot["explanation"]
    closure_request.decision_ai_scorer_kind = snapshot["scorer_kind"]
    closure_request.decision_ai_evaluated_at = (
        closure_request.finalized_at
        or closure_request.rejected_at
        or closure_request.updated_at
        or closure_request.created_at
    )


def backfill_urgent_closure_ai_snapshots(apps, schema_editor):
    VacationUrgentClosureRequest = apps.get_model("leave", "VacationUrgentClosureRequest")
    update_fields = [
        "ai_score",
        "ai_confidence",
        "ai_model_version",
        "ai_recommendation",
        "ai_explanation",
        "ai_scorer_kind",
        "ai_evaluated_at",
        "decision_ai_score",
        "decision_ai_confidence",
        "decision_ai_model_version",
        "decision_ai_recommendation",
        "decision_ai_explanation",
        "decision_ai_scorer_kind",
        "decision_ai_evaluated_at",
    ]
    closure_requests = VacationUrgentClosureRequest.objects.filter(
        models.Q(status__in=ACTIVE_STATUSES, ai_score__isnull=True)
        | models.Q(status__in=TERMINAL_STATUSES, decision_ai_score__isnull=True)
    )
    for closure_request in closure_requests.iterator():
        if closure_request.ai_score is None:
            active_snapshot = _snapshot_from_saved_risk(
                closure_request,
                model_version="urgent-closure-active-backfill-v1",
            )
            _apply_active_snapshot(closure_request, active_snapshot)
        if closure_request.status in TERMINAL_STATUSES and closure_request.decision_ai_score is None:
            decision_snapshot = _snapshot_from_saved_risk(
                closure_request,
                model_version="urgent-closure-decision-backfill-v1",
            )
            _apply_decision_snapshot(closure_request, decision_snapshot)
        closure_request.save(update_fields=update_fields)


class Migration(migrations.Migration):
    dependencies = [
        ("leave", "0029_backfill_schedule_change_decision_ai"),
    ]

    operations = [
        migrations.AddField(
            model_name="vacationurgentclosurerequest",
            name="ai_confidence",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=5,
                null=True,
                verbose_name="Уверенность ИИ",
            ),
        ),
        migrations.AddField(
            model_name="vacationurgentclosurerequest",
            name="ai_evaluated_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Дата оценки ИИ"),
        ),
        migrations.AddField(
            model_name="vacationurgentclosurerequest",
            name="ai_explanation",
            field=models.TextField(blank=True, default="", verbose_name="Пояснение ИИ"),
        ),
        migrations.AddField(
            model_name="vacationurgentclosurerequest",
            name="ai_model_version",
            field=models.CharField(blank=True, default="", max_length=80, verbose_name="Версия ИИ-модели"),
        ),
        migrations.AddField(
            model_name="vacationurgentclosurerequest",
            name="ai_recommendation",
            field=models.CharField(blank=True, default="", max_length=32, verbose_name="Рекомендация ИИ"),
        ),
        migrations.AddField(
            model_name="vacationurgentclosurerequest",
            name="ai_score",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=6,
                null=True,
                verbose_name="Оценка ИИ",
            ),
        ),
        migrations.AddField(
            model_name="vacationurgentclosurerequest",
            name="ai_scorer_kind",
            field=models.CharField(blank=True, default="", max_length=32, verbose_name="Тип ИИ-оценки"),
        ),
        migrations.AddField(
            model_name="vacationurgentclosurerequest",
            name="decision_ai_confidence",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=5,
                null=True,
                verbose_name="Уверенность ИИ на момент решения",
            ),
        ),
        migrations.AddField(
            model_name="vacationurgentclosurerequest",
            name="decision_ai_evaluated_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Дата оценки ИИ при решении"),
        ),
        migrations.AddField(
            model_name="vacationurgentclosurerequest",
            name="decision_ai_explanation",
            field=models.TextField(blank=True, default="", verbose_name="Пояснение ИИ на момент решения"),
        ),
        migrations.AddField(
            model_name="vacationurgentclosurerequest",
            name="decision_ai_model_version",
            field=models.CharField(
                blank=True,
                default="",
                max_length=80,
                verbose_name="Версия ИИ-модели на момент решения",
            ),
        ),
        migrations.AddField(
            model_name="vacationurgentclosurerequest",
            name="decision_ai_recommendation",
            field=models.CharField(
                blank=True,
                default="",
                max_length=32,
                verbose_name="Рекомендация ИИ на момент решения",
            ),
        ),
        migrations.AddField(
            model_name="vacationurgentclosurerequest",
            name="decision_ai_score",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=6,
                null=True,
                verbose_name="Оценка ИИ на момент решения",
            ),
        ),
        migrations.AddField(
            model_name="vacationurgentclosurerequest",
            name="decision_ai_scorer_kind",
            field=models.CharField(
                blank=True,
                default="",
                max_length=32,
                verbose_name="Тип ИИ-оценки на момент решения",
            ),
        ),
        migrations.AddConstraint(
            model_name="vacationurgentclosurerequest",
            constraint=models.CheckConstraint(
                check=models.Q(("ai_score__isnull", True))
                | (models.Q(("ai_score__gte", 0)) & models.Q(("ai_score__lte", 100))),
                name="urgent_closure_ai_score_0_100",
            ),
        ),
        migrations.AddConstraint(
            model_name="vacationurgentclosurerequest",
            constraint=models.CheckConstraint(
                check=models.Q(("ai_confidence__isnull", True))
                | (models.Q(("ai_confidence__gte", 0)) & models.Q(("ai_confidence__lte", 100))),
                name="urgent_closure_ai_confidence_0_100",
            ),
        ),
        migrations.AddConstraint(
            model_name="vacationurgentclosurerequest",
            constraint=models.CheckConstraint(
                check=models.Q(("decision_ai_score__isnull", True))
                | (models.Q(("decision_ai_score__gte", 0)) & models.Q(("decision_ai_score__lte", 100))),
                name="urgent_closure_decision_ai_score_0_100",
            ),
        ),
        migrations.AddConstraint(
            model_name="vacationurgentclosurerequest",
            constraint=models.CheckConstraint(
                check=models.Q(("decision_ai_confidence__isnull", True))
                | (
                    models.Q(("decision_ai_confidence__gte", 0))
                    & models.Q(("decision_ai_confidence__lte", 100))
                ),
                name="urgent_closure_decision_ai_conf_0_100",
            ),
        ),
        migrations.RunPython(backfill_urgent_closure_ai_snapshots, migrations.RunPython.noop),
    ]
