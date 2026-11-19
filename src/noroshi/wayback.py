"""Internet Archive lookups: when a file first appeared, and where a dead host's last capture went."""
import re
import time
from datetime import date
from urllib.parse import urlsplit

import httpx

from noroshi.domains import registrable_domain

CDX = "http://web.archive.org/cdx/search/cdx"
REPLAY = "http://web.archive.org/web/{ts}id_/http://{host}/"
_REPLAY_TARGET = re.compile(r"/web/\d+\w*_?/(https?://.+)$")


class WaybackError(RuntimeError):
    """The Internet Archive answered with an error or something that is not the expected JSON."""


def throttled(get, interval: float = 1.0, clock=time.monotonic, sleep=time.sleep):
    """Wrap `get` so calls are at least `interval` seconds apart."""
    last = [None]

    def wrapper(url, **kw):
        if last[0] is not None and clock() - last[0] < interval:
            sleep(interval - (clock() - last[0]))
        last[0] = clock()
        return get(url, **kw)

    return wrapper


def refused(exc: BaseException) -> bool:
    """True when the exception, or one it was raised from, is a refused connection (the archive turning us away)."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ConnectionRefusedError):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _rows(resp) -> list[dict]:
    """CDX json: a header row then data rows. An empty 200 reply means no capture; anything else
    that is not that JSON, including a non-200 status, raises WaybackError."""
    if resp.status_code != 200:
        raise WaybackError(f"Internet Archive answered HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        if not (getattr(resp, "text", "") or "").strip():
            return []
        raise WaybackError("Internet Archive reply was not JSON") from None
    if not data:
        return []
    if not (isinstance(data, list) and all(isinstance(r, list) for r in data)):
        raise WaybackError("Internet Archive reply was not a CDX JSON table")
    return [dict(zip(data[0], row)) for row in data[1:]]


def first_seen(host: str, path: str, get=httpx.get) -> date | None:
    """Date of the earliest capture of host+path that was a 200 text/plain file, if any."""
    params = {"url": f"{host}{path}", "output": "json", "fl": "timestamp", "limit": 1,
              "filter": ["statuscode:200", "mimetype:text/plain"]}
    rows = _rows(get(CDX, params=params, timeout=30))
    if not rows:
        return None
    ts = rows[0]["timestamp"]
    return date(int(ts[:4]), int(ts[4:6]), int(ts[6:8]))


def last_redirect_target(host: str, get=httpx.get) -> str | None:
    """If the host's most recent homepage capture was a redirect to another registrable domain,
    that domain; otherwise None."""
    params = {"url": host, "output": "json", "fl": "timestamp,statuscode", "limit": -1}
    rows = _rows(get(CDX, params=params, timeout=30))
    if not rows or not rows[0]["statuscode"].startswith("3"):
        return None
    resp = get(REPLAY.format(ts=rows[0]["timestamp"], host=host), timeout=30, follow_redirects=False)
    if resp.status_code == 200:
        return None  # the replay no longer redirects
    if resp.status_code not in (301, 302, 303, 307, 308):
        raise WaybackError(f"Internet Archive answered HTTP {resp.status_code}")
    m = _REPLAY_TARGET.search(resp.headers.get("location", ""))
    if not m:
        return None
    target = urlsplit(m.group(1)).hostname
    if not target:
        return None
    target = registrable_domain(target)
    return target if target != registrable_domain(host) else None
