
from datetime import datetime, timezone

from models import Notification, User

from .message_bus import reply


class NotificationAgent:

    # Current UTC Time

    def now(self):

        return datetime.now(timezone.utc).replace(
            tzinfo=None
        )


    # =====================================
    # Receive Agent Messages
    # =====================================

    def receive(self, message, db):

        if message.performative == "INFORM":

            return self.create_notification(
                message,
                db
            )

        return reply(

            message,

            "REFUSE",

            {
                "reason": "NotificationAgent only accepts INFORM messages"
            }

        )


    # =====================================
    # Create Notification
    # =====================================

    def create_notification(self, message, db):

        data = message.content

        user_id = data.get("user_id")

        notification_text = data.get("message")


        # Ignore Messages Without Notification Data

        if user_id is None or not notification_text:

            return reply(

                message,

                "INFORM",

                {
                    "message": "No notification required"
                }

            )


        # Validate User

        user = db.get(
            User,
            user_id
        )

        if not user:

            return reply(

                message,

                "REFUSE",

                {
                    "reason": "Notification recipient not found"
                }

            )


        # Validate Notification Message

        if not isinstance(notification_text, str):

            return reply(

                message,

                "REFUSE",

                {
                    "reason": "Invalid notification message"
                }

            )

        notification_text = notification_text.strip()

        if not notification_text:

            return reply(

                message,

                "REFUSE",

                {
                    "reason": "Notification message cannot be empty"
                }

            )


        # Create Notification Record

        notification = Notification(

            user_id=user_id,

            message=notification_text,

            is_read=False,

            created_at=self.now()

        )


        # Save Notification

        db.add(notification)

        db.flush()


        # Return Successful Response

        return reply(

            message,

            "AGREE",

            {
                "message": "Notification created successfully",

                "notification_id": notification.id,

                "user_id": user_id,

                "source_agent": message.sender
            }

        )
