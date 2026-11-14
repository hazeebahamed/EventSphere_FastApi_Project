from uuid import uuid4
from sqlalchemy import update
from models import TicketCategory, Event, Booking, now
from agents.message_bus import Message, bus, reply


class BookingAgent:

    def __init__(self):
        self.max_tickets = 6

    def refund_rate(self, start_time):

        hours = (
            start_time - now()
        ).total_seconds() / 3600

        if hours >= 48:
            return 1.0

        if hours >= 24:
            return 0.5

        return 0.0

    def receive(self, message, db):

        if message.performative != "REQUEST":
            return reply(message, "INFORM", {})

        data = message.content

        if data["action"] == "book":
            return self.book(message, db)

        return reply(message, "REFUSE", {
            "reason": "Unknown booking action"
        })

    def book(self, message, db):

        data = message.content
        quantity = data["quantity"]

        if not 1 <= quantity <= self.max_tickets:
            return reply(message, "REFUSE", {
                "reason": "Book between 1 and 6 tickets"
            })

        category = db.get(
            TicketCategory, data["category_id"]
        )

        if category is None:
            return reply(message, "REFUSE", {
                "reason": "Ticket category not found"
            })

        event = db.get(Event, category.event_id)

        if event.status != "published" or event.start_time <= now():
            return reply(message, "REFUSE", {
                "reason": "Event is not open for booking"
            })

        result = db.execute(
            update(TicketCategory)
            .where(
                TicketCategory.id == category.id,
                TicketCategory.sold
                + TicketCategory.held
                + quantity <= TicketCategory.quantity
            )
            .values(
                sold=TicketCategory.sold + quantity
            )
        )

        if result.rowcount != 1:
            return reply(message, "REFUSE", {
                "reason": "Not enough tickets available"
            })

        booking = Booking(
            user_id=data["user_id"],
            category_id=category.id,
            quantity=quantity,
            total=round(category.price * quantity, 2),
            payment_reference="PAY-" + uuid4().hex,
            status="confirmed"
        )

        db.add(booking)
        db.flush()

        result = bus.send(
            Message(
                sender="BookingAgent",
                receiver="TicketAgent",
                performative="REQUEST",
                content={
                    "action": "issue",
                    "booking_id": booking.id,
                    "quantity": quantity
                },
                conversation_id=message.conversation_id
            ),
            db
        )

        if result.performative != "AGREE":
            raise RuntimeError("Ticket issuance failed")

        bus.broadcast(
            "BookingAgent",
            {
                "action": "booking_confirmed",
                "user_id": data["user_id"],
                "message": "Your booking is confirmed."
            },
            db,
            message.conversation_id
        )

        return reply(message, "AGREE", {
            "booking_id": booking.id,
            "payment_reference": booking.payment_reference,
            "total": booking.total
        })