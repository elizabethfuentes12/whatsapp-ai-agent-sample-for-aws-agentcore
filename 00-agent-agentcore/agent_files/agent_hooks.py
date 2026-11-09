"""Strands plugin: tool-call logging and tool-result redaction."""

import logging

from strands.hooks import AfterToolCallEvent
from strands.plugins import Plugin, hook

from agent_helpers import GENERIC_TOOL_ERROR, redact_sensitive

logger = logging.getLogger(__name__)


class ToolSafetyPlugin(Plugin):
    """Logs every tool call and keeps internal details away from the model.

    * Logs tool name, status and duration (never inputs or outputs, which hold
      phone numbers, user text and S3 locations).
    * Error results are replaced with a generic message, so stack traces, bucket
      names and ARNs never enter the conversation (and therefore never memory).
    * Text in successful results is scrubbed of ARNs, account IDs and key-like
      values. Structured ``json`` blocks are left untouched on purpose: the
      video workflow needs the S3 URI and IDs.

    The system prompt rules stay as defense in depth.
    """

    name = "tool-safety"

    @hook
    def after_tool_call(self, event: AfterToolCallEvent) -> None:
        result = event.result
        tool_name = (event.tool_use or {}).get("name", "unknown")
        status = result.get("status", "unknown") if isinstance(result, dict) else "unknown"
        logger.info(
            "tool_call",
            extra={
                "fields": {
                    "tool": tool_name,
                    "status": status,
                    "duration_ms": int((event.duration or 0) * 1000),
                    "exception": type(event.exception).__name__ if event.exception else None,
                }
            },
        )

        if not isinstance(result, dict):
            return

        if status == "error":
            result["content"] = [{"text": GENERIC_TOOL_ERROR}]
            event.result = result
            return

        for block in result.get("content", []) or []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                block["text"] = redact_sensitive(block["text"])
        event.result = result
