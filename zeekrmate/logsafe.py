"""Log hygiene shared by the whole logger: VINs never reach the log in full.

A VIN-like token is 17 characters from the VIN alphabet (no I, O, Q), with
at least one digit, in either case, not part of a longer alphanumeric run
(so ``trip_<VIN>``, ``<VIN>_2025`` and lower-case VINs are caught, while
64-character hashes are not). It is replaced by ``***`` plus its last four
characters, the same form as ``config.mask_vin``.

``VIN_FILTER`` rewrites a record's message, exception text and stack text.
It is attached to the ``zeekrmate`` logger when the package is imported
(records logged there, e.g. a psycopg2 error whose DETAIL line holds a VIN,
are masked before any handler sees them), and to the root handlers by
``install_handler_filters()`` so records from any other logger are masked
too.
"""
import logging
import re

VIN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-HJ-NPR-Z0-9]{0,16}[0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Za-z0-9])",
    re.IGNORECASE,
)

_MASKED = "_fordlogger_vin_masked"


def mask_vins(text) -> str:
    """Mask every VIN-like token in text (``***`` + last 4 characters)."""
    return VIN_RE.sub(lambda m: "***" + m.group(0)[-4:], str(text))


class VinMaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, _MASKED, False):
            return True
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = mask_vins(message)
        record.args = ()
        if record.exc_info:
            record.exc_text = mask_vins(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = mask_vins(record.exc_text)
        if record.stack_info:
            record.stack_info = mask_vins(record.stack_info)
        setattr(record, _MASKED, True)
        return True


VIN_FILTER = VinMaskingFilter()


def install_logger_filter() -> None:
    logger = logging.getLogger("zeekrmate")
    if VIN_FILTER not in logger.filters:
        logger.addFilter(VIN_FILTER)


def install_handler_filters(*filters: logging.Filter) -> None:
    """Attach the given filters (default: VIN masking) to every root handler.

    Handler filters see records from all loggers, including loggers that are
    created later (logger filters are not inherited by child loggers).
    """
    for handler in logging.getLogger().handlers:
        for f in filters or (VIN_FILTER,):
            if f not in handler.filters:
                handler.addFilter(f)
