# Task 5 — Movie Discovery (search, filters, sorting, recommendations)

## What was added

| Requirement | Implementation |
|---|---|
| Search by title | `?q=` (case-insensitive contains). The old `?search=` links still work. When a search is active and no sort is chosen, titles that *start with* the query come first. |
| Filters | genre (multi), language (multi), city, theater, release-date range, minimum rating, show timing (morning / afternoon / evening / night, multi), show date. Different filters combine with AND; several values of one filter combine with OR. |
| Sorting | popularity (default), newest releases, rating, ticket price low→high, ticket price high→low |
| Live result count | Every change re-requests the page in the background (JSON: `count`, `html`, `url`) and updates the count, chips and grid without a reload; the URL is updated so results are bookmarkable and the back button works. Without JavaScript it falls back to normal links/forms. |
| Pagination | 12 per page, page links keep every filter; out-of-range / garbage page numbers are clamped. |
| Recommended for You | Home page and top of the discovery page (see below). |

Everything is in `movies/discovery.py`; the view is `movie_list` in `movies/views.py`.

### Definitions
* **Popularity** = tickets booked in the last 90 days.
* **Rating** = average of real reviews once a movie has any, otherwise the legacy manual rating.
* **Ticket price** = the cheapest *upcoming* show (that matches the active show filters). Movies with no matching upcoming show sort last and show "No upcoming shows".
* **Show timings** use the dashboard timezone (`DASHBOARD_TIMEZONE`, default Asia/Kolkata): morning 5-12, afternoon 12-16, evening 16-20, night 20-5.
* **All show filters apply to the same show.** "Pune + evening" means a show that is in Pune *and* in the evening, not a Pune show plus some other evening show.
* A link to a city/theater that has no upcoming shows returns 0 movies (it is not silently ignored); other invalid values (bad ids, dates, sort names) are ignored.

### Recommended for You
1. Seeds: the user's last 50 booked movies (weight 3) and their recently viewed movies (weight 1).
2. Taste profile: genre and language weights, summed in one aggregate query each.
3. Candidates: movies **not** booked or viewed, with an upcoming show, scored in SQL (genre match counts double, language match once); ties broken by rating, then release date.
4. Few matches → topped up with what is trending. No history → "Trending Now".
5. A short reason is shown ("Because you like Drama and Horror movies").

Recently viewed: logged-in users' views are stored in the new `MovieView` table (one row per user+movie, newest 50 kept); anonymous visitors (and views made before logging in) live in the session (newest 20). The discovery page also shows a "Recently viewed" row.

## Query design (why it stays fast)

* **No JOINs for filtering.** Genre/language/show filters are correlated `EXISTS` subqueries; "from" price and popularity are scalar subqueries. A movie row can therefore never be duplicated, so no `DISTINCT` is needed and `COUNT(*)` for pagination is exact and cheap.
* **One `Show` subquery** carries all show-level filters, so they are evaluated together.
* A page costs **3 queries** (count, 12 movies, one prefetch for their genres) regardless of catalogue size; the full HTML page adds the cached filter lists and the recommendation queries (≈8-16 total). A test fails if the query count grows with the number of movies.
* Description/cast text columns are deferred on list pages; the filter-sidebar option lists are cached for 5 minutes.

### Indexes added (migration `0006`)

| Index | Used by | Evidence |
|---|---|---|
| `Booking(movie, booked_at)` | popularity (bookings per movie in the last 90 days) | 39,054 index scans during the benchmark; popular-sort page 55 → 37 ms |
| `Show(movie, date_time)` | "has an upcoming show" and cheapest-price subqueries | 11,154 index scans |
| `Movie(release_date)` | newest-first sort, release-date filter | used; newest sort 6.7 → 4.8 ms |

A `Theater(city)` index was tried and **removed**: PostgreSQL never used it (0 scans). New table `MovieView` has `UNIQUE(user, movie)` and an index on `(user, -viewed_at)`.

## Measured performance

Test bed: PostgreSQL 16, **3,040 movies, 197,344 bookings, 11,693 shows** (4,508 upcoming, 100 theaters in 12 cities), tables `VACUUM ANALYZE`d, median of 9-15 runs. Timings are from a shared sandbox VM and vary ±30% between runs; treat the ratios as the message. Reproduce with `seed_analytics_data`, `seed_discovery_data --movies 3000`, then `benchmark_discovery --naive`.

Optimized implementation vs a naive version (JOIN genres/languages/shows/bookings, `DISTINCT`, `Count(..., distinct=True)`/`Min` annotations; same filters, **same results** — checked):

| Scenario | Optimized | Naive | Speed-up |
|---|---|---|---|
| Default list, most popular, page 1 | 37-100 ms | > 20,000 ms (aborted; 107 s when allowed to finish) | > 200x |
| Deep page (page 100) | 42-97 ms | > 20,000 ms (aborted) | > 400x |
| Newest releases | 5-12 ms | > 20,000 ms (aborted) | > 1,500x |
| Title search "silent" | 9.5 ms | 50 ms | 5x |
| 2 genres + language | 9.4 ms | 43 ms | 5x |
| Rating ≥ 4, released 2024+, by rating | 9.5 ms | 363 ms | 38x |
| City = Pune, cheapest first | 15.5 ms | 153 ms | 10x |
| City + evening shows; everything combined | 11-29 ms | no sensible naive equivalent | — |
| Recommended for You (heavy users, 7 queries) | 34-47 ms warm (82 ms first run) | — | — |

**Why the naive version collapses:** joining both `bookings` and `shows` to each movie multiplies rows per movie (bookings × shows). The seeded popular movies have thousands of bookings *and* dozens of shows, so each produces hundreds of thousands of joined rows that `DISTINCT`/`GROUP BY` must then collapse. That is exactly the pitfall for popular movies in a real catalogue. Subqueries avoid it entirely.

**Effect of the new indexes** (same queries, indexes dropped vs present): popular sort 55 → 37 ms, deep page 68 → 42 ms, newest 6.7 → 4.8 ms; the other scenarios were within noise at this data size. The indexes matter more as the tables grow; the per-movie popularity subquery is linear in the number of movies (≈ 35 ms per 3,000 movies for the unfiltered popular sort).

## Limitations / next steps
* Title search is `icontains`; beyond ~100k movies add a PostgreSQL `pg_trgm` GIN index (or full-text search).
* Unfiltered "most popular" computes popularity for every movie. At tens of thousands of movies, keep a periodically refreshed popularity counter on `Movie` instead.
* Recommendations are content-based (genre/language). They do not use other users' behaviour ("people who booked X also booked Y").
* Filter option lists are cached per process for 5 minutes, so a brand-new city can take a few minutes to appear in the dropdown.
* Seeded benchmark movies have no seats on upcoming shows (not needed for discovery).
