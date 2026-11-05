import hashlib
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.core.cache import cache


class OpenStreetMapError(Exception):
    """Raised when OpenStreetMap search cannot return cinema results."""


class OpenStreetMapClient:
    NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
    USER_AGENT = "BookMySeatCinemaFinder/1.0 (https://github.com/sairaj-1906/Django-BookMyShow-Clone)"
    TIMEOUT_SECONDS = 12
    MAX_RESPONSE_BYTES = 2 * 1024 * 1024
    MAX_RESULTS = 10
    SEARCH_CACHE_SECONDS = 6 * 60 * 60

    @staticmethod
    def _cache_key(location):
        digest = hashlib.sha256(location.encode("utf-8")).hexdigest()
        return f"osm-cinema:results:{digest}"

    def search_cinemas(self, location):
        normalized = " ".join(location.casefold().split())
        cache_key = self._cache_key(normalized)
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        if not cache.add("osm-cinema:nominatim:last-request", True, timeout=1):
            raise OpenStreetMapError(
                "The free map search is busy. Please wait a moment and try again."
            )

        request = Request(
            f"{self.NOMINATIM_URL}?"
            f"{urlencode({'q': f'cinema in {normalized}, India', 'countrycodes': 'in', 'format': 'jsonv2', 'addressdetails': 1, 'limit': 30})}",
            headers={
                "Accept": "application/json",
                "User-Agent": self.USER_AGENT,
            },
        )
        try:
            with urlopen(request, timeout=self.TIMEOUT_SECONDS) as response:
                body = response.read(self.MAX_RESPONSE_BYTES + 1)
            if len(body) > self.MAX_RESPONSE_BYTES:
                raise OpenStreetMapError(
                    "OpenStreetMap response exceeded the size limit."
                )
            data = json.loads(body.decode("utf-8"))
        except HTTPError as exc:
            raise OpenStreetMapError(
                f"OpenStreetMap search returned HTTP {exc.code}."
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise OpenStreetMapError(
                "Could not connect to OpenStreetMap search."
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenStreetMapError(
                "OpenStreetMap returned invalid JSON."
            ) from exc

        if not isinstance(data, list):
            raise OpenStreetMapError("OpenStreetMap returned an invalid result list.")
        cinemas = []
        seen = set()
        for item in data:
            if (
                not isinstance(item, dict)
                or item.get("category") != "amenity"
                or item.get("type") != "cinema"
                or not isinstance(item.get("name"), str)
            ):
                continue
            try:
                latitude = float(item["lat"])
                longitude = float(item["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                continue
            key = (item.get("osm_type"), item.get("osm_id"))
            if key != (None, None) and key in seen:
                continue
            seen.add(key)
            cinemas.append(
                {
                    "name": item["name"][:200],
                    "address": self._address(item),
                    "maps_url": (
                        f"https://www.openstreetmap.org/?mlat={latitude:.6f}"
                        f"&mlon={longitude:.6f}#map=17/{latitude:.6f}/{longitude:.6f}"
                    ),
                }
            )
            if len(cinemas) >= self.MAX_RESULTS:
                break

        cache.set(cache_key, cinemas, self.SEARCH_CACHE_SECONDS)
        return cinemas

    @staticmethod
    def _address(item):
        address = item.get("address")
        if not isinstance(address, dict):
            address = {}
        parts = [
            address.get("road"),
            address.get("suburb") or address.get("neighbourhood"),
            address.get("city") or address.get("town") or address.get("village"),
            address.get("state"),
        ]
        result = ", ".join(str(part) for part in parts if part)
        if result:
            return result
        display_name = item.get("display_name", "")
        if isinstance(display_name, str):
            return display_name.replace(f"{item.get('name', '')}, ", "", 1)[:400]
        return ""
