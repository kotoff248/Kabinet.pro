from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("employees", "0013_substitution_rule_capacity"),
        ("leave", "0025_vacationplanningcycle"),
    ]

    operations = [
        migrations.CreateModel(
            name="VacationNeuralTrainingJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token", models.CharField(max_length=96, unique=True, verbose_name="Токен статуса")),
                ("year", models.PositiveIntegerField(verbose_name="Год графика")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "В очереди"),
                            ("running", "Выполняется"),
                            ("succeeded", "Завершено"),
                            ("failed", "Ошибка"),
                        ],
                        default="queued",
                        max_length=16,
                        verbose_name="Статус",
                    ),
                ),
                ("progress_percent", models.PositiveSmallIntegerField(default=0, verbose_name="Прогресс")),
                ("stage_label", models.CharField(blank=True, default="", max_length=160, verbose_name="Этап")),
                ("message", models.TextField(blank=True, default="", verbose_name="Сообщение")),
                ("error_message", models.TextField(blank=True, default="", verbose_name="Ошибка")),
                (
                    "candidate_version",
                    models.CharField(
                        default="vacation-candidate-mlp-v2",
                        max_length=80,
                        verbose_name="Версия v2",
                    ),
                ),
                (
                    "package_version",
                    models.CharField(
                        default="vacation-package-ranker-v3",
                        max_length=80,
                        verbose_name="Версия v3",
                    ),
                ),
                ("metrics_payload", models.JSONField(blank=True, default=dict, verbose_name="Метрики обучения")),
                ("source_fingerprint", models.CharField(blank=True, default="", max_length=64, verbose_name="Отпечаток данных")),
                ("process_id", models.PositiveIntegerField(blank=True, null=True, verbose_name="PID процесса")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Дата обновления")),
                ("started_at", models.DateTimeField(blank=True, null=True, verbose_name="Дата запуска")),
                ("finished_at", models.DateTimeField(blank=True, null=True, verbose_name="Дата завершения")),
                (
                    "started_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="vacation_neural_training_jobs",
                        to="employees.employees",
                        verbose_name="Запустил",
                    ),
                ),
            ],
            options={
                "verbose_name": "Фоновое обучение нейромодуля",
                "verbose_name_plural": "Фоновые обучения нейромодуля",
                "db_table": "leave_vacation_neural_training_job",
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="vacationneuraltrainingjob",
            index=models.Index(fields=["year", "status"], name="leave_neural_year_status_idx"),
        ),
        migrations.AddIndex(
            model_name="vacationneuraltrainingjob",
            index=models.Index(fields=["token"], name="leave_neural_token_idx"),
        ),
    ]
