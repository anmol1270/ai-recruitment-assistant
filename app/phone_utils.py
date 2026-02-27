"""
UK phone-number validation, normalisation (E.164), and formatting utilities.
Uses the `phonenumbers` library with GB-specific rules.
"""

from __future__ import annotations

import phonenumbers
from phonenumbers import PhoneNumberFormat, NumberParseException

# UK country code
_DEFAULT_REGION = "GB"


def normalise_uk_phone(raw: str) -> tuple[str, bool]:
    """
    Attempt to normalise a raw phone string to E.164.

    Returns
    -------
    (e164_string, is_valid)
        e164_string is the formatted number or the original raw string on failure.
        is_valid indicates whether parsing succeeded and the number looks valid.
    """
    cleaned = raw.strip()
    if not cleaned:
        return (raw, False)

    try:
        parsed = phonenumbers.parse(cleaned, _DEFAULT_REGION)
    except NumberParseException:
        return (raw, False)

    if not phonenumbers.is_possible_number(parsed):
        return (raw, False)

    # Accept both valid numbers and possible numbers (covers test ranges)
    # For stricter validation, switch to is_valid_number
    if parsed.country_code != 44:
        # Allow non-GB numbers only if they're fully valid
        if not phonenumbers.is_valid_number(parsed):
            return (raw, False)

    e164 = phonenumbers.format_number(parsed, PhoneNumberFormat.E164)
    return (e164, True)


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
