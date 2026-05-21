from django.db import migrations, models


def backfill_schedule_change_decision_ai_snapshots(apps, schema_editor):
    VacationScheduleChangeRequest = apps.get_model("leave", "VacationScheduleChangeRequest")
    reviewed_changes = VacationScheduleChangeRequest.objects.exclude(status="pending").filter(
        decision_ai_score__isnull=True,
        ai_score__isnull=False,
    )
    for change_request in reviewed_changes.iterator():
        change_request.decision_ai_score = change_request.ai_score
        change_request.decision_ai_confidence = change_request.ai_confidence
        change_request.decision_ai_model_version = change_request.ai_model_version
        change_request.decision_ai_recommendation = change_request.ai_recommendation
        change_request.decision_ai_explanation = change_request.ai_explanation
        change_request.decision_ai_scorer_kind = change_request.ai_scorer_kind
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
        ("leave", "0027_schedule_change_ai_support"),
    ]

    operations = [
        migrations.AddField(
            model_name="vacationschedulechangerequest",
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
            model_name="vacationschedulechangerequest",
            name="decision_ai_evaluated_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Дата оценки ИИ при решении"),
        ),
        migrations.AddField(
            model_name="vacationschedulechangerequest",
            name="decision_ai_explanation",
            field=models.TextField(blank=True, default="", verbose_name="Пояснение ИИ на момент решения"),
        ),
        migrations.AddField(
            model_name="vacationschedulechangerequest",
            name="decision_ai_model_version",
            field=models.CharField(
                blank=True,
                default="",
                max_length=80,
                verbose_name="Версия ИИ-модели на момент решения",
            ),
        ),
        migrations.AddField(
            model_name="vacationschedulechangerequest",
            name="decision_ai_recommendation",
            field=models.CharField(
                blank=True,
                default="",
                max_length=32,
                verbose_name="Рекомендация ИИ на момент решения",
            ),
        ),
        migrations.AddField(
            model_name="vacationschedulechangerequest",
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
            model_name="vacationschedulechangerequest",
            name="decision_ai_scorer_kind",
            field=models.CharField(
                blank=True,
                default="",
                max_length=32,
                verbose_name="Тип ИИ-оценки на момент решения",
            ),
        ),
        migrations.AddConstraint(
            model_name="vacationschedulechangerequest",
            constraint=models.CheckConstraint(
                check=models.Q(("decision_ai_score__isnull", True))
                | (models.Q(("decision_ai_score__gte", 0)) & models.Q(("decision_ai_score__lte", 100))),
                name="schedule_change_decision_ai_score_0_100",
            ),
        ),
        migrations.AddConstraint(
            model_name="vacationschedulechangerequest",
            constraint=models.CheckConstraint(
                check=models.Q(("decision_ai_confidence__isnull", True))
                | (
                    models.Q(("decision_ai_confidence__gte", 0))
                    & models.Q(("decision_ai_confidence__lte", 100))
                ),
                name="schedule_change_decision_ai_confidence_0_100",
            ),
        ),
        migrations.RunPython(backfill_schedule_change_decision_ai_snapshots, migrations.RunPython.noop),
    ]
