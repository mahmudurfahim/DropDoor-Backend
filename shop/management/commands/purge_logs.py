from django.core.management.base import BaseCommand
from shop import activity


class Command(BaseCommand):
    help = "Delete reseller logs and admin logs older than 30 days (this also happens by itself about every 6 hours)."

    def handle(self, *a, **k):
        self.stdout.write(f"Deleted {activity.purge()} old log rows")
