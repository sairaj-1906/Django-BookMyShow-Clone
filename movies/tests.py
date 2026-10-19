import re
from datetime import date, datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from decimal import Decimal
from unittest import mock, skipUnless

from django.conf import settings
from django.contrib.auth.models import Permission, User
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import analytics
from .models import Booking, Cancellation, Movie, Payment, Seat, Show, Theater

UTC = dt_timezone.utc


def utc(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=UTC)


class DashboardTestBase(TestCase):
    """Small hand-computable data set (all times UTC; the dashboard buckets in IST = UTC+5:30).

    Theater A: show s1 (M1, 10 Mar, Rs200, 4 seats, 2 sold) + show s_old (M1, Feb, outside range)
    Theater B: show s2 (M2, 11 Mar, Rs100, 5 seats, 1 sold)
    Payments: p1 Rs400 (10 Mar 14:00Z -> 19:30 IST), p2 Rs100 (11 Mar 06:00Z -> 11:30 IST),
              p3 failed, p_old Rs999 in February (must be excluded from March reports)
    1 processed refund of Rs100 on 12 Mar.
    """

    @classmethod
    def setUpTestData(cls):
        cls.m1 = Movie.objects.create(name="Alpha", image="x.jpg")
        cls.m2 = Movie.objects.create(name="Beta", image="x.jpg")
        cls.tA = Theater.objects.create(name="Theater A", city="Pune")
        cls.tB = Theater.objects.create(name="Theater B", city="Mumbai")

        def make_show(movie, theater, when, price, n_seats, n_sold):
            show = Show.objects.create(movie=movie, theater=theater, date_time=when, price=price)
            seats = [
                Seat.objects.create(show=show, seat_number=f"A{i}", is_booked=i < n_sold)
                for i in range(n_seats)
            ]
            return show, seats

        cls.s1, seats1 = make_show(cls.m1, cls.tA, utc(2026, 3, 10, 18), 200, 4, 2)
        cls.s2, seats2 = make_show(cls.m2, cls.tB, utc(2026, 3, 11, 18), 100, 5, 1)
        cls.s_old, _ = make_show(cls.m1, cls.tA, utc(2026, 2, 10, 18), 200, 4, 4)

        cls.buyer = User.objects.create_user("buyer", password="x")

        def pay(order, amount, status, show, paid_at=None, created=None):
            p = Payment.objects.create(
                user=cls.buyer, show=show, seat_numbers="A0", amount=amount,
                razorpay_order_id=order, status=status, paid_at=paid_at,
            )
            Payment.objects.filter(pk=p.pk).update(created_at=created or paid_at)
            return p

        cls.p1 = pay("o1", 400, Payment.STATUS_SUCCESS, cls.s1, utc(2026, 3, 10, 14))
        cls.p2 = pay("o2", 100, Payment.STATUS_SUCCESS, cls.s2, utc(2026, 3, 11, 6))
        pay("o3", 100, Payment.STATUS_FAILED, cls.s2, created=utc(2026, 3, 11, 7))
        pay("o_old", 999, Payment.STATUS_SUCCESS, cls.s_old, utc(2026, 2, 20, 10))

        def book(seat, show, payment, when):
            b = Booking.objects.create(
                user=cls.buyer, seat=seat, movie=show.movie, theater=show.theater, show=show, payment=payment
            )
            Booking.objects.filter(pk=b.pk).update(booked_at=when)

        book(seats1[0], cls.s1, cls.p1, utc(2026, 3, 10, 14))
        book(seats1[1], cls.s1, cls.p1, utc(2026, 3, 10, 14, 5))
        book(seats2[0], cls.s2, cls.p2, utc(2026, 3, 11, 6))

        Cancellation.objects.create(
            user=cls.buyer, show=cls.s1, payment=cls.p1, seat_number="A9", refund_amount=100,
            refund_status=Cancellation.REFUND_PROCESSED, cancelled_at=utc(2026, 3, 12, 10),
        )

        for name, joined in (("early", utc(2026, 2, 1)), ("in1", utc(2026, 3, 5, 3)), ("in2", utc(2026, 3, 10, 3))):
            u = User.objects.create_user(name, password="x")
            User.objects.filter(pk=u.pk).update(date_joined=joined)

        cls.f = analytics.build_filters(date(2026, 3, 1), date(2026, 3, 31), "day")


class AnalyticsCorrectnessTests(DashboardTestBase):
    def test_revenue_trend_buckets_by_local_day_and_excludes_out_of_range(self):
        series = analytics.revenue_trend(self.f)
        self.assertEqual(len(series), 31)  # gap-free: every day of March is present
        by_day = {p["bucket"]: p for p in series}
        self.assertEqual(by_day[date(2026, 3, 10)]["revenue"], Decimal("400"))
        self.assertEqual(by_day[date(2026, 3, 11)]["revenue"], Decimal("100"))
        self.assertEqual(sum(p["revenue"] for p in series), Decimal("500"))  # Feb Rs999 excluded

    def test_end_date_is_inclusive_and_start_boundary_respected(self):
        only_10th = analytics.build_filters(date(2026, 3, 10), date(2026, 3, 10), "day")
        self.assertEqual(sum(p["revenue"] for p in analytics.revenue_trend(only_10th)), Decimal("400"))
        only_11th = analytics.build_filters(date(2026, 3, 11), date(2026, 3, 11), "day")
        self.assertEqual(sum(p["revenue"] for p in analytics.revenue_trend(only_11th)), Decimal("100"))

    def test_booking_trend_and_peak_hours(self):
        self.assertEqual(sum(p["bookings"] for p in analytics.booking_trend(self.f)), 3)
        hours = {h["hour"]: h["bookings"] for h in analytics.peak_hours(self.f)}
        self.assertEqual(hours[19], 2)  # 14:00Z / 14:05Z == 19:30 / 19:35 IST
        self.assertEqual(hours[11], 1)  # 06:00Z == 11:30 IST
        self.assertEqual(sum(hours.values()), 3)
        self.assertEqual(len(analytics.peak_hours(self.f)), 24)

    def test_top_movies_and_theaters(self):
        movies = analytics.top_movies(self.f)
        self.assertEqual([(m["movie"], m["bookings"]) for m in movies], [("Alpha", 2), ("Beta", 1)])
        self.assertEqual(movies[0]["revenue"], Decimal("400.00"))
        theaters = analytics.top_theaters(self.f)
        self.assertEqual([(t["theater"], t["revenue"]) for t in theaters],
                         [("Theater A", Decimal("400.00")), ("Theater B", Decimal("100.00"))])
        self.assertEqual(theaters[0]["bookings"], 2)

    def test_occupancy_per_theater(self):
        occ = {o["theater"]: o for o in analytics.occupancy_by_theater(self.f)}
        self.assertEqual(occ["Theater A"]["occupancy"], 50.0)  # 2 / 4 (February show excluded)
        self.assertEqual(occ["Theater B"]["occupancy"], 20.0)  # 1 / 5

    def test_cancellations_refunds_and_payment_outcomes(self):
        c = analytics.cancellation_stats(self.f)
        self.assertEqual(c["cancellations"], 1)
        self.assertEqual(c["cancellation_rate"], 25.0)  # 1 / (3 bookings + 1 cancellation)
        self.assertEqual(c["refund_processed_amount"], Decimal("100.00"))
        self.assertEqual((c["payments_success"], c["payments_failed"]), (2, 1))
        self.assertEqual(c["payment_success_rate"], 66.67)

    def test_user_growth_is_cumulative(self):
        series = analytics.user_growth(self.f)
        self.assertEqual(sum(p["new_users"] for p in series), 2)
        before = User.objects.filter(date_joined__lt=self.f.start).count()
        self.assertEqual(series[-1]["total_users"], before + 2)
        self.assertEqual(series[0]["total_users"], before)

    def test_revenue_summary_calendar_periods(self):
        now = utc(2026, 3, 11, 12)  # a Wednesday; week starts Mon 9 Mar
        s = analytics.revenue_summary(now=now)
        self.assertEqual(s["today"], Decimal("100.00"))
        self.assertEqual(s["week"], Decimal("500.00"))
        self.assertEqual(s["month"], Decimal("500.00"))
        self.assertEqual(s["year"], Decimal("1499.00"))  # includes the February payment

    def test_granularity_week_month_year(self):
        for gran in ("week", "month", "year"):
            f = analytics.build_filters(date(2026, 3, 1), date(2026, 3, 31), gran)
            self.assertEqual(sum(p["revenue"] for p in analytics.revenue_trend(f)), Decimal("500"), gran)

    def test_no_n_plus_one_query_count_is_independent_of_data_size(self):
        with CaptureQueriesContext(connection) as small:
            analytics.dashboard_data(self.f)
        for i in range(60):  # add lots more rows, then measure again
            seat = Seat.objects.create(show=self.s2, seat_number=f"Z{i}")
            Booking.objects.create(user=self.buyer, seat=seat, movie=self.m2, theater=self.tB, show=self.s2)
        with CaptureQueriesContext(connection) as large:
            analytics.dashboard_data(self.f)
        self.assertEqual(len(small), len(large))


class TimezoneBucketingTests(DashboardTestBase):
    @skipUnless(connection.vendor == "sqlite", "fast path only exists for SQLite")
    def test_sqlite_fast_path_matches_standard_trunc_for_every_granularity(self):
        for gran in analytics.GRANULARITIES:
            f = analytics.build_filters(date(2026, 1, 1), date(2026, 12, 31), gran)
            self.assertIsNotNone(analytics._sqlite_fast_minutes(f))  # IST: constant offset
            fast = (analytics.revenue_trend(f), analytics.booking_trend(f),
                    analytics.user_growth(f), analytics.peak_hours(f))
            with mock.patch.object(analytics, "_sqlite_fast_minutes", return_value=None):
                slow = (analytics.revenue_trend(f), analytics.booking_trend(f),
                        analytics.user_growth(f), analytics.peak_hours(f))
            self.assertEqual(fast, slow, gran)

    @override_settings(DASHBOARD_TIMEZONE="America/New_York")
    def test_dst_timezone_never_uses_fixed_offset_shortcut(self):
        across_dst = analytics.build_filters(date(2026, 3, 1), date(2026, 3, 31), "day")
        self.assertIsNone(analytics._fixed_offset_minutes(across_dst))
        # and the standard path still produces correct, local-day buckets
        self.assertEqual(sum(p["bookings"] for p in analytics.booking_trend(across_dst)), 3)

    def test_ist_offset_is_330_minutes(self):
        self.assertEqual(analytics._fixed_offset_minutes(self.f), 330)


class DashboardAccessTests(DashboardTestBase):
    def setUp(self):
        perm = Permission.objects.get(codename="view_dashboard")
        self.admin = User.objects.create_superuser("root", "r@x.com", "pw")
        self.analyst = User.objects.create_user("analyst", password="pw", is_staff=True)
        self.analyst.user_permissions.add(perm)
        self.noperm = User.objects.create_user("noperm", password="pw", is_staff=True)
        self.customer = User.objects.create_user("customer", password="pw")
        self.customer.user_permissions.add(perm)  # has the permission but is NOT staff

    def _status(self, username, url):
        self.client.logout()
        if username:
            self.client.login(username=username, password="pw")
        return self.client.get(url).status_code

    def test_dashboard_and_exports_are_protected(self):
        urls = [reverse("dashboard"), reverse("dashboard_export", args=["all"]),
                reverse("dashboard_export", args=["bookings_detail"])]
        for url in urls:
            self.assertEqual(self._status(None, url), 302, url)  # -> admin login
            self.assertEqual(self._status("customer", url), 302, url)  # not staff
            self.assertEqual(self._status("noperm", url), 403, url)  # staff, no permission
            self.assertEqual(self._status("analyst", url), 200, url)
            self.assertEqual(self._status("root", url), 200, url)

    def test_anonymous_is_redirected_to_admin_login(self):
        r = self.client.get(reverse("dashboard"))
        self.assertIn("/admin/login/", r["Location"])

    def test_dashboard_renders_and_respects_date_filter(self):
        self.client.login(username="root", password="pw")
        r = self.client.get(reverse("dashboard"), {"start": "2026-03-01", "end": "2026-03-31"})
        self.assertContains(r, "Gross revenue")
        self.assertEqual(r.context["data"]["kpis"]["revenue"], Decimal("500.00"))
        self.assertEqual(r.context["data"]["kpis"]["bookings"], 3)
        r = self.client.get(reverse("dashboard"), {"start": "2026-03-11", "end": "2026-03-11"})
        self.assertEqual(r.context["data"]["kpis"]["revenue"], Decimal("100.00"))

    def test_invalid_range_is_rejected_gracefully(self):
        self.client.login(username="root", password="pw")
        r = self.client.get(reverse("dashboard"), {"start": "2026-05-01", "end": "2026-01-01"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "must not be after")


class CsvExportTests(DashboardTestBase):
    def setUp(self):
        self.client.force_login(User.objects.create_superuser("root", "r@x.com", "pw"))
        self.qs = {"start": "2026-03-01", "end": "2026-03-31"}

    def _csv(self, report):
        r = self.client.get(reverse("dashboard_export", args=[report]), self.qs)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r["Content-Type"].startswith("text/csv"))
        self.assertIn("attachment", r["Content-Disposition"])
        return b"".join(r.streaming_content).decode().splitlines()

    def test_revenue_csv(self):
        lines = self._csv("revenue")
        self.assertEqual(lines[0], "period_start,revenue_inr,paid_orders")
        self.assertIn("2026-03-10,400.00,1", lines)
        self.assertEqual(len(lines), 1 + 31)

    def test_every_report_exports(self):
        for name in analytics.REPORT_NAMES:
            self.assertGreater(len(self._csv(name)), 1, name)
        self.assertTrue(any(line.startswith("## ") for line in self._csv("all")))

    def test_detailed_booking_export(self):
        lines = self._csv("bookings_detail")
        self.assertEqual(len(lines), 1 + 3)
        self.assertTrue(lines[0].startswith("booking_id,booked_at_local"))

    def test_unknown_report_is_404(self):
        r = self.client.get(reverse("dashboard_export", args=["nope"]))
        self.assertEqual(r.status_code, 404)

    def test_csv_formula_injection_is_neutralised(self):
        Movie.objects.filter(pk=self.m1.pk).update(name='=HYPERLINK("http://evil","click")')
        lines = self._csv("movies")
        self.assertTrue(any("'=HYPERLINK" in line for line in lines), lines)
        self.assertFalse(any(line.startswith("=") or line.startswith('"=') for line in lines))


class TemplateIntegrityTests(SimpleTestCase):
    """Django only recognises {% tags %} and {{ variables }} that sit on ONE line. An editor's
    format-on-save can wrap a long tag over two lines; the template then either shows raw
    braces or crashes with 'Invalid block tag'. This guard fails loudly if that ever happens."""

    SPLIT_TAG = re.compile(
        r"\{\{(?:(?!\}\}).)*\n(?:(?!\}\}).)*\}\}|\{%(?:(?!%\}).)*\n(?:(?!%\}).)*%\}", re.S
    )

    def test_no_template_tag_is_split_across_lines(self):
        offenders = []
        for path in Path(settings.BASE_DIR, "templates").rglob("*.html"):
            text = path.read_text(encoding="utf-8")
            for m in self.SPLIT_TAG.finditer(text):
                offenders.append(f"{path.name}: {' '.join(m.group(0).split())}")
        self.assertEqual(offenders, [], "Django tags must not span lines (disable format-on-save for templates)")


class PublicPagesRenderTests(DashboardTestBase):
    def test_showtimes_page_renders_with_upcoming_show(self):
        Show.objects.create(
            movie=self.m1, theater=self.tA, date_time=timezone.now() + timedelta(days=1), price=150
        )
        r = self.client.get(reverse("theater_list", args=[self.m1.id]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Theater A")
        self.assertNotContains(r, "{%")

    def test_profile_page_shows_booking_details_not_raw_template_code(self):
        self.client.force_login(self.buyer)
        r = self.client.get(reverse("profile"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Theater A")
        self.assertNotContains(r, "{{")
        self.assertNotContains(r, "{%")
