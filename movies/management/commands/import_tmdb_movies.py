from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from movies.models import Genre, Language, Movie
from movies.tmdb import TMDBClient, TMDBError


LANGUAGE_NAMES = {
    "bn": "Bengali",
    "en": "English",
    "gu": "Gujarati",
    "hi": "Hindi",
    "kn": "Kannada",
    "ml": "Malayalam",
    "mr": "Marathi",
    "pa": "Punjabi",
    "ta": "Tamil",
    "te": "Telugu",
    "ur": "Urdu",
}
POSTER_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


class Command(BaseCommand):
    help = "Import or update movies currently playing in India from TMDB."

    def add_arguments(self, parser):
        parser.add_argument(
            "--pages",
            type=int,
            default=1,
            help="Number of now-playing result pages to import (20 movies per page; 1-500).",
        )
        parser.add_argument(
            "--region",
            default="IN",
            help="Two-letter TMDB region code (default: IN).",
        )
        parser.add_argument(
            "--language",
            default="en-US",
            help="TMDB metadata language (default: en-US).",
        )

    def handle(self, *args, **options):
        pages = options["pages"]
        region = options["region"].upper()
        language = options["language"]
        if not 1 <= pages <= 500:
            raise CommandError("--pages must be between 1 and 500.")
        if len(region) != 2 or not region.isalpha():
            raise CommandError("--region must be a two-letter region code.")

        try:
            client = TMDBClient()
            genre_data = client.get_json(
                "genre/movie/list", {"language": language}
            )
            genres = {
                item["id"]: Genre.objects.get_or_create(name=item["name"])[0]
                for item in genre_data.get("genres", [])
                if item.get("id") and item.get("name")
            }
            imported = updated = skipped = 0
            for page in range(1, pages + 1):
                result = client.get_json(
                    "movie/now_playing",
                    {"language": language, "region": region, "page": page},
                )
                for item in result.get("results", []):
                    outcome = self._import_movie(client, item, genres)
                    if outcome == "created":
                        imported += 1
                    elif outcome == "updated":
                        updated += 1
                    else:
                        skipped += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"TMDB import complete: {imported} imported, "
                    f"{updated} updated, {skipped} skipped."
                )
            )
        except TMDBError as exc:
            raise CommandError(str(exc)) from exc

    def _import_movie(self, client, item, genres):
        tmdb_id = item.get("id")
        title = item.get("title")
        release_date = self._release_date(item.get("release_date"))
        if not tmdb_id or not title or release_date is None:
            self.stderr.write(
                self.style.WARNING("Skipped a TMDB movie with incomplete metadata.")
            )
            return "skipped"

        movie = Movie.objects.filter(tmdb_id=tmdb_id).first()
        if movie is None:
            movie = Movie.objects.filter(
                Q(tmdb_id__isnull=True),
                name__iexact=title[:255],
                release_date=release_date,
            ).first()
        created = movie is None
        if created:
            movie = Movie()

        old_image_name = movie.image.name if movie.image else ""
        poster_path = item.get("poster_path")
        if poster_path:
            try:
                content, content_type = client.get_poster(poster_path)
            except TMDBError as exc:
                if created or not movie.image:
                    self.stderr.write(
                        self.style.WARNING(f"Skipped {title}: {exc}")
                    )
                    return "skipped"
                self.stderr.write(
                    self.style.WARNING(f"Keeping the existing poster for {title}: {exc}")
                )
            else:
                extension = POSTER_EXTENSIONS[content_type]
                movie.image.save(
                    f"tmdb/{tmdb_id}{extension}",
                    ContentFile(content),
                    save=False,
                )
        elif created or not movie.image:
            self.stderr.write(
                self.style.WARNING(f"Skipped {title}: TMDB has no poster.")
            )
            return "skipped"

        movie.tmdb_id = tmdb_id
        movie.name = title[:255]
        movie.description = item.get("overview", "")
        movie.release_date = release_date
        movie.rating = self._rating(item.get("vote_average"))
        self._save_movie(movie, old_image_name)

        movie.genres.set(
            [genres[genre_id] for genre_id in item.get("genre_ids", []) if genre_id in genres]
        )
        self._set_original_language(movie, item.get("original_language"))
        return "created" if created else "updated"

    @staticmethod
    def _save_movie(movie, old_image_name):
        movie.save()
        if (
            old_image_name
            and old_image_name.startswith("movies/tmdb/")
            and old_image_name != movie.image.name
        ):
            default_storage.delete(old_image_name)

    @staticmethod
    def _release_date(value):
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _rating(value):
        try:
            return Decimal(str(value)).quantize(
                Decimal("0.1"), rounding=ROUND_HALF_UP
            )
        except (InvalidOperation, TypeError, ValueError):
            return Decimal("0")

    @staticmethod
    def _set_original_language(movie, code):
        if not code:
            return
        language = Language.objects.filter(code=code).first()
        if language is None:
            language = Language.objects.create(
                name=LANGUAGE_NAMES.get(code, code.upper()),
                code=code,
            )
        movie.languages.add(language)
