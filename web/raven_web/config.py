import re
from urllib.parse import urlsplit


def parse_origin(value):
    value.encode("ascii")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or re.search(r"[\s\\]", value)
    ):
        raise ValueError("The origin must be an HTTPS origin, e.g. https://localhost:8443")
    parsed.port
    if parsed.netloc != parsed.netloc.lower():
        raise ValueError("Use a lowercase hostname in the origin")
    return parsed
