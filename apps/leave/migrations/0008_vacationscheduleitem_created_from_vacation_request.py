import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("leave", "0007_vacationscheduleitem_created_from_change_request"),
    ]

    operations = [
        migrations.AddField(
            model_name="vacationscheduleitem",
            name="created_from_vacation_request",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="created_schedule_items",
                to="leave.vacationrequest",
                verbose_name="Создан из оплачиваемой заявки",
            ),
        ),
    ]
