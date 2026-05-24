"""Tests for smoothing utility."""

import time
from curtailment.util.smoothing import sample_median


class TestSampleMedian:
    def test_basic_median(self):
        """Median of [1, 2, 3, 4, 5] is 3."""
        calls = []
        result = sample_median(
            lambda: (calls.append(1) or calls[-1] * 0 + len(calls)),
            count=5,
            interval_seconds=0,  # no sleep for tests
        )
        assert result == 3

    def test_with_outlier(self):
        """Median filters out outliers."""
        values = [100, 200, 150, 10000, 180]  # outlier: 10000
        idx = [0]

        def read_fn():
            val = values[idx[0]]
            idx[0] += 1
            return val

        result = sample_median(read_fn, count=5, interval_seconds=0)
        assert result == 180  # median of [100, 150, 180, 200, 10000]

    def test_even_count(self):
        """Median of even count averages two middle values."""
        idx = [0]
        values = [10, 20, 30, 40]

        def read_fn():
            val = values[idx[0]]
            idx[0] += 1
            return val

        result = sample_median(read_fn, count=4, interval_seconds=0)
        assert result == 25.0  # (20 + 30) / 2

    def test_single_sample(self):
        result = sample_median(lambda: 42.0, count=1, interval_seconds=0)
        assert result == 42.0

    def test_zero_count_raises(self):
        try:
            sample_median(lambda: 1.0, count=0, interval_seconds=1)
            assert False, "Should have raised"
        except ValueError:
            pass
