# BookMySeat — Django BookMyShow Clone 🎬

A movie ticket booking web application built with Django, inspired by BookMyShow. Users can browse movies, view details, and book seats — all through a clean, responsive interface.

**Live demo:** [django-book-my-show-clone.vercel.app](https://django-book-my-show-clone.vercel.app/)

## Features

- Browse a catalog of movies with posters and details
- User registration and login (`users` app)
- Movie listings and details (`movies` app)
- Seat/ticket booking flow
- Confirmed bookings generate downloadable PDF tickets with QR verification; Celery sends ticket emails asynchronously and retries delivery failures
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

4. Apply migrations

   ```bash
   python manage.py migrate
   ```

5. Create a superuser (optional, for admin access)

   ```bash
   python manage.py createsuperuser
   ```

6. Run the development server

   ```bash
   python manage.py runserver
   ```

7. Visit `http://127.0.0.1:8000/` in your browser.

## Deployment

This project is configured for deployment on **Vercel** using `vercel.json`, with Gunicorn as the WSGI server and PostgreSQL as the production database (via `dj-database-url`).

### Ticket email worker

Ticket delivery requires a Redis broker, an SMTP provider, and a continuously running
Celery worker in addition to the Django web process. Set these environment variables:

- `CELERY_BROKER_URL` — Redis URL, for example `redis://localhost:6379/0`
- `EMAIL_BACKEND` — `django.core.mail.backends.smtp.EmailBackend`
- `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`
- `EMAIL_USE_TLS` (defaults to `true`) and `EMAIL_USE_SSL` (defaults to `false`)
- `DEFAULT_FROM_EMAIL` — verified sender address
- `SITE_URL` — public base URL used by the ticket QR verification link

For local development, start Redis and run the Django server and worker in separate
terminals. On Windows, Celery's `solo` pool is suitable for local testing:

```powershell
python manage.py runserver
celery -A bookmymovie worker --loglevel=INFO --pool=solo
```

In production, run the worker as a separate persistent service using the same code,
database, Redis broker, and mail configuration as the web service. Vercel functions do
not keep a Celery worker alive, so the worker must run on a worker-capable host.
The development default is Django's console email backend; it prints messages instead
of delivering them.

Movie and show listings in this project are managed through the Django admin. No
licensed live Indian cinema/theater feed is configured, so the site does not claim
real-time nationwide listings. Connect a data provider for that separately rather than
displaying fabricated current listings.

### Import movie metadata from TMDB

The app can import movie metadata and poster images for movies currently playing in
India. TMDB does not provide theater showtimes, seat availability, or booking inventory.
Create a TMDB API credential and set either the API Read Access Token or API key in
PowerShell before running the import:

```powershell
$env:TMDB_ACCESS_TOKEN = "your-TMDB-API-read-access-token"
python manage.py migrate
python manage.py import_tmdb_movies
```

Alternatively, set `TMDB_API_KEY` instead of `TMDB_ACCESS_TOKEN`. The access token is
sent as a bearer token; the API key is sent only in server-side API requests. Do not
commit either credential. Each run imports one page (20 results) by default and updates
already-imported records using their TMDB ID. To import more pages, use
`python manage.py import_tmdb_movies --pages 3`. Use `--region` and `--language` to
override the defaults (`IN` and `en-US`). The command downloads posters into the
configured media storage and updates movie catalog entries; it does not create theaters
or shows.

TMDB requires its approved logo and attribution notice in the app; the shared footer
includes both. TMDB's free developer API is for non-commercial use. Contact TMDB for a
commercial license before using this data in a revenue-generating service.

### Find nearby cinemas with OpenStreetMap

The showtimes page has an optional cinema search using OpenStreetMap's public Nominatim geocoder and Overpass data service. No API key, account, or paid billing is required. Search by city or area; results are community-maintained and can be incomplete. This feature does not provide showtimes, seat availability, or ticket inventory.

The app caches search results and rate-limits requests to the public services. Do not turn this into autocomplete or high-volume/bulk queries. The built-in in-memory cache is suitable for local development and a single app process; for a multi-worker production deployment, configure a shared cache backend or use a hosted OSM-compatible provider. Results are attributed to [OpenStreetMap contributors](https://www.openstreetmap.org/copyright) and link to OpenStreetMap. See the [Nominatim usage policy](https://operations.osmfoundation.org/policies/nominatim/) and [Overpass API guidance](https://wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances).

## Contributing

Contributions, issues, and feature requests are welcome. Feel free to check the [issues page](https://github.com/sairaj-1906/Django-BookMyShow-Clone/issues).
