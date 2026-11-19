"""Exact intervals and exact tests. The pre-specified analysis has no regression model."""
from scipy import stats as sps


def exact_ci(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Clopper-Pearson interval from beta quantiles. An empty sample has no interval (nan, nan)."""
    if n <= 0:
        return (float("nan"), float("nan"))
    lo = 0.0 if k == 0 else float(sps.beta.ppf(alpha / 2, k, n - k + 1))
    hi = 1.0 if k == n else float(sps.beta.ppf(1 - alpha / 2, k + 1, n - k))
    return lo, hi


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p on the discordant cells (a binomial sign test at 0.5)."""
    n = b + c
    if n == 0:
        return 1.0
    return float(min(1.0, 2 * sps.binom.cdf(min(b, c), n, 0.5)))


def fisher_2x2(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher exact p for [[a, b], [c, d]]."""
    return float(sps.fisher_exact([[a, b], [c, d]])[1])


KAPPA_UNDEFINED = "kappa is not defined because both raters gave the same answer every time"
KAPPA_ONE_CONSTANT = "kappa is 0 because one rater gave the same answer every time"
KAPPA_BOTH_CONSTANT = "kappa is 0 because both raters gave the same answer every time"


def kappa_and_note(a: list, b: list) -> tuple[float | None, str]:
    """Cohen's kappa of two raters over the same units, for any number of categories, (observed agreement - expected)
    / (1 - expected), and the note a table prints for a degenerate case, both from one test of those cases: when both
    raters gave one same answer to every unit, expected agreement is 1 and kappa is not defined (None); when one rater,
    or each with a different answer, gave the same answer every time, kappa is the formula's 0. (None, "") for no
    units."""
    if len(a) != len(b):
        raise ValueError(f"unequal lengths: {len(a)} and {len(b)}")
    n = len(a)
    if n == 0:
        return None, ""
    constant = len(set(a)) == 1, len(set(b)) == 1
    if all(constant) and set(a) == set(b):
        return None, KAPPA_UNDEFINED
    if any(constant):  # observed agreement equals expected agreement
        return 0.0, KAPPA_BOTH_CONSTANT if all(constant) else KAPPA_ONE_CONSTANT
    observed = sum(x == y for x, y in zip(a, b)) / n
    expected = sum((list(a).count(c) / n) * (list(b).count(c) / n) for c in set(a) | set(b))
    return (observed - expected) / (1 - expected), ""


def cohen_kappa(a: list, b: list) -> float | None:
    """Cohen's kappa (`kappa_and_note`), without the note."""
    return kappa_and_note(a, b)[0]
