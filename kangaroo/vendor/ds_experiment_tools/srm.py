"""Sample ratio mismatch: are assignment counts consistent with the planned split?

Chi-square goodness-of-fit with k - 1 degrees of freedom. Fails when the
p-value is below `alpha` (default 0.001, the usual SRM bar). A failed SRM means
the arms are not comparable, so any lift computed from them is untrustworthy.
"""

import math

from _common import COULD_NOT_RUN, FAIL, PASS, Result, SpecError, require

NAME = "srm"
DEFAULT_ALPHA = 0.001


def _gamma_q(a, x):
    """Regularized upper incomplete gamma Q(a, x) (Numerical Recipes gammq)."""
    if x <= 0:
        return 1.0
    log_prefix = -x + a * math.log(x) - math.lgamma(a)
    if x < a + 1:
        term = total = 1.0 / a
        ap = a
        for _ in range(10_000):
            ap += 1
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return max(0.0, 1.0 - total * math.exp(log_prefix))
    b = x + 1 - a
    c = 1 / 1e-300
    d = 1 / b
    h = d
    for i in range(1, 10_000):
        an = -i * (i - a)
        b += 2
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-15:
            break
    return math.exp(log_prefix) * h


def chi_square_p(observed, ratios):
    total = sum(observed)
    stat = sum((o - total * r) ** 2 / (total * r) for o, r in zip(observed, ratios))
    return stat, _gamma_q((len(observed) - 1) / 2, stat / 2)


def run(spec):
    try:
        observed = require(spec, "observed", list)
        ratios = require(spec, "expected_ratio", list)
        if len(observed) < 2 or len(observed) != len(ratios):
            raise SpecError("'observed' and 'expected_ratio' need the same length, at least 2")
        for count in observed:
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise SpecError("'observed' must be non-negative integer counts")
        ratios = [float(r) for r in ratios]
        if any(r <= 0 for r in ratios) or abs(sum(ratios) - 1) > 1e-6:
            raise SpecError("'expected_ratio' must be positive and sum to 1")
        if sum(observed) == 0:
            raise SpecError("no units observed")
        alpha = float(spec.get("alpha", DEFAULT_ALPHA))
    except (SpecError, TypeError, ValueError) as err:
        return Result(NAME, COULD_NOT_RUN, f"could not run: {err}")

    stat, p_value = chi_square_p(observed, ratios)
    total = sum(observed)
    shares = [round(o / total, 6) for o in observed]
    details = {"observed": observed, "observed_share": shares, "expected_ratio": ratios,
               "chi2": round(stat, 4), "p_value": p_value, "alpha": alpha}
    summary = f"observed shares {shares} vs expected {ratios}: chi2 {stat:.2f}, p {p_value:.3g}"
    if p_value < alpha:
        return Result(NAME, FAIL, "sample ratio mismatch: " + summary
                      + "; arms are not comparable until the cause is found", details)
    return Result(NAME, PASS, summary, details)
