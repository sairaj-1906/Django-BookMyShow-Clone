from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from .management.commands.seed_sample_catalog import MOVIES, SEAT_NUMBERS, THEATERS
from .models import Movie, Seat, Show, Theater


class SampleCatalogCommandTests(TestCase):
    @patch(
        "movies.management.commands.seed_sample_catalog.make_poster",
        return_value="sample/poster.jpg",
    )
    def test_command_creates_browseable_sample_catalog_and_is_idempotent(self, poster):
        call_command("seed_sample_catalog", stdout=StringIO())
        call_command("seed_sample_catalog", stdout=StringIO())

        self.assertEqual(Movie.objects.count(), len(MOVIES))
        self.assertEqual(Theater.objects.count(), len(THEATERS))
        self.assertEqual(Show.objects.count(), len(MOVIES) * 3)
        self.assertEqual(Seat.objects.count(), len(MOVIES) * 3 * len(SEAT_NUMBERS))
        self.assertFalse(Show.objects.filter(date_time__lte=timezone.now()).exists())
        self.assertTrue(
            Movie.objects.filter(tagline__startswith="SAMPLE DATA").exists()
        )
        self.assertTrue(
            Theater.objects.filter(name__startswith="Demo Cinema").exists()
        )
        self.assertEqual(poster.call_count, len(MOVIES))

