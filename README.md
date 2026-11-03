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

### Ticket emails and background worker

Ticket PDFs are generated after payment confirmation and emailed by Celery. Run a Redis
server, then start a worker in a separate terminal:

```bash
celery -A bookmymovie worker --loglevel=info
```

On Windows development machines, add `--pool=solo` to the worker command.

Set `CELERY_BROKER_URL` to your Redis URL. For real email delivery, configure `EMAIL_HOST`,
`EMAIL_PORT`, `EMAIL_USE_TLS`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, and
`DEFAULT_FROM_EMAIL`. The default console email backend is intended for local development.
Set `PUBLIC_BASE_URL` to the public HTTPS origin of the deployed app so QR codes open the
live ticket verification page. If the broker is unavailable, the confirmed booking is kept
and an error is logged; email delivery failures after a task starts are retried up to five
times with exponential backoff.

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

Nearby cinemas are looked up on demand through Google Places API (New). Enable Places API
for a Google Cloud project, configure billing and an appropriately restricted server key,
then set `GOOGLE_PLACES_API_KEY`. Users can use the "Nearby cinemas" page and grant browser
location permission. Place results are not saved to the database; the page identifies
Google Maps as the source. See Google's [Nearby Search documentation](https://developers.google.com/maps/documentation/places/web-service/nearby-search).

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
