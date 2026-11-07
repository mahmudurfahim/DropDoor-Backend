import os
from django.core.management.base import BaseCommand
from shop.models import User


class Command(BaseCommand):
    help = "Create the admin login (default dropdoor / dropdoor). Runs on every deploy."

    def handle(self, *a, **k):
        name, pw = os.getenv("ADMIN_USER", "dropdoor"), os.getenv("ADMIN_PASSWORD", "")
        if "ADMIN_USER" not in os.environ and not User.objects.filter(username=name).exists():
            old = User.objects.filter(is_staff=True, phone__isnull=True)  # the admin made under the old default name: rename it, so no second admin appears
            if old.count() == 1:
                old.update(username=name)
        u, created = User.objects.get_or_create(username=name, defaults={"first_name": "Admin"})
        u.is_staff = u.is_superuser = u.phone_verified = u.is_active = True
        u.status = "approved"
        if created or pw:  # an existing admin keeps the password you changed in the panel
            u.set_password(pw or name)
        u.save()
        self.stdout.write(f"Admin ready: {name}")
