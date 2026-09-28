"""Turn a URL or website into the company's domain, e.g.
"https://www.app.Accurx.com/about" -> "accurx.com".

The domain is how the app recognises the same company across searches and
search sources, so it must be computed the same way everywhere.
"""

import re
from urllib.parse import urlparse

# Second-level labels used under country codes, e.g. example.co.uk
_COUNTRY_SLDS = {"co", "com", "org", "net", "ac", "gov", "ltd", "plc", "me"}


def normalise_domain(value):
    """The registrable domain of a URL or bare host, lower-case, or None."""
    if not value:
        return None
    value = value.strip().lower()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", value):
        value = "http://" + value
    host = (urlparse(value).hostname or "").strip(".")
    if not host or "." not in host or re.fullmatch(r"[\d.]+", host):
        return None
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in _COUNTRY_SLDS and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])
