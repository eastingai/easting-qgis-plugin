"""Arc densification for drawing, mirrored from the server's sampler.

The plugin deliberately vendors no verification math (the build fails if
`traverse.py` lands in `_vendor`), but drawing a curve as its bare chord
misstates the boundary by the middle ordinate: 22.7 feet on the
customer-reported 1,635.67-foot Garfield County highway curve. Densification
is rendering, the same category as the affine transform in `place_tool.py`,
so it lives here in plugin source, qgis-free so the parity test in
`groundtruth_core/tests/test_core.py` can hold it against
`traverse.arc_points` point for point. Change one and the test makes you
change the other.
"""

from __future__ import annotations

import math

# Same constants as groundtruth_core.traverse.arc_points; the parity test
# asserts the outputs match exactly.
MAX_SAGITTA_FT = 0.25
MAX_ARC_POINTS = 128


def arc_points(
    start: tuple[float, float],
    end: tuple[float, float],
    radius: float,
    direction: str,
) -> list[tuple[float, float]]:
    """Intermediate points along an arc, endpoints excluded; [] when impossible."""
    x0, y0 = start
    x1, y1 = end
    dx, dy = x1 - x0, y1 - y0
    chord = math.hypot(dx, dy)
    if chord == 0 or radius < chord / 2:
        return []
    offset = math.sqrt(max(radius**2 - (chord / 2) ** 2, 0.0))
    sign = 1.0 if direction == "left" else -1.0  # the traveler's left holds the center
    cx = (x0 + x1) / 2 + sign * offset * (-dy / chord)
    cy = (y0 + y1) / 2 + sign * offset * (dx / chord)
    theta = 2.0 * math.asin(min(1.0, chord / (2.0 * radius)))
    if theta <= 0:
        return []
    ratio = max(0.0, 1.0 - MAX_SAGITTA_FT / radius)
    max_half = math.acos(min(1.0, ratio)) if ratio < 1.0 else 0.0
    n = MAX_ARC_POINTS if max_half <= 0 else math.ceil(theta / (2.0 * max_half))
    n = max(1, min(int(n), MAX_ARC_POINTS))
    if n == 1:
        return []
    a0 = math.atan2(y0 - cy, x0 - cx)
    sweep = theta if direction == "left" else -theta
    return [
        (cx + radius * math.cos(a0 + sweep * k / n), cy + radius * math.sin(a0 + sweep * k / n))
        for k in range(1, n)
    ]
