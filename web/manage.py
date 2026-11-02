#!/usr/bin/env python3
import os
import sys


def main():
    os.umask(0o077)
    if len(sys.argv) > 1 and sys.argv[1] == "change-password":
        sys.argv[1] = "change_password"
    if len(sys.argv) > 1 and sys.argv[1] in ("setup", "init-data"):
        from raven_web.bootstrap import initialize, initialize_directory

        if sys.argv[1] == "init-data":
            initialize_directory(sys.argv[2:])
            return
        initialize(sys.argv[2:])
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "raven_web.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
