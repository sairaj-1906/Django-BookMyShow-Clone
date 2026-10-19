import csv
import time
from datetime import timedelta
from decimal import Decimal
from functools import wraps

from django import forms
from django.contrib import admin
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import permission_required
from django.http import Http404, StreamingHttpResponse
from django.shortcuts import render
from django.utils import timezone

from . import analytics
from .models import Booking

MAX_RANGE_DAYS = 366 * 5


def dashboard_access_required(view):
    """Only active staff users that hold the `movies.view_dashboard` permission (superusers
    always do). Anonymous / non-staff users are sent to the admin login; staff without the
    permission get a 403. This is Django's own auth + permission system — no custom checks."""

    @wraps(view)
    @staff_member_required(login_url="admin:login")
    @permission_required("movies.view_dashboard", raise_exception=True)
    def wrapper(request, *args, **kwargs):
        return view(request, *args, **kwargs)

    return wrapper


class DashboardFilterForm(forms.Form):
    start = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    end = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    granularity = forms.ChoiceField(
        required=False,
        choices=[("", "Auto")] + [(g, g.title()) for g in analytics.GRANULARITIES],
    )

    def clean(self):
        data = super().clean()
        start, end = data.get("start"), data.get("end")
        if start and end:
            if start > end:
                raise forms.ValidationError("The start date must not be after the end date.")
            if (end - start).days > MAX_RANGE_DAYS:
                raise forms.ValidationError("Please choose a range of at most 5 years.")
        return data


def _filters(request):
    """Parse ?start=&end=&granularity= into (form, Filters). Bad input falls back to the
    default last-30-days window and the form shows the errors."""
    form = DashboardFilterForm(request.GET or None)
    if request.GET and form.is_valid():
        d = form.cleaned_data
        today = timezone.now().astimezone(analytics.dashboard_tz()).date()
        end = d.get("end") or today
        start = d.get("start") or end - timedelta(days=29)
        if start > end:
            start = end
        return form, analytics.build_filters(start, end, d.get("granularity"))
    return form, analytics.default_filters()


def _presets():
    today = timezone.now().astimezone(analytics.dashboard_tz()).date()
    return [
        ("Today", today, today),
        ("Last 7 days", today - timedelta(days=6), today),
        ("Last 30 days", today - timedelta(days=29), today),
        ("Last 90 days", today - timedelta(days=89), today),
        ("This year", today.replace(month=1, day=1), today),
        ("Last 365 days", today - timedelta(days=364), today),
    ]


def _num(v):
    return float(v) if isinstance(v, Decimal) else v


@dashboard_access_required
def dashboard(request):
    form, f = _filters(request)
    t0 = time.perf_counter()
    data = analytics.dashboard_data(f)
    elapsed_ms = round((time.perf_counter() - t0) * 1000)

    c = data["cancellations"]
    chart = {
        "revenue": {
            "labels": [r["label"] for r in data["revenue_trend"]],
            "revenue": [_num(r["revenue"]) for r in data["revenue_trend"]],
            "orders": [r["orders"] for r in data["revenue_trend"]],
        },
        "bookings": {
            "labels": [r["label"] for r in data["booking_trend"]],
            "values": [r["bookings"] for r in data["booking_trend"]],
        },
        "hours": {
            "labels": [r["label"] for r in data["peak_hours"]],
            "values": [r["bookings"] for r in data["peak_hours"]],
        },
        "users": {
            "labels": [r["label"] for r in data["user_growth"]],
            "new": [r["new_users"] for r in data["user_growth"]],
            "total": [r["total_users"] for r in data["user_growth"]],
        },
        "payments": {
            "labels": ["Successful", "Failed", "Cancelled"],
            "values": [c["payments_success"], c["payments_failed"], c["payments_cancelled"]],
        },
        "occupancy": {
            "labels": [o["theater"] for o in data["occupancy"]],
            "values": [o["occupancy"] for o in data["occupancy"]],
        },
    }
    peak = max(data["peak_hours"], key=lambda r: r["bookings"]) if data["peak_hours"] else None
    context = {
        **admin.site.each_context(request),
        "title": "Analytics dashboard",
        "form": form,
        "presets": _presets(),
        "data": data,
        "chart": chart,
        "peak_hour": peak,
        "elapsed_ms": elapsed_ms,
        "tz_name": str(analytics.dashboard_tz()),
        "report_names": analytics.REPORT_NAMES,
        "query_string": f"start={f.start_date}&end={f.end_date}&granularity={f.granularity}",
        "range_days": (f.end_date - f.start_date).days + 1,
    }
    return render(request, "dashboard/dashboard.html", context)


# ------------------------------------------------------------------ CSV export
_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe(cell):
    """Neutralise spreadsheet formula injection: a movie/theater name that starts with
    '=', '+', '-' or '@' would otherwise be executed as a formula when the CSV is opened
    in Excel. Numbers are passed through untouched."""
    if isinstance(cell, str) and cell.startswith(_DANGEROUS_PREFIXES):
        return "'" + cell
    return cell


class _Echo:
    def write(self, value):
        return value


def _csv_response(filename, row_iter):
    writer = csv.writer(_Echo())
    response = StreamingHttpResponse(
        (writer.writerow([_safe(c) for c in row]) for row in row_iter),
        content_type="text/csv; charset=utf-8",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _booking_detail_rows(f):
    yield [
        "booking_id", "booked_at_local", "user", "movie", "theater", "seat",
        "show_start_utc", "razorpay_payment_id",
    ]
    tz = analytics.dashboard_tz()
    qs = (
        Booking.objects.filter(booked_at__gte=f.start, booked_at__lt=f.end)
        .order_by("booked_at")
        .values_list(
            "id", "booked_at", "user__username", "movie__name", "theater__name",
            "seat__seat_number", "show__date_time", "payment__razorpay_payment_id",
        )
    )
    # .iterator() streams rows from the DB cursor in chunks, so exporting 100k+ bookings
    # never holds more than a couple of thousand rows in memory at once.
    for bid, booked_at, user, movie, theater, seat, show_dt, pay_id in qs.iterator(chunk_size=2000):
        yield [
            bid, booked_at.astimezone(tz).isoformat(sep=" ", timespec="seconds"), user, movie,
            theater, seat, show_dt.isoformat(sep=" ", timespec="minutes") if show_dt else "", pay_id or "",
        ]


@dashboard_access_required
def export_report(request, report):
    _, f = _filters(request)
    stamp = f"{f.start_date}_to_{f.end_date}"

    if report == "bookings_detail":
        return _csv_response(f"bookmyseat_bookings_{stamp}.csv", _booking_detail_rows(f))

    if report == "all":
        def rows():
            for name, title in analytics.REPORT_NAMES.items():
                header, body = analytics.report_rows(name, f)
                yield [f"## {title}"]
                yield header
                yield from body
                yield []

        return _csv_response(f"bookmyseat_full_report_{stamp}.csv", rows())

    if report not in analytics.REPORT_NAMES:
        raise Http404("Unknown report")
    header, body = analytics.report_rows(report, f)
    return _csv_response(f"bookmyseat_{report}_{stamp}.csv", [header, *body])
