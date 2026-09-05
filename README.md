# BookMySeat — Django BookMyShow Clone 🎬

A movie ticket booking web application built with Django, inspired by BookMyShow. Users can browse movies, view details, and book seats — all through a clean, responsive interface.

**Live demo:** [django-book-my-show-clone.vercel.app](https://django-book-my-show-clone.vercel.app/)

## Features

- Browse a catalog of movies with posters and details
- User registration and login (`users` app)
- Movie listings and details (`movies` app)
- Seat/ticket booking flow
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

## Contributing

Contributions, issues, and feature requests are welcome. Feel free to check the [issues page](https://github.com/sairaj-1906/Django-BookMyShow-Clone/issues).
