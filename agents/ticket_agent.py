
import secrets

from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from models import (
    Ticket,
    Booking,
    TicketCategory,
    Event
)

from .message_bus import reply


class TicketAgent:

    # Agent Rules

    checkin_before = timedelta(minutes=60)


    # Current UTC Time

    def now(self):

        return datetime.now(timezone.utc).replace(
            tzinfo=None
        )


    # Refuse Request

    def refuse(self, message, reason):

        return reply(

            message,

            "REFUSE",

            {"reason": reason}

        )


    # =====================================
    # Receive Agent Messages
    # =====================================

    def receive(self, message, db):

        if message.performative != "REQUEST":

            return reply(

                message,

                "INFORM",

                {"message": "Message received"}

            )

        action = message.content.get("action")

        if action == "issue":

            return self.issue(message, db)

        if action == "scan":

            return self.scan(message, db)

        if action == "invalidate":

            return self.invalidate(message, db)

        return self.refuse(

            message,

            "Unknown ticket action"

        )


    # =====================================
    # Generate Secure Ticket Code
    # =====================================

    def generate_code(self, db):

        while True:

            code = secrets.token_urlsafe(32)

            existing = db.query(Ticket).filter_by(

                code=code

            ).first()

            if not existing:

                return code


    # =====================================
    # Issue Tickets
    # =====================================

    def issue(self, message, db):

        data = message.content

        booking_id = data["booking_id"]

        quantity = data["quantity"]


        # Validate Booking

        booking = db.get(
            Booking,
            booking_id
        )

        if not booking:

            return self.refuse(
                message,
                "Booking not found"
            )

        if booking.status != "confirmed":

            return self.refuse(
                message,
                "Booking is not confirmed"
            )

        if quantity <= 0 or quantity != booking.quantity:

            return self.refuse(
                message,
                "Invalid ticket quantity"
            )


        # Prevent Duplicate Ticket Issuing

        existing_tickets = db.query(Ticket).filter_by(

            booking_id=booking_id

        ).all()

        if existing_tickets:

            return self.refuse(
                message,
                "Tickets have already been issued"
            )


        # Generate Individual Tickets

        codes = []

        for i in range(quantity):

            code = self.generate_code(db)

            ticket = Ticket(

                booking_id=booking_id,

                code=code,

                status="valid",

                scan_count=0,

                checked_in_at=None

            )

            db.add(ticket)

            db.flush()

            codes.append(code)


        # Return Successful Response

        return reply(

            message,

            "AGREE",

            {
                "message": "Tickets issued successfully",
                "booking_id": booking_id,
                "quantity": quantity,
                "codes": codes
            }

        )


    # =====================================
    # Validate and Scan QR Ticket
    # =====================================

    def scan(self, message, db):

        data = message.content

        code = data["code"].strip()

        event_id = data["event_id"]


        # Validate Ticket Code

        if not code:

            return self.refuse(
                message,
                "Ticket code is required"
            )


        # Find Ticket

        ticket = db.query(Ticket).filter_by(

            code=code

        ).first()

        if not ticket:

            return self.refuse(
                message,
                "Invalid ticket code"
            )


        # Find Booking

        booking = db.get(
            Booking,
            ticket.booking_id
        )

        if not booking:

            return self.refuse(
                message,
                "Booking not found"
            )


        # Find Ticket Category

        category = db.get(
            TicketCategory,
            booking.category_id
        )

        if not category:

            return self.refuse(
                message,
                "Ticket category not found"
            )


        # Lock Event
        # Prevent cancellation and check-in
        # from modifying the same event simultaneously.

        actual_event_id = category.event_id

        db.execute(

            update(Event)

            .where(Event.id == actual_event_id)

            .values(status=Event.status)

        )

        db.expire_all()

        event = db.get(Event, actual_event_id)


        # Record Scan Attempt

        db.execute(

            update(Ticket)

            .where(Ticket.id == ticket.id)

            .values(

                scan_count=Ticket.scan_count + 1

            )

        )

        db.refresh(ticket)


        # Check Correct Event

        if actual_event_id != event_id:

            return self.refuse(

                message,

                "This ticket belongs to another event"

            )


        # Check Event Status

        if not event or event.status != "published":

            return self.refuse(

                message,

                "Event is not active or has been cancelled"

            )


        # Check Booking Status

        if booking.status != "confirmed":

            return self.refuse(

                message,

                "Booking has been cancelled or is invalid"

            )


        # Check Ticket Status

        if ticket.status == "cancelled":

            return self.refuse(
                message,
                "This ticket has been cancelled"
            )

        if ticket.status == "used":

            return reply(

                message,

                "REFUSE",

                {
                    "reason": "Ticket already checked in",

                    "checked_in_at": (
                        ticket.checked_in_at.isoformat() + "Z"
                        if ticket.checked_in_at
                        else None
                    )
                }

            )

        if ticket.status != "valid":

            return self.refuse(
                message,
                "Ticket is not valid"
            )


        # =================================
        # Validate Check-in Time
        # =================================

        current_time = self.now()

        opening_time = (

            event.start_time - self.checkin_before

        )

        if current_time < opening_time:

            return reply(

                message,

                "REFUSE",

                {
                    "reason": "Check-in has not started",

                    "checkin_opens": (
                        opening_time.isoformat() + "Z"
                    )
                }

            )

        if current_time >= event.end_time:

            return self.refuse(
                message,
                "Event has already ended"
            )


        # =================================
        # Atomic Check-in
        # =================================

        result = db.execute(

            update(Ticket)

            .where(

                Ticket.id == ticket.id,

                Ticket.status == "valid"

            )

            .values(

                status="used",

                checked_in_at=current_time

            )

        )


        # Prevent Simultaneous Duplicate Scans

        if result.rowcount != 1:

            return self.refuse(

                message,

                "Ticket has already been used or invalidated"

            )


        # Successful Check-in

        return reply(

            message,

            "AGREE",

            {
                "message": "Check-in successful",

                "ticket_id": ticket.id,

                "event_id": event.id,

                "event": event.title,

                "ticket_category": category.name,

                "status": "used",

                "checked_in_at": (
                    current_time.isoformat() + "Z"
                )
            }

        )


    # =====================================
    # Invalidate Cancelled Booking Tickets
    # =====================================

    def invalidate(self, message, db):

        data = message.content

        booking_id = data["booking_id"]


        # Find Booking

        booking = db.get(
            Booking,
            booking_id
        )

        if not booking:

            return self.refuse(
                message,
                "Booking not found"
            )

        if booking.status != "cancelled":

            return self.refuse(
                message,
                "Booking has not been cancelled"
            )


        # Invalidate All Unused Tickets

        result = db.execute(

            update(Ticket)

            .where(

                Ticket.booking_id == booking_id,

                Ticket.status == "valid"

            )

            .values(

                status="cancelled"

            )

        )


        # Return Response

        return reply(

            message,

            "AGREE",

            {
                "message": "Tickets invalidated successfully",

                "booking_id": booking_id,

                "invalidated_tickets": result.rowcount
            }

        )
