import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings


class TMDBError(Exception):
    """Raised when TMDB cannot return valid data."""


class TMDBClient:
    API_BASE = "https://api.themoviedb.org/3"
    IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
    TIMEOUT_SECONDS = 20
    MAX_POSTER_BYTES = 5 * 1024 * 1024

    def __init__(self):
        self.api_key = settings.TMDB_API_KEY
        self.access_token = settings.TMDB_ACCESS_TOKEN
        if not (self.access_token or self.api_key):
            raise TMDBError(
                "Set TMDB_ACCESS_TOKEN or TMDB_API_KEY in the environment."
            )

    def get_json(self, endpoint, params=None):
        query = dict(params or {})
        if self.api_key and not self.access_token:
            query["api_key"] = self.api_key
        url = f"{self.API_BASE}/{endpoint.lstrip('/')}"
        if query:
            url = f"{url}?{urlencode(query)}"
        headers = {"Accept": "application/json", "User-Agent": "BookMySeat/1.0"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=self.TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise TMDBError(
                f"TMDB returned HTTP {exc.code} for {endpoint}. "
                "Check the credentials and API access."
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise TMDBError(
                f"Could not connect to TMDB for {endpoint}."
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TMDBError(f"TMDB returned invalid JSON for {endpoint}.") from exc

    def get_poster(self, poster_path):
        if (
            not isinstance(poster_path, str)
            or not poster_path.startswith("/")
            or ".." in poster_path
        ):
            raise TMDBError("TMDB returned an invalid poster path.")
        request = Request(
            f"{self.IMAGE_BASE}{poster_path}",
            headers={"User-Agent": "BookMySeat/1.0"},
        )
        try:
            with urlopen(request, timeout=self.TIMEOUT_SECONDS) as response:
                content_type = response.headers.get_content_type()
                if content_type not in {"image/jpeg", "image/png", "image/webp"}:
                    raise TMDBError("TMDB returned a non-image poster.")
                content = response.read(self.MAX_POSTER_BYTES + 1)
        except HTTPError as exc:
            raise TMDBError(f"TMDB poster download returned HTTP {exc.code}.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise TMDBError("Could not download a TMDB poster.") from exc
        if not content or len(content) > self.MAX_POSTER_BYTES:
            raise TMDBError("TMDB poster is empty or exceeds the 5 MB limit.")
        return content, content_type
