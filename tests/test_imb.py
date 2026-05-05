"""Tests for the IMb encoder. The reference value comes from the
USPS-Tech-Guide-SPUSPSG examples bundled in the original Python implementation.
"""

from __future__ import annotations

import pytest

from mailtrace import imb

# Spec example 4 from SPUSPSG (also documented in the upstream Python port).
SPEC_EXAMPLE_BARS = "AADTFFDFTDADTAADAATFDTDDAAADDTDTTDAFADADDDTFFFDDTTTADFAAADFTDAADA"


def test_encode_spec_example_4() -> None:
    assert imb.encode(1, 234, 567094, 987654321, "01234567891") == SPEC_EXAMPLE_BARS


def test_encode_returns_65_chars() -> None:
    bars = imb.encode(0, 700, 314159, 1, "95008200130")
    assert len(bars) == 65
    assert set(bars) <= {"A", "D", "F", "T"}


@pytest.mark.parametrize("zip_code", ["", "12345", "123456789", "12345678901"])
def test_encode_accepts_valid_zip_lengths(zip_code: str) -> None:
    assert len(imb.encode(0, 40, 314159, 1, zip_code)) == 65


@pytest.mark.parametrize("bad_zip", ["1234", "1234567", "1234567890123"])
def test_encode_rejects_invalid_zip(bad_zip: str) -> None:
    with pytest.raises(ValueError):
        imb.encode(0, 40, 314159, 1, bad_zip)


def test_human_readable_format() -> None:
    assert imb.human_readable(0, 40, 314159, 5, "12345") == "00-040-314159-000005-12345"


def test_to_raw_imb_concatenates_digits() -> None:
    assert imb.to_raw_imb(0, 40, 314159, 5, "12345") == "0004031415900000512345"


def test_mailer_id_starting_with_9_uses_9_digit_layout() -> None:
    bars = imb.encode(0, 40, 900000001, 5, "12345")
    assert len(bars) == 65
