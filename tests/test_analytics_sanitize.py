"""
Tests for the shared analytics sanitization helpers (#315).

These guard the boundary between numpy/pandas statistical computation
(which can legitimately produce NaN/±Infinity) and Pydantic analytics
models (which reject non-finite Decimal/float values with a
`finite_number` validation error).
"""

from decimal import Decimal

import numpy as np
import pytest

from data_manager.analytics.sanitize import is_non_finite, safe_decimal


class TestIsNonFinite:
    """Tests for is_non_finite()."""

    def test_none_is_finite(self):
        assert is_non_finite(None) is False

    def test_finite_decimal_is_finite(self):
        assert is_non_finite(Decimal("1.5")) is False

    def test_decimal_nan_is_non_finite(self):
        assert is_non_finite(Decimal("NaN")) is True

    def test_decimal_infinity_is_non_finite(self):
        assert is_non_finite(Decimal("Infinity")) is True
        assert is_non_finite(Decimal("-Infinity")) is True

    def test_float_nan_is_non_finite(self):
        assert is_non_finite(float("nan")) is True

    def test_float_inf_is_non_finite(self):
        assert is_non_finite(float("inf")) is True
        assert is_non_finite(float("-inf")) is True

    def test_numpy_nan_is_non_finite(self):
        assert is_non_finite(np.nan) is True

    def test_finite_float_is_finite(self):
        assert is_non_finite(0.0) is False
        assert is_non_finite(-42.7) is False

    def test_finite_int_is_finite(self):
        assert is_non_finite(0) is False
        assert is_non_finite(100) is False

    def test_non_numeric_string_is_non_finite(self):
        assert is_non_finite("not-a-number") is True

    def test_numeric_string_is_finite(self):
        assert is_non_finite("3.14") is False


class TestSafeDecimal:
    """Tests for safe_decimal()."""

    def test_none_returns_default(self):
        assert safe_decimal(None) == Decimal("0")

    def test_none_returns_custom_default(self):
        assert safe_decimal(None, default=Decimal("7")) == Decimal("7")

    def test_finite_decimal_passthrough(self):
        assert safe_decimal(Decimal("1.23456")) == Decimal("1.23456")

    def test_finite_float_converted(self):
        assert safe_decimal(0.5) == Decimal("0.5")

    def test_nan_decimal_substituted(self):
        result = safe_decimal(Decimal("NaN"), default=Decimal("0"))
        assert result == Decimal("0")
        assert result is not None
        assert not result.is_nan()

    def test_nan_float_substituted(self):
        result = safe_decimal(float("nan"))
        assert result == Decimal("0")

    def test_numpy_nan_substituted(self):
        result = safe_decimal(np.nan)
        assert result == Decimal("0")

    def test_infinite_substituted(self):
        assert safe_decimal(float("inf")) == Decimal("0")
        assert safe_decimal(float("-inf")) == Decimal("0")

    def test_none_default_allows_optional_field(self):
        """Non-finite input with default=None returns None (Optional[Decimal] fields)."""
        assert safe_decimal(float("nan"), default=None) is None

    def test_never_returns_non_finite_decimal(self):
        """No matter the input, the returned Decimal (if any) must be finite."""
        for raw in [
            Decimal("NaN"),
            Decimal("Infinity"),
            Decimal("-Infinity"),
            float("nan"),
            float("inf"),
            np.nan,
            "garbage",
            object(),
        ]:
            result = safe_decimal(raw)
            assert result is not None
            assert not is_non_finite(result)

    def test_zero_variance_correlation_scenario(self):
        """Regression for #315: 0/0 Pearson correlation of a constant series."""
        constant_series_std = 0.0
        pearson_result = (
            0.0 / constant_series_std if constant_series_std else float("nan")
        )
        assert np.isnan(pearson_result)
        result = safe_decimal(pearson_result, context="constant-series correlation")
        assert result == Decimal("0")

    def test_non_numeric_returns_default(self):
        assert safe_decimal(object(), default=Decimal("9")) == Decimal("9")

    @pytest.mark.parametrize("value", [Decimal("0"), 0, 0.0])
    def test_legitimate_zero_is_preserved(self, value):
        """A real zero value must not be treated as non-finite."""
        assert safe_decimal(value) == Decimal("0")
        assert is_non_finite(value) is False
