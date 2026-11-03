import json
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from django.conf import settings


class CatalogIntegrationError(Exception):
    """A provider is unavailable, misconfigured, or returned an invalid response."""


def _request_json(url, *, headers=None, body=None, timeout=8):
    request = Request(url, data=body, headers=headers or {}, method="POST" if body else "GET")
    host = urlsplit(url).hostname or "catalog provider"
    try:
        with urlopen(
            request,
            timeout=(
                settings.TMDB_API_TIMEOUT
                if host == "api.themoviedb.org"
                else timeout
            ),
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in (401, 403):
            message = (
                f"{host} rejected the API credentials (HTTP {exc.code}); verify the key/token "
                "and, for Google Places, that Places API (New) is enabled with billing."
            )
        elif exc.code == 429:
            message = f"{host} rate-limited the request (HTTP 429); retry later or check your quota."
        else:
            message = f"{host} returned HTTP {exc.code}."
        raise CatalogIntegrationError(message) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise CatalogIntegrationError(
            f"Connection to {host} timed out. Check outbound HTTPS, DNS, firewall/proxy "
            "settings, and provider availability from the machine running Django."
        ) from exc
    except URLError as exc:
        raise CatalogIntegrationError(
            f"Could not connect to {host}: {exc.reason}. Check DNS and outbound HTTPS access."
        ) from exc
    except (OSError, ValueError) as exc:
        raise CatalogIntegrationError(
            f"{host} returned an invalid response or the connection failed: {exc}."
        ) from exc


def _tmdb_request(path, params=None):
    if not settings.TMDB_API_KEY and not settings.TMDB_ACCESS_TOKEN:
        raise CatalogIntegrationError("TMDB_API_KEY or TMDB_ACCESS_TOKEN is not configured.")
    query = dict(params or {})
    headers = {"Accept": "application/json"}
    if settings.TMDB_ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {settings.TMDB_ACCESS_TOKEN}"
    else:
        query["api_key"] = settings.TMDB_API_KEY
    url = f"https://api.themoviedb.org/3/{path}"
    if query:
        url = f"{url}?{urlencode(query)}"
    return _request_json(url, headers=headers)


def search_tmdb_movies(query, *, language="en-US", region="IN", page=1):
    """Prefer India release matches, then retry globally if TMDb has no India release data."""
    data = _tmdb_request(
        "search/movie",
        {
            "query": query,
            "language": language,
            "region": region,
            "include_adult": "false",
            "page": max(1, min(int(page), 500)),
        },
    )
    results = data.get("results", [])
    if results or not region:
        return results

    fallback = _tmdb_request(
        "search/movie",
        {
            "query": query,
            "language": language,
            "include_adult": "false",
            "page": max(1, min(int(page), 500)),
        },
    )
    return fallback.get("results", [])


def get_tmdb_movie(tmdb_id, *, language="en-US"):
    return _tmdb_request(
        f"movie/{int(tmdb_id)}",
        {"language": language, "append_to_response": "credits,videos"},
    )


def tmdb_poster_url(poster_path):
    if (
        not poster_path
        or not poster_path.startswith("/")
        or poster_path.startswith("//")
        or not re.fullmatch(r"/[A-Za-z0-9_./-]+", poster_path)
    ):
        return ""
    return f"https://image.tmdb.org/t/p/w500{poster_path}"


def find_nearby_theaters(latitude, longitude, *, radius_meters=25000):
    """Query Places API (New) on demand; Google place content is not persisted."""
    if not settings.GOOGLE_PLACES_API_KEY:
        raise CatalogIntegrationError("GOOGLE_PLACES_API_KEY is not configured.")
    body = json.dumps(
        {
            "includedTypes": ["movie_theater"],
            "maxResultCount": 20,
            "rankPreference": "DISTANCE",
            "locationRestriction": {
                "circle": {
                    "center": {
                        "latitude": latitude,
                        "longitude": longitude,
                    },
                    "radius": radius_meters,
                }
            },
        }
    ).encode("utf-8")
    data = _request_json(
        "https://places.googleapis.com/v1/places:searchNearby",
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": settings.GOOGLE_PLACES_API_KEY,
            "X-Goog-FieldMask": (
                "places.id,places.displayName,places.formattedAddress,"
                "places.location,places.googleMapsUri,places.rating,"
                "places.userRatingCount"
            ),
        },
        body=body,
        timeout=settings.GOOGLE_PLACES_TIMEOUT,
    )
    return data.get("places", [])
