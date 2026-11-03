from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from importlib import import_module
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


@dataclass(frozen=True)
class ProviderShowtime:
    """A licensed external offer. It is not a local Show and cannot use local checkout."""

    provider: str
    external_id: str
    cinema_name: str
    address: str
    starts_at: datetime
    language: str
    screen_format: str
    booking_url: str
    price: Decimal | None = None


class ShowtimeProvider:
    """Base contract for an authorized ticketing/exhibitor integration.

    Implement ``search_showtimes(movie, latitude, longitude, show_date)`` and return
    ``ProviderShowtime`` objects whose booking URLs point to the provider's checkout.
    """

    def search_showtimes(self, *, movie, latitude, longitude, show_date):
        raise NotImplementedError


def get_showtime_provider():
    dotted_path = settings.SHOWTIME_PROVIDER
    if not dotted_path:
        return None
    module_path, separator, class_name = dotted_path.rpartition(".")
    if not separator:
        raise ImproperlyConfigured("SHOWTIME_PROVIDER must be a dotted Python class path.")
    provider_class = getattr(import_module(module_path), class_name)
    if not issubclass(provider_class, ShowtimeProvider):
        raise ImproperlyConfigured("SHOWTIME_PROVIDER must subclass ShowtimeProvider.")
    return provider_class()


def validate_provider_showtimes(showtimes):
    """Reject malformed or unsafe provider responses before rendering outbound links."""
    validated = []
    for showtime in showtimes:
        if not isinstance(showtime, ProviderShowtime):
            raise ValueError("Showtime adapters must return ProviderShowtime records.")
        if (
            not isinstance(showtime.starts_at, datetime)
            or not showtime.starts_at.tzinfo
            or showtime.starts_at.utcoffset() is None
        ):
            raise ValueError("Provider showtimes must have timezone-aware start times.")
        parsed_url = urlsplit(showtime.booking_url)
        if (
            parsed_url.scheme != "https"
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
        ):
            raise ValueError("Provider booking links must use HTTPS.")
        if not showtime.external_id or not showtime.cinema_name:
            raise ValueError("Provider showtimes must identify the cinema and external event.")
        validated.append(showtime)
    return validated


def parse_provider_date(value):
    if not value:
        return datetime.now(ZoneInfo("Asia/Kolkata")).date()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("show_date must use YYYY-MM-DD format.") from exc
