from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_alter_notification_event_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="demodataresetjob",
            name="preset",
            field=models.CharField(
                choices=[
                    ("standard", "Обычная демо-база"),
                    ("fast", "Быстрая демо-база"),
                    ("full", "Полная исследовательская база"),
                ],
                default="standard",
                max_length=24,
                verbose_name="Режим",
            ),
        ),
        migrations.AddField(
            model_name="demodataresetjob",
            name="history_years",
            field=models.PositiveSmallIntegerField(default=2, verbose_name="Лет истории"),
        ),
        migrations.AddField(
            model_name="demodataresetjob",
            name="employee_count",
            field=models.PositiveSmallIntegerField(default=50, verbose_name="Сотрудников"),
        ),
    ]
