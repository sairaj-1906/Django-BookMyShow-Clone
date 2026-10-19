"""
Analytics for the admin dashboard.

Design rules (so it stays fast with 100k+ bookings):
  * Every metric is computed by the database with GROUP BY / aggregate functions — no
    function here ever loads Booking/Payment rows into Python and loops over them. The
    only Python-side work is reshaping small, already-aggregated result sets (at most
    a few hundred rows) e.g. filling empty days with zero.
  * Date filters use half-open ranges (col >= start AND col < end) on the raw timestamp
    column, never `col__date=` — wrapping the column in a function would stop the
    database from using its index on that column.
  * Each query is shaped to match an index defined in movies/models.py.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection
from django.db.models import Count, DateField, F, Func, IntegerField, Q, Sum, Value
from django.db.models.functions import ExtractHour, Trunc
from django.utils import timezone

from .models import Booking, Cancellation, Payment, Show

GRANULARITIES = ("day", "week", "month", "year")
ZERO = Decimal("0.00")


def dashboard_tz():
    return ZoneInfo(getattr(settings, "DASHBOARD_TIMEZONE", "Asia/Kolkata"))


def _money(value):
    return (value or ZERO).quantize(Decimal("0.01"))


# ------------------------------------------- timezone-aware bucketing (fast path)
def _fixed_offset_minutes(f):
    """UTC offset of the dashboard timezone in minutes if it is the SAME for the whole
    filter range (true for IST, false for zones that observe daylight saving), else None."""
    tz = dashboard_tz()
    offsets, cursor = set(), f.start
    while cursor < f.end:
        offsets.add(cursor.astimezone(tz).utcoffset())
        cursor += timedelta(days=30)
    offsets.add((f.end - timedelta(seconds=1)).astimezone(tz).utcoffset())
    if len(offsets) != 1:
        return None
    return int(offsets.pop().total_seconds() // 60)


def _sqlite_fast_minutes(f):
    return _fixed_offset_minutes(f) if connection.vendor == "sqlite" else None


def _bucket(field, f):
    """Expression that maps a UTC timestamp column to its local day/week/month/year start.

    PostgreSQL (production): Trunc(..., tzinfo=) -> native DATE_TRUNC(... AT TIME ZONE ...).
    SQLite (local dev): Django would call a *Python* function once per row, which is ~6x
    slower on 100k+ rows. When the timezone offset is constant over the range we instead
    shift by that offset and use SQLite's built-in C date functions."""
    minutes = _sqlite_fast_minutes(f)
    if minutes is None:
        return Trunc(field, f.granularity, tzinfo=dashboard_tz())
    shift = Value(f"{minutes:+d} minutes")
    col = F(field)
    if f.granularity == "day":
        return Func(col, shift, function="date", output_field=DateField())
    if f.granularity == "week":  # Monday on/before the local date
        return Func(col, shift, Value("-6 days"), Value("weekday 1"), function="date", output_field=DateField())
    if f.granularity == "month":
        return Func(Value("%Y-%m-01"), col, shift, function="strftime", output_field=DateField())
    return Func(Value("%Y-01-01"), col, shift, function="strftime", output_field=DateField())


def _hour(field, f):
    minutes = _sqlite_fast_minutes(f)
    if minutes is None:
        return ExtractHour(field, tzinfo=dashboard_tz())
    return Func(
        Func(Value("%H"), F(field), Value(f"{minutes:+d} minutes"), function="strftime"),
        function="CAST", template="CAST(%(expressions)s AS INTEGER)", output_field=IntegerField(),
    )


# --------------------------------------------------------------------- filters
@dataclass(frozen=True)
class Filters:
    start: datetime  # inclusive, timezone-aware
    end: datetime  # exclusive, timezone-aware
    start_date: date
    end_date: date  # inclusive (as the admin typed it)
    granularity: str


def default_granularity(start_date, end_date):
    days = (end_date - start_date).days + 1
    if days <= 31:
        return "day"
    if days <= 180:
        return "week"
    if days <= 800:
        return "month"
    return "year"


def build_filters(start_date, end_date, granularity=None):
    tz = dashboard_tz()
    if granularity not in GRANULARITIES:
        granularity = default_granularity(start_date, end_date)
    return Filters(
        start=datetime.combine(start_date, time.min, tzinfo=tz),
        end=datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=tz),
        start_date=start_date,
        end_date=end_date,
        granularity=granularity,
    )


def default_filters(days=30):
    today = timezone.now().astimezone(dashboard_tz()).date()
    return build_filters(today - timedelta(days=days - 1), today)


# ------------------------------------------------------------- bucket helpers
def _bucket_start(d, granularity):
    if granularity == "week":
        return d - timedelta(days=d.weekday())
    if granularity == "month":
        return d.replace(day=1)
    if granularity == "year":
        return d.replace(month=1, day=1)
    return d


def _next_bucket(d, granularity):
    if granularity == "day":
        return d + timedelta(days=1)
    if granularity == "week":
        return d + timedelta(days=7)
    if granularity == "month":
        return (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return d.replace(year=d.year + 1)


def bucket_label(d, granularity):
    if granularity == "month":
        return d.strftime("%b %Y")
    if granularity == "year":
        return d.strftime("%Y")
    return d.strftime("%d %b %Y")


def _series(rows, f, value_keys):
    """Turn [{'bucket': dt, k: v}] into a gap-free list (empty periods become zeros)."""
    tz = dashboard_tz()
    by_bucket = {}
    for row in rows:
        b = row["bucket"]
        b = b.astimezone(tz).date() if isinstance(b, datetime) else b
        by_bucket[b] = row
    out = []
    cursor = _bucket_start(f.start_date, f.granularity)
    while cursor <= f.end_date:
        row = by_bucket.get(cursor, {})
        out.append(
            {"bucket": cursor, "label": bucket_label(cursor, f.granularity), **{k: row.get(k) or 0 for k in value_keys}}
        )
        cursor = _next_bucket(cursor, f.granularity)
    return out


def _paid(f):
    return Payment.objects.filter(
        status=Payment.STATUS_SUCCESS, paid_at__gte=f.start, paid_at__lt=f.end
    )


def _bookings(f):
    return Booking.objects.filter(booked_at__gte=f.start, booked_at__lt=f.end)


# --------------------------------------------------------------------- metrics
def revenue_summary(now=None):
    """Revenue for today / this week / this month / this year (calendar periods, in the
    dashboard timezone). ONE query: conditional SUMs over a single index range scan."""
    tz = dashboard_tz()
    local_now = (now or timezone.now()).astimezone(tz)
    today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = today - timedelta(days=today.weekday())
    month = today.replace(day=1)
    year = today.replace(month=1, day=1)
    # the widest window is the year; everything else is a sub-range of it
    agg = Payment.objects.filter(
        status=Payment.STATUS_SUCCESS, paid_at__gte=year
    ).aggregate(
        today=Sum("amount", filter=Q(paid_at__gte=today)),
        week=Sum("amount", filter=Q(paid_at__gte=week)),
        month=Sum("amount", filter=Q(paid_at__gte=month)),
        year=Sum("amount"),
    )
    return {k: _money(v) for k, v in agg.items()}


def revenue_trend(f):
    rows = (
        _paid(f)
        .annotate(bucket=_bucket("paid_at", f))
        .values("bucket")
        .annotate(revenue=Sum("amount"), orders=Count("id"))
        .order_by("bucket")
    )
    return _series(rows, f, ["revenue", "orders"])


def booking_trend(f):
    rows = (
        _bookings(f)
        .annotate(bucket=_bucket("booked_at", f))
        .values("bucket")
        .annotate(bookings=Count("id"))
        .order_by("bucket")
    )
    return _series(rows, f, ["bookings"])


def occupancy_by_theater(f):
    """Seats sold / seats available across every show that screens in the range."""
    rows = (
        Show.objects.filter(date_time__gte=f.start, date_time__lt=f.end)
        .values("theater_id", "theater__name", "theater__city")
        .annotate(
            shows=Count("id", distinct=True),
            seats_total=Count("seats"),
            seats_booked=Count("seats", filter=Q(seats__is_booked=True)),
        )
    )
    out = []
    for r in rows:
        total = r["seats_total"]
        out.append(
            {
                "theater": r["theater__name"],
                "city": r["theater__city"],
                "shows": r["shows"],
                "seats_total": total,
                "seats_booked": r["seats_booked"],
                "occupancy": round(100 * r["seats_booked"] / total, 1) if total else 0.0,
            }
        )
    return sorted(out, key=lambda r: r["occupancy"], reverse=True)


def top_movies(f, limit=10):
    rows = list(
        _bookings(f)
        .values("movie_id", "movie__name")
        .annotate(bookings=Count("id"))
        .order_by("-bookings")[:limit]
    )
    revenue = dict(
        _paid(f)
        .filter(show__movie_id__in=[r["movie_id"] for r in rows])
        .values_list("show__movie_id")
        .annotate(r=Sum("amount"))
    )
    return [
        {"movie": r["movie__name"], "bookings": r["bookings"], "revenue": _money(revenue.get(r["movie_id"]))}
        for r in rows
    ]


def top_theaters(f, limit=10):
    rows = list(
        _paid(f)
        .values("show__theater_id", "show__theater__name", "show__theater__city")
        .annotate(revenue=Sum("amount"))
        .order_by("-revenue")[:limit]
    )
    counts = dict(
        _bookings(f)
        .filter(theater_id__in=[r["show__theater_id"] for r in rows])
        .values_list("theater_id")
        .annotate(n=Count("id"))
    )
    return [
        {
            "theater": r["show__theater__name"],
            "city": r["show__theater__city"],
            "bookings": counts.get(r["show__theater_id"], 0),
            "revenue": _money(r["revenue"]),
        }
        for r in rows
    ]


def peak_hours(f):
    rows = (
        _bookings(f)
        .annotate(hour=_hour("booked_at", f))
        .values("hour")
        .annotate(bookings=Count("id"))
        .order_by("hour")
    )
    by_hour = {r["hour"]: r["bookings"] for r in rows}
    return [{"hour": h, "label": f"{h:02d}:00", "bookings": by_hour.get(h, 0)} for h in range(24)]


def cancellation_stats(f, bookings_count=None):
    c = Cancellation.objects.filter(cancelled_at__gte=f.start, cancelled_at__lt=f.end).aggregate(
        total=Count("id"),
        refund_total=Sum("refund_amount"),
        processed=Count("id", filter=Q(refund_status=Cancellation.REFUND_PROCESSED)),
        processed_amount=Sum("refund_amount", filter=Q(refund_status=Cancellation.REFUND_PROCESSED)),
        pending=Count("id", filter=Q(refund_status=Cancellation.REFUND_PENDING)),
        pending_amount=Sum("refund_amount", filter=Q(refund_status=Cancellation.REFUND_PENDING)),
        failed=Count("id", filter=Q(refund_status=Cancellation.REFUND_FAILED)),
        failed_amount=Sum("refund_amount", filter=Q(refund_status=Cancellation.REFUND_FAILED)),
    )
    if bookings_count is None:
        bookings_count = _bookings(f).count()
    attempts = bookings_count + c["total"]
    pay = dict(
        Payment.objects.filter(created_at__gte=f.start, created_at__lt=f.end)
        .values_list("status")
        .annotate(n=Count("id"))
    )
    pay_total = sum(pay.values())
    return {
        "cancellations": c["total"],
        "cancellation_rate": round(100 * c["total"] / attempts, 2) if attempts else 0.0,
        "refund_total": _money(c["refund_total"]),
        "refund_processed": c["processed"],
        "refund_processed_amount": _money(c["processed_amount"]),
        "refund_pending": c["pending"],
        "refund_pending_amount": _money(c["pending_amount"]),
        "refund_failed": c["failed"],
        "refund_failed_amount": _money(c["failed_amount"]),
        "payments_total": pay_total,
        "payments_success": pay.get(Payment.STATUS_SUCCESS, 0),
        "payments_failed": pay.get(Payment.STATUS_FAILED, 0),
        "payments_cancelled": pay.get(Payment.STATUS_CANCELLED, 0),
        "payment_success_rate": round(100 * pay.get(Payment.STATUS_SUCCESS, 0) / pay_total, 2) if pay_total else 0.0,
    }


def user_growth(f):
    rows = (
        User.objects.filter(date_joined__gte=f.start, date_joined__lt=f.end)
        .annotate(bucket=_bucket("date_joined", f))
        .values("bucket")
        .annotate(new_users=Count("id"))
        .order_by("bucket")
    )
    series = _series(rows, f, ["new_users"])
    running = User.objects.filter(date_joined__lt=f.start).count()
    for point in series:
        running += point["new_users"]
        point["total_users"] = running
    return series


# ------------------------------------------------------------------- dashboard
def dashboard_data(f):
    """Everything the dashboard page shows, in a fixed number of aggregate queries."""
    rev_trend = revenue_trend(f)
    book_trend = booking_trend(f)
    occupancy = occupancy_by_theater(f)
    paid = _paid(f).aggregate(revenue=Sum("amount"), orders=Count("id"))
    bookings_count = sum(p["bookings"] for p in book_trend)
    cancel = cancellation_stats(f, bookings_count=bookings_count)
    users = user_growth(f)

    seats_total = sum(o["seats_total"] for o in occupancy)
    seats_booked = sum(o["seats_booked"] for o in occupancy)
    revenue = _money(paid["revenue"])
    orders = paid["orders"] or 0
    return {
        "filters": f,
        "revenue_summary": revenue_summary(),
        "kpis": {
            "revenue": revenue,
            "orders": orders,
            "avg_order_value": _money(revenue / orders) if orders else ZERO,
            "bookings": bookings_count,
            "net_revenue": _money(revenue - cancel["refund_processed_amount"]),
            "occupancy": round(100 * seats_booked / seats_total, 1) if seats_total else 0.0,
            "new_users": sum(u["new_users"] for u in users),
            "total_users": users[-1]["total_users"] if users else User.objects.count(),
        },
        "revenue_trend": rev_trend,
        "booking_trend": book_trend,
        "occupancy": occupancy,
        "top_movies": top_movies(f),
        "top_theaters": top_theaters(f),
        "peak_hours": peak_hours(f),
        "cancellations": cancel,
        "user_growth": users,
    }


# ------------------------------------------------------------------ CSV reports
def _fmt_date(d):
    return d.isoformat()


def report_rows(name, f):
    """Return (header, rows) for one CSV report. Mirrors the dashboard widgets."""
    if name == "summary":
        data = dashboard_data(f)
        k, rs, c = data["kpis"], data["revenue_summary"], data["cancellations"]
        rows = [
            ("Report period", f"{f.start_date} to {f.end_date}"),
            ("Revenue (selected range)", k["revenue"]),
            ("Refunds processed (selected range)", c["refund_processed_amount"]),
            ("Net revenue (selected range)", k["net_revenue"]),
            ("Paid orders", k["orders"]),
            ("Average order value", k["avg_order_value"]),
            ("Bookings", k["bookings"]),
            ("Occupancy %", k["occupancy"]),
            ("New users", k["new_users"]),
            ("Total users", k["total_users"]),
            ("Revenue today", rs["today"]),
            ("Revenue this week", rs["week"]),
            ("Revenue this month", rs["month"]),
            ("Revenue this year", rs["year"]),
            ("Cancellations", c["cancellations"]),
            ("Cancellation rate %", c["cancellation_rate"]),
            ("Refund amount (all statuses)", c["refund_total"]),
            ("Refunds processed", c["refund_processed"]),
            ("Refunds pending", c["refund_pending"]),
            ("Refunds failed", c["refund_failed"]),
            ("Payments attempted", c["payments_total"]),
            ("Payments successful", c["payments_success"]),
            ("Payments failed", c["payments_failed"]),
            ("Payments cancelled", c["payments_cancelled"]),
            ("Payment success rate %", c["payment_success_rate"]),
        ]
        return ["metric", "value"], rows
    if name == "revenue":
        return ["period_start", "revenue_inr", "paid_orders"], [
            (_fmt_date(r["bucket"]), _money(r["revenue"]), r["orders"]) for r in revenue_trend(f)
        ]
    if name == "bookings":
        return ["period_start", "bookings"], [(_fmt_date(r["bucket"]), r["bookings"]) for r in booking_trend(f)]
    if name == "occupancy":
        return ["theater", "city", "shows", "seats_total", "seats_booked", "occupancy_pct"], [
            (r["theater"], r["city"], r["shows"], r["seats_total"], r["seats_booked"], r["occupancy"])
            for r in occupancy_by_theater(f)
        ]
    if name == "movies":
        return ["movie", "bookings", "revenue_inr"], [
            (r["movie"], r["bookings"], r["revenue"]) for r in top_movies(f, limit=50)
        ]
    if name == "theaters":
        return ["theater", "city", "bookings", "revenue_inr"], [
            (r["theater"], r["city"], r["bookings"], r["revenue"]) for r in top_theaters(f, limit=50)
        ]
    if name == "peak_hours":
        return ["hour_of_day", "bookings"], [(r["label"], r["bookings"]) for r in peak_hours(f)]
    if name == "cancellations":
        c = cancellation_stats(f)
        return ["metric", "value"], list(c.items())
    if name == "users":
        return ["period_start", "new_users", "total_users"], [
            (_fmt_date(r["bucket"]), r["new_users"], r["total_users"]) for r in user_growth(f)
        ]
    raise KeyError(name)


REPORT_NAMES = {
    "summary": "Summary KPIs",
    "revenue": "Revenue trend",
    "bookings": "Booking trend",
    "occupancy": "Theater occupancy",
    "movies": "Most booked movies",
    "theaters": "Top theaters",
    "peak_hours": "Peak booking hours",
    "cancellations": "Cancellations & refunds",
    "users": "User growth",
}
