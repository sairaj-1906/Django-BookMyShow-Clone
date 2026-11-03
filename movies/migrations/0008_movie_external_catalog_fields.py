from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("movies", "0007_show_screen_name"),
    ]

    operations = [
        migrations.AlterField(
            model_name="movie",
            name="image",
            field=models.ImageField(
                blank=True, help_text="Primary poster image", upload_to="movies/"
            ),
        ),
        migrations.AddField(
            model_name="movie",
            name="poster_url",
            field=models.URLField(blank=True, help_text="External poster URL supplied by TMDb"),
        ),
        migrations.AddField(
            model_name="movie",
            name="tmdb_id",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Movie Database (TMDb) movie ID",
                null=True,
                unique=True,
            ),
        ),
    ]
