import json
from datetime import datetime, timedelta, timezone as datetime_timezone
from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .catalog_integrations import (
    CatalogIntegrationError,
    find_nearby_theaters,
    search_tmdb_movies,
    tmdb_poster_url,
)
from .models import Language, Movie
from .showtime_providers import (
    ProviderShowtime,
    ShowtimeProvider,
    parse_provider_date,
    validate_provider_showtimes,
)


class FixtureShowtimeProvider(ShowtimeProvider):
    def search_showtimes(self, *, movie, latitude, longitude, show_date):
        return [
            ProviderShowtime(
                provider="Licensed Partner",
                external_id="partner-show-1",
                cinema_name="Partner Cinema",
                address="Mumbai",
                starts_at=datetime(2026, 11, 1, 15, tzinfo=datetime_timezone.utc),
                language="Hindi",
                screen_format="2D",
                booking_url="https://partner.example/book/partner-show-1",
                price=Decimal("250.00"),
            )
        ]


class CatalogClientTests(SimpleTestCase):
    @override_settings(TMDB_API_KEY="test-tmdb-key", TMDB_ACCESS_TOKEN="")
    @patch("movies.catalog_integrations.urlopen")
    def test_tmdb_search_uses_india_region_and_hides_api_key_in_headers(self, urlopen):
        response = urlopen.return_value.__enter__.return_value
        response.read.return_value = b'{"results":[{"id":42}]}'

        results = search_tmdb_movies("Example")

        self.assertEqual(results, [{"id": 42}])
        request = urlopen.call_args.args[0]
        self.assertIn("region=IN", request.full_url)
        self.assertIn("api_key=test-tmdb-key", request.full_url)
        self.assertEqual(request.get_method(), "GET")

    @override_settings(TMDB_API_KEY="", TMDB_ACCESS_TOKEN="secret-token")
    @patch("movies.catalog_integrations.urlopen")
    def test_tmdb_prefers_bearer_access_token(self, urlopen):
        response = urlopen.return_value.__enter__.return_value
        response.read.return_value = b'{"results":[]}'

        search_tmdb_movies("Example")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        self.assertNotIn("api_key", request.full_url)

    @override_settings(TMDB_API_KEY="", TMDB_ACCESS_TOKEN="")
    def test_tmdb_requires_credentials(self):
        with self.assertRaises(CatalogIntegrationError):
            search_tmdb_movies("Example")

    @override_settings(TMDB_API_KEY="test-tmdb-key", TMDB_ACCESS_TOKEN="")
    @patch("movies.catalog_integrations._tmdb_request")
    def test_tmdb_retries_search_without_india_release_filter(self, request):
        request.side_effect = [
            {"results": []},
            {"results": [{"id": 42, "title": "Global result"}]},
        ]

        results = search_tmdb_movies("Example")

        self.assertEqual(results, [{"id": 42, "title": "Global result"}])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].args[1]["region"], "IN")
        self.assertNotIn("region", request.call_args_list[1].args[1])

    @patch(
        "movies.catalog_integrations.urlopen",
        side_effect=HTTPError(
            "https://api.themoviedb.org/3/configuration",
            401,
            "Unauthorized",
            {},
            None,
        ),
    )
    def test_tmdb_http_error_explains_credentials(self, urlopen):
        from .catalog_integrations import _request_json

        with self.assertRaisesRegex(CatalogIntegrationError, "rejected the API credentials"):
            _request_json("https://api.themoviedb.org/3/configuration")

    @override_settings(
        OSM_OVERPASS_URL="https://overpass.test/api/interpreter",
        OSM_OVERPASS_TIMEOUT=12,
    )
    @patch("movies.catalog_integrations.urlopen")
    def test_overpass_search_normalizes_nearby_cinema_data(self, urlopen):
        response = urlopen.return_value.__enter__.return_value
        response.read.return_value = json.dumps(
            {
                "elements": [
                    {
                        "type": "way",
                        "id": 123,
                        "center": {"lat": 19.077, "lon": 72.878},
                        "tags": {
                            "name": "Central Cinema",
                            "addr:street": "Cinema Road",
                            "addr:city": "Mumbai",
                        },
                    },
                    {
                        "type": "node",
                        "id": 456,
                        "lat": 19.078,
                        "lon": 72.879,
                        "tags": {"name": "Second Cinema"},
                    },
                ]
            }
        ).encode()

        places = find_nearby_theaters(19.076, 72.8777)

        self.assertEqual([place["name"] for place in places], ["Central Cinema", "Second Cinema"])
        self.assertEqual(places[0]["address"], "Cinema Road, Mumbai")
        self.assertEqual(places[0]["osm_url"], "https://www.openstreetmap.org/way/123")
        self.assertGreater(places[0]["distance_meters"], 0)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://overpass.test/api/interpreter")
        self.assertEqual(
            request.get_header("User-agent"),
            "BookMySeat/1.0 (nearby cinema search; "
            "https://github.com/sairaj-1906/Django-BookMyShow-Clone)",
        )
        body = parse_qs(request.data.decode())
        self.assertIn('node["amenity"="cinema"](around:25000,19.076,72.8777);', body["data"][0])
        self.assertIn("out center tags 30;", body["data"][0])
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 12)

    @patch("movies.catalog_integrations.urlopen")
    def test_overpass_does_not_require_api_credentials(self, urlopen):
        response = urlopen.return_value.__enter__.return_value
        response.read.return_value = b'{"elements":[]}'

        self.assertEqual(find_nearby_theaters(19.076, 72.8777), [])
        request = urlopen.call_args.args[0]
        self.assertNotIn("api_key", request.full_url)

    def test_tmdb_poster_url_rejects_non_provider_paths(self):
        self.assertEqual(
            tmdb_poster_url("/abc.jpg"),
            "https://image.tmdb.org/t/p/w500/abc.jpg",
        )
        self.assertEqual(tmdb_poster_url("https://evil.example/x.jpg"), "")


class ShowtimeAdapterTests(SimpleTestCase):
    def test_adapter_requires_timezone_aware_time_and_https_booking_url(self):
        valid = ProviderShowtime(
            provider="Partner",
            external_id="show-123",
            cinema_name="Partner Cinema",
            address="Mumbai",
            starts_at=datetime.now(datetime_timezone.utc) + timedelta(days=1),
            language="Hindi",
            screen_format="2D",
            booking_url="https://partner.example/book/show-123",
            price=Decimal("250.00"),
        )
        self.assertEqual(validate_provider_showtimes([valid]), [valid])

        with self.assertRaises(ValueError):
            validate_provider_showtimes(
                [
                    ProviderShowtime(
                        **{
                            **valid.__dict__,
                            "starts_at": datetime.now(),
                        }
                    )
                ]
            )
        with self.assertRaises(ValueError):
            validate_provider_showtimes(
                [
                    ProviderShowtime(
                        **{**valid.__dict__, "booking_url": "javascript:alert(1)"}
                    )
                ]
            )
        with self.assertRaises(ValueError):
            validate_provider_showtimes(
                [
                    ProviderShowtime(
                        **{
                            **valid.__dict__,
                            "booking_url": "https://user:pass@partner.example/book",
                        }
                    )
                ]
            )

    def test_provider_date_validation(self):
        self.assertEqual(parse_provider_date("2026-10-31").isoformat(), "2026-10-31")
        with self.assertRaises(ValueError):
            parse_provider_date("tomorrow")


class NearbyTheaterViewTests(TestCase):
    @patch("movies.views.find_nearby_theaters", return_value=[])
    def test_nearby_cinema_search_requires_valid_browser_coordinates(self, search):
        response = self.client.get(reverse("nearby_theaters"), {"lat": "91", "lng": "72"})
        self.assertContains(response, "valid latitude and longitude")
        search.assert_not_called()

        response = self.client.get(
            reverse("nearby_theaters"), {"lat": "19.076", "lng": "72.8777"}
        )
        self.assertEqual(response.status_code, 200)
        search.assert_called_once_with(19.076, 72.8777)

    @patch(
        "movies.views.find_nearby_theaters",
        return_value=[
            {
                "name": "Central Cinema",
                "address": "Mumbai",
                "distance_meters": 250,
                "osm_url": "https://www.openstreetmap.org/way/123",
            }
        ],
    )
    def test_nearby_places_are_rendered_with_openstreetmap_attribution(self, search):
        response = self.client.get(
            reverse("nearby_theaters"), {"lat": "19.076", "lng": "72.8777"}
        )

        self.assertContains(response, "Central Cinema")
        self.assertContains(response, "View on OpenStreetMap")
        self.assertContains(response, "OpenStreetMap contributors")
        self.assertContains(response, "250 m away")
        search.assert_called_once()

    def test_nearby_search_does_not_require_provider_credentials(self):
        response = self.client.get(reverse("nearby_theaters"))
        self.assertNotContains(response, "not configured")
        self.assertNotContains(response, "OpenStreetMap contributors")


class TmdbImportCommandTests(TestCase):
    def test_command_imports_movie_metadata_and_can_be_repeated(self):
        search_results = [{"id": 123}]
        details = {
            "id": 123,
            "title": "Imported Movie",
            "original_title": "Imported Movie",
            "tagline": "A test movie",
            "overview": "Imported from the provider",
            "runtime": 128,
            "release_date": "2026-10-30",
            "vote_average": 7.8,
            "poster_path": "/poster.jpg",
            "original_language": "hi",
            "genres": [{"name": "Drama"}],
            "credits": {"cast": [{"name": "Actor One"}]},
            "videos": {
                "results": [
                    {"site": "YouTube", "type": "Trailer", "key": "abcdefghijk"}
                ]
            },
        }
        with (
            patch(
                "movies.management.commands.import_tmdb_movies.search_tmdb_movies",
                return_value=search_results,
            ),
            patch(
                "movies.management.commands.import_tmdb_movies.get_tmdb_movie",
                return_value=details,
            ),
        ):
            call_command("import_tmdb_movies", query="Imported Movie", stdout=StringIO())
            call_command("import_tmdb_movies", query="Imported Movie", stdout=StringIO())

        self.assertEqual(Movie.objects.filter(tmdb_id=123).count(), 1)
        movie = Movie.objects.get(tmdb_id=123)
        self.assertEqual(movie.duration_minutes, 128)
        self.assertEqual(movie.cast, "Actor One")
        self.assertEqual(movie.poster_url, "https://image.tmdb.org/t/p/w500/poster.jpg")
        self.assertEqual(movie.trailer_youtube_id, "abcdefghijk")
        self.assertTrue(movie.genres.filter(name="Drama").exists())
        self.assertTrue(Language.objects.filter(code="hi").exists())
        response = self.client.get(reverse("movie_detail", args=[movie.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This product uses the TMDB API")
        self.assertNotContains(response, "{{")
        self.assertNotContains(response, "{%")

    @override_settings(
        SHOWTIME_PROVIDER="movies.test_catalog_integrations.FixtureShowtimeProvider"
    )
    def test_configured_provider_showtimes_link_out_to_partner_checkout(self):
        movie = Movie.objects.create(name="Partner Movie", image="poster.jpg")
        response = self.client.get(
            reverse("theater_list", args=[movie.pk]),
            {"lat": "19.076", "lng": "72.8777", "show_date": "2026-11-01"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Partner Cinema")
        self.assertContains(
            response,
            'href="https://partner.example/book/partner-show-1"',
        )
        self.assertContains(response, "Book with Licensed Partner")
