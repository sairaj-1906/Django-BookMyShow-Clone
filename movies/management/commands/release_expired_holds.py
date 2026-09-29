from django.core.management.base import BaseCommand
from django.utils import timezone

from movies.models import SeatHold


class Command(BaseCommand):
    help = (
        "Delete expired seat holds so their seats become available again. "
        "Expired holds are already ignored by the app on every request, so this "
        "isn't required for correctness — but scheduling it (e.g. every minute via "
        "cron or Celery beat) keeps the SeatHold table from accumulating stale rows."
    )

    def handle(self, *args, **options):
        deleted, _ = SeatHold.objects.filter(expires_at__lte=timezone.now()).delete()
        self.stdout.write(
            self.style.SUCCESS(f"Released {deleted} expired seat hold(s).")
        )
