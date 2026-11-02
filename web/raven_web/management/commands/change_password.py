from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.management.base import BaseCommand, CommandError

from raven_web.management.credentials import read_password


class Command(BaseCommand):
    help = "Change the Raven web password and revoke existing login sessions"

    def add_arguments(self, parser):
        parser.add_argument("--username")

    def handle(self, *args, **options):
        users = get_user_model().objects.all()
        if users.count() != 1:
            raise CommandError("Exactly one Raven web account must exist")
        user = users.get()
        if options["username"] and options["username"] != user.username:
            raise CommandError("The username does not match the Raven web account")
        user.set_password(read_password(user))
        user.save(update_fields=["password"])
        Session.objects.all().delete()
        self.stdout.write("Password changed. Existing browser sessions are revoked.")
