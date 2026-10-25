"""
Movie discovery: search, filters, sorting and "Recommended for You".

Query design (see docs/TASK5_NOTES.md for measurements)
-------------------------------------------------------
* Every filter is a correlated EXISTS / scalar subquery — never a JOIN onto genres, languages,
  shows or bookings. Joining those would multiply rows (a movie with 3 genres x 20 shows x
  5,000 bookings becomes 300,000 rows), force DISTINCT, and break COUNT(). With subqueries a
  movie row can never be duplicated, so pagination and counts stay exact and cheap.
* All show-level filters (city, theater, time of day, date) go into ONE subquery on Show, so
  "evening shows at PVR in Pune" means *the same show* satisfies all three, not three
  different shows.
* "Starting from" price and popularity are scalar subqueries backed by composite indexes
  (Show: movie+date_time, Booking: movie+booked_at), so each is an index range scan.
* Nothing is loaded into Python except the 12 movies on the current page (+ one prefetch for
  their genres). Description/cast text columns are deferred.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django import forms
from django.core.cache import cache
from django.db.models import (
    Case,
    DecimalField,
    Exists,
    F,
    IntegerField,
    Min,
    OuterRef,
    Q,
    Subquery,
    Sum,
    Value,
    When,
    Count,
)
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone

from .analytics import dashboard_tz, local_hour
from .models import Booking, Genre, Language, Movie, MovieView, Show, Theater

PAGE_SIZE = 12
POPULARITY_DAYS = 90  # "popular" = most tickets booked in this many recent days
BOOKED_WEIGHT = 3  # a movie the user booked says more about taste than one they merely opened
VIEWED_WEIGHT = 1
MAX_SEED_BOOKINGS = 50
MAX_RECENT_VIEWS = 20
MAX_STORED_VIEWS = 50
FACET_CACHE_KEY = "discovery_facets_v1"
FACET_CACHE_SECONDS = 300

# Show timings, in the dashboard timezone (hour of day, end exclusive).
TIMING_SLOTS = {
    "morning": ("Morning (5am-12pm)", [(5, 12)]),
    "afternoon": ("Afternoon (12pm-4pm)", [(12, 16)]),
    "evening": ("Evening (4pm-8pm)", [(16, 20)]),
    "night": ("Night (8pm-5am)", [(20, 24), (0, 5)]),
}
SORT_CHOICES = [
    ("", "Most popular"),
    ("newest", "Newest releases"),
    ("rating", "Highest rated"),
    ("price_asc", "Ticket price: low to high"),
    ("price_desc", "Ticket price: high to low"),
]
RATING_CHOICES = [("", "Any rating"), ("4", "4 stars & above"), ("3", "3 stars & above"), ("2", "2 stars & above")]

EFFECTIVE_RATING = Case(
    # Once real reviews exist use their average, otherwise the legacy manually-set rating.
    When(ratings_count__gt=0, then=F("average_rating")),
    default=F("rating"),
    output_field=DecimalField(max_digits=4, decimal_places=2),
)


# --------------------------------------------------------------------------- facets
def get_facets():
    """Option lists for the filter form. Small tables, cached briefly so a page of results
    costs no extra queries for the sidebar."""

    def load():
        now = timezone.now()
        upcoming = Show.objects.filter(theater=OuterRef("pk"), date_time__gte=now)
        theaters = list(
            Theater.objects.filter(Exists(upcoming))
            .order_by("city", "name")
            .values_list("id", "name", "city")
        )
        return {
            "genres": list(Genre.objects.order_by("name").values_list("id", "name")),
            "languages": list(Language.objects.order_by("name").values_list("id", "name")),
            "cities": sorted({c for _, _, c in theaters if c}),
            "theaters": theaters,
        }

    return cache.get_or_set(FACET_CACHE_KEY, load, FACET_CACHE_SECONDS)


# ---------------------------------------------------------------------------- form
class LenientChoiceField(forms.ChoiceField):
    """The dropdown only LISTS cities/theaters that have upcoming shows, but a link to a city
    or theater without any should return "0 movies", not be silently ignored (which would
    show every movie and look like a bug)."""

    def valid_value(self, value):
        return True


class DiscoveryForm(forms.Form):
    """Validates and renders the filter sidebar. Anything invalid (an unknown genre id, a
    malformed date...) is simply ignored, so a stale bookmarked URL never errors."""

    q = forms.CharField(
        required=False,
        max_length=100,
        label="Search",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "Search for movies by title",
                "type": "search",
                "autocomplete": "off",
                "form": "filter-form",  # the box sits above the form but belongs to it
                "aria-label": "Search movies by title",
            }
        ),
    )
    genre = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple)
    language = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple)
    city = LenientChoiceField(required=False, widget=forms.Select(attrs={"class": "form-control"}))
    theater = LenientChoiceField(required=False, widget=forms.Select(attrs={"class": "form-control"}))
    release_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}))
    release_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}))
    min_rating = forms.ChoiceField(
        required=False, choices=RATING_CHOICES, widget=forms.Select(attrs={"class": "form-control"})
    )
    timing = forms.MultipleChoiceField(
        required=False,
        choices=[(slug, label) for slug, (label, _) in TIMING_SLOTS.items()],
        widget=forms.CheckboxSelectMultiple,
    )
    show_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}))
    sort = forms.ChoiceField(
        required=False,
        choices=SORT_CHOICES,
        widget=forms.Select(attrs={"class": "form-control form-control-sm", "form": "filter-form"}),
    )
    page = forms.IntegerField(required=False, min_value=1)

    def __init__(self, data=None, *args, facets=None, **kwargs):
        if data is not None and "search" in data and "q" not in data:  # legacy ?search= links
            data = data.copy()
            data["q"] = data["search"]
        super().__init__(data, *args, **kwargs)
        facets = facets or get_facets()
        self.fields["genre"].choices = [(str(i), n) for i, n in facets["genres"]]
        self.fields["language"].choices = [(str(i), n) for i, n in facets["languages"]]
        self.fields["city"].choices = [("", "All cities")] + [(c, c) for c in facets["cities"]]
        grouped = {}
        for tid, name, city in facets["theaters"]:
            grouped.setdefault(city or "Other", []).append((str(tid), name))
        self.fields["theater"].choices = [("", "All theaters")] + [(c, opts) for c, opts in grouped.items()]


@dataclass(frozen=True)
class Filters:
    q: str = ""
    genres: tuple = ()
    languages: tuple = ()
    city: str = ""
    theater: int = 0
    release_from: date = None
    release_to: date = None
    min_rating: str = ""
    timings: tuple = ()
    show_date: date = None
    sort: str = ""
    page: int = 1

    @property
    def show_level(self):
        """True if any filter constrains *shows* (then only movies with a matching upcoming
        show are listed)."""
        return bool(self.city or self.theater or self.timings or self.show_date)

    @property
    def active(self):
        return bool(
            self.q or self.genres or self.languages or self.release_from or self.release_to
            or self.min_rating or self.show_level
        )


def parse(query):
    """QueryDict -> (bound form, Filters)."""
    form = DiscoveryForm(query)
    form.is_valid()  # populates cleaned_data with every field that DID validate
    d = getattr(form, "cleaned_data", {})
    release_from, release_to = d.get("release_from"), d.get("release_to")
    if release_from and release_to and release_from > release_to:
        release_from, release_to = release_to, release_from
    filters = Filters(
        q=(d.get("q") or "").strip(),
        genres=tuple(sorted(int(g) for g in d.get("genre") or [])),
        languages=tuple(sorted(int(x) for x in d.get("language") or [])),
        city=(d.get("city") or "")[:100],
        theater=int(d["theater"]) if str(d.get("theater") or "").isdigit() else 0,
        release_from=release_from,
        release_to=release_to,
        min_rating=d.get("min_rating") or "",
        timings=tuple(t for t in TIMING_SLOTS if t in (d.get("timing") or [])),
        show_date=d.get("show_date"),
        sort=d.get("sort") or "",
        page=d.get("page") or 1,
    )
    return form, filters


# ------------------------------------------------------------------------- queryset
def _show_subquery(f, now, tz):
    """Upcoming shows of the outer movie that satisfy EVERY show-level filter at once."""
    qs = Show.objects.filter(movie=OuterRef("pk"), date_time__gte=now)
    if f.city:
        qs = qs.filter(theater__city=f.city)
    if f.theater:
        qs = qs.filter(theater_id=f.theater)
    if f.show_date:
        day_start = datetime.combine(f.show_date, time.min, tzinfo=tz)
        qs = qs.filter(date_time__gte=day_start, date_time__lt=day_start + timedelta(days=1))
    if f.timings:
        in_slot = Q()
        for slug in f.timings:
            for lo, hi in TIMING_SLOTS[slug][1]:
                in_slot |= Q(_hour__gte=lo, _hour__lt=hi)
        qs = qs.annotate(_hour=local_hour("date_time", now, now + timedelta(days=365))).filter(in_slot)
    return qs


def _popularity(now):
    cutoff = now - timedelta(days=POPULARITY_DAYS)
    bookings = (
        Booking.objects.filter(movie=OuterRef("pk"), booked_at__gte=cutoff)
        .order_by()
        .values("movie")
        .annotate(c=Count("pk"))
        .values("c")
    )
    return Coalesce(Subquery(bookings, output_field=IntegerField()), 0)


def _from_price(show_qs):
    cheapest = show_qs.order_by().values("movie").annotate(m=Min("price")).values("m")
    return Subquery(cheapest, output_field=DecimalField(max_digits=8, decimal_places=2))


def card_queryset(qs, show_qs, defer_text=True):
    """What a movie card needs: rating, "from" price, genres — in a constant number of queries.
    Long text columns are skipped unless the caller shows them (the home page does)."""
    qs = qs.annotate(effective_rating=EFFECTIVE_RATING, from_price=_from_price(show_qs)).prefetch_related("genres")
    return qs.defer("description", "cast", "tagline") if defer_text else qs


def discovery_queryset(f, now=None):
    now = now or timezone.now()
    tz = dashboard_tz()
    show_qs = _show_subquery(f, now, tz)
    qs = card_queryset(Movie.objects.all(), show_qs)

    if f.q:
        qs = qs.filter(name__icontains=f.q)
    if f.genres:
        qs = qs.filter(Exists(Movie.genres.through.objects.filter(movie_id=OuterRef("pk"), genre_id__in=f.genres)))
    if f.languages:
        qs = qs.filter(Exists(Movie.languages.through.objects.filter(movie_id=OuterRef("pk"), language_id__in=f.languages)))
    if f.release_from:
        qs = qs.filter(release_date__gte=f.release_from)
    if f.release_to:
        qs = qs.filter(release_date__lte=f.release_to)
    if f.min_rating:
        qs = qs.filter(effective_rating__gte=Decimal(f.min_rating))
    if f.show_level:
        qs = qs.filter(Exists(show_qs))

    sort = f.sort or ("relevance" if f.q else "popular")
    if sort == "newest":
        return qs.order_by("-release_date", "-id")
    if sort == "rating":
        return qs.order_by("-effective_rating", "-ratings_count", "-id")
    if sort == "price_asc":  # movies with no matching upcoming show go last
        return qs.order_by(F("from_price").asc(nulls_last=True), "name", "id")
    if sort == "price_desc":
        return qs.order_by(F("from_price").desc(nulls_last=True), "name", "id")
    qs = qs.annotate(popularity=_popularity(now))
    if sort == "relevance":  # titles that START with the query first
        qs = qs.annotate(title_match=Case(When(name__istartswith=f.q, then=0), default=1, output_field=IntegerField()))
        return qs.order_by("title_match", "-popularity", "-effective_rating", "-id")
    return qs.order_by("-popularity", "-effective_rating", "-release_date", "-id")


# ------------------------------------------------------- URLs, chips, page links
def canonical_pairs(f):
    """The filters as a clean, stable list of (param, value) pairs (used for every link)."""
    pairs = []
    if f.q:
        pairs.append(("q", f.q))
    pairs += [("genre", str(g)) for g in f.genres]
    pairs += [("language", str(x)) for x in f.languages]
    if f.city:
        pairs.append(("city", f.city))
    if f.theater:
        pairs.append(("theater", str(f.theater)))
    if f.release_from:
        pairs.append(("release_from", f.release_from.isoformat()))
    if f.release_to:
        pairs.append(("release_to", f.release_to.isoformat()))
    if f.min_rating:
        pairs.append(("min_rating", f.min_rating))
    pairs += [("timing", t) for t in f.timings]
    if f.show_date:
        pairs.append(("show_date", f.show_date.isoformat()))
    if f.sort:
        pairs.append(("sort", f.sort))
    return pairs


def build_url(pairs, page=None):
    pairs = list(pairs)
    if page and page > 1:
        pairs.append(("page", str(page)))
    base = reverse("movie_list")
    return f"{base}?{urlencode(pairs)}" if pairs else base


def active_chips(f, form):
    """One removable chip per active filter value."""
    names = {
        "genre": dict(form.fields["genre"].choices),
        "language": dict(form.fields["language"].choices),
        "timing": dict(form.fields["timing"].choices),
        "theater": {v: n for _, opts in form.fields["theater"].choices[1:] for v, n in opts},
    }
    pairs = canonical_pairs(f)
    chips = []
    for i, (key, value) in enumerate(pairs):
        if key == "sort":
            continue
        label = {
            "q": f"Title: {value}",
            "genre": names["genre"].get(value, value),
            "language": names["language"].get(value, value),
            "city": f"City: {value}",
            "theater": names["theater"].get(value, "Theater"),
            "release_from": f"Released from {value}",
            "release_to": f"Released until {value}",
            "min_rating": f"{value}+ stars",
            "timing": names["timing"].get(value, value).split(" (")[0],
            "show_date": f"Show on {value}",
        }[key]
        chips.append({"label": label, "url": build_url(pairs[:i] + pairs[i + 1 :])})
    return chips


def page_links(page_obj, f):
    pairs = canonical_pairs(f)
    total, current = page_obj.paginator.num_pages, page_obj.number
    numbers = sorted({1, total, *range(max(1, current - 2), min(total, current + 2) + 1)})
    links, previous = [], 0
    for n in numbers:
        if previous and n - previous > 1:
            links.append({"ellipsis": True})
        links.append({"number": n, "url": build_url(pairs, n), "current": n == current})
        previous = n
    return links


# ------------------------------------------------------------ recently viewed
def record_view(request, movie):
    """Remember that this visitor opened `movie` (DB for users, session for anonymous)."""
    if request.user.is_authenticated:
        MovieView.objects.update_or_create(
            user=request.user, movie=movie, defaults={"viewed_at": timezone.now()}
        )
        stale = list(
            MovieView.objects.filter(user=request.user)
            .order_by("-viewed_at")
            .values_list("pk", flat=True)[MAX_STORED_VIEWS:]
        )
        if stale:
            MovieView.objects.filter(pk__in=stale).delete()
    ids = [movie.id] + [i for i in request.session.get("recent_movie_ids", []) if i != movie.id]
    request.session["recent_movie_ids"] = ids[:MAX_RECENT_VIEWS]


def recent_movie_ids(request):
    """Most-recent-first ids: saved views for logged-in users, then this browser's session
    (which also covers pages opened before logging in)."""
    ids = []
    if request.user.is_authenticated:
        ids = list(
            MovieView.objects.filter(user=request.user)
            .order_by("-viewed_at")
            .values_list("movie_id", flat=True)[:MAX_RECENT_VIEWS]
        )
    for movie_id in request.session.get("recent_movie_ids", []):
        if movie_id not in ids:
            ids.append(movie_id)
    return ids[:MAX_RECENT_VIEWS]


def recently_viewed(request, limit=6):
    ids = recent_movie_ids(request)[:limit]
    if not ids:
        return []
    now = timezone.now()
    show_qs = Show.objects.filter(movie=OuterRef("pk"), date_time__gte=now)
    movies = card_queryset(Movie.objects.filter(id__in=ids), show_qs).in_bulk()
    return [movies[i] for i in ids if i in movies]


# ------------------------------------------------------------- recommendations
@dataclass
class Recommendation:
    title: str
    subtitle: str
    movies: list
    personalized: bool


def _seed_movies(request):
    """(booked_ids, viewed_ids), both most recent first."""
    booked, viewed = [], []
    if request.user.is_authenticated:
        seen = set()
        rows = (
            Booking.objects.filter(user=request.user)
            .order_by("-booked_at")
            .values_list("movie_id", flat=True)[:300]
        )
        for movie_id in rows:
            if movie_id not in seen:
                seen.add(movie_id)
                booked.append(movie_id)
            if len(booked) >= MAX_SEED_BOOKINGS:
                break
    return booked, recent_movie_ids(request)


def _taste(through, field, seeds, booked):
    """{genre_or_language_id: weight} from the seed movies (booked count more than viewed)."""
    weight = (
        Case(When(movie_id__in=booked, then=Value(BOOKED_WEIGHT)), default=Value(VIEWED_WEIGHT), output_field=IntegerField())
        if booked
        else Value(VIEWED_WEIGHT, output_field=IntegerField())
    )
    rows = through.objects.filter(movie_id__in=seeds).values(field).annotate(score=Sum(weight))
    return {r[field]: r["score"] for r in rows}


def _match_score(through, field, weights, factor):
    cases = [When(**{field: key}, then=Value(value * factor)) for key, value in weights.items()]
    per_movie = (
        through.objects.filter(movie_id=OuterRef("pk"), **{f"{field}__in": list(weights)})
        .values("movie_id")
        .annotate(s=Sum(Case(*cases, default=Value(0), output_field=IntegerField())))
        .values("s")
    )
    return Coalesce(Subquery(per_movie, output_field=IntegerField()), 0)


def recommend_for(request, limit=8, now=None):
    """"Recommended for You": movies similar (genre first, language second) to what the user
    booked or recently viewed, excluding those they have already seen, limited to movies they
    can actually book now. Cold start (no history) falls back to what is trending."""
    now = now or timezone.now()
    upcoming = Show.objects.filter(movie=OuterRef("pk"), date_time__gte=now)
    booked, viewed = _seed_movies(request)
    seen = set(booked) | set(viewed)
    picks, reason = [], ""

    if seen:
        genres_t, langs_t = Movie.genres.through, Movie.languages.through
        gw = _taste(genres_t, "genre_id", seen, booked)
        lw = _taste(langs_t, "language_id", seen, booked)
        if gw or lw:
            score = Value(0, output_field=IntegerField())
            if gw:
                score = score + _match_score(genres_t, "genre_id", gw, 2)  # genre counts double
            if lw:
                score = score + _match_score(langs_t, "language_id", lw, 1)
            picks = list(
                card_queryset(Movie.objects.exclude(pk__in=seen).filter(Exists(upcoming)), upcoming, defer_text=False)
                .annotate(match_score=score)
                .filter(match_score__gt=0)
                .order_by("-match_score", "-effective_rating", "-release_date", "-id")[:limit]
            )
            if gw:
                top = sorted(gw, key=gw.get, reverse=True)[:2]
                names = dict(Genre.objects.filter(id__in=top).values_list("id", "name"))
                reason = " and ".join(names[i] for i in top if i in names)
                reason = f"Because you like {reason} movies" if reason else ""
            elif lw:
                top = max(lw, key=lw.get)
                name = Language.objects.filter(id=top).values_list("name", flat=True).first()
                reason = f"Because you watch {name} movies" if name else ""

    personalized = bool(picks)
    if len(picks) < limit:  # top up (or cold start) with what is popular right now
        have = seen | {m.pk for m in picks}
        trending = list(
            card_queryset(Movie.objects.exclude(pk__in=have).filter(Exists(upcoming)), upcoming, defer_text=False)
            .annotate(popularity=_popularity(now))
            .order_by("-popularity", "-effective_rating", "-id")[: limit - len(picks)]
        )
        picks += trending
    if personalized:
        return Recommendation("Recommended for You", reason or "Based on your bookings and recently viewed movies", picks, True)
    return Recommendation("Trending Now", "Book a movie or browse a few to get picks made for you", picks, False)
