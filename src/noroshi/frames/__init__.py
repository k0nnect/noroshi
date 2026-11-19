"""Study frames: the organisations a measurement covers."""
import re

NAME_STOPWORDS: frozenset[str] = frozenset({
    "hospital", "medical", "center", "health", "healthcare", "regional",
    "community", "memorial", "system", "clinic", "inc", "corp", "corporation",
    "company", "limited", "gmbh",
})

DOMAIN_COLUMNS = ["sector", "domain", "start_url", "n_units", "beds_sum", "name_tokens"]


class FrameCheckError(ValueError):
    """A domain table holds a domain that cannot be an organization's own site."""


def check_domain_table(table) -> None:
    """Run by every frame build. No domain may itself be a public or private suffix, every other one must be its own
    registrable domain, written in lower case with no trailing dot (the opt-out purge and the site limits key on it),
    and none may be a third party for its row's sector (`domains.third_party_domains`: shared platforms and parking
    services; the federal portals count only for the hospital sector)."""
    from noroshi.domains import is_suffix, registrable_domain, third_party_domains

    bad = sorted({d for d in table["domain"] if is_suffix(d)})
    not_own = sorted({d for d in table["domain"]  # as optout.site_name writes it: lower case, no trailing dot
                      if not is_suffix(d) and registrable_domain(d.strip().lower().rstrip(".")) != d})
    third = sorted({d for sector, d in zip(table["sector"], table["domain"]) if d in third_party_domains(sector)})
    if bad or not_own or third:
        raise FrameCheckError("frame check failed: " + "; ".join(
            part for part in (bad and f"public or private suffixes: {', '.join(bad)}",
                              not_own and f"not its own registrable domain: {', '.join(not_own)}",
                              third and f"third-party or parking domains: {', '.join(third)}") if part))


def name_tokens(names: list[str]) -> str:
    """Space-separated, sorted, unique lowercase words of 4+ letters, minus stopwords."""
    found: set[str] = set()
    for name in names:
        for word in re.findall(r"[a-z]+", (name or "").lower()):
            if len(word) >= 4 and word not in NAME_STOPWORDS:
                found.add(word)
    return " ".join(sorted(found))
