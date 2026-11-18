import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from raven_web.logins import LOGIN_ID, account_logins, revoke


class Command(BaseCommand):
    help = "List the Raven web account's browser logins, or revoke one or all of them; prints JSON"

    def add_arguments(self, parser):
        parser.add_argument("action", nargs="?", choices=("list", "revoke"), default="list")
        parser.add_argument("id", nargs="?", help="login ID to revoke")
        parser.add_argument("--all", action="store_true", help="revoke every login")

    def handle(self, *args, **options):
        users = get_user_model().objects.all()
        if users.count() != 1:
            raise CommandError("Exactly one Raven web account must exist")
        found = account_logins(users.get())
        if options["action"] == "list":
            if options["id"] or options["all"]:
                raise CommandError("list takes no login ID or --all")
            self.stdout.write(json.dumps({"logins": [entry for _, entry in found]}))
            return
        if bool(options["id"]) == options["all"]:
            raise CommandError("Give one login ID or --all")
        if options["all"]:
            keys = [key for key, _ in found]
        else:
            if not LOGIN_ID.fullmatch(options["id"]):
                raise CommandError("A login ID is 16 hexadecimal characters; raven web logins lists them")
            keys = [key for key, entry in found if entry["id"] == options["id"]]
            if not keys:
                raise CommandError(f"No login has ID {options['id']}; raven web logins lists them")
        # The gateway's session check closes the revoked logins' open desktop connections within seconds.
        self.stdout.write(json.dumps({"revoked": revoke(keys)}))
