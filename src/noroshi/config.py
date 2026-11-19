"""Fixed measurement parameters. Values come from the study design; do not tune casually."""
import os
from dataclasses import dataclass
from pathlib import Path

UA_IDENTIFIED = "noroshi-research/1.0 (+https://github.com/k0nnect/noroshi)"
UA_BROWSER = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.1 Safari/605.1.15"
)

MAX_REQUESTS_PER_DOMAIN = 12  # sent attempts per listed domain per pass
MAX_REQUESTS_PER_SITE = 12  # sent attempts per site (registrable domain) per pass, across all listed domains,
# beyond homepage requests (each listed domain's start URL, its other host form after a network error and a
# meta-refresh target, each with its browser retry when refused)
MIN_DELAY_SECONDS = 2.0  # between two requests to one site
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 20.0
TOTAL_TIMEOUT = 60.0  # per attempt, counting only the time each hop is out (from sending it to reading its
# answer), summed over its redirect hops; waiting for a site's slot or gap never counts
RETRY_WAIT_SECONDS = TOTAL_TIMEOUT  # the longest a hop waits, for each pending retry, while another request's browser
# retry at that site is pending (fetch.PoliteFetcher._get_once); then it goes on as it would without the wait
MAX_BODY_BYTES = 1_048_576  # every response but the homepage
MAX_HOMEPAGE_BYTES = 4_194_304  # a homepage request's response
MAX_REDIRECTS = 5
GLOBAL_CONCURRENCY = 20
RETRY_PASS_MIN_HOURS = 48
WAYBACK_INTERVAL_SECONDS = 4.5  # between two requests of `noroshi wayback` to the Internet Archive, every request
# counted (a redirect lookup's replay request too); one a second was refused (after the freeze)
WAYBACK_MAX_FAILED_IN_A_ROW = 3  # failed lookups in a row that end `noroshi wayback` (a refusal ends it at once)

JUDGE_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5")
JUDGE_TIEBREAK = "claude-fable-5-2"
IDENTITY_MODELS = ("claude-sonnet-5-5", "claude-haiku-5-5")  # the two raters of the homepage identity check
IDENTITY_TIEBREAK = "claude-opus-5-5"  # decides when the two raters differ

REPO = Path(__file__).resolve().parents[2]


def root() -> Path:
    """The project root: $NOROSHI_ROOT when set, else the repository. Read each time it is asked for."""
    env = os.environ.get("NOROSHI_ROOT")
    return Path(env).expanduser().resolve() if env else REPO


@dataclass(frozen=True)
class Paths:
    """Every file location the commands use, all under one root."""
    root: Path

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def frames(self) -> Path:
        return self.data / "frames"

    @property
    def passes(self) -> Path:
        return self.frames / "resolution_passes"

    @property
    def info(self) -> Path:
        return self.root / "info"

    @property
    def protocol(self) -> Path:
        return self.root / "protocol"

    @property
    def frozen(self) -> Path:
        return self.protocol / "FROZEN.txt"

    @property
    def raw(self) -> Path:
        return self.data / "raw"

    @property
    def derived(self) -> Path:
        return self.data / "derived"

    @property
    def results(self) -> Path:
        return self.root / "results"

    @property
    def reference(self) -> Path:
        return self.data / "reference"

    @property
    def registries(self) -> Path:
        return self.reference / "registries"

    @property
    def registries_big(self) -> Path:
        return self.raw / "registries"

    @property
    def rucc(self) -> Path:
        return self.reference / "rucc2023.csv"

    @property
    def llm_log(self) -> Path:
        return self.raw / "llm_log.jsonl"


def paths() -> Paths:
    return Paths(root())
