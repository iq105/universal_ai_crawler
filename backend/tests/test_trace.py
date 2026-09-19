"""trace_id 单元测试（AGENT.md #5：日志必须携带 trace_id）。"""
import logging

from app.core import logger as app_logger
from app.core import trace


def test_new_trace_id_unique():
    assert trace.new_trace_id() != trace.new_trace_id()


def test_set_get_reset():
    assert trace.get_trace_id() == trace.DEFAULT_TRACE_ID
    token = trace.set_trace_id("abc123")
    assert trace.get_trace_id() == "abc123"
    trace.reset_trace_id(token)
    assert trace.get_trace_id() == trace.DEFAULT_TRACE_ID


def test_ensure_trace_id_generates_and_reuses():
    token = trace.set_trace_id("")
    try:
        tid = trace.ensure_trace_id()
        assert tid and tid != trace.DEFAULT_TRACE_ID
        assert trace.ensure_trace_id() == tid
    finally:
        trace.reset_trace_id(token)


def test_log_filter_injects_trace_id():
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    log = logging.getLogger("trace_test")
    log.setLevel(logging.INFO)
    handler = _Capture()
    handler.addFilter(app_logger.TraceIdFilter())
    handler.setFormatter(logging.Formatter(app_logger._DEFAULT_FORMAT))
    log.addHandler(handler)

    token = trace.set_trace_id("deadbeef")
    try:
        log.info("hello")
    finally:
        trace.reset_trace_id(token)

    assert records and records[0].trace_id == "deadbeef"
    assert "deadbeef" in handler.format(records[0])
    log.removeHandler(handler)