"""Sampling and smoothing helpers."""

from __future__ import annotations

import time


def sample_median(
    read_fn: callable,
    count: int,
    interval_seconds: float,
) -> float:
    """Call read_fn count times with sleeps between, return median."""
    if count <= 0:
        raise ValueError("count must be positive")

    samples = []
    for i in range(count):
        if i > 0:
            time.sleep(interval_seconds)
        samples.append(read_fn())

    return _median(samples)


def _median(values: list[float]) -> float:
    """Return the median of a list of numbers."""
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2
