# Task 4 Report — Admin Analytics Dashboard (BookMySeat)

## 1. Admin credentials

These accounts are created by the seeding command (`python manage.py seed_analytics_data`) on whatever
database you run it against. Log in at **`/admin/login/`**, then open **`/dashboard/`**
(or click *Analytics dashboard* at the top of the admin home page).

| Username | Password | Type | What it demonstrates |
|---|---|---|---|
| `dashboard_admin` | `DashAdmin#2026` | Superuser | Full access: dashboard, CSV exports, everything in the admin |
| `analyst` | `DashAdmin#2026` | Staff + only the `movies.view_dashboard` permission | Can use the dashboard/exports, cannot edit data |
| `staff_noaccess` | `DashAdmin#2026` | Staff, no dashboard permission | Gets **403 Forbidden** on `/dashboard/` |

* These are **demo credentials for a local/seeded database only.** Change the password with
  `python manage.py seed_analytics_data --reset --admin-password "<new>"` or `python manage.py changepassword dashboard_admin`.
  On a real deployment create the admin with `python manage.py createsuperuser` and never reuse these.
* Anonymous visitors and logged-in customers (non-staff) are redirected to the admin login.

## 2. How to run it

```bash
pip install -r requirements.txt
python manage.py migrate                       # applies 0005, 0006, 0007 (see §5)
python manage.py seed_analytics_data           # ~116k bookings, 5k users, demo staff accounts (~30 s)
python manage.py runserver                     # then log in and open /dashboard/
python manage.py benchmark_dashboard --days 30 # reproduce the timings in §7
python manage.py test movies                   # 25 automated tests
```

`seed_analytics_data` tags everything it creates, so `--reset` removes **only** seeded rows (verified: real
movies/theaters/users are untouched). Use a development database, not production.

## 3. Requirements coverage

| Requirement | Where |
|---|---|
| Total revenue — daily / weekly / monthly / yearly | "Revenue — calendar periods" cards (today, this week, this month, this year) **and** a revenue trend chart that can be grouped by day/week/month/year |
| Booking trends | Bookings chart, same granularity selector |
| Occupancy % per theater | Table with bars + chart (seats sold ÷ seats available across shows in the range) |
| Most booked movies | Top-10 table (bookings and revenue) |
| Top-performing theaters | Top-10 by revenue (with bookings) |
| Peak booking hours | 24-hour chart; peak hour highlighted and shown as a KPI |
| Cancellation & refund statistics | Cancellation count/rate, refunds processed / pending / failed (count and ₹), payment attempts, failed and cancelled payments, payment success rate |
| User growth | New users per period + running total |
| Custom date ranges | From/To pickers, presets (today, 7/30/90/365 days, this year), granularity selector; end date inclusive; invalid ranges are rejected with a message |
| CSV export | Per-widget CSV links, a combined "Full report", and a streamed "All bookings (detailed rows)" export |
| Admin-only access | §4 |
| Optimised ORM aggregation, 100k+ bookings | §6–§7 |
| Indexing + explained improvements | §6–§7 |

## 4. Access control

Implemented with Django's own auth and permission system (`movies/dashboard_views.py`):

```python
@staff_member_required(login_url="admin:login")                      # active staff only, else -> admin login
@permission_required("movies.view_dashboard", raise_exception=True)  # else -> 403
```

* `movies.view_dashboard` is a custom permission declared on the `Payment` model (`Meta.permissions`), so it
  appears in the admin's user/group permission picker. Superusers hold every permission automatically.
* The same decorator protects the dashboard page **and every export endpoint**.
* Tests cover all five cases (anonymous, customer holding the permission but not staff, staff without
  permission, staff with permission, superuser) on the page and on two export URLs.
* CSV cells beginning with `=`, `+`, `-` or `@` are prefixed with `'` so a malicious movie/theater name cannot
  run as a spreadsheet formula (tested).

## 5. Data-model changes

| Change | Why |
|---|---|
| `Payment.paid_at` (+ migration 0007 backfills it for existing successful payments) | Revenue must be dated when money was **confirmed**, not when checkout started |
| `Cancellation` model (seat, refund amount, `pending/processed/failed`, timestamps) | The app had no cancellation/refund concept, so there was nothing to report. A booking is deleted on cancellation (freeing the seat) and a `Cancellation` row keeps the history |
| Admin actions: *Cancel selected bookings and create refund records* (upcoming shows only) and *Mark selected pending refunds as processed* | Way to create real cancellation data. No Razorpay refund API call is made — see §9 |
| `movies.view_dashboard` permission | §4 |

**Metric definitions.** Revenue = sum of successful payments by `paid_at`. Net revenue = revenue − processed
refunds. Booking counts are *active* bookings (cancelled ones leave the table). Cancellation rate =
cancellations ÷ (bookings + cancellations). Occupancy = booked seats ÷ all seats of shows screening in the
range. Days/hours are bucketed in `DASHBOARD_TIMEZONE` (default `Asia/Kolkata`; the DB stays UTC).

## 6. Query design and indexing

**Aggregation only.** Every metric is a `GROUP BY`/aggregate computed by the database
(`movies/analytics.py`). No function loads Booking/Payment rows into Python; the only Python-side work is
reshaping already-aggregated results (tens to hundreds of rows), e.g. filling empty days with zero. The whole
dashboard is a fixed **14 queries** regardless of data size (a test adds 60 bookings and asserts the query
count is unchanged — no N+1). Specific techniques:

* One query for four revenue windows: conditional `Sum(..., filter=Q(...))` over a single index range scan.
* Occupancy: `Count("seats", filter=Q(seats__is_booked=True))` per theater in one join — no per-theater loops.
* Date filters are half-open ranges on the raw column (`col >= start AND col < end`), never `col__date=`,
  which would wrap the column in a function and prevent index use.
* The detailed booking export uses `.iterator(chunk_size=2000)` + `StreamingHttpResponse`, so 100k+ rows stream
  without being held in memory.

**Indexes added** (kept only if the plan/benchmark showed they were used and helped):

| Index | Serves | Plan change |
|---|---|---|
| `Booking(booked_at)` | booking trend, peak hours, top movies/theaters | Seq Scan → Bitmap Index Scan |
| `Payment(status, paid_at)` | revenue trend/summary, top theaters/movies revenue | Seq Scan → Bitmap Index Scan |
| `Payment(created_at)` | payment-outcome stats | range scan |
| `Show(date_time)` | occupancy report | Seq Scan → Bitmap Index Scan |
| `Cancellation(cancelled_at)` | cancellation/refund stats | range scan |
| `auth_user(date_joined)` (raw SQL in migration 0007; Django does not index it) | user growth | range scan |

**Tried and rejected:** `Seat(show, is_booked)` (planner ignores it — the query needs every seat row, so the
existing FK index is enough; removed); covering composites `Booking(booked_at, movie)` / `(booked_at, theater)`
(no measurable gain); `Payment(status, paid_at) INCLUDE (amount)` (10.8 → 7.6 ms on one query — not worth a
PostgreSQL-only index).

## 7. Measured performance

**Test bed:** PostgreSQL 16 (your production engine), **115,534 bookings**, 156,000 seats, 52,074 payments,
7,517 cancellations, 5,000 users, 2,600 shows, 25 theaters, 40 movies; one year of history. Median of 15
runs after a warm-up, tables `VACUUM ANALYZE`d. "Before" = identical code with the new indexes dropped
(existing foreign-key indexes still present). Measured on a shared sandbox VM, so treat absolute numbers as
indicative; the ratios and plan changes are the point. Reproduce with `benchmark_dashboard`.

**Whole dashboard (14 queries), median ms**

| Date range | No new indexes | With indexes | Speed-up |
|---|---|---|---|
| Last 7 days | 72.5 | 34.0 | 2.1× |
| Last 30 days | 90.2 | 55.2 | 1.6× |
| Last 365 days | 311.5 | 304.4 | none |

The full-year range reads essentially the whole table, so a sequential scan is the *correct* plan and the
planner keeps using it; indexes pay off for selective ranges, which is how dashboards are normally used.

**Selected queries, 7-day range (before → after, ms):** top movies 19.8 → 4.4 · top theaters 12.3 → 4.2 ·
cancellation stats 11.2 → 4.9 · revenue trend 6.3 → 2.0 · booking trend 6.4 → 2.5 · peak hours 6.4 → 2.4.
`EXPLAIN ANALYZE` (30 days, first measurement run): peak hours 10.2 → 5.7 ms, top movies 9.0 → 4.1 ms,
revenue 5.9 → 1.1 ms — each changing from `Seq Scan` to `Bitmap Index Scan`.

**Aggregating in the database vs looping in Python** (peak hours + per-movie counts over 365 days):

| Approach | Time | Peak Python memory |
|---|---|---|
| Fetch every booking and count in a loop | 2,622 ms | 208.7 MB |
| ORM aggregation (what the dashboard does) | 86 ms | < 1 MB |

≈ 30× faster and effectively no memory.

**SQLite (typical local dev DB):** SQLite has no native timezone-aware date functions, so Django calls a
Python function per row. For a constant-offset timezone (such as IST) the dashboard now uses SQLite's built-in
date functions instead (`_bucket`/`_hour` in `analytics.py`; falls back to standard `Trunc` for
daylight-saving zones; tests assert both paths return identical results). Full-year dashboard on 116k
bookings: **≈1.7 s → 0.6 s**; 30 days: **193 ms → 84 ms**.

**End to end (page render incl. templates, 115k bookings):** last 30 days ≈ 60 ms, 7 days ≈ 40 ms, full year
≈ 340 ms. Detailed CSV export of 114,801 bookings streamed in ≈ 2.1 s.

**Cost of indexing:** inserting 115,534 booking rows took 86 ms without and 229 ms with the `booked_at`
index (~1.2 µs extra per booking) — negligible next to a payment round-trip, which is why only indexes that
demonstrably help were kept.

## 8. Testing

25 automated tests (`movies/tests.py`), passing on **PostgreSQL and SQLite** (one SQLite-specific test is skipped on PostgreSQL): hand-computed revenue, booking,
peak-hour, occupancy, top-movie/theater, cancellation, refund and user-growth figures on a small dataset
(including out-of-range exclusion and inclusive end dates); timezone bucketing (IST and DST zones); the
no-N+1 query-count check; all access-control cases; every CSV report, the streamed export, 404 for unknown
reports, and CSV formula-injection protection; plus a guard that fails if any template tag is split across lines, and render tests for the showtimes and profile pages.

## 9. Limitations and next steps

* **Refunds are recorded, not executed.** Marking a refund "processed" does not call Razorpay's refund API;
  wire that in before using it for real money.
* Customers cannot yet cancel from their profile — cancellations are created by admins (action above).
  A user-facing cancel flow would reuse the same `Cancellation` model.
* "Real-time" means computed live on every page load (no caching). At several million bookings, add
  pre-aggregated daily summary tables (or PostgreSQL materialized views) refreshed on a schedule; the
  `analytics.py` interface would not change.
* Seeded payments do not populate the `Payment.seats` many-to-many (not needed by any report).
* Charts load Chart.js 4.4.1 from a CDN; vendor it locally if the admin must work offline.
* Deploying: run `migrate` (0005–0007) and set `DASHBOARD_TIMEZONE` if not India.

## 10. Note on editor auto-formatting of templates

Django only recognises `{% tags %}` and `{{ variables }}` that sit on a single line. An HTML formatter with
format-on-save (e.g. Prettier at its default 80-column width) wraps long lines *inside* those tags, which
makes a template show raw `{{ ... }}` text or crash with `Invalid block tag` (this is what produced the
`'endblock'` error when the dashboard template was reformatted, and it had already broken
`theater_list.html` — the showtimes page returned a 500 — and the booking details on `profile.html`).

* `dashboard.html` is now written to survive formatting: Django tags are wrapped in HTML comments
  (formatters never reflow comments), variables have no inner spaces, and structural tags are separated by
  comments. Verified by formatting it with Prettier (widths 40-200) and js-beautify and checking that the page
  renders identically.
* `theater_list.html` and `profile.html` were repaired (split tags re-joined).
* `.prettierignore` (`templates/`) and `.vscode/settings.json` (no format-on-save for HTML) stop it recurring;
  a test fails if any template in `templates/` contains a tag split across lines.
