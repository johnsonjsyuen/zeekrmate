import sys
from unittest.mock import Mock

import pytest

import fordlogger.main as main_module



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
