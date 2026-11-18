"""Browser logins kept in their own session rows: metadata, activity, listing and revocation without a model.

Each login stores only coarse browser facts (family, OS, phone or not), its login time and its last activity. They
live in the session row, so logout, revocation and the 8-hour expiry remove them with the login.
"""

import re
import secrets
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.contrib.auth import HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

LOGIN_KEY = "raven_login"
LOGIN_ID = re.compile(r"[0-9a-f]{16}")
ACTIVITY_INTERVAL = timedelta(seconds=60)
LEGACY_ID_SALT = "raven_web.logins.legacy-id"
# First match wins: Edge, Opera and Samsung Internet also name Chrome and Safari, and Chrome names Safari.
BROWSERS = (
    ("Edge", ("Edg/", "EdgA/", "EdgiOS/", "Edge/")),
    ("Opera", ("OPR/", "OPT/", "OPiOS/", "Opera")),
    ("Samsung Internet", ("SamsungBrowser/",)),
    ("Firefox", ("Firefox/", "FxiOS/")),
    ("Chrome", ("Chrome/", "CriOS/", "Chromium/")),
    ("Safari", ("Safari/",)),
)
# iOS says "like Mac OS X", Android says Linux: check them before macOS and Linux.
SYSTEMS = (
    ("Windows", ("Windows",)),
    ("iOS", ("iPhone", "iPad", "iPod")),
    ("Android", ("Android",)),
    ("ChromeOS", ("CrOS",)),
    ("macOS", ("Macintosh", "Mac OS X")),
    ("Linux", ("Linux", "X11")),
)
MOBILE_SYSTEMS = ("Android", "iOS")


def _family(user_agent, families):
    return next((name for name, tokens in families if any(token in user_agent for token in tokens)), None)


def classify(user_agent):
    """(browser, os, device) families from a User-Agent; None for a family it does not recognize."""
    browser = _family(user_agent, BROWSERS)
    system = _family(user_agent, SYSTEMS)
    device = "mobile" if system in MOBILE_SYSTEMS or "Mobi" in user_agent else "desktop"
    return browser, system, device


def _timestamp(moment):
    return moment.astimezone(dt_timezone.utc).isoformat(timespec="seconds")


def start(request):
    """Describe the login that login() just created and fix its expiry SESSION_COOKIE_AGE from now."""
    now = timezone.now()
    browser, system, device = classify(request.headers.get("User-Agent", ""))
    moment = _timestamp(now)
    request.session[LOGIN_KEY] = {"id": secrets.token_hex(8), "created": moment, "seen": moment,
                                  "browser": browser, "os": system, "device": device}
    # An absolute expiry: saving the session later, for activity or anything else, never extends it.
    request.session.set_expiry(now + timedelta(seconds=settings.SESSION_COOKIE_AGE))
    SessionStore.clear_expired()


def _activity_due(login, now):
    if not isinstance(login, dict):
        return False
    try:
        seen = datetime.fromisoformat(login["seen"])
    except (KeyError, TypeError, ValueError):
        return True
    return not timedelta(0) <= now - seen < ACTIVITY_INTERVAL


def activity_due(session):
    """Whether this login's last activity is older than ACTIVITY_INTERVAL, from the already loaded session."""
    return _activity_due(session.get(LOGIN_KEY), timezone.now())


def record_activity(session_key):
    """Set the login's last activity to now unless it was recorded within ACTIVITY_INTERVAL.

    Only the stored data changes, never expire_date, and only while the row still holds what was read: a login
    revoked or changed meanwhile is left alone. A login from before metadata existed has nothing to update.
    """
    now = timezone.now()
    row = Session.objects.filter(session_key=session_key, expire_date__gt=now).first()
    if row is None:
        return False
    store = SessionStore()
    data = store.decode(row.session_data)
    login = data.get(LOGIN_KEY)
    if not _activity_due(login, now):
        return False
    login["seen"] = _timestamp(now)
    return Session.objects.filter(session_key=session_key, session_data=row.session_data).update(
        session_data=store.encode(data)) == 1


def legacy_id(session_key):
    """A stable ID for a login without metadata that does not reveal its session key."""
    return salted_hmac(LEGACY_ID_SALT, session_key, algorithm="sha256").hexdigest()[:16]


def _describe(row, data):
    login = data.get(LOGIN_KEY)
    if not isinstance(login, dict) or not LOGIN_ID.fullmatch(str(login.get("id", ""))):
        login = {"id": legacy_id(row.session_key)}
    entry = {"id": login["id"]}
    entry.update((field, login.get(field)) for field in ("browser", "os", "device", "created", "seen"))
    entry["expires"] = _timestamp(row.expire_date)
    return entry


def account_logins(user):
    """[(session_key, entry)] for each unexpired login of user, most recent activity first.

    The session key stays on the server side: callers show entries only.
    """
    auth_hash = user.get_session_auth_hash()
    found = []
    for row in Session.objects.filter(expire_date__gt=timezone.now()):
        data = row.get_decoded()
        if data.get(SESSION_KEY) != str(user.pk) or not constant_time_compare(data.get(HASH_SESSION_KEY, ""),
                                                                            auth_hash):
            continue
        found.append((row.session_key, _describe(row, data)))
    found.sort(key=lambda item: (item[1]["seen"] or "", item[1]["expires"]), reverse=True)
    return found


def revoke(session_keys):
    """Delete these logins; return how many existed."""
    return Session.objects.filter(session_key__in=session_keys).delete()[0]
