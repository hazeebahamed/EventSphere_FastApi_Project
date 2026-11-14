from models import Notification
from agents.message_bus import reply


class NotificationAgent:

    def receive(self, message, db):

        if message.performative == "INFORM":

            data = message.content

            if "user_id" in data and "message" in data:

                notification = Notification(
                    user_id=data["user_id"],
                    message=data["message"]
                )

                db.add(notification)

                return reply(message, "AGREE", {
                    "message": "Notification created"
                })

        return reply(message, "INFORM", {
            "message": "No notification required"
        })