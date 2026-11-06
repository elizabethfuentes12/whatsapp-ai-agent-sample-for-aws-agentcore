"""WhatsApp message handling via AWS End User Messaging Social."""

import json
import logging
import os

import boto3

from safe_logging import mask_phone

logger = logging.getLogger(__name__)

BUCKET_NAME = os.environ.get("S3_BUCKET", "")
ATTACHMENT_PREFIX = os.environ.get("ATTACHMENT_PREFIX", "attachment_")
VOICE_PREFIX = os.environ.get("VOICE_PREFIX", "voice_")
VIDEO_PREFIX = os.environ.get("VIDEO_PREFIX", "video_")
DOCUMENT_PREFIX = os.environ.get("DOCUMENT_PREFIX", "document_")

# Same value as GRAPH_API_VERSION in whatsapp_end_user_messaging/config.py (set by CDK).
META_API_VERSION = os.environ.get("META_API_VERSION", "v25.0")


class MediaDownloadError(Exception):
    """The media could not be copied to S3; the invocation is retried."""


class WhatsAppMessage:
    """Represents a single WhatsApp message."""

    def __init__(self, meta_phone_number, message, metadata=None, client=None,
                 meta_api_version=META_API_VERSION, contact_name="", s3_client=None):
        self.meta_phone_number = meta_phone_number
        self.phone_number_arn = meta_phone_number.get("arn", "")
        # phone-number-id-976c72a700aac43eaf573ae050example
        self.phone_number_id = self.phone_number_arn.split(":")[-1].replace("/", "-")
        self.message = message
        self.metadata = metadata or {}
        self.client = client or boto3.client("socialmessaging")
        self.s3_client = s3_client
        self.phone_number = message.get("from", "")
        self.meta_api_version = meta_api_version
        self.message_id = message.get("id", "")
        self.contact_name = contact_name

    def get_text(self) -> str:
        return self.message.get("text", {}).get("body", "")

    def get_message_type(self) -> str:
        return self.message.get("type", "text")

    def _media(self, key, prefix, fields, download):
        media = self.message.get(key)
        if not media:
            return {}
        result = {"media_id": media.get("id", ""), "mime_type": media.get("mime_type", "")}
        for field in fields:
            result[field] = media.get(field, "")
        if download and result["media_id"]:
            result.update(self._download_media(result["media_id"], prefix))
        return result

    def get_image(self, download=True) -> dict:
        """Get image message data, optionally downloading to S3."""
        return self._media("image", ATTACHMENT_PREFIX, ["caption"], download)

    def get_audio(self, download=True) -> dict:
        """Get audio message data, optionally downloading to S3."""
        return self._media("audio", VOICE_PREFIX, [], download)

    def get_video(self, download=True) -> dict:
        """Get video message data, optionally downloading to S3."""
        return self._media("video", VIDEO_PREFIX, ["caption"], download)

    def get_document(self, download=True) -> dict:
        """Get document message data, optionally downloading to S3."""
        return self._media("document", DOCUMENT_PREFIX, ["filename", "caption"], download)

    def _download_media(self, media_id: str, prefix: str) -> dict:
        """Download media from WhatsApp to S3 via the End User Messaging Social API."""
        try:
            response = self.client.get_whatsapp_message_media(
                mediaId=media_id,
                originationPhoneNumberId=self.phone_number_id,
                destinationS3File={"bucketName": BUCKET_NAME, "key": prefix},
            )
        except Exception as error:
            logger.error("media_download_failed", extra={"fields": {"error_type": type(error).__name__}})
            raise MediaDownloadError(type(error).__name__) from error
        extension = response.get("mimeType", "").split("/")[-1].split(";")[0]
        key = f"{prefix}{media_id}.{extension}"
        logger.info("media_downloaded", extra={"fields": {"prefix": prefix}})
        return {"s3_url": f"s3://{BUCKET_NAME}/{key}", "s3_bucket": BUCKET_NAME, "s3_key": key}

    def _send(self, origination, payload, what):
        try:
            self.client.send_whatsapp_message(
                originationPhoneNumberId=origination,
                metaApiVersion=self.meta_api_version,
                message=bytes(json.dumps(payload), "utf-8"),
            )
        except Exception as error:
            # Best effort UX calls: never fail the message because a reaction did not go out.
            logger.warning(
                "send_failed",
                extra={"fields": {"what": what, "to": mask_phone(self.phone_number),
                                  "error_type": type(error).__name__}},
            )

    def text_reply(self, text_message: str):
        """Send a text reply to the user."""
        self._send(self.phone_number_id, {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "context": {"message_id": self.message_id},
            "to": f"+{self.phone_number}",
            "type": "text",
            "text": {"preview_url": False, "body": text_message},
        }, "text_reply")

    def mark_as_read(self):
        """Mark the message as read."""
        self._send(self.phone_number_arn, {
            "messaging_product": "whatsapp",
            "message_id": self.message_id,
            "status": "read",
        }, "read_receipt")

    def reaction(self, emoji: str):
        """Send a reaction emoji to the message."""
        self._send(self.phone_number_arn, {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": f"+{self.phone_number}",
            "type": "reaction",
            "reaction": {"message_id": self.message_id, "emoji": emoji},
        }, "reaction")


class WhatsAppService:
    """Parses SNS events from AWS End User Messaging Social."""

    def __init__(self, sns_message: dict):
        self.messages = []
        self.context = sns_message.get("context", {})
        self.meta_phone_number_ids = self.context.get("MetaPhoneNumberIds", [])

        webhook_entry = sns_message.get("whatsAppWebhookEntry", {})
        if isinstance(webhook_entry, str):
            webhook_entry = json.loads(webhook_entry)

        for change in webhook_entry.get("changes", []):
            if change.get("field") != "messages":
                continue
            value = change.get("value", {})
            metadata = value.get("metadata", {})
            phone_number_id = metadata.get("phone_number_id", "")
            phone_number = self._get_phone_number_arn(phone_number_id)
            contacts = value.get("contacts", [])
            contact_name = contacts[0].get("profile", {}).get("name", "") if contacts else ""
            for message in value.get("messages", []):
                self.messages.append(
                    WhatsAppMessage(phone_number, message, metadata, contact_name=contact_name)
                )

    def _get_phone_number_arn(self, phone_number_id: str) -> dict:
        """Find the phone number ARN from the SNS context metadata."""
        for phone_number in self.meta_phone_number_ids:
            if phone_number.get("metaPhoneNumberId") == phone_number_id:
                return phone_number
        return {}
