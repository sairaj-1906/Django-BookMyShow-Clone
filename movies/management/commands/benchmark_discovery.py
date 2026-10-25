"""
Time the movie-discovery queries.

    python manage.py benchmark_discovery                 # optimized implementation
    python manage.py benchmark_discovery --naive         # ...and a naive JOIN + DISTINCT version
    python manage.py benchmark_discovery --explain       # EXPLAIN ANALYZE (PostgreSQL)
    python manage.py benchmark_discovery --runs 15 --json out.json

Each scenario measures everything one request does in the database: build the queryset,
COUNT the matches, fetch page 1 (+ the genre prefetch). Needs data — see seed_discovery_data.
"""
import json
import statistics
import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.core.paginator import Paginator
from django.db import OperationalError, connection
from django.db.models import Case, Count, DecimalField, F, IntegerField, Min, Q, When
from django.http import QueryDict
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from movies import discovery
from movies.models import Booking, Genre, Language, Movie


def scenarios():
    action = Genre.objects.filter(name="Action").values_list("id", flat=True).first() or 1
    drama = Genre.objects.filter(name="Drama").values_list("id", flat=True).first() or 2
    hindi = Language.objects.filter(name="Hindi").values_list("id", flat=True).first() or 1
    return [
        ("default: most popular, page 1", ""),
        ("deep page (page 100)", "page=100"),
        ("title search 'silent'", "q=silent"),
        ("newest releases", "sort=newest"),
        ("2 genres + language", f"genre={action}&genre={drama}&language={hindi}"),
        ("rating>=4, released 2024+, by rating", "min_rating=4&release_from=2024-01-01&sort=rating"),
        ("city=Pune, cheapest first", "city=Pune&sort=price_asc"),
        ("city + evening shows", "city=Pune&timing=evening"),
        ("everything combined", f"genre={action}&language={hindi}&city=Mumbai&timing=evening&min_rating=3&sort=price_desc"),
    ]


def optimized(query):
    form, f = discovery.parse(QueryDict(query))
    paginator = Paginator(discovery.discovery_queryset(f), discovery.PAGE_SIZE)
    page = paginator.get_page(f.page)
    return paginator.count, [m.pk for m in page]


def naive(query):
    """What a typical first implementation looks like: JOIN every related table, DISTINCT away
    the duplicates, aggregate over the joins. (distinct=True on the counts keeps it correct —
    without it the popularity numbers would be multiplied by the number of shows.)"""
    form, f = discovery.parse(QueryDict(query))
    if f.timings:
        return None  # no sane naive equivalent for local-time-of-day filtering
    now = timezone.now()
    cutoff = now - timedelta(days=discovery.POPULARITY_DAYS)
    qs = Movie.objects.all()
    if f.q:
        qs = qs.filter(name__icontains=f.q)
    if f.genres:
        qs = qs.filter(genres__in=f.genres)
    if f.languages:
        qs = qs.filter(languages__in=f.languages)
    if f.release_from:
        qs = qs.filter(release_date__gte=f.release_from)
    if f.min_rating:
        qs = qs.annotate(er=discovery.EFFECTIVE_RATING).filter(er__gte=f.min_rating)
    show_filter = Q(shows__date_time__gte=now)
    if f.city:
        show_filter &= Q(shows__theater__city=f.city)
        qs = qs.filter(shows__date_time__gte=now, shows__theater__city=f.city)
    qs = qs.annotate(
        popularity=Count("bookings", filter=Q(bookings__booked_at__gte=cutoff), distinct=True),
        from_price=Min("shows__price", filter=show_filter),
        effective_rating=discovery.EFFECTIVE_RATING,
    ).distinct()
    sort = f.sort or "popular"
    order = {
        "newest": ["-release_date", "-id"],
        "rating": ["-effective_rating", "-id"],
        "price_asc": [F("from_price").asc(nulls_last=True), "id"],
        "price_desc": [F("from_price").desc(nulls_last=True), "id"],
        "popular": ["-popularity", "-effective_rating", "-id"],
    }[sort]
    qs = qs.order_by(*order).prefetch_related("genres")
    paginator = Paginator(qs, discovery.PAGE_SIZE)
    return paginator.count, [m.pk for m in paginator.get_page(f.page)]


def measure(fn, query, runs, slow_ms=None):
    """Median over `runs`. If the first (warm-up) run already took longer than `slow_ms`,
    that single timing is reported instead of repeating a multi-second query."""
    t0 = time.perf_counter()
    with CaptureQueriesContext(connection) as ctx0:
        result = fn(query)  # warm-up
    first = (time.perf_counter() - t0) * 1000
    if slow_ms and first > slow_ms:
        return first, len(ctx0), result
    times, nq = [], 0
    for _ in range(runs):
        with CaptureQueriesContext(connection) as ctx:
            t0 = time.perf_counter()
            result = fn(query)
            times.append((time.perf_counter() - t0) * 1000)
        nq = len(ctx)
    return statistics.median(times), nq, result


NAIVE_TIMEOUT_MS = 20000


def measure_naive(query, runs):
    """Like measure(), but abandons the query after NAIVE_TIMEOUT_MS (PostgreSQL only) and
    returns None, so one pathological query cannot hang the whole benchmark."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cur:
            cur.execute(f"SET statement_timeout = {NAIVE_TIMEOUT_MS}")
    try:
        return measure(naive, query, runs, slow_ms=4000)
    except OperationalError:
        return None
    finally:
        if connection.vendor == "postgresql":
            with connection.cursor() as cur:
                cur.execute("SET statement_timeout = 0")


class Command(BaseCommand):
    help = "Benchmark movie discovery queries."

    def add_arguments(self, parser):
        parser.add_argument("--runs", type=int, default=9)
        parser.add_argument("--naive", action="store_true")
        parser.add_argument("--explain", action="store_true")
        parser.add_argument("--json", default="")
        parser.add_argument("--only", default="", help="Comma-separated scenario numbers to run (1-based).")
        parser.add_argument("--no-recs", action="store_true", help="Skip the recommendation benchmark.")

    def handle(self, *args, **o):
        self.stdout.write(
            f"Movies: {Movie.objects.count():,} | bookings: {Booking.objects.count():,} | DB: {connection.vendor} | median of {o['runs']} runs"
        )
        head = f"{'scenario':42s} {'optimized':>10s} {'q':>3s}"
        if o["naive"]:
            head += f" {'naive':>10s} {'q':>3s} {'speed-up':>9s} {'same result':>12s}"
        self.stdout.write("\n" + head + "\n" + "-" * len(head))
        results = {}
        chosen = {int(x) for x in o["only"].split(",") if x.strip()}
        for number, (label, query) in enumerate(scenarios(), start=1):
            if chosen and number not in chosen:
                continue
            ms, nq, res = measure(optimized, query, o["runs"])
            row = f"{label:42s} {ms:9.1f}ms {nq:3d}"
            results[label] = {"optimized_ms": round(ms, 1), "optimized_queries": nq, "count": res[0]}
            if o["naive"]:
                if naive(query) is None:
                    row += f" {'n/a':>10s}"
                else:
                    outcome = measure_naive(query, max(3, o["runs"] // 3))
                    if outcome is None:
                        row += f" {'>' + str(NAIVE_TIMEOUT_MS) + 'ms':>11s}     {'>' + format(NAIVE_TIMEOUT_MS / ms, '.0f') + 'x':>9s}  (aborted by timeout)"
                        results[label].update(naive_ms=None, naive_timeout_ms=NAIVE_TIMEOUT_MS)
                    else:
                        nms, nnq, nres = outcome
                        same = nres[0] == res[0]
                        row += f" {nms:9.1f}ms {nnq:3d} {nms / ms:8.1f}x {'yes' if same else 'NO (' + str(nres[0]) + ')':>12s}"
                        results[label].update(naive_ms=round(nms, 1), naive_queries=nnq, same_count=same)
            self.stdout.write(row + f"   [{res[0]} matches]")

        if not o["no_recs"]:
            self._recommendations(o["runs"], results)
        if o["explain"] and connection.vendor == "postgresql":
            self._explain()
        if o["json"]:
            with open(o["json"], "w") as fh:
                json.dump(results, fh, indent=2)

    def _recommendations(self, runs, results):
        from django.contrib.auth.models import User

        heavy = (
            User.objects.annotate(n=Count("bookings")).filter(n__gte=5).order_by("-n").values_list("id", flat=True)[:5]
        )
        users = list(User.objects.filter(id__in=list(heavy)))
        if not users:
            return
        times, queries = [], 0
        for user in users:
            req = RequestFactory().get("/")
            req.user, req.session = user, {}
            discovery.recommend_for(req)
            for _ in range(max(3, runs // 3)):
                with CaptureQueriesContext(connection) as ctx:
                    t0 = time.perf_counter()
                    rec = discovery.recommend_for(req)
                    [m.pk for m in rec.movies]
                    times.append((time.perf_counter() - t0) * 1000)
                queries = max(queries, len(ctx))
        self.stdout.write(
            f"\n{'Recommended for You (5 heavy users)':42s} {statistics.median(times):9.1f}ms {queries:3d}   [{len(rec.movies)} movies, personalized={rec.personalized}]"
        )
        results["recommendations"] = {"ms": round(statistics.median(times), 1), "queries": queries}

    def _explain(self):
        self.stdout.write("\nEXPLAIN (ANALYZE) of the page query for 'city + evening shows' and the default list:\n")
        for label, query in (("default (popular)", ""), ("city + evening", "city=Pune&timing=evening")):
            form, f = discovery.parse(QueryDict(query))
            plan = discovery.discovery_queryset(f)[: discovery.PAGE_SIZE].explain(analyze=True)
            self.stdout.write(f"--- {label}")
            for line in plan.splitlines():
                if any(k in line for k in ("Scan", "Execution Time", "Planning Time", "SubPlan", "Sort")):
                    self.stdout.write("   " + line.strip()[:150])
