from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0007_demodataresetjob_preset_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="demodataresetjob",
            name="history_years",
            field=models.PositiveSmallIntegerField(default=3, verbose_name="Лет истории"),
        ),
    ]
