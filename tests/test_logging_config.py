import logging

from linkedin_mcp_server.debug_trace import get_trace_dir, reset_trace_state_for_testing
from linkedin_mcp_server.logging_config import (
    _BROWSER_LIFECYCLE_LOGGERS,
    configure_logging,
    teardown_trace_logging,
)


def setup_function():
    reset_trace_state_for_testing()


def teardown_function():
    teardown_trace_logging()
    reset_trace_state_for_testing()


def test_configure_logging_registers_trace_cleanup_once(monkeypatch, tmp_path):
    registrations = []

    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(
        "linkedin_mcp_server.logging_config.atexit.register",
        lambda fn: registrations.append(fn),
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.logging_config._TRACE_CLEANUP_REGISTERED",
        False,
    )

    configure_logging()
    configure_logging()

    assert registrations == [teardown_trace_logging]


def test_error_log_level_keeps_browser_loggers_at_error(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))

    configure_logging(log_level="ERROR")

    for name in _BROWSER_LIFECYCLE_LOGGERS:
        assert logging.getLogger(name).level == logging.ERROR


def test_warning_log_level_raises_browser_loggers_to_info(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))

    configure_logging(log_level="WARNING")

    for name in _BROWSER_LIFECYCLE_LOGGERS:
        assert logging.getLogger(name).level == logging.INFO


def test_registered_trace_cleanup_removes_ephemeral_trace_dir(monkeypatch, tmp_path):
    registrations = []

    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr(
        "linkedin_mcp_server.logging_config.atexit.register",
        lambda fn: registrations.append(fn),
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.logging_config._TRACE_CLEANUP_REGISTERED",
        False,
    )

    configure_logging()
    trace_dir = get_trace_dir()

    assert trace_dir is not None
    assert trace_dir.exists()
    assert registrations == [teardown_trace_logging]

    registrations[0]()

    assert not trace_dir.exists()
    assert not any(
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging.FileHandler)
    )


def _persisted_log(monkeypatch, tmp_path, log_level: str = "WARNING"):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr("linkedin_mcp_server.logging_config.atexit.register", id)
    configure_logging(log_level=log_level)
    trace_dir = get_trace_dir()
    assert trace_dir is not None
    return trace_dir / "server.log"


def test_the_persisted_log_keeps_browser_lifecycle_info(monkeypatch, tmp_path):
    """The lines around a browser exit are INFO, and the default level is not.

    Both browser modules, because the launch is logged by one and the close
    by the other; an unrelated module at INFO stays out, so this is not a
    global raise.
    """
    log_path = _persisted_log(monkeypatch, tmp_path)

    logging.getLogger("linkedin_mcp_server.drivers.browser").info("Closing browser")
    logging.getLogger("linkedin_mcp_server.core.browser").info("Browser closed")
    logging.getLogger("linkedin_mcp_server.scraping.extractor").info("scraped")
    for handler in logging.getLogger().handlers:
        handler.flush()

    persisted = log_path.read_text(encoding="utf-8")
    assert "Closing browser" in persisted
    assert "Browser closed" in persisted
    assert "scraped" not in persisted


def test_a_debug_run_is_not_raised_to_info(monkeypatch, tmp_path):
    log_path = _persisted_log(monkeypatch, tmp_path, log_level="DEBUG")

    logging.getLogger("linkedin_mcp_server.drivers.browser").debug("fine detail")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert "fine detail" in log_path.read_text(encoding="utf-8")


def _runs(tmp_path):
    root = tmp_path / "trace-runs"
    return sorted(root.glob("run-*")) if root.exists() else []


def test_a_process_that_logs_nothing_leaves_no_run(monkeypatch, tmp_path):
    """A proxying frontend is killed by its host, so it never cleans up."""
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr("linkedin_mcp_server.logging_config.atexit.register", id)

    configure_logging(log_level="WARNING")
    logging.getLogger("linkedin_mcp_server.scraping.extractor").info("below level")

    assert _runs(tmp_path) == []


def test_the_first_record_creates_the_run_and_its_log(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr("linkedin_mcp_server.logging_config.atexit.register", id)

    configure_logging(log_level="WARNING")
    logging.getLogger("linkedin_mcp_server.scraping.extractor").warning("first")
    logging.getLogger("linkedin_mcp_server.scraping.extractor").warning("second")
    for handler in logging.getLogger().handlers:
        handler.flush()

    [run] = _runs(tmp_path)
    persisted = (run / "server.log").read_text(encoding="utf-8")
    assert "first" in persisted and "second" in persisted
    assert (run / "server.log").stat().st_mode & 0o777 == 0o600


def test_a_record_logged_while_creating_the_run_does_not_recurse(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr("linkedin_mcp_server.logging_config.atexit.register", id)
    prunes = []

    def noisy_prune(source_profile, root):
        prunes.append(root)
        logging.getLogger("linkedin_mcp_server.debug_trace").warning("pruning")

    monkeypatch.setattr(
        "linkedin_mcp_server.debug_trace._prune_stale_runs", noisy_prune
    )

    configure_logging(log_level="WARNING")
    logging.getLogger("linkedin_mcp_server.scraping.extractor").warning("first")

    assert len(prunes) == 1
    assert len(_runs(tmp_path)) == 1


def test_a_kept_failed_action_leaves_its_evidence_in_the_log(monkeypatch, tmp_path):
    """The run is kept and its log says why, with the page reads behind it."""
    from linkedin_mcp_server.debug_trace import retain_trace_for_failed_action

    monkeypatch.setenv("USER_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setattr("linkedin_mcp_server.logging_config.atexit.register", id)
    configure_logging(log_level="WARNING")

    logging.getLogger("linkedin_mcp_server.scraping.connection_actions").info(
        "Invite dialog text: weekly limit"
    )
    retain_trace_for_failed_action(
        "connect_with_person", "not_sent", succeeded={"connected"}
    )
    teardown_trace_logging()

    [run] = _runs(tmp_path)
    persisted = (run / "server.log").read_text(encoding="utf-8")
    assert "Invite dialog text: weekly limit" in persisted
    assert "connect_with_person ended with status 'not_sent'" in persisted
