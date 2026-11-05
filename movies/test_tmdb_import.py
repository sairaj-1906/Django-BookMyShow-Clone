from io import StringIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings

from .models import Genre, Language, Movie
from .tmdb import TMDBClient, TMDBError


class TMDBImportCommandTests(TestCase):
    def setUp(self):
        self.media_dir = TemporaryDirectory()
        self.media_settings = override_settings(MEDIA_ROOT=self.media_dir.name)
        self.media_settings.enable()
        self.client_patcher = patch(
            "movies.management.commands.import_tmdb_movies.TMDBClient"
        )
        self.client_class = self.client_patcher.start()
        self.tmdb = self.client_class.return_value
        self.tmdb.get_json.side_effect = [
            {"genres": [{"id": 18, "name": "Drama"}]},
            {
                "results": [
                    {
                        "id": 12345,
                        "title": "India Film",
                        "release_date": "2026-10-25",
                        "overview": "A story from India.",
                        "vote_average": 8.26,
                        "genre_ids": [18],
                        "original_language": "hi",
                        "poster_path": "/poster.jpg",
                    }
                ]
            },
        ]
        self.tmdb.get_poster.return_value = (b"poster-bytes", "image/jpeg")

    def tearDown(self):
        self.client_patcher.stop()
        self.media_settings.disable()
        self.media_dir.cleanup()

    def test_import_adds_movie_metadata_genre_language_and_poster(self):
        output = StringIO()
        call_command("import_tmdb_movies", stdout=output)

        movie = Movie.objects.get(tmdb_id=12345)
        self.assertEqual(movie.name, "India Film")
        self.assertEqual(movie.description, "A story from India.")
        self.assertEqual(str(movie.rating), "8.3")
        self.assertEqual(list(movie.genres.values_list("name", flat=True)), ["Drama"])
        self.assertEqual(list(movie.languages.values_list("code", flat=True)), ["hi"])
        self.assertEqual(movie.image.name, "movies/tmdb/12345.jpg")
        self.assertIn("1 imported, 0 updated, 0 skipped", output.getvalue())

    def test_import_is_idempotent_and_updates_existing_tmdb_movie(self):
        call_command("import_tmdb_movies", stdout=StringIO())
        self.tmdb.get_json.side_effect = [
            {"genres": [{"id": 18, "name": "Drama"}]},
            {
                "results": [
                    {
                        "id": 12345,
                        "title": "India Film Updated",
                        "release_date": "2026-10-25",
                        "overview": "Updated synopsis.",
                        "vote_average": 7.0,
                        "genre_ids": [18],
                        "original_language": "hi",
                        "poster_path": "/poster.jpg",
                    }
                ]
            },
        ]

        output = StringIO()
        call_command("import_tmdb_movies", stdout=output)

        self.assertEqual(Movie.objects.filter(tmdb_id=12345).count(), 1)
        movie = Movie.objects.get(tmdb_id=12345)
        self.assertEqual(movie.name, "India Film Updated")
        self.assertEqual(movie.description, "Updated synopsis.")
        self.assertEqual(Genre.objects.count(), 1)
        self.assertEqual(Language.objects.count(), 1)
        self.assertIn("0 imported, 1 updated, 0 skipped", output.getvalue())

    def test_command_rejects_invalid_page_count(self):
        with self.assertRaises(CommandError):
            call_command("import_tmdb_movies", pages=0)

    def test_tmdb_credentials_are_required(self):
        with override_settings(TMDB_API_KEY="", TMDB_ACCESS_TOKEN=""):
            with self.assertRaisesMessage(TMDBError, "Set TMDB_ACCESS_TOKEN"):
                TMDBClient()
