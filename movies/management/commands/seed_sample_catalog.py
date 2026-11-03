from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import connection
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from movies.management.commands.seed_analytics_data import make_poster
from movies.models import Genre, Language, Movie, Seat, Show, Theater


MOVIES = [
    {
        "name": "The Monsoon Signal",
        "description": "A radio engineer follows a mysterious transmission across the Western Ghats.",
        "duration_minutes": 124,
        "age_certification": Movie.CERT_UA,
        "genres": ["Drama", "Mystery"],
        "language": ("Hindi", "hi"),
        "cast": "Aarav Mehta, Nisha Rao",
    },
    {
        "name": "Orbit of Tomorrow",
        "description": "A small research crew races to bring a stranded observatory home.",
        "duration_minutes": 137,
        "age_certification": Movie.CERT_UA,
        "genres": ["Sci-Fi", "Adventure"],
        "language": ("English", "en"),
        "cast": "Mira Sen, Kabir Anand",
    },
    {
        "name": "Chennai Blue",
        "description": "Two estranged siblings rebuild their family's neighborhood music hall.",
        "duration_minutes": 116,
        "age_certification": Movie.CERT_U,
        "genres": ["Drama", "Family"],
        "language": ("Tamil", "ta"),
        "cast": "Kavya Iyer, Rohan Das",
    },
    {
        "name": "The Last Banyan",
        "description": "A village botanist uncovers the story behind a centuries-old tree.",
        "duration_minutes": 109,
        "age_certification": Movie.CERT_U,
        "genres": ["Mystery", "Family"],
        "language": ("Hindi", "hi"),
        "cast": "Leela Kapoor, Dev Malhotra",
    },
    {
        "name": "Midnight Local",
        "description": "A night-shift train conductor helps a lost passenger find her way home.",
        "duration_minutes": 128,
        "age_certification": Movie.CERT_UA,
        "genres": ["Thriller", "Drama"],
        "language": ("Marathi", "mr"),
        "cast": "Anaya Kulkarni, Ishaan Patil",
    },
    {
        "name": "Paper Kite Club",
        "description": "Young friends turn a rooftop kite contest into a citywide celebration.",
        "duration_minutes": 101,
        "age_certification": Movie.CERT_U,
        "genres": ["Comedy", "Family"],
        "language": ("Telugu", "te"),
        "cast": "Tara Reddy, Neel Varma",
    },
    {
        "name": "Salt and Starlight",
        "description": "A coastal chef returns home to save a family restaurant.",
        "duration_minutes": 113,
        "age_certification": Movie.CERT_U,
        "genres": ["Romance", "Drama"],
        "language": ("Malayalam", "ml"),
        "cast": "Diya Nair, Arjun Menon",
    },
    {
        "name": "Red Earth Rally",
        "description": "A rookie mechanic enters a cross-country rally to protect her workshop.",
        "duration_minutes": 132,
        "age_certification": Movie.CERT_UA,
        "genres": ["Action", "Adventure"],
        "language": ("Kannada", "kn"),
        "cast": "Sana Rao, Vikram Hegde",
    },
]

THEATERS = [
    ("Demo Cinema Pune", "Pune"),
    ("Demo Cinema Mumbai", "Mumbai"),
    ("Demo Cinema Delhi", "Delhi"),
    ("Demo Cinema Bengaluru", "Bengaluru"),
    ("Demo Cinema Chennai", "Chennai"),
    ("Demo Cinema Hyderabad", "Hyderabad"),
]

SHOW_TIMES_UTC = ((12, 30), (15, 30), (18, 30))
SEAT_NUMBERS = [f"{row}{number}" for row in "ABCDE" for number in range(1, 9)]
DEMO_TAGLINE = "SAMPLE DATA - demo listing; not a real release or show schedule"


class Command(BaseCommand):
    help = "Create an idempotent sample catalog of fictional movies, demo cinemas, shows, and seats."

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm-nonlocal",
            action="store_true",
            help="Allow seeding a non-SQLite database after verifying it is safe to modify.",
        )

    def handle(self, *args, **options):
        if connection.vendor != "sqlite" and not options["confirm_nonlocal"]:
            raise CommandError(
                "This command is connected to a non-SQLite database. "
                "Verify it is a development database, then rerun with --confirm-nonlocal."
            )

        genres = {
            name: Genre.objects.get_or_create(name=name)[0]
            for movie in MOVIES
            for name in movie["genres"]
        }
        languages = {
            code: Language.objects.get_or_create(
                name=name, defaults={"code": code}
            )[0]
            for movie in MOVIES
            for name, code in [movie["language"]]
        }
        theaters = [
            Theater.objects.get_or_create(
                name=name,
                city=city,
                defaults={
                    "address": f"Sample venue for local testing, {city}, India"
                },
            )[0]
            for name, city in THEATERS
        ]

        today = timezone.localdate()
        movies_created = shows_created = seats_created = 0
        for movie_index, sample in enumerate(MOVIES):
            movie = Movie.objects.filter(name=sample["name"]).first()
            created = movie is None
            if created:
                movie = Movie.objects.create(
                    name=sample["name"],
                    tagline=DEMO_TAGLINE,
                    image=make_poster(sample["name"], movie_index + 101),
                    description=sample["description"],
                    duration_minutes=sample["duration_minutes"],
                    age_certification=sample["age_certification"],
                    release_date=today - timedelta(days=30 + movie_index * 9),
                    cast=sample["cast"],
                )
            movies_created += int(created)
            movie.genres.add(*(genres[name] for name in sample["genres"]))
            movie.languages.add(languages[sample["language"][1]])

            theater = theaters[movie_index % len(theaters)]
            for slot, (hour, minute) in enumerate(SHOW_TIMES_UTC, start=1):
                show_date = today + timedelta(days=1 + (movie_index + slot - 1) % 7)
                show_time = timezone.make_aware(
                    datetime.combine(show_date, datetime.min.time()).replace(
                        hour=hour, minute=minute
                    ),
                    ZoneInfo("UTC"),
                )
                show, show_created = Show.objects.update_or_create(
                    movie=movie,
                    theater=theater,
                    screen_name=f"Demo Screen {slot}",
                    defaults={
                        "language": languages[sample["language"][1]],
                        "screen_format": Show.FORMAT_2D,
                        "date_time": show_time,
                        "price": Decimal("180.00") + Decimal(slot * 30),
                    },
                )
                shows_created += int(show_created)
                existing_seats = set(
                    Seat.objects.filter(show=show).values_list(
                        "seat_number", flat=True
                    )
                )
                new_seats = [
                    Seat(
                        show=show,
                        theater=theater,
                        seat_number=seat_number,
                        is_booked=False,
                    )
                    for seat_number in SEAT_NUMBERS
                    if seat_number not in existing_seats
                ]
                Seat.objects.bulk_create(new_seats, ignore_conflicts=True)
                seats_created += len(new_seats)

        self.stdout.write(
            self.style.SUCCESS(
                "Sample catalog ready: "
                f"{len(MOVIES)} movies ({movies_created} new), "
                f"{len(theaters)} demo theaters, "
                f"{len(MOVIES) * len(SHOW_TIMES_UTC)} upcoming shows "
                f"({shows_created} new), {seats_created} seats added."
            )
        )
        self.stdout.write(
            "All sample titles and theater addresses are fictional. "
            "Showtimes are for local testing only."
        )
