"""
Time every dashboard query so the effect of indexes / query design can be measured.

    python manage.py benchmark_dashboard                       # last 365 days, median of 5 runs
    python manage.py benchmark_dashboard --days 30 --runs 9
    python manage.py benchmark_dashboard --label baseline --json before.json
    python manage.py benchmark_dashboard --explain             # EXPLAIN ANALYZE (PostgreSQL)
    python manage.py benchmark_dashboard --naive               # compare with loading rows into Python
"""

import json
import statistics
import time
import tracemalloc
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import connection
from django.test.utils import CaptureQueriesContext

from movies import analytics as an
from movies.models import Booking


def _timed(fn, runs):
    fn()  # warm-up (plan cache, buffer cache)
    samples = []
    queries = 0
    for _ in range(runs):
        with CaptureQueriesContext(connection) as ctx:
            t0 = time.perf_counter()
            fn()
            samples.append((time.perf_counter() - t0) * 1000)
        queries = len(ctx)
    return statistics.median(samples), queries


class Command(BaseCommand):
    help = "Benchmark the dashboard analytics queries."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=365)
        parser.add_argument("--runs", type=int, default=5)
        parser.add_argument("--label", default="")
        parser.add_argument(
            "--json", default="", help="Write results to this JSON file."
        )
        parser.add_argument("--explain", action="store_true")
        parser.add_argument("--naive", action="store_true")

    def handle(self, *args, **o):
        f = an.default_filters(o["days"])
        total = Booking.objects.count()
        self.stdout.write(
            f"Bookings in DB: {total:,} | range: {f.start_date} -> {f.end_date} "
            f"({o['days']} days) | granularity: {f.granularity} | DB: {connection.vendor}"
        )
        metrics = {
            "revenue_summary (today/week/month/year)": lambda: an.revenue_summary(),
            "revenue_trend": lambda: an.revenue_trend(f),
            "booking_trend": lambda: an.booking_trend(f),
            "occupancy_by_theater": lambda: an.occupancy_by_theater(f),
            "top_movies": lambda: an.top_movies(f),
            "top_theaters": lambda: an.top_theaters(f),
            "peak_hours": lambda: an.peak_hours(f),
            "cancellation_stats": lambda: an.cancellation_stats(f),
            "user_growth": lambda: an.user_growth(f),
            "WHOLE DASHBOARD (dashboard_data)": lambda: an.dashboard_data(f),
        }
        results = {}
        self.stdout.write(f"\n{'metric':44s} {'median ms':>10s} {'queries':>8s}")
        self.stdout.write("-" * 64)
        for name, fn in metrics.items():
            ms, q = _timed(fn, o["runs"])
            results[name] = {"ms": round(ms, 1), "queries": q}
            self.stdout.write(f"{name:44s} {ms:10.1f} {q:8d}")

        if o["naive"]:
            self._naive(f, o["runs"], results)
        if o["explain"] and connection.vendor == "postgresql":
            self._explain(f)
        if o["json"]:
            with open(o["json"], "w") as fh:
                json.dump(
                    {"label": o["label"], "bookings": total, "results": results},
                    fh,
                    indent=2,
                )
            self.stdout.write(f"\nSaved {o['json']}")

    def _naive(self, f, runs, results):
        """The anti-pattern the dashboard avoids: fetch every row, aggregate in Python."""
        self.stdout.write(
            "\nNaive in-Python approach vs database aggregation (same answers):"
        )

        def naive_hours_and_movies():
            hours, movies = Counter(), Counter()
            for b in Booking.objects.filter(
                booked_at__gte=f.start, booked_at__lt=f.end
            ).select_related("movie"):
                hours[b.booked_at.astimezone(an.dashboard_tz()).hour] += 1
                movies[b.movie.name] += 1
            return hours, movies

        def orm_hours_and_movies():
            return an.peak_hours(f), an.top_movies(f, limit=10_000)

        for label, fn in (
            ("naive loop (peak hours + movie counts)", naive_hours_and_movies),
            ("ORM aggregation (same two reports)", orm_hours_and_movies),
        ):
            tracemalloc.start()
            fn()
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            ms, q = _timed(fn, max(1, runs // 2))
            results[label] = {
                "ms": round(ms, 1),
                "queries": q,
                "peak_mem_mb": round(peak / 1e6, 1),
            }
            self.stdout.write(
                f"  {label:44s} {ms:9.1f} ms  {q:6d} queries  peak python memory {peak/1e6:7.1f} MB"
            )

    def _explain(self, f):
        self.stdout.write("\nEXPLAIN (ANALYZE, BUFFERS) for the heaviest queries:\n")
        from django.db.models import Count, Q, Sum
        from django.db.models.functions import ExtractHour
        from movies.models import Show

        tz = an.dashboard_tz()
        qs = {
            "peak hours (bookings by hour)": an._bookings(f)
            .annotate(hour=ExtractHour("booked_at", tzinfo=tz))
            .values("hour")
            .annotate(n=Count("id")),
            "top movies (bookings by movie)": an._bookings(f)
            .values("movie__name")
            .annotate(n=Count("id")),
            "revenue (successful payments)": an._paid(f)
            .values("status")
            .annotate(s=Sum("amount")),
            "occupancy (shows x seats)": Show.objects.filter(
                date_time__gte=f.start, date_time__lt=f.end
            )
            .values("theater__name")
            .annotate(
                t=Count("seats"), b=Count("seats", filter=Q(seats__is_booked=True))
            ),
        }
        for name, qset in qs.items():
            self.stdout.write(f"--- {name}")
            plan = qset.explain(analyze=True, buffers=True)
            for line in plan.splitlines():
                if any(
                    k in line
                    for k in (
                        "Scan",
                        "Aggregate",
                        "Execution Time",
                        "Planning Time",
                        "Join",
                        "Sort",
                    )
                ):
                    self.stdout.write("   " + line.strip())
