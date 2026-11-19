"""robots.txt policy (RFC 9309 matching via protego). Missing or unreadable robots means allowed."""
from protego import Protego

AGENT = "noroshi-research"


class RobotsPolicy:
    def __init__(self, text: str | None):
        self._parser = None
        self._deny_all = False
        if text:
            try:
                self._parser = Protego.parse(text)
            except Exception:
                self._parser = None  # unreadable robots means allowed

    @classmethod
    def disallow_all(cls) -> "RobotsPolicy":
        """RFC 9309 2.3.1.4: robots.txt unreachable (5xx, network error) means complete disallow."""
        policy = cls(None)
        policy._deny_all = True
        return policy

    @property
    def unreachable(self) -> bool:
        """True for the disallow-all decision taken because robots.txt could not be read (RFC 9309 2.3.1.4)."""
        return self._deny_all

    def allowed(self, path: str) -> bool:
        if self._deny_all:
            return False
        if self._parser is None:
            return True
        return bool(self._parser.can_fetch("http://example.invalid" + path, AGENT))
