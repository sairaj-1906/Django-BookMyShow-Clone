import json
from io import BytesIO
from unittest.mock import patch
from urllib.parse import parse_qs

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .models import Movie
from .places import OpenStreetMapClient, OpenStreetMapError


class OpenStreetMapClientTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    @patch("movies.places.urlopen")
    def test_search_geocodes_then_finds_nearest_cinemas(self, urlopen):
        geocoding_response = BytesIO(
            json.dumps([{"lat": "18.5204", "lon": "73.8567"}]).encode()
        )
        cinema_response = BytesIO(
            json.dumps(
                {
                    "elements": [
                        {
                            "type": "way",
                            "center": {"lat": 18.53, "lon": 73.86},
                            "tags": {
                                "amenity": "cinema",
                                "name": "Central Cinema",
                                "addr:street": "Main Street",
                                "addr:city": "Pune",
                            },
                        },
                        {
                            "type": "node",
                            "lat": 18.52,
                            "lon": 73.85,
                            "tags": {
                                "amenity": "cinema",
                                "name": "Closest Cinema",
                            },
                        },
                        {
                            "type": "node",
                            "lat": 18.52,
                            "lon": 73.85,
                            "tags": {"amenity": "cinema"},
                        },
                    ]
                }
            ).encode()
        )
        urlopen.return_value.__enter__.side_effect = [
            geocoding_response,
            cinema_response,
        ]

        cinemas = OpenStreetMapClient().search_cinemas("Pune")

        self.assertEqual(len(cinemas), 2)
        self.assertEqual(cinemas[0]["name"], "Closest Cinema")
        self.assertEqual(cinemas[1]["address"], "Main Street, Pune")
        self.assertIn("openstreetmap.org/?mlat=", cinemas[0]["maps_url"])
        self.assertGreaterEqual(cinemas[1]["distance_km"], cinemas[0]["distance_km"])
        geocode_request = urlopen.call_args_list[0].args[0]
        self.assertIn("nominatim.openstreetmap.org/search", geocode_request.full_url)
        self.assertEqual(
            geocode_request.get_header("User-agent"),
            OpenStreetMapClient.USER_AGENT,
        )
        overpass_request = urlopen.call_args_list[1].args[0]
        query = parse_qs(overpass_request.data.decode())["data"][0]
        self.assertIn('["amenity"="cinema"]', query)
        self.assertIn("around:15000", query)

    @patch("movies.places.urlopen")
    def test_search_results_are_cached(self, urlopen):
        geocoding_response = BytesIO(json.dumps([]).encode())
        urlopen.return_value.__enter__.return_value = geocoding_response
        client = OpenStreetMapClient()

        self.assertEqual(client.search_cinemas("Pune"), [])
        self.assertEqual(client.search_cinemas("  PUNE  "), [])

        urlopen.assert_called_once()

    @patch("movies.places.urlopen")
    def test_uncached_geocoding_is_throttled(self, urlopen):
        cache.set("osm-cinema:nominatim:last-request", True, timeout=30)
        with self.assertRaisesMessage(OpenStreetMapError, "wait a moment"):
            OpenStreetMapClient().search_cinemas("Pune")
        urlopen.assert_not_called()

    @patch("movies.places.urlopen")
    def test_invalid_coordinates_are_rejected(self, urlopen):
        response = BytesIO(json.dumps([{"lat": "900", "lon": "73"}]).encode())
        urlopen.return_value.__enter__.return_value = response
        with self.assertRaisesMessage(OpenStreetMapError, "out-of-range"):
            OpenStreetMapClient().search_cinemas("Pune")


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
                "distance_km": 1.2,
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
