# Generated by Django 3.2.14 on 2022-07-11 16:33

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("nautobot_golden_config", "0020_convert_dynamicgroup_part_2"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="goldenconfigsetting",
            name="scope",
        ),
        migrations.AlterField(
            model_name="goldenconfigsetting",
            name="dynamic_group",
            field=models.OneToOneField(
                on_delete=models.deletion.PROTECT, related_name="golden_config_setting", to="extras.dynamicgroup"
            ),
        ),
    ]
