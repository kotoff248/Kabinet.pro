from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("leave", "0030_urgent_closure_ai_support"),
    ]

    operations = [
        migrations.AddField(
            model_name="vacationpreference",
            name="ai_score",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=5,
                null=True,
                verbose_name="Оценка ИИ",
            ),
        ),
        migrations.AddField(
            model_name="vacationpreference",
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
            model_name="vacationpreference",
            name="ai_model_version",
            field=models.CharField(blank=True, default="", max_length=64, verbose_name="Версия ИИ-модели"),
        ),
        migrations.AddField(
            model_name="vacationpreference",
            name="ai_recommendation",
            field=models.CharField(blank=True, default="", max_length=32, verbose_name="Рекомендация ИИ"),
        ),
        migrations.AddField(
            model_name="vacationpreference",
            name="ai_explanation",
            field=models.TextField(blank=True, default="", verbose_name="Пояснение ИИ"),
        ),
        migrations.AddField(
            model_name="vacationpreference",
            name="ai_scorer_kind",
            field=models.CharField(blank=True, default="", max_length=32, verbose_name="Тип ИИ-оценки"),
        ),
        migrations.AddField(
            model_name="vacationpreference",
            name="ai_evaluated_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Дата ИИ-оценки"),
        ),
        migrations.AddConstraint(
            model_name="vacationpreference",
            constraint=models.CheckConstraint(
                check=models.Q(("ai_score__isnull", True))
                | (models.Q(("ai_score__gte", 0)) & models.Q(("ai_score__lte", 100))),
                name="vacation_preference_ai_score_0_100",
            ),
        ),
        migrations.AddConstraint(
            model_name="vacationpreference",
            constraint=models.CheckConstraint(
                check=models.Q(("ai_confidence__isnull", True))
                | (models.Q(("ai_confidence__gte", 0)) & models.Q(("ai_confidence__lte", 100))),
                name="vacation_preference_ai_conf_0_100",
            ),
        ),
    ]
