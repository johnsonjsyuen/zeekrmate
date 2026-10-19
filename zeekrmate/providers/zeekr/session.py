"""Authenticated, read-only access to zeekr_ev_api.

ZeekrSession owns one ZeekrClient. It:
- restores a persisted session (load_session) or logs in once;
- on AuthException performs ONE fresh login, persists it and retries the
  call once;
- exposes only an allow-list of read-only client methods (ReadOnlyClient),
  so vehicle commands (do_remote_control, set_charge_plan, set_travel_plan)
  can never be reached through this code path;
- converts every library exception into ZeekrProviderError whose message is
  a sanitised summary (library messages may embed response bodies).
"""

import logging
import types
from types import SimpleNamespace
from typing import Any, Callable, Optional

from .redact import mask_vin, quiet_library_loggers, safe_error
from .session_store import SessionStore
from .settings import ZeekrSettings

log = logging.getLogger("zeekrmate")

READ_ONLY_METHODS = frozenset({
    "login",
    "load_session",
    "export_session",
    "get_vehicle_list",
    "get_vehicle_status",
    "get_vehicle_charging_status",
    "get_vehicle_charging_limit",
    "get_journey_log",
    "get_trip_trackpoints",
})

# Default timeout/retries for the library client: (connect, read) seconds.
DEFAULT_TIMEOUT = (10.0, 30.0)
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF = 1.0


class ZeekrProviderError(RuntimeError):
    """Sanitised error from the Zeekr API. Safe to log."""


class ZeekrAuthError(ZeekrProviderError):
    """Authentication failed even after one fresh login."""


class ReadOnlyViolation(AttributeError):
    pass


class ReadOnlyClient:
    """Proxy exposing only READ_ONLY_METHODS of a ZeekrClient.

    The raw client is held only in a closure, never as an attribute, and the
    methods handed out are bound to the proxy (not to the raw client), so
    neither ``proxy._client`` nor ``proxy.login.__self__`` reaches it. This
    stops accidental access; Python cannot stop deliberate introspection.
    """

    __slots__ = ("__call",)

    def __init__(self, client):
        def call(name, *args, **kwargs):
            if name not in READ_ONLY_METHODS:
                raise ReadOnlyViolation(f"Zeekr client method {name!r} is not permitted (read-only)")
            return getattr(client, name)(*args, **kwargs)

        object.__setattr__(self, "_ReadOnlyClient__call", call)

    def __getattr__(self, name: str):
        if name not in READ_ONLY_METHODS:
            raise ReadOnlyViolation(f"Zeekr client method {name!r} is not permitted (read-only)")
        call = object.__getattribute__(self, "_ReadOnlyClient__call")

        def method(_proxy, *args, **kwargs):
            return call(name, *args, **kwargs)

        method.__name__ = method.__qualname__ = name
        return types.MethodType(method, self)

    def __setattr__(self, name, value):
        raise ReadOnlyViolation("ReadOnlyClient is immutable")


def _detach(method: str, result):
    """zeekr_ev_api Vehicle objects hold the raw client (Vehicle._client, with
    do_remote_control/set_charge_plan/...). Hand out plain vin/data records so
    no caller can bypass ReadOnlyClient through them."""
    if method == "get_vehicle_list" and isinstance(result, (list, tuple)):
        return [SimpleNamespace(vin=getattr(v, "vin", None), data=getattr(v, "data", None))
                for v in result]
    return result


def _library_exceptions():
    from zeekr_ev_api.exceptions import AuthException, ZeekrException
    return AuthException, ZeekrException


def default_client_factory(settings: ZeekrSettings, logger: logging.Logger):
    from zeekr_ev_api import ZeekrClient  # lazy: keeps provider imports light

    quiet_library_loggers()  # again, now that library module loggers exist
    return ZeekrClient(
        logger=logger,
        timeout=DEFAULT_TIMEOUT,
        max_retries=DEFAULT_MAX_RETRIES,
        retry_backoff=DEFAULT_RETRY_BACKOFF,
        **settings.client_kwargs(),
    )


ClientFactory = Callable[[ZeekrSettings, logging.Logger], Any]


class ZeekrSession:
    def __init__(
        self,
        settings: ZeekrSettings,
        store: SessionStore,
        client_factory: Optional[ClientFactory] = None,
        auth_exceptions: Optional[tuple] = None,
        api_exceptions: Optional[tuple] = None,
    ):
        self.settings = settings
        self.store = store
        self._factory = client_factory or default_client_factory
        self._logger = quiet_library_loggers()
        if auth_exceptions is None or api_exceptions is None:
            auth_exc, api_exc = _library_exceptions()
            auth_exceptions = auth_exceptions or (auth_exc,)
            api_exceptions = api_exceptions or (api_exc,)
        self._auth_exc = tuple(auth_exceptions)
        self._api_exc = tuple(api_exceptions)
        self._client: Optional[ReadOnlyClient] = None
        self._last_saved: Optional[dict] = None

    # -- lifecycle -------------------------------------------------------
    def _new_client(self) -> ReadOnlyClient:
        try:
            raw = self._factory(self.settings, self._logger)
        except Exception as e:
            raise ZeekrProviderError(f"Could not create Zeekr client ({safe_error(e)})") from None
        return ReadOnlyClient(raw)

    def _fresh_login(self) -> ReadOnlyClient:
        client = self._new_client()
        try:
            client.login()
        except self._auth_exc as e:
            raise ZeekrAuthError(
                f"Zeekr login failed ({safe_error(e)}). Check ZEEKR_USERNAME, "
                "ZEEKR_PASSWORD, ZEEKR_COUNTRY_CODE and the key variables."
            ) from None
        except Exception as e:
            raise ZeekrProviderError(f"Zeekr login failed ({safe_error(e)})") from None
        log.info("Zeekr: logged in with a fresh session")
        self._client = client
        self.persist()
        return client

    def connect(self) -> ReadOnlyClient:
        """Return a ready client: reuse the stored session, else log in."""
        if self._client is not None:
            return self._client
        stored = self.store.load()
        if stored:
            client = self._new_client()
            try:
                client.load_session(stored)
            except Exception as e:
                log.warning("Zeekr: stored session rejected (%s); logging in", safe_error(e))
                return self._fresh_login()
            log.info("Zeekr: reusing stored session")
            self._client = client
            self._last_saved = {k: v for k, v in stored.items()}
            return client
        return self._fresh_login()

    def persist(self) -> None:
        if self._client is None:
            return
        try:
            data = self._client.export_session()
        except Exception as e:
            log.warning("Zeekr: could not export session (%s)", safe_error(e))
            return
        if not isinstance(data, dict) or not data:
            return
        data = {k: v for k, v in data.items() if k != "password"}
        if data == self._last_saved:
            return
        try:
            self.store.save(data)
            self._last_saved = data
        except OSError as e:
            log.warning("Zeekr: could not write session file (%s)", type(e).__name__)

    # -- calls -----------------------------------------------------------
    def call(self, method: str, *args, **kwargs):
        """Call a read-only client method with one auth retry.

        The first positional argument, when present, is the VIN (masked in
        messages). Raises ZeekrAuthError / ZeekrProviderError with sanitised
        messages.
        """
        if method not in READ_ONLY_METHODS:
            raise ReadOnlyViolation(f"Zeekr client method {method!r} is not permitted (read-only)")
        vin = args[0] if args else kwargs.get("vin")
        where = f"{method}" + (f" [{mask_vin(vin)}]" if vin else "")
        client = self.connect()
        try:
            return _detach(method, getattr(client, method)(*args, **kwargs))
        except self._auth_exc as e:
            log.warning("Zeekr: auth error on %s (%s); logging in again once", where, safe_error(e))
        except self._api_exc as e:
            raise ZeekrProviderError(f"{where} failed ({safe_error(e)})") from None
        except Exception as e:
            raise ZeekrProviderError(f"{where} failed ({safe_error(e)})") from None

        client = self._fresh_login()
        try:
            return _detach(method, getattr(client, method)(*args, **kwargs))
        except self._auth_exc as e:
            raise ZeekrAuthError(f"{where} still unauthorised after re-login ({safe_error(e)})") from None
        except Exception as e:
            raise ZeekrProviderError(f"{where} failed after re-login ({safe_error(e)})") from None
