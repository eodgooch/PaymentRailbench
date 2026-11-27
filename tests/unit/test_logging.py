import json
import logging

from railbench.common.logging import JsonFormatter


def test_log_lines_are_json_with_service_level_message_and_extra_fields() -> None:
    record = logging.makeLogRecord(
        {
            "name": "railbench.ledger",
            "levelno": logging.INFO,
            "levelname": "INFO",
            "msg": "posted %s",
            "args": ("txn-1",),
            "created": 1772485200.0,  # 2026-03-02T21:00:00Z
            "payment_id": "pay-42",
        }
    )

    line = json.loads(JsonFormatter("ledger").format(record))

    assert line == {
        "ts": "2026-03-02T21:00:00+00:00",
        "level": "info",
        "service": "ledger",
        "logger": "railbench.ledger",
        "message": "posted txn-1",
        "payment_id": "pay-42",
    }
