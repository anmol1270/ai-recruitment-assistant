"""Tests for UK phone number validation and normalisation."""

import pytest
from app.phone_utils import normalise_uk_phone, is_uk_mobile, format_for_display


class TestNormaliseUKPhone:
    """Test phone number normalisation to E.164."""

    def test_standard_mobile(self):
        e164, valid = normalise_uk_phone("07700900001")
        assert valid is True
        assert e164 == "+447700900001"

    def test_with_country_code(self):
        e164, valid = normalise_uk_phone("+447700900002")
        assert valid is True
        assert e164 == "+447700900002"

    def test_with_spaces(self):
        e164, valid = normalise_uk_phone("07700 900 003")
        assert valid is True
        assert e164 == "+447700900003"

    def test_with_dashes(self):
        e164, valid = normalise_uk_phone("07700-900-004")
        assert valid is True
        assert e164 == "+447700900004"

    def test_with_brackets(self):
        e164, valid = normalise_uk_phone("(07700) 900005")
        assert valid is True
        assert e164 == "+447700900005"

    def test_with_double_zero_prefix(self):
        e164, valid = normalise_uk_phone("00447700900006")
        assert valid is True
        assert e164 == "+447700900006"

    def test_invalid_short_number(self):
        _, valid = normalise_uk_phone("123")
        assert valid is False

    def test_invalid_letters(self):
        _, valid = normalise_uk_phone("not-a-phone")
        assert valid is False

    def test_empty_string(self):
        _, valid = normalise_uk_phone("")
        assert valid is False

    def test_whitespace_only(self):
        _, valid = normalise_uk_phone("   ")
        assert valid is False

    def test_landline(self):
        e164, valid = normalise_uk_phone("02012345678")
        assert valid is True
        assert e164.startswith("+44")


class TestIsMobile:
    def test_mobile_number(self):
        assert is_uk_mobile("+447700900001") is True

    def test_landline_number(self):
        assert is_uk_mobile("+442012345678") is False


class TestFormatForDisplay:
    def test_format_mobile(self):
        display = format_for_display("+447700900001")
        assert "07700" in display

    def test_format_invalid(self):
        display = format_for_display("invalid")
        assert display == "invalid"
