"""Service for invoking AgentCore Runtime."""

import json
import logging

import boto3

logger = logging.getLogger(__name__)


class AgentCoreService:
    """Handles communication with AgentCore Runtime."""

    def __init__(self, agent_arn: str):
        self.client = boto3.client("bedrock-agentcore")
        self.agent_arn = agent_arn

    @staticmethod
    def _generate_actor_id(from_phone: str) -> str:
        """Generate actor ID identifying the USER (min 33 chars).

        Identifies who the user IS. Used for long-term memory (facts,
        preferences) that persists across all sessions.
        Based only on the user's phone number.
        """
        actor = f"wa-user-{from_phone}"
        return actor.ljust(33, "0")

    @staticmethod
    def _generate_session_id(from_phone: str) -> str:
        """Generate session ID for the user's conversation (min 33 chars).

        Identifies the conversation thread. Used for short-term memory
        (conversation turns) that expires per the configured TTL.
        One session per user — WhatsApp is a single continuous thread.
        """
        session = f"wa-chat-{from_phone}"
        return session.ljust(33, "0")

    def invoke_agent(
        self,
        from_phone: str,
        prompt: str,
        media: dict = None,
    ) -> str:
        """Invoke AgentCore Runtime with text and optional media.

        Args:
            from_phone: Sender's phone number.
            prompt: Text prompt from the user.
            media: Optional dict with keys 'type', 'format', 'data'.

        Returns:
            Agent response text.
        """
        actor_id = self._generate_actor_id(from_phone)
        session_id = self._generate_session_id(from_phone)

        payload_data = {
            "prompt": prompt.strip(),
            "actor_id": actor_id,
        }

        if media:
            payload_data["media"] = media

        payload = json.dumps(payload_data).encode()

        logger.info(
            "Invoking AgentCore: actor=%s, session=%s, has_media=%s",
            actor_id,
            session_id,
            media is not None,
        )

        response = self.client.invoke_agent_runtime(
            agentRuntimeArn=self.agent_arn,
            runtimeSessionId=session_id,
            runtimeUserId=actor_id,
            payload=payload,
        )

        content = []
        for chunk in response.get("response", []):
            if isinstance(chunk, bytes):
                content.append(chunk.decode("utf-8"))
            elif isinstance(chunk, dict) and "bytes" in chunk:
                content.append(chunk["bytes"].decode("utf-8"))

        response_text = "".join(content)

        try:
            response_json = json.loads(response_text)
            return response_json.get("result", response_text)
        except json.JSONDecodeError:
            return response_text
