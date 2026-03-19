"""WhatsApp message handling via AWS End User Messaging Social."""

import json
import logging
import os

import boto3

logger = logging.getLogger(__name__)

BUCKET_NAME = os.environ.get("S3_BUCKET", "")
ATTACHMENT_PREFIX = os.environ.get("ATTACHMENT_PREFIX", "attachment_")
VOICE_PREFIX = os.environ.get("VOICE_PREFIX", "voice_")
VIDEO_PREFIX = os.environ.get("VIDEO_PREFIX", "video_")
DOCUMENT_PREFIX = os.environ.get("DOCUMENT_PREFIX", "document_")


class WhatsAppMessage:
    """Represents a single WhatsApp message."""

    def __init__(self, meta_phone_number, message, metadata=None, client=None, meta_api_version="v20.0"):
        self.meta_phone_number = meta_phone_number
        self.phone_number_arn = meta_phone_number.get("arn", "")
        # phone-number-id-976c72a700aac43eaf573ae050example
        self.phone_number_id = self.phone_number_arn.split(":")[-1].replace("/", "-")
        self.message = message
        self.metadata = metadata or {}
        self.client = client or boto3.client("socialmessaging")
        self.s3_client = boto3.client("s3")
        self.phone_number = message.get("from", "")
        self.meta_api_version = meta_api_version
        self.message_id = message.get("id", "")

    def get_text(self) -> str:
        return self.message.get("text", {}).get("body", "")

    def get_message_type(self) -> str:
        return self.message.get("type", "text")

    def get_image(self, download=True) -> dict:
        """Get image message data, optionally downloading to S3."""
        image = self.message.get("image")
        if not image:
            return {}
        result = {
            "media_id": image.get("id", ""),
            "mime_type": image.get("mime_type", ""),
            "caption": image.get("caption", ""),
        }
        if download and result["media_id"]:
            media = self._download_media(result["media_id"], ATTACHMENT_PREFIX)
            result.update(media)
        return result

    def get_audio(self, download=True) -> dict:
        """Get audio message data, optionally downloading to S3."""
        audio = self.message.get("audio")
        if not audio:
            return {}
        result = {
            "media_id": audio.get("id", ""),
            "mime_type": audio.get("mime_type", ""),
        }
        if download and result["media_id"]:
            media = self._download_media(result["media_id"], VOICE_PREFIX)
            result.update(media)
        return result

    def get_video(self, download=True) -> dict:
        """Get video message data, optionally downloading to S3."""
        video = self.message.get("video")
        if not video:
            return {}
        result = {
            "media_id": video.get("id", ""),
            "mime_type": video.get("mime_type", ""),
            "caption": video.get("caption", ""),
        }
        if download and result["media_id"]:
            media = self._download_media(result["media_id"], VIDEO_PREFIX)
            result.update(media)
        return result

    def get_document(self, download=True) -> dict:
        """Get document message data, optionally downloading to S3."""
        document = self.message.get("document")
        if not document:
            return {}
        result = {
            "media_id": document.get("id", ""),
            "mime_type": document.get("mime_type", ""),
            "filename": document.get("filename", ""),
            "caption": document.get("caption", ""),
        }
        if download and result["media_id"]:
            media = self._download_media(result["media_id"], DOCUMENT_PREFIX)
            result.update(media)
        return result

    def _download_media(self, media_id: str, prefix: str) -> dict:
        """Download media from WhatsApp to S3 via social-messaging API."""
        try:
            response = self.client.get_whatsapp_message_media(
                mediaId=media_id,
                originationPhoneNumberId=self.phone_number_id,
                destinationS3File={
                    "bucketName": BUCKET_NAME,
                    "key": prefix,
                },
            )
            extension = response.get("mimeType", "").split("/")[-1]
            location = f"s3://{BUCKET_NAME}/{prefix}{media_id}.{extension}"
            logger.info("Media downloaded to: %s", location)
            return {"s3_url": location, "s3_bucket": BUCKET_NAME, "s3_key": f"{prefix}{media_id}.{extension}"}
        except Exception as e:
            logger.error("Failed to download media %s: %s", media_id, str(e))
            return {}

    def text_reply(self, text_message: str):
        """Send a text reply to the user."""
        message_object = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "context": {"message_id": self.message_id},
            "to": f"+{self.phone_number}",
            "type": "text",
            "text": {"preview_url": False, "body": text_message},
        }
        try:
            self.client.send_whatsapp_message(
                originationPhoneNumberId=self.phone_number_id,
                metaApiVersion=self.meta_api_version,
                message=bytes(json.dumps(message_object), "utf-8"),
            )
        except Exception as e:
            logger.error("Failed to send reply: %s", str(e))

    def mark_as_read(self):
        """Mark the message as read."""
        status_object = {
            "messaging_product": "whatsapp",
            "message_id": self.message_id,
            "status": "read",
        }
        try:
            self.client.send_whatsapp_message(
                originationPhoneNumberId=self.phone_number_arn,
                metaApiVersion=self.meta_api_version,
                message=bytes(json.dumps(status_object), "utf-8"),
            )
        except Exception as e:
            logger.error("Failed to mark as read: %s", str(e))

    def reaction(self, emoji: str):
        """Send a reaction emoji to the message."""
        reaction_object = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": f"+{self.phone_number}",
            "type": "reaction",
            "reaction": {"message_id": self.message_id, "emoji": emoji},
        }
        try:
            self.client.send_whatsapp_message(
                originationPhoneNumberId=self.phone_number_arn,
                metaApiVersion=self.meta_api_version,
                message=bytes(json.dumps(reaction_object), "utf-8"),
            )
        except Exception as e:
            logger.error("Failed to send reaction: %s", str(e))

    def save(self, table):
        """Save message to DynamoDB."""
        try:
            item = {
                "id": self.message_id,
                "from": self.phone_number,
                "phone_number_id": self.phone_number_id,
                "type": self.get_message_type(),
                "timestamp": self.message.get("timestamp", ""),
            }
            text = self.get_text()
            if text:
                item["text"] = text
            table.put_item(Item=item)
        except Exception as e:
            logger.error("Failed to save message: %s", str(e))


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
            for message in value.get("messages", []):
                self.messages.append(
                    WhatsAppMessage(phone_number, message, metadata)
                )

    def _get_phone_number_arn(self, phone_number_id: str) -> dict:
        """Find the phone number ARN from the SNS context metadata."""
        for phone_number in self.meta_phone_number_ids:
            if phone_number.get("metaPhoneNumberId") == phone_number_id:
                return phone_number
        return {}
