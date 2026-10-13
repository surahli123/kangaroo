"""Statistics the experiment tools share. Standard library only.

Every function works on summary numbers a scorecard already reports (means,
standard errors, intervals, p-values), never on raw rows.
"""

import math
from statistics import NormalDist

NORMAL = NormalDist()


def z_for(level):
    """Two-sided critical value, e.g. 1.96 for level 0.95."""
    if not 0.5 < level < 1:
        raise ValueError(f"confidence level {level} must be a fraction between 0.5 and 1 (0.95, not 95)")
    return NORMAL.inv_cdf(0.5 + level / 2)


def se_from_ci(low, high, level=0.95):
    """Standard error implied by a symmetric interval."""
    return (high - low) / (2 * z_for(level))


def relative_lift(control_mean, control_se, treatment_mean, treatment_se, level=0.95):
    """Relative lift t/c - 1 with a delta-method interval.

    var(t/c) ~= (t/c)^2 * (se_t^2 / t^2 + se_c^2 / c^2), treating the arms as
    independent. Returns (lift, low, high, se).
    """
    if control_mean == 0:
        raise ValueError("control mean is 0, so a relative lift is undefined")
    ratio = treatment_mean / control_mean
    rel_var = (control_se / control_mean) ** 2
    if treatment_mean != 0:
        rel_var += (treatment_se / treatment_mean) ** 2
    se = abs(ratio) * math.sqrt(rel_var)
    z = z_for(level)
    return ratio - 1, ratio - 1 - z * se, ratio - 1 + z * se, se


def two_sided_p(estimate, se):
    if se <= 0:
        return 0.0 if estimate else 1.0
    return 2 * (1 - NORMAL.cdf(abs(estimate) / se))


def achieved_mde(se, alpha=0.05, power=0.8):
    """Smallest true effect the test had `power` to detect: (z_{1-a/2} + z_power) * se."""
    return (NORMAL.inv_cdf(1 - alpha / 2) + NORMAL.inv_cdf(power)) * se


def benjamini_hochberg(p_values, q=0.05):
    """Indices of hypotheses rejected at false-discovery rate q."""
    order = sorted(range(len(p_values)), key=lambda i: p_values[i])
    m, cutoff = len(p_values), -1
    for rank, i in enumerate(order, 1):
        if p_values[i] <= q * rank / m:
            cutoff = rank
    return sorted(order[:cutoff]) if cutoff > 0 else []


def weighted_slope(xs, ys, ses):
    """Inverse-variance weighted least-squares slope of ys on xs, with its SE."""
    weights = [1 / s ** 2 for s in ses]
    total = sum(weights)
    x_bar = sum(w * x for w, x in zip(weights, xs)) / total
    y_bar = sum(w * y for w, y in zip(weights, ys)) / total
    sxx = sum(w * (x - x_bar) ** 2 for w, x in zip(weights, xs))
    if sxx == 0:
        raise ValueError("need at least two distinct days")
    slope = sum(w * (x - x_bar) * (y - y_bar) for w, x, y in zip(weights, xs, ys)) / sxx
    return slope, math.sqrt(1 / sxx)


def pooled(estimates, ses):
    """Inverse-variance pooled estimate and SE."""
    weights = [1 / s ** 2 for s in ses]
    total = sum(weights)
    return sum(w * e for w, e in zip(weights, estimates)) / total, math.sqrt(1 / total)
