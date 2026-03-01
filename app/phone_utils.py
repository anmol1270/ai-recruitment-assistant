"""
Phone-number validation, normalisation (E.164), and formatting utilities.
Supports international numbers with a default region of GB for local-format numbers.
Uses the `phonenumbers` library.
"""

from __future__ import annotations

import phonenumbers
from phonenumbers import PhoneNumberFormat, NumberParseException

# Default region for numbers without a country code
_DEFAULT_REGION = "GB"


def normalise_phone(raw: str) -> tuple[str, bool]:
    """
    Attempt to normalise a raw phone string to E.164.
    Accepts international numbers. Numbers without a + prefix are
    assumed to be GB by default.

    Returns
    -------
    (e164_string, is_valid)
        e164_string is the formatted number or the original raw string on failure.
        is_valid indicates whether parsing succeeded and the number looks valid.
    """
    cleaned = raw.strip()
    if not cleaned:
        return (raw, False)

    # Handle Excel scientific notation (e.g. "9.71585E+11" → "971585000000")
    try:
        if "e" in cleaned.lower() and "+" in cleaned:
            cleaned = str(int(float(cleaned)))
    except (ValueError, OverflowError):
        pass

    # If it's all digits, no + prefix, and doesn't start with 0 (local format),
    # try prepending + for international format
    if cleaned.isdigit() and len(cleaned) > 10 and not cleaned.startswith("0"):
        cleaned = "+" + cleaned

    try:
        parsed = phonenumbers.parse(cleaned, _DEFAULT_REGION)
    except NumberParseException:
        return (raw, False)

    if not phonenumbers.is_possible_number(parsed):
        return (raw, False)

    if not phonenumbers.is_valid_number(parsed):
        return (raw, False)

    e164 = phonenumbers.format_number(parsed, PhoneNumberFormat.E164)
    return (e164, True)


# Keep the old name as an alias for backward compatibility
normalise_uk_phone = normalise_phone


def is_uk_mobile(e164: str) -> bool:
    """Return True if the E.164 number is a UK mobile (07xxx)."""
    try:
        parsed = phonenumbers.parse(e164, _DEFAULT_REGION)
        ntype = phonenumbers.number_type(parsed)
        # type 1 = MOBILE, type 99 = UNKNOWN (test numbers)
        return ntype in (
            phonenumbers.PhoneNumberType.MOBILE,
            phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE,
            phonenumbers.PhoneNumberType.UNKNOWN,
        )
    except NumberParseException:
        return False


def format_for_display(e164: str) -> str:
    """Format an E.164 number into a human-readable UK national format."""
    try:
        parsed = phonenumbers.parse(e164, _DEFAULT_REGION)
        return phonenumbers.format_number(parsed, PhoneNumberFormat.NATIONAL)
    except NumberParseException:
        return e164
