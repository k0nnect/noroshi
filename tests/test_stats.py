import numpy as np
from scipy import stats as sps

from noroshi.analysis.stats import exact_ci, fisher_2x2, mcnemar_exact


def test_exact_ci_closed_forms():
    lo, hi = exact_ci(0, 10)
    assert lo == 0.0 and abs(hi - (1 - 0.025 ** 0.1)) < 1e-9
    lo, hi = exact_ci(10, 10)
    assert hi == 1.0 and abs(lo - 0.025 ** 0.1) < 1e-9


def test_exact_ci_matches_scipy():
    ci = sps.binomtest(7, 31).proportion_ci(0.95, method="exact")
    lo, hi = exact_ci(7, 31)
    assert abs(lo - ci.low) < 1e-9 and abs(hi - ci.high) < 1e-9


def test_mcnemar_exact_known():
    assert abs(mcnemar_exact(0, 5) - 0.0625) < 1e-12 and mcnemar_exact(3, 3) == 1.0
    assert mcnemar_exact(0, 0) == 1.0


def test_fisher_2x2():
    assert abs(fisher_2x2(3, 1, 1, 3) - 0.4857142857) < 1e-9


def test_no_regression_model_is_left():
    """B7: the pre-specified analysis has no regression model; Table 3 is always descriptive."""
    from noroshi import config
    from noroshi.analysis import stats
    assert not hasattr(stats, "firth_logit") and not hasattr(stats, "hospital_analysis_mode")
    assert not hasattr(config, "HOSPITAL_MODEL_MIN_POSITIVES")


def test_cohen_kappa_for_two_or_more_categories():
    """Post-pilot Y5 (T1): one definition, used for the judges' three questions and the identity raters."""
    from noroshi.analysis.stats import cohen_kappa

    assert cohen_kappa([True, True, False, False], [True, True, False, False]) == 1.0
    assert abs(cohen_kappa([True, True, False, False], [True, False, True, False])) < 1e-9
    assert abs(cohen_kappa([True, True, False, False], [True, True, False, True]) - 0.5) < 1e-9
    a, b = ["own", "own", "other", "unclear"], ["own", "other", "other", "unclear"]
    assert abs(cohen_kappa(a, b) - (0.75 - 0.3125) / (1 - 0.3125)) < 1e-9
    assert cohen_kappa([], []) is None
    import pytest
    with pytest.raises(ValueError, match="unequal"):
        cohen_kappa([True], [True, False])


def test_kappa_and_its_note_for_the_degenerate_cases_come_from_one_test():
    """Fix round 1, G2: both raters giving one same answer to every unit leaves kappa undefined (0/0); one rater
    constant gives the formula's 0; either way the note says why."""
    from noroshi.analysis.stats import cohen_kappa, kappa_and_note

    assert kappa_and_note([True] * 10, [True] * 10) == (None, "kappa is not defined because both raters gave the same "
                                                              "answer every time")
    assert cohen_kappa([True] * 10, [True] * 10) is None
    assert kappa_and_note(["own"] * 3, ["other"] * 3) == (0.0, "kappa is 0 because both raters gave the same "
                                                                "answer every time")  # fix round 2, H9
    assert kappa_and_note([True] * 4, [True, True, False, True]) == (0.0, "kappa is 0 because one rater gave the "
                                                                          "same answer every time")
    assert kappa_and_note([True, False], [True, False]) == (1.0, "")
    assert kappa_and_note([], []) == (None, "")
