import getpass
import sys

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import CommandError


def read_password(user):
    if not sys.stdin.isatty():
        raise CommandError("An interactive terminal is required for the password prompt")
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise CommandError("Passwords do not match")
    try:
        validate_password(password, user)
    except ValidationError as error:
        raise CommandError("; ".join(error.messages)) from error
    return password
