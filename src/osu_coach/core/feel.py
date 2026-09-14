"""Tuned "¿Cómo lo sentiste?" difficulty offsets applied on top of measured stars.

A feel never rewrites the measured star rating; it adjusts the effective
difficulty that feeds session, profile and recommendation calculations.
"""
from __future__ import annotations

import math

FEEL_MIN = 0.05
FEEL_MAX = 10.5
# Ordered from easier to harder; the word "justo" keeps its real accent.
FEEL_LABELS = ("mucho más fácil", "más fácil", "justo", "más difícil", "mucho más difícil")
_MULTIPLIERS = (-2.0, -1.0, 0.0, 1.0, 2.0)


def normalize_step(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0.25
    return max(0.05, min(1.0, float(value)))


def offsets_for_step(step):
    """Return label -> star offset for the configured feel step."""
    step = normalize_step(step)
    return {label: multiplier * step for label, multiplier in zip(FEEL_LABELS, _MULTIPLIERS)}


def offset_for_label(label, step):
    return offsets_for_step(step).get(label, 0.0)


def label_for_offset(offset, step):
    """Return the configured label for a stored offset, or None if it was customized."""
    if isinstance(offset, bool) or not isinstance(offset, (int, float)):
        return None
    allowed = offsets_for_step(step)
    return next((label for label, value in allowed.items() if abs(value - offset) < 1e-9), None)


def apply_feel(stars, offset):
    """Clamp measured stars plus a feel offset to the valid difficulty range."""
    stars = float(stars) if isinstance(stars, (int, float)) and math.isfinite(stars) else 0.0
    offset = float(offset) if isinstance(offset, (int, float)) and math.isfinite(offset) else 0.0
    return max(FEEL_MIN, min(FEEL_MAX, stars + offset))