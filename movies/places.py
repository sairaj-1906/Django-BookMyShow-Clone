import hashlib
import json
from math import asin, cos, radians, sin, sqrt
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode
from urllib.request import Request, urlopen

from django.core.cache import cache


class OpenStreetMapError(Exception):
    """Raised when OpenStreetMap search services cannot return cinema results."""


class OpenStreetMapClient:
    NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
    OVERPASS_URL = "https://overpass-api.de/api/interpreter"
    USER_AGENT = "BookMySeatCinemaFinder/1.0 (https://github.com/sairaj-1906/Django-BookMyShow-Clone)"
    TIMEOUT_SECONDS = 15
    MAX_RESPONSE_BYTES = 2 * 1024 * 1024
    SEARCH_RADIUS_METERS = 15000
    MAX_RESULTS = 20
    SEARCH_CACHE_SECONDS = 6 * 60 * 60
    GEOCODE_CACHE_SECONDS = 24 * 60 * 60

    @staticmethod
    def _cache_key(prefix, value):
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return f"osm-cinema:{prefix}:{digest}"

    def _get_json(self, url, params=None, data=None, headers=None):
        if params:
            url = f"{url}?{urlencode(params)}"
        request = Request(
            url,
            data=data,
            method="POST" if data is not None else "GET",
            headers={
                "Accept": "application/json",
                "User-Agent": self.USER_AGENT,
                **(headers or {}),
            },
        )
        try:
            with urlopen(request, timeout=self.TIMEOUT_SECONDS) as response:
                body = response.read(self.MAX_RESPONSE_BYTES + 1)
            if len(body) > self.MAX_RESPONSE_BYTES:
                raise OpenStreetMapError("OpenStreetMap response exceeded the size limit.")
            return json.loads(body.decode("utf-8"))
        except HTTPError as exc:
            raise OpenStreetMapError(
                f"OpenStreetMap search returned HTTP {exc.code}."
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise OpenStreetMapError("Could not connect to OpenStreetMap search.") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenStreetMapError("OpenStreetMap returned invalid JSON.") from exc

    @staticmethod
    def _take_rate_limit(service):
        if not cache.add(f"osm-cinema:{service}:last-request", True, timeout=1):
            raise OpenStreetMapError(
                "The free map search is busy. Please wait a moment and try again."
            )

    def search_cinemas(self, location):
        normalized = " ".join(location.casefold().split())
        cache_key = self._cache_key("results", normalized)
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        coordinates = self._geocode(normalized)
        if coordinates is None:
            cache.set(cache_key, [], self.SEARCH_CACHE_SECONDS)
            return []
        latitude, longitude = coordinates
        query = (
            "[out:json][timeout:12];("
            f'node["amenity"="cinema"](around:{self.SEARCH_RADIUS_METERS},{latitude},{longitude});'
            f'way["amenity"="cinema"](around:{self.SEARCH_RADIUS_METERS},{latitude},{longitude});'
            f'relation["amenity"="cinema"](around:{self.SEARCH_RADIUS_METERS},{latitude},{longitude});'
            ");out center tags;"
        )
        self._take_rate_limit("overpass")
        data = self._get_json(
            self.OVERPASS_URL,
            data=urlencode({"data": query}).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        elements = data.get("elements", [])
        if not isinstance(elements, list):
            raise OpenStreetMapError("OpenStreetMap returned an invalid cinema list.")

        cinemas = []
        for element in elements:
            if not isinstance(element, dict):
                continue
            tags = element.get("tags")
            if not isinstance(tags, dict) or not isinstance(tags.get("name"), str):
                continue
            point = self._element_coordinates(element)
            if point is None:
                continue
            lat, lon = point
            address = self._address(tags)
            cinemas.append(
                {
                    "name": tags["name"][:200],
                    "address": address[:400],
                    "maps_url": (
                        f"https://www.openstreetmap.org/?mlat={lat:.6f}"
                        f"&mlon={lon:.6f}#map=17/{lat:.6f}/{lon:.6f}"
                    ),
                    "distance_km": round(
                        self._distance_km(latitude, longitude, lat, lon), 1
                    ),
                }
            )
        cinemas.sort(key=lambda cinema: cinema["distance_km"])
        cinemas = cinemas[: self.MAX_RESULTS]
        cache.set(cache_key, cinemas, self.SEARCH_CACHE_SECONDS)
        return cinemas

    def _geocode(self, location):
        cache_key = self._cache_key("geocode", location)
        cached = cache.get(cache_key)
        if cached is not None:
            return tuple(cached) if cached else None

        self._take_rate_limit("nominatim")

        result = self._get_json(
            self.NOMINATIM_URL,
            params={
                "q": f"{location}, India",
                "countrycodes": "in",
                "format": "jsonv2",
                "limit": 1,
            },
        )
        if not isinstance(result, list) or not result:
            cache.set(cache_key, [], self.GEOCODE_CACHE_SECONDS)
            return None
        try:
            latitude = float(result[0]["lat"])
            longitude = float(result[0]["lon"])
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise OpenStreetMapError(
                "OpenStreetMap returned invalid location coordinates."
            ) from exc
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise OpenStreetMapError(
                "OpenStreetMap returned out-of-range location coordinates."
            )
        cache.set(cache_key, [latitude, longitude], self.GEOCODE_CACHE_SECONDS)
        return latitude, longitude

    @staticmethod
    def _element_coordinates(element):
        latitude = element.get("lat")
        longitude = element.get("lon")
        if latitude is None or longitude is None:
            center = element.get("center", {})
            latitude = center.get("lat")
            longitude = center.get("lon")
        try:
            latitude = float(latitude)
            longitude = float(longitude)
        except (TypeError, ValueError):
            return None
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return None
        return latitude, longitude

    @staticmethod
    def _address(tags):
        full_address = tags.get("addr:full")
        if full_address:
            return str(full_address)
        parts = [
            tags.get("addr:housenumber"),
            tags.get("addr:street"),
            tags.get("addr:suburb"),
            tags.get("addr:city") or tags.get("addr:town") or tags.get("addr:village"),
        ]
        return ", ".join(str(part) for part in parts if part)

    @staticmethod
    def _distance_km(lat1, lon1, lat2, lon2):
        lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
        delta_lat = lat2 - lat1
        delta_lon = lon2 - lon1
        value = (
            sin(delta_lat / 2) ** 2
            + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
        )
        return 6371 * 2 * asin(sqrt(value))
