"""VIN masking for every log record (security review #3/#4/#6)."""
import logging

import pytest

from fordlogger.logsafe import install_handler_filters, mask_vins
from fordlogger.providers.zeekr.redact import scrub

VIN = "LZEKR7XAU00000001"


@pytest.mark.parametrize("text", [
    f"trip_{VIN}", f"{VIN}_2025", VIN.lower(), f"Key (vin)=({VIN}) is not present",
    f"/vehicles/{VIN}/status",
])
def test_mask_vins(text):
    out = mask_vins(text)
    assert VIN.lower() not in out.lower() and "***0001" in out


@pytest.mark.parametrize("text", [
    "a" * 64, "0123456789abcdef0123456789abcdef", "characteristicsss",  # no digit
    "Position #12 ***0001 | SoC=78.0%",
])
def test_mask_vins_leaves_other_text(text):
    assert mask_vins(text) == text


def test_fordlogger_logger_masks_message_and_traceback(caplog):
    caplog.set_level(logging.INFO)
    try:
        raise RuntimeError(f"DETAIL: Key (vin)=({VIN}) is not present")
    except RuntimeError:
        logging.getLogger("fordlogger").error("failed for %s", VIN, exc_info=True)
    assert VIN not in caplog.text and "***0001" in caplog.text
    assert "RuntimeError" in caplog.text  # traceback kept, only masked


def test_root_handler_filter_masks_other_loggers(caplog):
    caplog.set_level(logging.INFO)
    install_handler_filters()
    logging.getLogger("psycopg2.some_late_logger").warning("row %s", VIN.lower())
    assert VIN.lower() not in caplog.text and "***0001" in caplog.text


@pytest.mark.parametrize("text,secret", [
    ("access_token=Zk7Qp2Lm9Xc4Rt8V", "Zk7Qp2Lm9Xc4Rt8V"),
    ('{"refreshToken": "abc123xyz"}', "abc123xyz"),
    ("Authorization: tok-123 next", "tok-123"),
    ("password=hunter22&x=1", "hunter22"),
])
def test_scrub_drops_short_credentials_in_text(text, secret):
    assert secret not in scrub(text)


def test_scrub_keeps_ordinary_words():
    assert scrub("design: ok; signal: 5") == "design: ok; signal: 5"
