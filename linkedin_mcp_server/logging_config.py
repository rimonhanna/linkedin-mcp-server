# linkedin_mcp_server/logging_config.py
"""
Logging configuration for LinkedIn MCP Server with format options.

Provides JSON and compact logging formats for different deployment scenarios.
JSON format for production MCP integration, compact format for development.
Includes proper logger hierarchy and external library noise reduction.
"""

import atexit
import json
import logging
import os
from typing import Any, Dict

from linkedin_mcp_server.common_utils import secure_mkdir
from linkedin_mcp_server.debug_trace import (
    cleanup_trace_dir,
    get_trace_dir,
    trace_enabled,
)

_TRACE_FILE_HANDLER: logging.Handler | None = None
_TRACE_CLEANUP_REGISTERED = False

#: Loggers whose INFO records are kept whatever level the server runs at. A
#: browser that exits mid-scrape is only explicable from the lines around it
#: -- the launch, the close that was or was not asked for -- and those are
#: INFO. At the default WARNING they were never created, so the persisted log
#: held the symptom and nothing that could say why.
_BROWSER_LIFECYCLE_LOGGERS = (
    "linkedin_mcp_server.core.browser",
    "linkedin_mcp_server.drivers.browser",
)

#: The same floor for the module that reads an action's page state. A trace
#: kept for a failed ``connect_with_person`` is only worth reading with the
#: dialog text and button signals it logged, and those are INFO. Scoped rather
#: than a root-wide INFO: these lines appear only while an action runs, in the
#: process that drives the browser, whereas a root at INFO would have every
#: proxying frontend write a run directory again and the long-lived daemon's
#: log grow with every scrape.
_ACTION_EVIDENCE_LOGGERS = ("linkedin_mcp_server.scraping.connection_actions",)


class _LazyTraceFileHandler(logging.Handler):
    """Write ``server.log`` into the run directory, creating both on first use.

    A frontend that only proxies to the daemon logs nothing at the default
    level, and hosts kill it rather than letting it exit, so an eager run
    directory was left behind empty by every spawn and read like a capture
    that had failed. Deferring creation to the first record means such a
    process leaves nothing.
    """

    def __init__(self) -> None:
        super().__init__()
        self._file: logging.FileHandler | None = None
        self._opening = False

    def emit(self, record: logging.LogRecord) -> None:
        if self._file is None:
            # Creating the run prunes old ones, which may itself log; those
            # records arrive here re-entrantly and are dropped, not recursed on.
            if self._opening:
                return
            self._opening = True
            try:
                self._file = self._open()
            except Exception:
                self.handleError(record)
                return
            finally:
                self._opening = False
            if self._file is None:
                return
        self._file.emit(record)

    def _open(self) -> logging.FileHandler | None:
        trace_dir = get_trace_dir()
        if trace_dir is None:
            return None
        secure_mkdir(trace_dir)
        log_path = trace_dir / "server.log"
        try:
            fd = os.open(str(log_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(self.formatter)
        return file_handler

    def flush(self) -> None:
        if self._file is not None:
            self._file.flush()

    def close(self) -> None:
        try:
            if self._file is not None:
                self._file.close()
                self._file = None
        finally:
            super().close()


class MCPJSONFormatter(logging.Formatter):
    """JSON formatter for MCP server logs."""

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON.

        Args:
            record: The log record to format

        Returns:
            JSON-formatted log string
        """
        log_data: Dict[str, Any] = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add error details if present
        if hasattr(record, "error_type"):
            log_data["error_type"] = record.error_type
        if hasattr(record, "error_details"):
            log_data["error_details"] = record.error_details

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data)


class CompactFormatter(logging.Formatter):
    """Compact formatter that shortens logger names and uses shorter timestamps."""

    def format(self, record: logging.LogRecord) -> str:
        """Format log record with compact formatting.

        Args:
            record: The log record to format

        Returns:
            Compact-formatted log string
        """
        # Create a copy of the record to avoid modifying the original
        record_copy = logging.LogRecord(
            name=record.name,
            level=record.levelno,
            pathname=record.pathname,
            lineno=record.lineno,
            msg=record.msg,
            args=record.args,
            exc_info=record.exc_info,
            func=record.funcName,
        )
        record_copy.stack_info = record.stack_info

        # Shorten the logger name by removing the linkedin_mcp_server prefix
        if record_copy.name.startswith("linkedin_mcp_server."):
            record_copy.name = record_copy.name[len("linkedin_mcp_server.") :]

        # Format the time as HH:MM:SS only
        record_copy.asctime = self.formatTime(record_copy, datefmt="%H:%M:%S")

        return f"{record_copy.asctime} - {record_copy.name} - {record.levelname} - {record.getMessage()}"


def configure_logging(log_level: str = "WARNING", json_format: bool = False) -> None:
    """Configure logging for the LinkedIn MCP Server.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        json_format: Whether to use JSON formatting for logs
    """
    # Convert string to logging level
    numeric_level = getattr(logging, log_level.upper(), logging.WARNING)

    if json_format:
        formatter = MCPJSONFormatter()
    else:
        formatter = CompactFormatter()

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    global _TRACE_CLEANUP_REGISTERED, _TRACE_FILE_HANDLER
    _TRACE_FILE_HANDLER = None

    # Add console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    if trace_enabled():
        file_handler = _LazyTraceFileHandler()
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
        _TRACE_FILE_HANDLER = file_handler
        if not _TRACE_CLEANUP_REGISTERED:
            # The atexit fallback intentionally delegates the keep/delete
            # decision to teardown_trace_logging(), which re-checks runtime
            # trace retention state via cleanup_trace_dir().
            atexit.register(teardown_trace_logging)
            _TRACE_CLEANUP_REGISTERED = True

    # Per logger rather than per handler: the root level is what gates record
    # creation, so a handler alone could never see an INFO record from these.
    # ``min`` so a DEBUG run is not raised to INFO. The floor only applies at
    # WARNING and below; an operator who asked for ERROR-only gets ERROR-only
    # rather than having it lowered back to INFO.
    browser_level = (
        min(numeric_level, logging.INFO)
        if numeric_level <= logging.WARNING
        else numeric_level
    )
    for name in (*_BROWSER_LIFECYCLE_LOGGERS, *_ACTION_EVIDENCE_LOGGERS):
        logging.getLogger(name).setLevel(browser_level)

    # Set specific loggers to reduce noise
    logging.getLogger("urllib3").setLevel(logging.ERROR)
    logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)
    logging.getLogger("fakeredis").setLevel(logging.WARNING)
    logging.getLogger("docket").setLevel(logging.WARNING)


def teardown_trace_logging(*, keep_traces: bool = False) -> None:
    """Close trace logging handlers and cleanup ephemeral traces when allowed."""
    global _TRACE_FILE_HANDLER

    if _TRACE_FILE_HANDLER is not None:
        root_logger = logging.getLogger()
        root_logger.removeHandler(_TRACE_FILE_HANDLER)
        try:
            _TRACE_FILE_HANDLER.close()
        finally:
            _TRACE_FILE_HANDLER = None

    if not keep_traces:
        cleanup_trace_dir()
