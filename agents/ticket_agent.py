import secrets
from datetime import timedelta
from sqlalchemy import update
from models import Ticket, Booking, TicketCategory, Event, now
from agents.message_bus import reply


class TicketAgent:

    def receive(self, message, db):

        if message.performative != "REQUEST":
            return reply(message, "INFORM", {})

        data = message.content

        if data["action"] == "issue":

            codes = []

            for _ in range(data["quantity"]):

                code = secrets.token_urlsafe(32)

                ticket = Ticket(
                    booking_id=data["booking_id"],
                    code=code,
                    status="valid"
                )

                db.add(ticket)
                codes.append(code)

            return reply(message, "AGREE", {
                "codes": codes
            })

        if data["action"] == "scan":
            return self.scan(message, db)

        return reply(message, "REFUSE", {
            "reason": "Unknown ticket action"
        })

    def scan(self, message, db):

        data = message.content

        ticket = db.query(Ticket).filter_by(
            code=data["code"]
        ).first()

        if ticket is None:
            return reply(message, "REFUSE", {
                "reason": "Invalid ticket"
            })

        db.execute(
            update(Ticket)
            .where(Ticket.id == ticket.id)
            .values(scan_count=Ticket.scan_count + 1)
        )

        booking = db.get(Booking, ticket.booking_id)

        category = db.get(
            TicketCategory, booking.category_id
        )

        event = db.get(Event, category.event_id)

        if event.id != data["event_id"]:
            return reply(message, "REFUSE", {
                "reason": "Wrong event"
            })

        if booking.status == "cancelled" or event.status == "cancelled":
            return reply(message, "REFUSE", {
                "reason": "Cancelled ticket"
            })

        if ticket.status == "used":
            return reply(message, "REFUSE", {
                "reason": "Ticket already used"
            })

        if ticket.status != "valid":
            return reply(message, "REFUSE", {
                "reason": "Ticket is not valid"
            })

        if not (
            event.start_time - timedelta(minutes=60)
            <= now()
            <= event.end_time
        ):
            return reply(message, "REFUSE", {
                "reason": "Outside check-in time"
            })

        result = db.execute(
            update(Ticket)
            .where(
                Ticket.id == ticket.id,
                Ticket.status == "valid"
            )
            .values(
                status="used",
                checked_in_at=now()
            )
        )

        if result.rowcount != 1:
            return reply(message, "REFUSE", {
                "reason": "Ticket already used"
            })

        return reply(message, "AGREE", {
            "message": "Entry approved"
        })