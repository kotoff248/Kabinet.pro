import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("employees", "0013_substitution_rule_capacity"),
        ("leave", "0020_vacationschedule_manual_suggestion_cache"),
    ]

    operations = [
        migrations.CreateModel(
            name="VacationScheduleAutoPlaceJob",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token", models.CharField(max_length=96, unique=True, verbose_name="Токен статуса")),
                ("year", models.PositiveIntegerField(verbose_name="Год планирования")),
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
                ("placed_count", models.PositiveIntegerField(default=0, verbose_name="Размещено пунктов")),
                ("unresolved_count", models.PositiveIntegerField(default=0, verbose_name="Осталось вручную")),
                ("processed_employees", models.PositiveIntegerField(default=0, verbose_name="Обработано сотрудников")),
                ("total_employees", models.PositiveIntegerField(default=0, verbose_name="Всего сотрудников")),
                ("process_id", models.PositiveIntegerField(blank=True, null=True, verbose_name="PID процесса")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Дата создания")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Дата обновления")),
                ("started_at", models.DateTimeField(blank=True, null=True, verbose_name="Дата запуска")),
                ("finished_at", models.DateTimeField(blank=True, null=True, verbose_name="Дата завершения")),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="vacation_schedule_auto_place_jobs",
                        to="employees.employees",
                        verbose_name="Инициатор",
                    ),
                ),
                (
                    "schedule",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="auto_place_jobs",
                        to="leave.vacationschedule",
                        verbose_name="Черновик графика",
                    ),
                ),
            ],
            options={
                "verbose_name": "Фоновый автодобор графика",
                "verbose_name_plural": "Фоновые автодоборы графика",
                "db_table": "leave_vacationschedule_autoplacejob",
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="vacationscheduleautoplacejob",
            index=models.Index(fields=["year", "status"], name="leave_auto_year_status_idx"),
        ),
        migrations.AddIndex(
            model_name="vacationscheduleautoplacejob",
            index=models.Index(fields=["schedule", "status"], name="leave_auto_schedule_status_idx"),
        ),
        migrations.AddIndex(
            model_name="vacationscheduleautoplacejob",
            index=models.Index(fields=["token"], name="leave_auto_token_idx"),
        ),
    ]
