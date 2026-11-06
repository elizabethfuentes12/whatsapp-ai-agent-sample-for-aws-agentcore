"""Service for invoking AgentCore Runtime."""

import json
import logging
import time
from contextvars import ContextVar

import boto3
from botocore.config import Config
from botocore.exceptions import ParamValidationError

from message_utils import actor_id_for, idempotency_key_for, session_id_for
from safe_logging import mask_phone

logger = logging.getLogger(__name__)

ACTOR_ID_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Actor-Id"
READ_TIMEOUT_SECONDS = 240
MAX_ATTEMPTS = 2
RETRY_BASE_DELAY = 2
MIN_SECONDS_FOR_ATTEMPT = 30

_actor_header: ContextVar = ContextVar("actor_header", default=None)


def _add_actor_header(request, **kwargs):
    """boto3 `before-sign` hook: InvokeAgentRuntime has no parameter for custom headers."""
    actor = _actor_header.get()
    if actor:
        request.headers.add_header(ACTOR_ID_HEADER, actor)


class AgentCoreUnavailable(Exception):
    """The runtime could not answer in time; the caller sends a friendly message."""


class AgentCoreService:
    """Handles communication with AgentCore Runtime."""

    def __init__(self, agent_arn: str, client=None):
        self.client = client or boto3.client(
            "bedrock-agentcore",
            config=Config(
                connect_timeout=10,
                read_timeout=READ_TIMEOUT_SECONDS,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )
        self.agent_arn = agent_arn
        try:
            self.client.meta.events.register_first(
                "before-sign.bedrock-agentcore.InvokeAgentRuntime", _add_actor_header
            )
        except AttributeError:  # test doubles
            pass

    def invoke_agent(self, from_phone, prompt, media=None, message_id="", seconds_left=None):
        """Invoke AgentCore Runtime with text and optional media.

        Args:
            from_phone: Sender's phone number (never logged unmasked).
            prompt: Text prompt from the user.
            media: Optional dict with keys 'type', 'format', 'data' (or 's3_uri').
            message_id: WhatsApp message id, sent as `idempotency_key`.
            seconds_left: Callable returning the remaining Lambda time in seconds.

        Returns:
            Agent response text.

        Raises:
            AgentCoreUnavailable: after the attempts or the time budget run out.
        """
        actor_id = actor_id_for(from_phone)
        session_id = session_id_for(from_phone)

        payload_data = {"prompt": prompt.strip(), "actor_id": actor_id}
        if message_id:
            payload_data["idempotency_key"] = idempotency_key_for(message_id)
        if media:
            payload_data["media"] = media
        payload = json.dumps(payload_data).encode()

        retryable = tuple(
            getattr(self.client.exceptions, name)
            for name in ("RuntimeClientError", "InternalServerException", "ThrottlingException")
            if hasattr(self.client.exceptions, name)
        )

        token = _actor_header.set(actor_id)
        try:
            for attempt in range(MAX_ATTEMPTS):
                if seconds_left is not None and seconds_left() < MIN_SECONDS_FOR_ATTEMPT:
                    break
                try:
                    logger.info(
                        "agent_invoke",
                        extra={"fields": {"attempt": attempt + 1, "has_media": media is not None,
                                          "user": mask_phone(from_phone)}},
                    )
                    response = self.client.invoke_agent_runtime(
                        agentRuntimeArn=self.agent_arn,
                        runtimeSessionId=session_id,
                        payload=payload,
                    )
                    return self._read_response(response)
                except retryable as error:
                    logger.warning(
                        "agent_invoke_retry",
                        extra={"fields": {"attempt": attempt + 1, "error_type": type(error).__name__}},
                    )
                    if attempt + 1 < MAX_ATTEMPTS:
                        time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
                except ParamValidationError as error:
                    # The managed-runtime boto3 rejected a parameter: a packaging problem, not an outage.
                    logger.error("agent_invoke_invalid_params", extra={"fields": {"error": str(error)[:200]}})
                    break
                except Exception as error:  # read timeout, connection error
                    logger.error("agent_invoke_failed", extra={"fields": {"error_type": type(error).__name__}})
                    break
        finally:
            _actor_header.reset(token)
        raise AgentCoreUnavailable("AgentCore did not return a response")

    @staticmethod
    def _read_response(response) -> str:
        content = []
        for chunk in response.get("response", []):
            if isinstance(chunk, bytes):
                content.append(chunk.decode("utf-8"))
            elif isinstance(chunk, dict) and "bytes" in chunk:
                content.append(chunk["bytes"].decode("utf-8"))
        response_text = "".join(content)
        try:
            return json.loads(response_text).get("result", response_text)
        except (json.JSONDecodeError, AttributeError):
            return response_text
