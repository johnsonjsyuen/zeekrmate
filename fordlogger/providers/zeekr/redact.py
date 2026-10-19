"""Log hygiene for the Zeekr provider.

- VINs are masked to their last 4 characters.
- Library exception messages can embed full API response bodies (for
  example "Failed to get vehicle status: {...}"), so only a short, sanitised
  summary is ever logged.
- zeekr_ev_api logs signed request headers (tokens) and response bodies at
  DEBUG. Its loggers, and the logger we inject into ZeekrClient, are pinned
  to WARNING regardless of the root level, and a redacting filter is added.
  The same filter is attached to the root handlers, where it acts on records
  from zeekr_ev_api.* / fordlogger.zeekr.client loggers, so library loggers
  created later (logger filters are not inherited) are redacted too.
"""

import logging
import re

from ...logsafe import VIN_FILTER, VIN_RE, install_handler_filters

LIBRARY_LOGGER_NAME = "zeekr_ev_api"
CLIENT_LOGGER_NAME = "fordlogger.zeekr.client"
MIN_LIBRARY_LEVEL = logging.WARNING

_MAX_SUMMARY = 120
_TOKENISH = re.compile(
    r"eyJ[\w-]+\.[\w-]+\.[\w-]*"          # JWT
    r"|(?i:bearer)\s+\S+"                 # bearer header value
    r"|[A-Za-z0-9+/=_-]{32,}"             # long opaque key/token-like strings
)
# key=value / key: value pairs whose key names a credential, in any text body
# (e.g. "access_token=Zk7Q...", "Authorization: abc"); the value is dropped.
_SECRET_KV = re.compile(
    r"(?i)((?<![A-Za-z0-9])(?:access|refresh|id|auth|session)?[_-]?(?:token|secret|password|passwd|pwd|"
    r"api[_-]?key|access[_-]?key|secret[_-]?key|signature|sign|authorization|cookie)"
    r"[\"']?\s*[=:]\s*)[\"']?[^\s&,;\"'}]+"
)
_VINISH = VIN_RE


def mask_vin(vin) -> str:
    s = "" if vin is None else str(vin)
    if len(s) <= 4:
        return "***"
    return "***" + s[-4:]


def scrub(text: str, extra_secrets=()) -> str:
    out = str(text)
    for secret in extra_secrets:
        if secret and len(secret) >= 4:
            out = out.replace(secret, "<redacted>")
    out = _SECRET_KV.sub(lambda m: m.group(1) + "<redacted>", out)
    out = _TOKENISH.sub("<redacted>", out)
    out = _VINISH.sub(lambda m: mask_vin(m.group(0)), out)
    return out


def safe_error(exc: BaseException) -> str:
    """A short description of an exception that is safe to log.

    Only the exception type and the text before the first ':' (the library's
    fixed prefix such as "Failed to get vehicle status") are kept; any
    embedded response body is dropped.
    """
    name = type(exc).__name__
    msg = str(exc).split(":", 1)[0].split("{", 1)[0].strip()
    msg = scrub(msg)[:_MAX_SUMMARY]
    return f"{name}: {msg}" if msg else name


_REDACTED = "_fordlogger_library_redacted"


def _is_library_record(record: logging.LogRecord) -> bool:
    name = record.name or ""
    return (name == LIBRARY_LOGGER_NAME or name.startswith(LIBRARY_LOGGER_NAME + ".")
            or name == CLIENT_LOGGER_NAME or name.startswith(CLIENT_LOGGER_NAME + "."))


class _RedactingFilter(logging.Filter):
    """On library loggers: redact every record. On root handlers: redact
    records from library loggers only (others pass through unchanged)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, _REDACTED, False) or not _is_library_record(record):
            return True
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        # Drop anything after a ':' that may be a payload dump, then scrub.
        head = message.split("{", 1)[0]
        record.msg = "zeekr_ev_api: " + scrub(head)[:200]
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        setattr(record, _REDACTED, True)
        return True


_FILTER = _RedactingFilter()


def _harden(logger: logging.Logger) -> None:
    if logger.level < MIN_LIBRARY_LEVEL:  # includes NOTSET (0)
        logger.setLevel(MIN_LIBRARY_LEVEL)
    if _FILTER not in logger.filters:
        logger.addFilter(_FILTER)


def quiet_library_loggers() -> logging.Logger:
    """Pin zeekr_ev_api loggers to WARNING+ and return the logger to inject."""
    _harden(logging.getLogger(LIBRARY_LOGGER_NAME))
    for name in list(logging.root.manager.loggerDict):
        if name == LIBRARY_LOGGER_NAME or name.startswith(LIBRARY_LOGGER_NAME + "."):
            _harden(logging.getLogger(name))
    client_logger = logging.getLogger(CLIENT_LOGGER_NAME)
    _harden(client_logger)
    install_handler_filters(_FILTER, VIN_FILTER)
    return client_logger
