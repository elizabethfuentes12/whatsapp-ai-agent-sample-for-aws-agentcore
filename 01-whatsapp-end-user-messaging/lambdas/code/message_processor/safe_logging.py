"""JSON logging helpers that keep phone numbers, message bodies and headers out of logs."""

import json
import logging
import os


def mask_phone(value: str) -> str:
    """Return only the last four digits of a phone number or channel id."""
    digits = "".join(ch for ch in str(value or "") if ch.isalnum())
    return f"***{digits[-4:]}" if digits else ""


class JsonFormatter(logging.Formatter):
    """One JSON object per line, easy to query with CloudWatch Logs Insights."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {"level": record.levelname, "message": record.getMessage()}
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            entry.update(fields)
        if record.exc_info:
            # Only the exception type: messages can echo request data.
            entry["error_type"] = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
        return json.dumps(entry, default=str)


def configure_logging() -> logging.Logger:
    """Attach the JSON formatter to the Lambda root logger and set the level."""
    root = logging.getLogger()
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    for handler in root.handlers:
        handler.setFormatter(JsonFormatter())
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    return root
