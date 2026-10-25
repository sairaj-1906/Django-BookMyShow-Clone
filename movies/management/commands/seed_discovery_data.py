"""
Seed a large movie catalogue (thousands of movies with genres, languages, ratings, upcoming
shows in many cities, and booking history) to try out and benchmark movie discovery.

    python manage.py seed_discovery_data                 # 500 movies (quick, good for SQLite)
    python manage.py seed_discovery_data --movies 3000   # large catalogue for benchmarking

Rows are tagged exactly like seed_analytics_data's, so
    python manage.py seed_analytics_data --clear
removes them too. Intended for a development database — never run against production.
"""
import random
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from movies.management.commands.seed_analytics_data import EMAIL_SUFFIX, MARK, make_poster
from movies.models import Booking, Genre, Language, Movie, Seat, Show, Theater

GENRES = ["Action", "Comedy", "Drama", "Thriller", "Romance", "Sci-Fi", "Horror", "Animation",
          "Adventure", "Crime", "Fantasy", "Family", "Biography", "Mystery"]
LANGUAGES = [("Hindi", "hi"), ("English", "en"), ("Tamil", "ta"), ("Telugu", "te"), ("Marathi", "mr"),
             ("Malayalam", "ml"), ("Kannada", "kn"), ("Bengali", "bn"), ("Gujarati", "gu")]
CITIES = ["Pune", "Mumbai", "Delhi", "Bengaluru", "Chennai", "Hyderabad", "Kolkata", "Ahmedabad",
          "Jaipur", "Nagpur", "Nashik", "Kochi"]
CHAINS = ["PVR", "INOX", "Cinepolis", "Carnival", "Miraj", "Wave"]
ADJ = ["Midnight", "Crimson", "Silent", "Golden", "Broken", "Hidden", "Last", "Electric", "Wild", "Paper",
       "Neon", "Iron", "Lost", "Rising", "Velvet", "Burning", "Hollow", "Royal", "Distant", "Savage"]
NOUN = ["Chronicle", "Horizon", "Empire", "Promise", "Garden", "Harvest", "Signal", "Kingdom", "River",
        "Storm", "Mirror", "Voyage", "Legacy", "Ember", "Orbit", "Whisper", "Fortress", "Echo", "Journey", "Tide"]
SHOW_HOURS = [9, 10, 12, 13, 15, 16, 18, 19, 21, 22, 23]


class Command(BaseCommand):
    help = "Seed a large movie catalogue for the discovery page / benchmark."

    def add_arguments(self, parser):
        parser.add_argument("--movies", type=int, default=500)
        parser.add_argument("--theaters", type=int, default=0, help="Default: movies/40 (min 30).")
        parser.add_argument("--users", type=int, default=1500)
        parser.add_argument("--seed", type=int, default=7)

    def handle(self, *args, **opts):
        rng = random.Random(opts["seed"])
        if Movie.objects.filter(tagline=MARK, name__startswith="DISC ").exists():
            raise CommandError("Discovery seed data already exists. Run seed_analytics_data --clear first.")
        n_movies = opts["movies"]
        n_theaters = opts["theaters"] or max(30, n_movies // 40)
        tz = ZoneInfo(getattr(settings, "DASHBOARD_TIMEZONE", "Asia/Kolkata"))
        now = timezone.now()
        Booking._meta.get_field("booked_at").auto_now_add = False  # allow historical timestamps

        genres = [Genre.objects.get_or_create(name=n)[0] for n in GENRES]
        languages = [Language.objects.get_or_create(name=n, defaults={"code": c})[0] for n, c in LANGUAGES]

        # ---- users (for booking history); reuse any seed users that already exist
        users = list(User.objects.filter(email__endswith=EMAIL_SUFFIX).values_list("id", flat=True))
        if len(users) < opts["users"]:
            pw = make_password("seed-user-password")
            base = len(users)
            User.objects.bulk_create(
                [User(username=f"disc_user_{base + i:05d}", email=f"disc_user_{base + i:05d}{EMAIL_SUFFIX}",
                      password=pw, date_joined=now - timedelta(days=rng.randint(30, 700)))
                 for i in range(opts["users"] - len(users))], batch_size=2000)
            users = list(User.objects.filter(email__endswith=EMAIL_SUFFIX).values_list("id", flat=True))

        # ---- theaters across cities
        theaters = Theater.objects.bulk_create(
            [Theater(name=f"{rng.choice(CHAINS)} {rng.choice(ADJ)} {i + 1}", city=CITIES[i % len(CITIES)], address=MARK)
             for i in range(n_theaters)])

        # ---- movies (a dozen shared placeholder posters keep the media folder small)
        posters = [make_poster(f"Poster {i + 1}", i + 3) for i in range(12)]
        movie_rows = []
        for i in range(n_movies):
            has_reviews = rng.random() < 0.8
            count = rng.randint(3, 400) if has_reviews else 0
            movie_rows.append(Movie(
                name=f"DISC {rng.choice(ADJ)} {rng.choice(NOUN)} {i + 1}", tagline=MARK, image=posters[i % 12],
                description="Seeded movie for the discovery demo.", duration_minutes=rng.randint(85, 180),
                age_certification=rng.choice(["U", "UA", "UA", "A"]),
                release_date=(now - timedelta(days=rng.randint(-30, 1100))).date(),  # a few are "coming soon"
                rating=Decimal(str(round(rng.uniform(2.5, 4.8), 1))),
                average_rating=Decimal(str(round(rng.uniform(1.8, 4.9), 2))) if has_reviews else Decimal("0"),
                ratings_count=count))
        movies = Movie.objects.bulk_create(movie_rows, batch_size=1000)
        G, L = Movie.genres.through, Movie.languages.through
        G.objects.bulk_create([G(movie_id=m.id, genre_id=g.id) for m in movies for g in rng.sample(genres, rng.randint(1, 3))], batch_size=5000)
        L.objects.bulk_create([L(movie_id=m.id, language_id=x.id) for m in movies for x in rng.sample(languages, rng.randint(1, 2))], batch_size=5000)

        # ---- shows (upcoming, no seats) + past shows with seats and bookings (popularity/history)
        # popular movies get more of everything
        weights = [1 / (i + 1) ** 0.6 for i in range(n_movies)]
        total_bookings = 0
        for start in range(0, n_movies, 200):
            chunk = movies[start:start + 200]
            upcoming, past = [], []
            for offset, m in enumerate(chunk):
                w = weights[start + offset]
                for _ in range(rng.randint(0, 3 + int(w * 9))):
                    day = rng.randint(0, 13)
                    local = (now.astimezone(tz) + timedelta(days=day)).replace(
                        hour=rng.choice(SHOW_HOURS), minute=rng.choice([0, 15, 30, 45]), second=0, microsecond=0)
                    upcoming.append(Show(movie=m, theater=rng.choice(theaters), date_time=local.astimezone(ZoneInfo("UTC")),
                                         screen_format=rng.choice(["2D", "2D", "3D", "IMAX"]),
                                         price=Decimal(rng.choice([120, 150, 180, 200, 250, 300, 400, 550]))))
                for _ in range(rng.randint(1, 2 + int(w * 3))):
                    past.append(Show(movie=m, theater=rng.choice(theaters),
                                     date_time=now - timedelta(days=rng.randint(1, 120), hours=rng.randint(0, 20)),
                                     price=Decimal(rng.choice([150, 200, 250, 300]))))
            Show.objects.bulk_create(upcoming, batch_size=5000)
            past = Show.objects.bulk_create(past, batch_size=5000)
            seats = Seat.objects.bulk_create(
                [Seat(show=s, seat_number=f"A{i}", is_booked=False) for s in past for i in range(30)], batch_size=5000)
            by_show = {}
            for seat in seats:
                by_show.setdefault(seat.show_id, []).append(seat)
            bookings = []
            for s in past:
                fill = rng.uniform(0.25, 0.95)
                for seat in by_show[s.id]:
                    if rng.random() < fill:
                        bookings.append(Booking(
                            user_id=rng.choice(users), seat=seat, movie_id=s.movie_id, theater_id=s.theater_id, show=s,
                            booked_at=s.date_time - timedelta(hours=rng.randint(2, 72))))
                        seat.is_booked = True
            Booking.objects.bulk_create(bookings, batch_size=5000)
            Seat.objects.bulk_update([x for x in seats if x.is_booked], ["is_booked"], batch_size=5000)
            total_bookings += len(bookings)
            self.stdout.write(f"  {min(start + 200, n_movies)}/{n_movies} movies | {total_bookings:,} bookings", ending="\r")
            self.stdout.flush()
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {n_movies:,} movies, {n_theaters} theaters, {Show.objects.filter(movie__in=movies).count():,} shows, {total_bookings:,} bookings."))
