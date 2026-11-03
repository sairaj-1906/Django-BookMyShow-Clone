from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from movies.catalog_integrations import (
    CatalogIntegrationError,
    get_tmdb_movie,
    search_tmdb_movies,
    tmdb_poster_url,
)
from movies.models import Genre, Language, Movie


class Command(BaseCommand):
    help = "Import or refresh movie metadata from TMDb by title search."

    def add_arguments(self, parser):
        parser.add_argument("--query", required=True, help="Movie title to search for")
        parser.add_argument(
            "--limit", type=int, default=5, help="Maximum results to import (1-20)"
        )

    def handle(self, *args, **options):
        limit = options["limit"]
        if not 1 <= limit <= 20:
            raise CommandError("--limit must be between 1 and 20.")

        try:
            results = search_tmdb_movies(options["query"])[:limit]
            if not results:
                self.stdout.write(self.style.WARNING("TMDb returned no matching movies."))
                return

            imported = 0
            for result in results:
                details = get_tmdb_movie(result["id"])
                self._save_movie(details)
                imported += 1
                self.stdout.write(f"Imported {details['title']} (TMDb {details['id']})")
        except (CatalogIntegrationError, KeyError, TypeError, ValueError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(f"Imported {imported} TMDb movie(s)."))

    @staticmethod
    def _save_movie(details):
        release_date = None
        if details.get("release_date"):
            try:
                release_date = date.fromisoformat(details["release_date"])
            except ValueError:
                release_date = None

        movie, _ = Movie.objects.update_or_create(
            tmdb_id=details["id"],
            defaults={
                "name": details.get("title") or details["original_title"],
                "tagline": details.get("tagline", ""),
                "image": "",
                "poster_url": tmdb_poster_url(details.get("poster_path")),
                "description": details.get("overview", ""),
                "duration_minutes": details.get("runtime") or 0,
                "release_date": release_date or timezone.localdate(),
                "rating": Decimal(str(details.get("vote_average") or 0)).quantize(
                    Decimal("0.1")
                ),
                "cast": ", ".join(
                    person["name"]
                    for person in details.get("credits", {}).get("cast", [])[:8]
                    if person.get("name")
                ),
            },
        )

        genres = [
            Genre.objects.get_or_create(name=genre["name"])[0]
            for genre in details.get("genres", [])
            if genre.get("name")
        ]
        movie.genres.set(genres)

        language_code = details.get("original_language", "")
        if language_code:
            language, _ = Language.objects.get_or_create(
                code=language_code,
                defaults={"name": language_code.upper()},
            )
            movie.languages.add(language)

        videos = details.get("videos", {}).get("results", [])
        trailer = next(
            (
                video["key"]
                for video in videos
                if video.get("site") == "YouTube"
                and video.get("type") == "Trailer"
                and video.get("key")
            ),
            "",
        )
        if trailer:
            movie.trailer_youtube_id = trailer
            movie.save(update_fields=["trailer_youtube_id"])
