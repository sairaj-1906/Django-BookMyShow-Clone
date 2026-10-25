from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.http import QueryDict
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import discovery
from .analytics import dashboard_tz
from .models import Booking, Genre, Language, Movie, MovieView, Seat, Show, Theater

TZ = dashboard_tz()


def local_dt(days_ahead, hour, minute=0):
    """A datetime `days_ahead` days from today at hour:minute in the dashboard timezone."""
    day = (timezone.now().astimezone(TZ) + timedelta(days=days_ahead)).date()
    return datetime.combine(day, time(hour, minute), tzinfo=TZ)


class DiscoveryBase(TestCase):
    def setUp(self):
        cache.clear()  # facet lists are cached for a few minutes

    @classmethod
    def setUpTestData(cls):
        cls.action, cls.drama, cls.comedy = (Genre.objects.create(name=n) for n in ("Action", "Drama", "Comedy"))
        cls.hindi, cls.english = Language.objects.create(name="Hindi"), Language.objects.create(name="English")
        cls.pvr = Theater.objects.create(name="PVR Pune", city="Pune")
        cls.inox = Theater.objects.create(name="INOX Pune", city="Pune")
        cls.mumbai = Theater.objects.create(name="Cinepolis Mumbai", city="Mumbai")
        cls.buyer = User.objects.create_user("buyer", password="pw")

    # -- helpers
    @staticmethod
    def movie(name, genres=(), languages=(), release=date(2024, 1, 1), rating="0", avg="0", count=0):
        m = Movie.objects.create(
            name=name, image="x.jpg", release_date=release, rating=Decimal(rating),
            average_rating=Decimal(avg), ratings_count=count,
        )
        m.genres.set(genres)
        m.languages.set(languages)
        return m

    @staticmethod
    def show(movie, theater, when, price=200):
        return Show.objects.create(movie=movie, theater=theater, date_time=when, price=price)

    def book(self, movie, n=1, days_ago=1, user=None):
        past = Show.objects.create(movie=movie, theater=self.pvr, date_time=timezone.now() - timedelta(days=days_ago + 1), price=100)
        for i in range(n):
            seat = Seat.objects.create(show=past, seat_number=f"S{i}")
            b = Booking.objects.create(user=user or self.buyer, seat=seat, movie=movie, theater=self.pvr, show=past)
            Booking.objects.filter(pk=b.pk).update(booked_at=timezone.now() - timedelta(days=days_ago))

    def names(self, query="", **extra):
        """Names of the movies on the first result page for a query string."""
        form, filters = discovery.parse(QueryDict(query))
        return [m.name for m in discovery.discovery_queryset(filters)]

    def get_json(self, query=""):
        r = self.client.get(reverse("movie_list") + "?" + query, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(r.status_code, 200)
        return r.json()


class FilterTests(DiscoveryBase):
    def test_title_search_is_case_insensitive_and_accepts_legacy_param(self):
        self.movie("Silent River")
        self.movie("Golden Gate")
        self.assertEqual(self.names("q=SILENT"), ["Silent River"])
        self.assertEqual(self.names("search=silent"), ["Silent River"])
        self.assertEqual(self.get_json("q=gate")["count"], 1)

    def test_search_box_is_on_the_page_once_and_belongs_to_the_filter_form(self):
        self.movie("Silent River")
        html = self.client.get(reverse("movie_list")).content.decode()
        self.assertEqual(html.count('name="q"'), 1)
        self.assertIn("Search for movies by title", html)
        self.assertIn('form="filter-form"', html)  # so Enter / the Search button submit the filters too
        self.assertIn(">Search</button>", html)
        # and a search typed there is applied (the box keeps its value after the page reloads)
        searched = self.client.get(reverse("movie_list"), {"q": "silent"}).content.decode()
        self.assertIn('value="silent"', searched)

    def test_genre_is_or_within_group_and_and_across_groups(self):
        a = self.movie("A", [self.action], [self.hindi])
        b = self.movie("B", [self.drama], [self.hindi])
        c = self.movie("C", [self.comedy], [self.english])
        d = self.movie("D", [self.action, self.drama], [self.english])
        both = f"genre={self.action.id}&genre={self.drama.id}"
        self.assertCountEqual(self.names(both), ["A", "B", "D"])  # Action OR Drama
        self.assertCountEqual(self.names(both + f"&language={self.hindi.id}"), ["A", "B"])  # ...AND Hindi
        self.assertCountEqual(self.names(f"language={self.english.id}"), ["C", "D"])

    def test_movie_with_many_genres_and_shows_is_listed_once(self):
        m = self.movie("Multi", [self.action, self.drama, self.comedy], [self.hindi, self.english])
        for i in range(5):
            self.show(m, self.pvr, local_dt(1 + i, 18))
        q = f"genre={self.action.id}&genre={self.drama.id}&genre={self.comedy.id}&city=Pune"
        self.assertEqual(self.names(q), ["Multi"])
        self.assertEqual(self.get_json(q)["count"], 1)

    def test_city_and_theater_only_match_upcoming_shows(self):
        a, b, c = self.movie("A"), self.movie("B"), self.movie("C")
        self.show(a, self.pvr, local_dt(1, 18))
        self.show(b, self.mumbai, local_dt(1, 18))
        self.show(c, self.pvr, timezone.now() - timedelta(days=1))  # already screened
        self.assertEqual(self.names("city=Pune"), ["A"])
        self.assertEqual(self.names("city=Mumbai"), ["B"])
        self.assertEqual(self.names(f"theater={self.pvr.id}"), ["A"])
        self.assertEqual(self.names(f"theater={self.inox.id}"), [])  # exists, but no upcoming shows
        self.assertEqual(self.names("city=Atlantis"), [])  # unknown city -> no results, not "everything"

    def test_all_show_filters_must_be_met_by_the_same_show(self):
        m = self.movie("Split")
        self.show(m, self.pvr, local_dt(1, 9))  # Pune, morning
        self.show(m, self.mumbai, local_dt(1, 19))  # Mumbai, evening
        self.assertEqual(self.names("city=Pune&timing=evening"), [])  # no single show is both
        self.assertEqual(self.names("city=Pune&timing=morning"), ["Split"])
        self.assertEqual(self.names("city=Mumbai&timing=evening"), ["Split"])

    def test_timing_slot_boundaries_use_local_time(self):
        slots = {"morning": [(5, 0), (11, 59)], "afternoon": [(12, 0), (15, 59)],
                 "evening": [(16, 0), (19, 59)], "night": [(20, 0), (23, 30), (0, 30), (4, 59)]}
        for slot, times in slots.items():
            for hour, minute in times:
                m = self.movie(f"{slot}-{hour:02d}{minute:02d}")
                self.show(m, self.pvr, local_dt(2, hour, minute))
        for slot, times in slots.items():
            got = set(self.names(f"timing={slot}"))
            self.assertEqual(got, {f"{slot}-{h:02d}{mi:02d}" for h, mi in times}, slot)
        both = set(self.names("timing=morning&timing=night"))
        self.assertEqual(len(both), len(slots["morning"]) + len(slots["night"]))

    def test_show_date_filter(self):
        a, b = self.movie("Tomorrow"), self.movie("Later")
        self.show(a, self.pvr, local_dt(1, 20))
        self.show(b, self.pvr, local_dt(3, 20))
        day = local_dt(1, 0).date().isoformat()
        self.assertEqual(self.names(f"show_date={day}"), ["Tomorrow"])

    def test_release_date_range_and_reversed_range(self):
        self.movie("Old", release=date(2020, 5, 1))
        self.movie("Mid", release=date(2023, 5, 1))
        self.movie("New", release=date(2025, 5, 1))
        self.assertEqual(set(self.names("release_from=2022-01-01&release_to=2024-01-01")), {"Mid"})
        self.assertEqual(set(self.names("release_from=2022-01-01")), {"Mid", "New"})
        self.assertEqual(set(self.names("release_from=2024-01-01&release_to=2022-01-01")), {"Mid"})  # swapped

    def test_min_rating_uses_review_average_when_reviews_exist(self):
        self.movie("Legacy4", rating="4.2")  # no reviews yet -> legacy rating counts
        self.movie("Reviewed2", rating="4.9", avg="2.10", count=40)  # reviews override the legacy value
        self.movie("Reviewed5", avg="4.80", count=10)
        self.assertEqual(set(self.names("min_rating=4")), {"Legacy4", "Reviewed5"})
        self.assertEqual(set(self.names("min_rating=2")), {"Legacy4", "Reviewed2", "Reviewed5"})

    def test_invalid_values_are_ignored_not_errors(self):
        self.movie("Only")
        junk = "genre=abc&genre=99999&language=-1&release_from=nope&timing=zzz&sort=hack&page=-5&theater=x&min_rating=9"
        self.assertEqual(self.names(junk), ["Only"])
        self.assertEqual(self.client.get(reverse("movie_list") + "?" + junk).status_code, 200)


class SortAndPriceTests(DiscoveryBase):
    def test_popularity_counts_recent_bookings_only(self):
        hot, warm, stale = self.movie("Hot"), self.movie("Warm"), self.movie("Stale")
        self.book(hot, n=5, days_ago=3)
        self.book(warm, n=2, days_ago=10)
        self.book(stale, n=9, days_ago=discovery.POPULARITY_DAYS + 30)  # outside the window
        self.assertEqual(self.names("")[:3], ["Hot", "Warm", "Stale"])

    def test_newest_and_rating_sorts(self):
        self.movie("A", release=date(2021, 1, 1), avg="3.0", count=5)
        self.movie("B", release=date(2025, 1, 1), avg="4.5", count=5)
        self.movie("C", release=date(2023, 1, 1), rating="4.9")
        self.assertEqual(self.names("sort=newest"), ["B", "C", "A"])
        self.assertEqual(self.names("sort=rating"), ["C", "B", "A"])

    def test_price_sorts_use_cheapest_upcoming_show_and_put_unscheduled_last(self):
        cheap, mid, rich, none = (self.movie(n) for n in ("Cheap", "Mid", "Rich", "NoShows"))
        self.show(cheap, self.pvr, local_dt(1, 18), 120)
        self.show(cheap, self.pvr, local_dt(2, 18), 400)
        self.show(mid, self.pvr, local_dt(1, 18), 250)
        self.show(rich, self.pvr, local_dt(1, 18), 550)
        self.show(none, self.pvr, timezone.now() - timedelta(days=2), 1)  # past show: ignored
        self.assertEqual(self.names("sort=price_asc"), ["Cheap", "Mid", "Rich", "NoShows"])
        self.assertEqual(self.names("sort=price_desc"), ["Rich", "Mid", "Cheap", "NoShows"])
        prices = {m.name: m.from_price for m in discovery.discovery_queryset(discovery.parse(QueryDict(""))[1])}
        self.assertEqual(prices["Cheap"], Decimal("120"))
        self.assertIsNone(prices["NoShows"])

    def test_from_price_respects_show_filters(self):
        m = self.movie("M")
        self.show(m, self.pvr, local_dt(1, 10), 150)  # Pune morning, cheap
        self.show(m, self.mumbai, local_dt(1, 19), 500)  # Mumbai evening
        got = {x.name: x.from_price for x in discovery.discovery_queryset(discovery.parse(QueryDict("city=Mumbai"))[1])}
        self.assertEqual(got["M"], Decimal("500"))

    def test_search_puts_titles_starting_with_the_query_first(self):
        self.movie("The Moonlight")
        self.movie("Moonrise")
        self.movie("Sailing by Moon")
        got = self.names("q=moon")
        self.assertEqual(got[0], "Moonrise")  # the only title that starts with the query
        self.assertCountEqual(got[1:], ["The Moonlight", "Sailing by Moon"])


class PaginationAndCountTests(DiscoveryBase):
    def setUp(self):
        super().setUp()
        for i in range(30):
            self.movie(f"Film {i:02d}", [self.action if i % 2 == 0 else self.drama])

    def test_count_and_page_sizes(self):
        data = self.get_json("")
        self.assertEqual(data["count"], 30)
        r = self.client.get(reverse("movie_list"))
        self.assertEqual(r.context["total"], 30)
        self.assertEqual(len(r.context["page_obj"]), 12)
        self.assertEqual(len(self.client.get(reverse("movie_list"), {"page": 3}).context["page_obj"]), 6)

    def test_count_updates_with_each_filter(self):
        self.assertEqual(self.get_json("")["count"], 30)
        self.assertEqual(self.get_json(f"genre={self.action.id}")["count"], 15)
        self.assertEqual(self.get_json(f"genre={self.action.id}&q=Film 0")["count"], 5)
        self.assertEqual(self.get_json("q=nothing-matches")["count"], 0)
        self.assertIn("0</span> movies found", self.get_json("q=nothing-matches")["html"])

    def test_out_of_range_and_garbage_pages_do_not_error(self):
        self.assertEqual(self.client.get(reverse("movie_list"), {"page": 999}).context["page_obj"].number, 3)
        self.assertEqual(self.client.get(reverse("movie_list"), {"page": "abc"}).context["page_obj"].number, 1)

    def test_page_links_keep_filters_and_json_reports_canonical_url(self):
        data = self.get_json(f"genre={self.action.id}&page=1")
        self.assertIn(f"genre={self.action.id}", data["html"])
        self.assertIn("page=2", data["html"])
        self.assertEqual(self.get_json(f"genre={self.action.id}&page=2")["url"], f"/movies/?genre={self.action.id}&page=2")

    def test_chips_remove_only_their_own_filter(self):
        r = self.client.get(reverse("movie_list"), {"genre": [self.action.id, self.drama.id], "q": "Film"})
        chips = {c["label"]: c["url"] for c in r.context["chips"]}
        self.assertEqual(set(chips), {"Action", "Drama", "Title: Film"})
        self.assertNotIn(f"genre={self.action.id}", chips["Action"])
        self.assertIn(f"genre={self.drama.id}", chips["Action"])
        self.assertIn("q=Film", chips["Action"])

    def test_query_count_does_not_grow_with_the_number_of_movies(self):
        def queries():
            cache.clear()
            with CaptureQueriesContext(connection) as ctx:
                self.client.get(reverse("movie_list") + "?sort=price_asc", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
            return len(ctx)

        small = queries()
        for i in range(100):
            m = self.movie(f"Extra {i}", [self.comedy], [self.hindi])
            self.show(m, self.pvr, local_dt(1, 18))
        self.assertEqual(small, queries())


class ViewTrackingTests(DiscoveryBase):
    def request(self, user=None, session=None):
        req = RequestFactory().get("/")
        req.user = user or type("Anon", (), {"is_authenticated": False})()
        req.session = session if session is not None else {}
        return req

    def test_logged_in_views_are_upserted_and_pruned(self):
        movies = [self.movie(f"M{i}") for i in range(discovery.MAX_STORED_VIEWS + 5)]
        req = self.request(self.buyer)
        for m in movies:
            discovery.record_view(req, m)
        discovery.record_view(req, movies[-1])  # revisiting must not duplicate
        self.assertEqual(MovieView.objects.filter(user=self.buyer).count(), discovery.MAX_STORED_VIEWS)
        self.assertEqual(discovery.recent_movie_ids(req)[0], movies[-1].id)

    def test_anonymous_views_live_in_the_session_deduplicated_and_capped(self):
        movies = [self.movie(f"M{i}") for i in range(discovery.MAX_RECENT_VIEWS + 3)]
        session = {}
        req = self.request(session=session)
        for m in movies:
            discovery.record_view(req, m)
        discovery.record_view(req, movies[5])
        ids = session["recent_movie_ids"]
        self.assertEqual(ids[0], movies[5].id)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertLessEqual(len(ids), discovery.MAX_RECENT_VIEWS)

    def test_detail_page_records_the_view(self):
        m = self.movie("Seen")
        self.client.force_login(self.buyer)
        self.assertEqual(self.client.get(reverse("movie_detail", args=[m.id])).status_code, 200)
        self.assertTrue(MovieView.objects.filter(user=self.buyer, movie=m).exists())

    def test_views_made_before_login_still_count(self):
        m = self.movie("EarlyLook")
        session = {"recent_movie_ids": [m.id]}
        self.assertEqual(discovery.recent_movie_ids(self.request(self.buyer, session)), [m.id])


class RecommendationTests(DiscoveryBase):
    def catalogue(self):
        """Bookable catalogue: 3 Action/Hindi, 3 Drama/English, 2 Comedy/Hindi (all upcoming)."""
        out = {}
        for i in range(3):
            out[f"act{i}"] = self.movie(f"Action {i}", [self.action], [self.hindi], rating="3.0")
            out[f"dra{i}"] = self.movie(f"Drama {i}", [self.drama], [self.english], rating="3.0")
        for i in range(2):
            out[f"com{i}"] = self.movie(f"Comedy {i}", [self.comedy], [self.hindi], rating="3.0")
        for m in out.values():
            self.show(m, self.pvr, local_dt(1, 18))
        return out

    def recs(self, user=None, session=None, limit=8):
        req = RequestFactory().get("/")
        req.user = user or type("Anon", (), {"is_authenticated": False})()
        req.session = session if session is not None else {}
        return discovery.recommend_for(req, limit=limit)

    def test_recommends_similar_movies_and_excludes_ones_already_booked(self):
        c = self.catalogue()
        self.book(c["act0"], user=self.buyer)
        rec = self.recs(self.buyer, limit=2)
        self.assertTrue(rec.personalized)
        self.assertEqual(rec.title, "Recommended for You")
        self.assertEqual({m.name for m in rec.movies}, {"Action 1", "Action 2"})  # same genre first, never Action 0
        self.assertIn("Action", rec.subtitle)

    def test_booked_movies_outweigh_merely_viewed_ones(self):
        c = self.catalogue()
        self.book(c["dra0"], user=self.buyer)  # booked Drama
        MovieView.objects.create(user=self.buyer, movie=c["act0"])  # only looked at Action
        top = self.recs(self.buyer, limit=1).movies[0]
        self.assertEqual(top.name.split()[0], "Drama")

    def test_recently_viewed_alone_drives_recommendations_and_is_excluded(self):
        c = self.catalogue()
        rec = self.recs(session={"recent_movie_ids": [c["com0"].id]}, limit=1)
        self.assertTrue(rec.personalized)
        self.assertEqual(rec.movies[0].name, "Comedy 1")

    def test_only_bookable_movies_are_recommended(self):
        c = self.catalogue()
        no_shows = self.movie("Action NoShows", [self.action], [self.hindi], rating="5")
        self.book(c["act0"], user=self.buyer)
        names = {m.name for m in self.recs(self.buyer).movies}
        self.assertNotIn("Action NoShows", names)

    def test_cold_start_falls_back_to_trending(self):
        c = self.catalogue()
        self.book(c["dra1"], n=4, user=self.buyer)  # someone else's popularity...
        rec = self.recs(User.objects.create_user("newbie", password="pw"))
        self.assertFalse(rec.personalized)
        self.assertEqual(rec.title, "Trending Now")
        self.assertEqual(rec.movies[0].name, "Drama 1")  # most booked recently

    def test_tops_up_with_trending_when_few_similar_movies_exist(self):
        c = self.catalogue()
        self.book(c["com0"], user=self.buyer)  # only 1 other Comedy exists
        rec = self.recs(self.buyer, limit=5)
        self.assertTrue(rec.personalized)
        self.assertEqual(len(rec.movies), 5)
        self.assertEqual(len({m.id for m in rec.movies}), 5)  # no duplicates
        self.assertNotIn(c["com0"].id, {m.id for m in rec.movies})

    def test_recommendation_queries_are_bounded(self):
        c = self.catalogue()
        for key in ("act0", "dra0", "com0"):
            self.book(c[key], user=self.buyer)
        with CaptureQueriesContext(connection) as ctx:
            self.recs(self.buyer)
        self.assertLessEqual(len(ctx), 10)

    def test_home_and_discover_pages_render_recommendations(self):
        c = self.catalogue()
        self.book(c["act0"], user=self.buyer)
        self.client.force_login(self.buyer)
        for url in (reverse("home"), reverse("movie_list")):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200)
            self.assertContains(r, "Recommended for You")
            self.assertNotContains(r, "{%")
            self.assertNotContains(r, "{{")
