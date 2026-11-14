from datetime import timedelta
from sqlalchemy import update
from models import Waitlist, TicketCategory, now
from agents.message_bus import Message, bus, reply


class WaitlistAgent:

    def __init__(self):
        self.hold_minutes = 10

    def receive(self, message, db):

        data = message.content
        action = data.get("action")

        if action == "join":

            waiting = Waitlist(
                user_id=data["user_id"],
                category_id=data["category_id"],
                quantity=data["quantity"],
                status="waiting"
            )

            db.add(waiting)

            return reply(message, "AGREE", {
                "message": "Joined waitlist"
            })

        if action == "seats_released":

            self.offer_next(
                db,
                data["category_id"],
                message.conversation_id
            )

        if action == "expiry_sweep":

            self.expire_holds(
                db,
                message.conversation_id
            )

        return reply(message, "INFORM", {
            "message": "Waitlist processed"
        })

    def offer_next(self, db, category_id, conversation_id):

        customer = db.query(Waitlist).filter_by(
            category_id=category_id,
            status="waiting"
        ).order_by(
            Waitlist.created_at,
            Waitlist.id
        ).first()

        if customer is None:
            return

        result = db.execute(
            update(TicketCategory)
            .where(
                TicketCategory.id == category_id,
                TicketCategory.sold
                + TicketCategory.held
                + customer.quantity
                <= TicketCategory.quantity
            )
            .values(
                held=TicketCategory.held + customer.quantity
            )
        )

        if result.rowcount != 1:
            return

        customer.status = "offered"

        customer.expires_at = now() + timedelta(
            minutes=self.hold_minutes
        )

        bus.send(
            Message(
                sender="WaitlistAgent",
                receiver="NotificationAgent",
                performative="INFORM",
                content={
                    "user_id": customer.user_id,
                    "message": "Seats available! Claim within 10 minutes."
                },
                conversation_id=conversation_id
            ),
            db
        )

    def expire_holds(self, db, conversation_id):

        expired = db.query(Waitlist).filter(
            Waitlist.status == "offered",
            Waitlist.expires_at <= now()
        ).all()

        for entry in expired:

            db.execute(
                update(TicketCategory)
                .where(
                    TicketCategory.id == entry.category_id
                )
                .values(
                    held=TicketCategory.held - entry.quantity
                )
            )

            entry.status = "expired"

            db.flush()

            self.offer_next(
                db,
                entry.category_id,
                conversation_id
            )