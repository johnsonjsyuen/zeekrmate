import sys
from unittest.mock import Mock
import signal

import pytest

import fordlogger.main as main_module
from fordlogger.poller import Poller, PollResult



def test_empty_provider_factory_after_db_setup_preserves_exit_one(monkeypatch):
    cfg = {"client_id": "test-client"}
    conn = object()
    events = []
    monkeypatch.setattr(sys, "argv", ["fordlogger"])
    monkeypatch.setattr(main_module, "load_config", Mock(return_value=cfg))
    monkeypatch.setattr(
        main_module.db,
        "connect",
        Mock(side_effect=lambda actual_cfg: events.append("connect") or conn),
    )
    monkeypatch.setattr(
        main_module.db,
        "ensure_schema",
        Mock(side_effect=lambda actual_conn: events.append("ensure_schema")),
    )
    monkeypatch.setattr(
        main_module,
        "create_providers",
        Mock(side_effect=lambda actual_cfg: events.append("create_providers") or []),
    )

    with pytest.raises(SystemExit) as exc_info:
        main_module.main()

    assert exc_info.value.code == 1
    assert events == ["connect", "ensure_schema", "create_providers"]


def test_once_passes_factory_providers_to_poller_and_polls_once(monkeypatch):
    cfg = {"client_id": "test-client"}
    conn = object()
    providers = [object(), object()]
    poller = Mock()
    poller.poll_once.return_value = PollResult(providers_ok=2, positions_stored=1)
    poller_class = Mock(return_value=poller)
    monkeypatch.setattr(sys, "argv", ["fordlogger", "--once"])
    monkeypatch.setattr(main_module, "load_config", Mock(return_value=cfg))
    monkeypatch.setattr(main_module.db, "connect", Mock(return_value=conn))
    monkeypatch.setattr(main_module.db, "ensure_schema", Mock())
    monkeypatch.setattr(
        main_module, "create_providers", Mock(return_value=providers)
    )
    monkeypatch.setattr(main_module, "Poller", poller_class)

    main_module.main()

    poller_class.assert_called_once_with(cfg, conn, providers)
    poller.poll_once.assert_called_once_with()
    poller.run_forever.assert_not_called()


def _run_once(monkeypatch, result):
    poller = Mock()
    poller.poll_once.return_value = result
    monkeypatch.setattr(sys, "argv", ["fordlogger", "--once"])
    monkeypatch.setattr(main_module, "load_config", Mock(return_value={}))
    monkeypatch.setattr(main_module.db, "connect", Mock(return_value=object()))
    monkeypatch.setattr(main_module.db, "ensure_schema", Mock())
    monkeypatch.setattr(main_module, "create_providers", Mock(return_value=[object()]))
    monkeypatch.setattr(main_module, "Poller", Mock(return_value=poller))
    main_module.main()


@pytest.mark.parametrize("result", [
    PollResult(providers_failed=1),
    PollResult(providers_ok=1, positions_stored=0),
    PollResult(db_ok=False),
    PollResult(providers_ok=1, positions_stored=1, position_errors=1),
], ids=["provider-failed", "nothing-stored", "db-down", "position-error"])
def test_once_exits_nonzero_on_failure(monkeypatch, result):
    with pytest.raises(SystemExit) as exc_info:
        _run_once(monkeypatch, result)
    assert exc_info.value.code == 1


def test_once_success_exits_zero(monkeypatch):
    _run_once(monkeypatch, PollResult(providers_ok=1, positions_stored=1))


def test_sigterm_stops_daemon_loop(monkeypatch):
    poller = Poller({}, None, [])
    handlers = {}
    monkeypatch.setattr(main_module.signal, "signal",
                        lambda sig, h: handlers.__setitem__(sig, h))
    main_module._install_signal_handlers(poller)
    handlers[signal.SIGTERM](signal.SIGTERM, None)
    monkeypatch.setattr(poller, "poll_once", lambda: pytest.fail("polled after SIGTERM"))
    poller.run_forever()  # returns immediately, i.e. a clean exit


def test_provider_config_error_exits_one_without_traceback(monkeypatch, caplog):
    class ZeekrConfigError(ValueError):
        pass

    monkeypatch.setattr(sys, "argv", ["fordlogger"])
    monkeypatch.setattr(main_module, "load_config", Mock(return_value={}))
    monkeypatch.setattr(main_module.db, "connect", Mock(return_value=object()))
    monkeypatch.setattr(main_module.db, "ensure_schema", Mock())
    monkeypatch.setattr(main_module, "create_providers",
                        Mock(side_effect=ZeekrConfigError("ZEEKR_USERNAME is not set")))
    with caplog.at_level("INFO", logger="fordlogger"):
        with pytest.raises(SystemExit) as exc_info:
            main_module.main()
    assert exc_info.value.code == 1
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and "ZEEKR_USERNAME is not set" in errors[0].getMessage()
    assert errors[0].exc_info is None


@pytest.mark.parametrize("password", ["change-me-too", " Change-Me-as-well", "change-me-x"])
def test_placeholder_db_password_is_refused(monkeypatch, caplog, password):
    connect = Mock()
    monkeypatch.setattr(sys, "argv", ["fordlogger", "--once"])
    monkeypatch.setattr(main_module, "load_config", Mock(return_value={"db_password": password}))
    monkeypatch.setattr(main_module.db, "connect", connect)
    with pytest.raises(SystemExit) as exc_info:
        main_module.main()
    assert exc_info.value.code == 1
    connect.assert_not_called()
    assert "placeholder" in caplog.text and password.strip() not in caplog.text


def test_db_unavailable_is_one_line(monkeypatch, caplog):
    import psycopg2
    monkeypatch.setattr(sys, "argv", ["fordlogger"])
    monkeypatch.setattr(main_module, "load_config",
                        Mock(return_value={"db_host": "db", "db_port": 5432, "db_password": "x"}))
    monkeypatch.setattr(main_module.db, "connect",
                        Mock(side_effect=psycopg2.OperationalError("could not connect\nDETAIL: x")))
    with pytest.raises(SystemExit) as exc_info:
        main_module.main()
    assert exc_info.value.code == 1
    assert "Database unavailable at db:5432 (OperationalError): could not connect" in caplog.text
    assert "DETAIL" not in caplog.text
