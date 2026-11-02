from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from raven_web.management.credentials import read_password


class Command(BaseCommand):
    help = "Initialize Raven's single web account and private TLS configuration"

    def add_arguments(self, parser):
        parser.add_argument("--username")
        parser.add_argument("--origin", required=True)

    def handle(self, *args, **options):
        call_command("migrate", interactive=False, verbosity=0)
        users = get_user_model().objects
        if users.exists():
            raise CommandError("An account already exists; use raven web password to change its password")
        username = options["username"] or input("Username: ").strip()
        user = get_user_model()(username=username)
        try:
            user.full_clean(exclude=["password"])
        except ValidationError as error:
            raise CommandError("; ".join(error.messages)) from error
        user.set_password(read_password(user))
        user.save()
        self.stdout.write("Raven web account initialized. Trust the TLS certificate before logging in.")
