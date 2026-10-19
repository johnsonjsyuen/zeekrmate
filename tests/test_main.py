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
