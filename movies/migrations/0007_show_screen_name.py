from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("movies", "0006_discovery_models_and_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="show",
            name="screen_name",
            field=models.CharField(default="Screen 1", max_length=100),
        ),
    ]
