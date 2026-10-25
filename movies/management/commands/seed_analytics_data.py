"""
Seed a large, realistic data set so the admin dashboard can be demonstrated and
benchmarked with 100,000+ bookings.

    python manage.py seed_analytics_data                # ~115k bookings
    python manage.py seed_analytics_data --reset        # remove previously seeded rows first
    python manage.py seed_analytics_data --fix-posters  # (re)generate placeholder poster images only
    python manage.py seed_analytics_data --clear        # remove all seeded data and exit

Everything this command creates is tagged so --reset can remove ONLY seeded data:
  * users          -> email ends with @seed.example
  * movies         -> tagline == "SEED-DATA"
  * theaters       -> address == "SEED-DATA"
(shows, seats, bookings, payments and cancellations hang off those and go with them)

It also creates three demo staff accounts so the permission system can be tried out.
Intended for a local/dev database — do not run against production.
"""
import bisect
import colorsys
import os
import random
import re
import shutil
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import Permission, User
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from movies.models import (
    Booking,
    Cancellation,
    Movie,
    Payment,
    Review,
    Seat,
    SeatHold,
    Show,
    Theater,
)

MARK = "SEED-DATA"
EMAIL_SUFFIX = "@seed.example"

MOVIE_NAMES = [
    "Avengers: Endgame", "Oppenheimer", "Dune: Part Two", "Jawan", "Pathaan", "RRR",
    "KGF Chapter 2", "Animal", "Barbie", "Inception", "Interstellar", "The Batman",
    "Spider-Man: No Way Home", "Top Gun: Maverick", "Gadar 2", "Pushpa 2",
    "Kalki 2898 AD", "Stree 2", "Fighter", "Salaar", "Dangal", "3 Idiots",
    "Drishyam 2", "Brahmastra", "Tiger 3", "Leo", "Vikram", "Kantara",
    "Joker", "Tenet", "Avatar: The Way of Water", "Black Panther", "Deadpool & Wolverine",
    "Inside Out 2", "Godzilla x Kong", "Mission Impossible 7", "Fast X",
    "John Wick 4", "The Marvels", "Wonka",
]
THEATERS = [
    ("PVR Phoenix", "Pune"), ("INOX Amanora", "Pune"), ("Cinepolis Seasons", "Pune"),
    ("PVR Oberoi", "Mumbai"), ("INOX Nariman Point", "Mumbai"), ("Cinepolis Andheri", "Mumbai"),
    ("PVR Select Citywalk", "Delhi"), ("INOX Nehru Place", "Delhi"), ("Cinepolis DLF", "Delhi"),
    ("PVR Forum", "Bengaluru"), ("INOX Garuda", "Bengaluru"), ("Cinepolis Orion", "Bengaluru"),
    ("PVR Ampa", "Chennai"), ("Sathyam Cinemas", "Chennai"), ("AGS Cinemas", "Chennai"),
    ("PVR Inorbit", "Hyderabad"), ("AMB Cinemas", "Hyderabad"), ("INOX GVK", "Hyderabad"),
    ("PVR Acropolis", "Kolkata"), ("INOX South City", "Kolkata"), ("Carnival Cinemas", "Nagpur"),
    ("Cinepolis Nashik", "Nashik"), ("PVR Ahmedabad One", "Ahmedabad"), ("INOX Jaipur", "Jaipur"),
    ("Miraj Cinemas", "Ahmednagar"),
]
# Relative booking activity per hour of day (IST): quiet overnight, strong evening peak.
HOUR_WEIGHTS = [1, 1, 0.5, 0.5, 0.5, 1, 2, 3, 4, 5, 5, 6, 6, 6, 6, 7, 8, 10, 13, 15, 16, 14, 8, 3]
SHOW_HOURS = [10, 13, 16, 19, 22]
SHOW_HOUR_BOOST = {10: 0.80, 13: 0.9, 16: 0.95, 19: 1.12, 22: 1.0}


POSTER_DIR = "movies/seed"


def _poster_path(name):
    return f"{POSTER_DIR}/{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')}.jpg"


def make_poster(name, index):
    """Write a simple 300x450 placeholder poster (gradient + movie title) and return its
    path relative to MEDIA_ROOT. Needs Pillow, which the project already requires."""
    from PIL import Image, ImageDraw, ImageFont

    w, h = 300, 450
    hue = (index * 0.618) % 1.0  # golden-ratio spacing -> visually distinct colours
    top = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.65, 0.85))
    bottom = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.8, 0.30))
    img = Image.new("RGB", (w, h))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        draw.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    try:
        font = ImageFont.load_default(size=28)  # Pillow >= 10.1
    except TypeError:
        font = ImageFont.load_default()
    # wrap the title into lines that fit the poster width
    lines, line = [], ""
    for word in name.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= w - 40:
            line = trial
        else:
            lines.append(line)
            line = word
    lines.append(line)
    y = h // 2 - len(lines) * 18
    for ln in lines:
        x = (w - draw.textlength(ln, font=font)) / 2
        draw.text((x + 1, y + 1), ln, font=font, fill=(0, 0, 0))
        draw.text((x, y), ln, font=font, fill=(255, 255, 255))
        y += 36
    rel = _poster_path(name)
    full = os.path.join(settings.MEDIA_ROOT, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    img.save(full, "JPEG", quality=80)
    return rel


class Command(BaseCommand):
    help = "Seed 100k+ realistic bookings (plus users, shows, payments, cancellations) for the dashboard."

    def add_arguments(self, parser):
        parser.add_argument("--shows", type=int, default=2600, help="Number of shows (60 seats each).")
        parser.add_argument("--users", type=int, default=5000)
        parser.add_argument("--days", type=int, default=365, help="History window in days.")
        parser.add_argument("--reset", action="store_true", help="Delete previously seeded data first.")
        parser.add_argument("--seed", type=int, default=42, help="Random seed (reproducible data).")
        parser.add_argument("--admin-password", default="DashAdmin#2026")
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Remove all seeded data (movies, theaters, users, bookings, posters) and exit.",
        )
        parser.add_argument(
            "--fix-posters",
            action="store_true",
            help="Only (re)generate placeholder poster images for already-seeded movies, then exit.",
        )

    # ------------------------------------------------------------------ helpers
    def _reset(self):
        self.stdout.write("Removing previously seeded data...")
        movie_ids = list(Movie.objects.filter(tagline=MARK).values_list("id", flat=True))
        theater_ids = list(Theater.objects.filter(address=MARK).values_list("id", flat=True))
        shows = Show.objects.filter(movie_id__in=movie_ids)
        # _raw_delete skips Django's in-memory cascade collector, which matters at 100k+ rows.
        seeded_bookings = Booking.objects.filter(show__in=shows)
        seeded_payments = Payment.objects.filter(show__in=shows)
        Review.objects.filter(booking__in=seeded_bookings).update(booking=None)
        Cancellation.objects.filter(show__in=shows)._raw_delete("default")
        Booking.objects.filter(show__in=shows)._raw_delete("default")
        SeatHold.objects.filter(show__in=shows)._raw_delete("default")  # holds/payments made while testing
        Payment.seats.through.objects.filter(payment__in=seeded_payments)._raw_delete("default")
        Payment.objects.filter(show__in=shows)._raw_delete("default")
        Seat.objects.filter(show__in=shows)._raw_delete("default")
        shows._raw_delete("default")
        Movie.objects.filter(id__in=movie_ids).delete()
        shutil.rmtree(os.path.join(settings.MEDIA_ROOT, POSTER_DIR), ignore_errors=True)
        Theater.objects.filter(id__in=theater_ids).delete()
        User.objects.filter(email__endswith=EMAIL_SUFFIX).delete()

    def _create_demo_staff(self, password):
        perm = Permission.objects.get(codename="view_dashboard", content_type__app_label="movies")
        admin, _ = User.objects.get_or_create(
            username="dashboard_admin",
            defaults={"email": "dashboard_admin@example.com", "is_staff": True, "is_superuser": True},
        )
        admin.is_staff = admin.is_superuser = True
        admin.set_password(password)
        admin.save()

        analyst, _ = User.objects.get_or_create(
            username="analyst", defaults={"email": "analyst@example.com", "is_staff": True}
        )
        analyst.is_staff = True
        analyst.is_superuser = False
        analyst.set_password(password)
        analyst.save()
        analyst.user_permissions.set([perm])

        nopriv, _ = User.objects.get_or_create(
            username="staff_noaccess", defaults={"email": "staff_noaccess@example.com", "is_staff": True}
        )
        nopriv.is_staff = True
        nopriv.is_superuser = False
        nopriv.set_password(password)
        nopriv.save()
        nopriv.user_permissions.clear()

    # ------------------------------------------------------------------- main
    def _fix_posters(self):
        movies = list(Movie.objects.filter(tagline=MARK).order_by("id"))
        for i, movie in enumerate(movies):
            movie.image = make_poster(movie.name, i)
            movie.save(update_fields=["image"])
        self.stdout.write(self.style.SUCCESS(f"Generated placeholder posters for {len(movies)} seeded movies."))

    def handle(self, *args, **opts):
        if opts["fix_posters"]:
            return self._fix_posters()
        if opts["clear"]:
            self._reset()
            return self.stdout.write(self.style.SUCCESS("Seeded data removed."))
        rng = random.Random(opts["seed"])
        if opts["reset"]:
            self._reset()
        elif Movie.objects.filter(tagline=MARK).exists():
            raise CommandError("Seed data already exists. Re-run with --reset to rebuild it.")

        tz = ZoneInfo(getattr(settings, "DASHBOARD_TIMEZONE", "Asia/Kolkata"))
        now = timezone.now()
        days = opts["days"]

        # Let us write historical timestamps (auto_now/auto_now_add would overwrite them).
        for model, names in ((Booking, ["booked_at"]), (Payment, ["created_at", "updated_at"])):
            for name in names:
                field = model._meta.get_field(name)
                field.auto_now_add = False
                field.auto_now = False

        # ---- users: signups accelerate over time (recent days have more signups)
        self.stdout.write("Creating users...")
        pw_hash = make_password("seed-user-password")
        joins = sorted(now - timedelta(days=days + 30) * (rng.random() ** 1.6) for _ in range(opts["users"]))
        users = User.objects.bulk_create(
            [
                User(
                    username=f"seed_user_{i:05d}",
                    email=f"seed_user_{i:05d}{EMAIL_SUFFIX}",
                    password=pw_hash,
                    date_joined=joined,
                )
                for i, joined in enumerate(joins)
            ],
            batch_size=2000,
        )
        user_ids = [u.id for u in users]
        join_ts = [j.timestamp() for j in joins]

        # ---- movies & theaters (with popularity weights so rankings are interesting)
        movies = Movie.objects.bulk_create(
            [
                Movie(
                    name=name,
                    tagline=MARK,
                    image=make_poster(name, i),
                    description="Seeded movie for analytics demo.",
                    duration_minutes=rng.randint(95, 175),
                    release_date=(now - timedelta(days=rng.randint(10, days))).date(),
                )
                for i, name in enumerate(MOVIE_NAMES)
            ]
        )
        movie_w = [1 / (i + 1) ** 0.75 for i in range(len(movies))]
        theaters = Theater.objects.bulk_create(
            [Theater(name=n, city=c, address=MARK) for n, c in THEATERS]
        )
        theater_pop = {t.id: rng.uniform(0.62, 0.97) for t in theaters}

        # ---- shows, seats, payments, bookings, cancellations (chunked to bound memory)
        seat_labels = [f"{row}{col}" for row in "ABCDEF" for col in range(1, 11)]
        n_shows, chunk = opts["shows"], 100
        order_counter = 0
        totals = {"shows": 0, "seats": 0, "payments": 0, "bookings": 0, "cancellations": 0}
        self.stdout.write(f"Creating {n_shows} shows with {len(seat_labels)} seats each...")

        for start in range(0, n_shows, chunk):
            count = min(chunk, n_shows - start)
            show_objs = []
            for _ in range(count):
                day_offset = rng.randint(-days, 5)  # a few upcoming shows too
                local = (now.astimezone(tz) + timedelta(days=day_offset)).replace(
                    hour=rng.choice(SHOW_HOURS), minute=rng.choice([0, 30]), second=0, microsecond=0
                )
                theater = rng.choices(theaters)[0]
                movie = rng.choices(movies, weights=movie_w)[0]
                show_objs.append(
                    Show(
                        movie=movie,
                        theater=theater,
                        date_time=local.astimezone(ZoneInfo("UTC")),
                        screen_format=rng.choice(["2D", "2D", "2D", "3D", "IMAX"]),
                        price=Decimal(rng.choice([150, 180, 200, 250, 300, 400])),
                    )
                )
            show_objs = Show.objects.bulk_create(show_objs)

            seat_objs, plan = [], []  # plan[i] -> {label: ('book'|'cancel'|'free')}
            for show in show_objs:
                local_dt = show.date_time.astimezone(tz)
                occ = theater_pop[show.theater_id] * SHOW_HOUR_BOOST[local_dt.hour]
                occ *= 1.12 if local_dt.weekday() >= 4 else 0.94
                occ *= 0.75 + 0.5 * (movie_w[[m.id for m in movies].index(show.movie_id)] / movie_w[0]) ** 0.3
                occ = max(0.25, min(0.99, occ * rng.uniform(0.85, 1.1)))
                if show.date_time > now:
                    occ *= 0.4  # future shows are only partly sold so far
                n_taken = round(len(seat_labels) * occ)
                taken = rng.sample(seat_labels, n_taken)
                states = {}
                for label in taken:
                    states[label] = "cancel" if rng.random() < 0.06 else "book"
                plan.append(states)
                for label in seat_labels:
                    seat_objs.append(Seat(show=show, seat_number=label, is_booked=states.get(label) == "book"))
            seat_objs = Seat.objects.bulk_create(seat_objs, batch_size=5000)
            seat_id = {(s.show_id, s.seat_number): s.id for s in seat_objs}

            payments, pay_meta = [], []
            for show, states in zip(show_objs, plan):
                labels = list(states)
                rng.shuffle(labels)
                i = 0
                while i < len(labels):
                    size = rng.choices([1, 2, 3, 4, 5, 6], weights=[18, 38, 20, 14, 6, 4])[0]
                    group = labels[i : i + size]
                    i += size
                    # booking time: 0-3 days before the show, hour-of-day weighted to the evening
                    k = rng.choices([0, 1, 2, 3], weights=[45, 30, 15, 10])[0]
                    day = (show.date_time.astimezone(tz) - timedelta(days=k)).replace(
                        hour=rng.choices(range(24), weights=HOUR_WEIGHTS)[0],
                        minute=rng.randint(0, 59),
                        second=rng.randint(0, 59),
                        microsecond=0,
                    )
                    booked_at = day.astimezone(ZoneInfo("UTC"))
                    if booked_at >= show.date_time:
                        booked_at = show.date_time - timedelta(minutes=rng.randint(10, 120))
                    booked_at = min(booked_at, now - timedelta(minutes=rng.randint(1, 600)))
                    # the buyer must already have signed up
                    hi = bisect.bisect_right(join_ts, booked_at.timestamp())
                    uid = user_ids[rng.randrange(max(hi, 1))]
                    order_counter += 1
                    paid_at = booked_at + timedelta(seconds=rng.randint(20, 240))
                    payments.append(
                        Payment(
                            user_id=uid,
                            show=show,
                            seat_numbers=", ".join(sorted(group)),
                            amount=show.price * len(group),
                            razorpay_order_id=f"order_seed_{order_counter:08d}",
                            razorpay_payment_id=f"pay_seed_{order_counter:08d}",
                            status=Payment.STATUS_SUCCESS,
                            paid_at=paid_at,
                            created_at=booked_at,
                            updated_at=paid_at,
                        )
                    )
                    pay_meta.append((show, group, states, uid, booked_at))
                    # ~9% extra checkout attempts that never completed
                    if rng.random() < 0.09:
                        order_counter += 1
                        outcome = rng.choice([Payment.STATUS_FAILED, Payment.STATUS_CANCELLED])
                        payments.append(
                            Payment(
                                user_id=uid,
                                show=show,
                                seat_numbers=group[0],
                                amount=show.price,
                                razorpay_order_id=f"order_seed_{order_counter:08d}",
                                status=outcome,
                                failure_reason="Card declined" if outcome == Payment.STATUS_FAILED else "Cancelled by user",
                                created_at=booked_at - timedelta(minutes=rng.randint(3, 30)),
                                updated_at=booked_at,
                            )
                        )
                        pay_meta.append(None)

            payments = Payment.objects.bulk_create(payments, batch_size=5000)

            bookings, cancels = [], []
            for payment, meta in zip(payments, pay_meta):
                if meta is None:
                    continue
                show, group, states, uid, booked_at = meta
                for label in group:
                    if states[label] == "book":
                        bookings.append(
                            Booking(
                                user_id=uid,
                                seat_id=seat_id[(show.id, label)],
                                movie_id=show.movie_id,
                                theater_id=show.theater_id,
                                show=show,
                                payment=payment,
                                booked_at=booked_at,
                            )
                        )
                    else:
                        cancelled_at = min(
                            booked_at + timedelta(hours=rng.randint(1, 48)),
                            max(show.date_time - timedelta(minutes=30), booked_at + timedelta(minutes=5)),
                        )
                        cancelled_at = min(cancelled_at, now)
                        status = rng.choices(
                            [Cancellation.REFUND_PROCESSED, Cancellation.REFUND_PENDING, Cancellation.REFUND_FAILED],
                            weights=[70, 22, 8],
                        )[0]
                        cancels.append(
                            Cancellation(
                                user_id=uid,
                                show=show,
                                payment=payment,
                                seat_number=label,
                                refund_amount=show.price,
                                refund_status=status,
                                reason=rng.choice(["Change of plans", "Booked wrong show", "Emergency", "Duplicate booking"]),
                                cancelled_at=cancelled_at,
                                refunded_at=cancelled_at + timedelta(hours=rng.randint(2, 72))
                                if status == Cancellation.REFUND_PROCESSED and cancelled_at + timedelta(hours=72) < now
                                else None,
                            )
                        )
            Booking.objects.bulk_create(bookings, batch_size=5000)
            Cancellation.objects.bulk_create(cancels, batch_size=5000)

            totals["shows"] += len(show_objs)
            totals["seats"] += len(seat_objs)
            totals["payments"] += len(payments)
            totals["bookings"] += len(bookings)
            totals["cancellations"] += len(cancels)
            self.stdout.write(
                f"  {totals['shows']}/{n_shows} shows | {totals['bookings']:,} bookings", ending="\r"
            )
            self.stdout.flush()

        self._create_demo_staff(opts["admin_password"])
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Seeding complete:"))
        for key, value in totals.items():
            self.stdout.write(f"  {key:14s} {value:>10,}")
        self.stdout.write(f"  {'users':14s} {len(user_ids):>10,}")
        self.stdout.write("Demo staff accounts created: dashboard_admin / analyst / staff_noaccess")
