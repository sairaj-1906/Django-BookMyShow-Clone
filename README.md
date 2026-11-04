# BookMySeat — Django BookMyShow Clone 🎬

A movie ticket booking web application built with Django, inspired by BookMyShow. Users can browse movies, view details, and book seats — all through a clean, responsive interface.

**Live demo:** [django-book-my-show-clone.vercel.app](https://django-book-my-show-clone.vercel.app/)

## Features

- Browse a catalog of movies with posters and details
- User registration and login (`users` app)
- Movie listings and details (`movies` app)
- Seat/ticket booking flow
- Razorpay payment confirmation with downloadable PDF tickets and signed QR verification
- Celery-based ticket emails with automatic delivery retries
- Media handling for movie posters/images
- Deployed and live on Vercel

> Update this list with the specific features your app actually supports (e.g. seat selection, showtimes, search, admin panel).

## Tech Stack

- **Backend:** Django (Python)
- **Database:** SQLite (development), PostgreSQL via `dj-database-url` (production)
- **Server:** Gunicorn (WSGI)
- **Deployment:** Vercel
- **Frontend:** Django templates (HTML/CSS)

## Project Structure

```
Django-BookMyShow-Clone/
├── bookmymovie/     # Django project settings
├── movies/          # Movie listing & booking app
├── users/           # Authentication app
├── templates/        # HTML templates
├── media/movies/     # Uploaded movie images
├── manage.py
├── requirements.txt
└── vercel.json        # Vercel deployment config
```

## Getting Started

### Prerequisites

- Python 3.x
- pip

### Installation

1. Clone the repository

   ```bash
   git clone https://github.com/sairaj-1906/Django-BookMyShow-Clone.git
   cd Django-BookMyShow-Clone
   ```

2. Create and activate a virtual environment

   ```bash
   python -m venv venv
   source venv/bin/activate   # On Windows: venv\Scripts\activate
   ```

3. Install dependencies

   ```bash
   pip install -r requirements.txt
   ```

4. Configure the database. Without `DATABASE_URL`, the project uses the local
   `db.sqlite3` file. To use PostgreSQL, set `DATABASE_URL` to its connection URL.
   Keep this value in your deployment environment settings; do not commit it. For
   deployment, also set `DJANGO_SECRET_KEY`, `DEBUG=false`, and `ALLOWED_HOSTS`.

5. Apply migrations

   ```bash
   python manage.py migrate
   ```

6. Create a superuser (optional, for admin access). Django prompts for a password;
   this project intentionally does not ship with default administrator credentials.

   ```bash
   python manage.py createsuperuser
   ```

7. Run the development server

   ```bash
   python manage.py runserver
   ```

8. Visit `http://127.0.0.1:8000/` in your browser.

### Add local sample movies, theaters, shows, and seats

For a ready-to-browse local demo without external movie or theater APIs, run:

```powershell
python manage.py seed_sample_catalog
```

The command is safe to rerun: it adds fictional demo movies, six clearly labeled demo
theaters, upcoming sample shows, and available seats without deleting existing data.
It refuses to seed non-SQLite databases unless you explicitly add `--confirm-nonlocal`
after checking the target is a development database:

```powershell
python manage.py seed_sample_catalog --confirm-nonlocal
```

### Admin access and analytics

Create the initial admin account interactively with `python manage.py createsuperuser`.
For an existing staff account that needs the analytics dashboard, grant the
`movies.view_dashboard` permission in Django Admin or through a group. Superusers
automatically have access. Never put administrator passwords or payment/database
credentials in source control or reports; provision them through a secure secrets
manager and rotate any credentials that may have been committed previously.

The analytics dashboard uses database-side aggregates and streaming CSV exports. The
date-filter and join paths have indexes on booking time, movie plus booking time,
successful payment status plus paid time, show time, and movie plus show time. To
measure the implementation on a disposable local database with the 100k+ seeded data,
run:

```powershell
python manage.py seed_analytics_data
python manage.py benchmark_dashboard --days 365 --runs 5 --naive
```

The benchmark compares ORM aggregation with loading booking records into Python and
reports median runtime, query count, and peak Python memory. On PostgreSQL, add
`--explain` to print `EXPLAIN ANALYZE` plans. Do not seed benchmark data into a live
database. The benchmark output is environment-dependent; capture it from the target
database before publishing quantitative performance claims.

Verification run in this development environment (SQLite, 116,383 bookings, 3 timed
runs) measured the whole dashboard at 974 ms and 15 aggregate queries. For peak-hour
and movie-count reports, database aggregation took 309 ms versus 4,351 ms for loading
and counting booking objects in Python (about 14x faster); measured peak Python memory
was under 0.1 MB versus 236 MB. This is a local SQLite result, not a production
PostgreSQL guarantee; run the benchmark and query plans against the intended deployment
database before capacity planning.

These are not real releases, venues, or bookable showtimes. Browse them at `/movies/`;
manage the catalog through `/admin/`.

### Ticket emails and background worker

Ticket PDFs are generated after payment confirmation and emailed by Celery. Run a Redis
server, then start a worker in a separate terminal:

```bash
celery -A bookmymovie worker --loglevel=info
```

On Windows development machines, add `--pool=solo` to the worker command.
Start Celery Beat as a separate process to release expired hold rows and re-enqueue
pending ticket-email/refund jobs:

```bash
celery -A bookmymovie beat --loglevel=info
```

Expired holds are also ignored and lazily removed on seat-map and checkout requests, so
seat availability remains correct if Beat is temporarily unavailable.

Set `CELERY_BROKER_URL` to your Redis URL. For real email delivery, configure `EMAIL_HOST`,
`EMAIL_PORT`, `EMAIL_USE_TLS`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, and
`DEFAULT_FROM_EMAIL`. The default console email backend is intended for local development.
Set `PUBLIC_BASE_URL` to the public HTTPS origin of the deployed app so QR codes open the
live ticket verification page. If the broker is unavailable, the confirmed booking is kept
and its email remains pending for Celery Beat to enqueue again; delivery failures after a
task starts are retried up to five times with exponential backoff. Beat also re-enqueues
stale email/refund tasks if a worker stops before marking delivery complete.

Venues and local show schedules remain manageable through Django Admin. Current
third-party provider availability depends on the credentials configured below.

### Movie and cinema discovery integrations

Movie metadata can be searched and imported from TMDb:

```powershell
$env:TMDB_API_KEY = "your-tmdb-v3-key"
python manage.py import_tmdb_movies --query "Dhurandhar" --limit 5
```

Alternatively, set `TMDB_ACCESS_TOKEN` to a TMDb v4 read token. Imported posters use
TMDb's image service and imported movies display TMDb attribution. Review TMDb's
[attribution requirements](https://developer.themoviedb.org/docs/attribution) and
[terms of use](https://www.themoviedb.org/terms-of-use) before launching or redistributing
metadata.

TMDb's API is a global service; the `region=IN` parameter filters by Indian release
information, not by server location. If that regional search returns no titles, this
integration retries without the region filter. If requests time out, check connectivity
from the machine/container running Django and allow outbound HTTPS to
`api.themoviedb.org:443`. On Windows, test network access with:

```powershell
Test-NetConnection api.themoviedb.org -Port 443
```

Provider errors now distinguish missing/invalid credentials, quota limits, and network
timeouts. You can increase the request timeout with `TMDB_API_TIMEOUT` (seconds; default
15). Restart Django after changing environment variables. Never commit API keys.

Nearby cinemas are queried on demand from the public
[OpenStreetMap Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API); no API key
or billing account is required. Users can use the "Nearby cinemas" page and grant browser
location permission. Results show the distance, any mapped address, and a link to the
OpenStreetMap object. Cinema data is attributed to OpenStreetMap contributors under the
[ODbL](https://www.openstreetmap.org/copyright) and is not stored by the app. The public
Overpass service is community-run, has fair-use/rate limits, and is not guaranteed to be
available for production traffic. Set `OSM_OVERPASS_URL` to another compatible Overpass
instance if needed and `OSM_OVERPASS_TIMEOUT` to adjust the request timeout.

Actual showtimes and seat inventory need a commercial agreement and credentials from the
exhibitor or ticketing partner. The app provides a `ShowtimeProvider` adapter contract in
`movies/showtime_providers.py`; implement an adapter that returns `ProviderShowtime`
records from `search_showtimes(movie, latitude, longitude, show_date)`, then set
`SHOWTIME_PROVIDER` to its dotted class path (for example
`myproject.ticketing.PartnerShowtimeProvider`). Provider results link to the partner's
HTTPS checkout and do not masquerade as local inventory. Until an authorized adapter and
credentials are configured, only local schedules are shown.

## Deployment

This project is configured for deployment on **Vercel** using `vercel.json`, with Gunicorn as the WSGI server and PostgreSQL as the production database (via `dj-database-url`).

## Contributing

Contributions, issues, and feature requests are welcome. Feel free to check the [issues page](https://github.com/sairaj-1906/Django-BookMyShow-Clone/issues).
