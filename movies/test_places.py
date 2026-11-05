import json
from io import BytesIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .models import Movie
from .places import OpenStreetMapClient, OpenStreetMapError


class OpenStreetMapClientTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    @patch("movies.places.urlopen")
    def test_search_returns_cinema_places_with_attribution_links(self, urlopen):
        response = BytesIO(
            json.dumps(
                [
                    {
                        "category": "amenity",
                        "type": "cinema",
                        "osm_type": "node",
                        "osm_id": 123,
                        "name": "Central Cinema",
                        "lat": "18.5204",
                        "lon": "73.8567",
                        "address": {"road": "Main Street", "city": "Pune"},
                    },
                    {
                        "category": "amenity",
                        "type": "clinic",
                        "osm_type": "node",
                        "osm_id": 124,
                        "name": "Cinema Clinic",
                        "lat": "18.5205",
                        "lon": "73.8568",
                    },
                    {
                        "category": "amenity",
                        "type": "cinema",
                        "osm_type": "way",
                        "osm_id": 125,
                        "name": "Cinema without coordinates",
                    },
                ]
            ).encode()
        )
        urlopen.return_value.__enter__.return_value = response

        cinemas = OpenStreetMapClient().search_cinemas("Pune")

        self.assertEqual(len(cinemas), 1)
        self.assertEqual(cinemas[0]["name"], "Central Cinema")
        self.assertEqual(cinemas[0]["address"], "Main Street, Pune")
        self.assertIn("openstreetmap.org/?mlat=", cinemas[0]["maps_url"])
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.get_header("User-agent"), OpenStreetMapClient.USER_AGENT
        )
        query = parse_qs(urlparse(request.full_url).query)
        self.assertEqual(query["q"], ["cinema in pune, India"])
        self.assertEqual(query["countrycodes"], ["in"])
        self.assertEqual(query["limit"], ["30"])

    @patch("movies.places.urlopen")
    def test_search_results_are_cached(self, urlopen):
        urlopen.return_value.__enter__.return_value = BytesIO(b"[]")
        client = OpenStreetMapClient()

        self.assertEqual(client.search_cinemas("Pune"), [])
        self.assertEqual(client.search_cinemas("  PUNE  "), [])

        urlopen.assert_called_once()

    @patch("movies.places.urlopen")
    def test_uncached_requests_are_rate_limited(self, urlopen):
        cache.set("osm-cinema:nominatim:last-request", True, timeout=30)
        with self.assertRaisesMessage(OpenStreetMapError, "wait a moment"):
            OpenStreetMapClient().search_cinemas("Pune")
        urlopen.assert_not_called()

    @patch("movies.places.urlopen")
    def test_invalid_coordinate_results_are_ignored(self, urlopen):
        response = BytesIO(
            json.dumps(
                [
                    {
                        "category": "amenity",
                        "type": "cinema",
                        "name": "Bad coordinates",
                        "lat": "900",
                        "lon": "73",
                    }
                ]
            ).encode()
        )
        urlopen.return_value.__enter__.return_value = response
        self.assertEqual(OpenStreetMapClient().search_cinemas("Pune"), [])


class NearbyCinemaViewTests(TestCase):
    def setUp(self):
        self.movie = Movie.objects.create(name="Cinema Search Test", image="test.jpg")

    def test_search_requires_a_valid_location(self):
        url = reverse("nearby_cinemas")
        self.assertEqual(self.client.get(url).status_code, 400)
        self.assertEqual(self.client.get(url, {"location": "x"}).status_code, 400)
        self.assertEqual(
            self.client.get(url, {"location": "Pune\n"}).status_code, 400
        )

    @patch("movies.views.OpenStreetMapClient")
    def test_search_returns_cinemas_without_saving_results(self, client):
        client.return_value.search_cinemas.return_value = [
            {
                "name": "Central Cinema",
                "address": "Main Street, Pune",
                "maps_url": "https://www.openstreetmap.org/?mlat=18.5&mlon=73.8",
            }
        ]

        response = self.client.get(
            reverse("nearby_cinemas"), {"location": "Pune"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["cinemas"][0]["name"], "Central Cinema")
        self.assertEqual(client.return_value.search_cinemas.call_args.args, ("Pune",))

    def test_movie_showtimes_page_includes_openstreetmap_attribution(self):
        response = self.client.get(reverse("theater_list", args=[self.movie.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Find cinemas nearby")
        self.assertContains(response, "not BookMySeat showtimes")
        self.assertContains(response, "OpenStreetMap contributors")

    @patch("movies.views.OpenStreetMapClient")
    def test_service_errors_return_a_safe_retry_message(self, client):
        client.return_value.search_cinemas.side_effect = OpenStreetMapError(
            "upstream failure"
        )
        response = self.client.get(
            reverse("nearby_cinemas"), {"location": "Pune"}
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("temporarily unavailable", response.json()["error"])
