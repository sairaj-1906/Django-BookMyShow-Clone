import json
import math
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
                f"{host} rejected the API credentials or request (HTTP {exc.code}); "
                "verify the provider configuration and access policy."
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
    """Find nearby cinema venues through OpenStreetMap's public Overpass API."""
    try:
        latitude, longitude = float(latitude), float(longitude)
        radius_meters = int(radius_meters)
    except (TypeError, ValueError) as exc:
        raise CatalogIntegrationError("A valid location and radius are required.") from exc
    if (
        not math.isfinite(latitude)
        or not math.isfinite(longitude)
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
        or not 1 <= radius_meters <= 50000
    ):
        raise CatalogIntegrationError("The location or search radius is out of range.")

    overpass_url = getattr(
        settings,
        "OSM_OVERPASS_URL",
        "https://overpass-api.de/api/interpreter",
    )
    timeout = getattr(settings, "OSM_OVERPASS_TIMEOUT", 25)
    query = (
        f"[out:json][timeout:{max(1, int(timeout))}];("
        f'node["amenity"="cinema"](around:{radius_meters},{latitude},{longitude});'
        f'way["amenity"="cinema"](around:{radius_meters},{latitude},{longitude});'
        f'relation["amenity"="cinema"](around:{radius_meters},{latitude},{longitude});'
        ");out center tags 30;"
    )
    body = urlencode({"data": query}).encode("utf-8")
    data = _request_json(
        overpass_url,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": (
                "BookMySeat/1.0 (nearby cinema search; "
                "https://github.com/sairaj-1906/Django-BookMyShow-Clone)"
            ),
        },
        body=body,
        timeout=timeout,
    )

    def distance_meters(lat, lon):
        earth_radius = 6_371_000
        lat1, lat2 = math.radians(latitude), math.radians(lat)
        dlat = lat2 - lat1
        dlon = math.radians(lon - longitude)
        value = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        )
        return int(2 * earth_radius * math.asin(min(1, math.sqrt(value))))

    venues = []
    seen = set()
    for element in data.get("elements", []):
        tags = element.get("tags") or {}
        name = tags.get("name") or tags.get("brand") or tags.get("operator")
        center = element.get("center") or element
        try:
            lat, lon = float(center["lat"]), float(center["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not name or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        osm_type, osm_id = element.get("type"), element.get("id")
        if osm_type not in {"node", "way", "relation"} or not isinstance(osm_id, int):
            continue
        key = (osm_type, osm_id)
        if key in seen:
            continue
        seen.add(key)
        address = ", ".join(
            value
            for value in (
                " ".join(
                    part
                    for part in (
                        tags.get("addr:housenumber"),
                        tags.get("addr:street"),
                    )
                    if part
                ),
                tags.get("addr:suburb"),
                tags.get("addr:city") or tags.get("addr:town") or tags.get("addr:village"),
                tags.get("addr:postcode"),
            )
            if value
        )
        distance = distance_meters(lat, lon)
        if distance > radius_meters:
            continue
        venues.append(
            {
                "name": name,
                "address": address,
                "distance_meters": distance,
                "latitude": lat,
                "longitude": lon,
                "osm_url": f"https://www.openstreetmap.org/{osm_type}/{osm_id}",
            }
        )
    return sorted(venues, key=lambda venue: venue["distance_meters"])[:30]
