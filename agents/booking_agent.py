
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import update

from models import (
    Booking,
    Ticket,
    TicketCategory,
    Event,
    Waitlist
)

from .message_bus import Message, bus, reply


class BookingAgent:

    # Agent Rules

    max_tickets = 6


    # Current UTC Time

    def now(self):

        return datetime.now(timezone.utc).replace(tzinfo=None)


    # =====================================
    # Calculate Refund Rate
    # =====================================

    def refund_rate(self, hours):

        if hours >= 48:

            return 1.0

        elif hours >= 24:

            return 0.5

        else:

            return 0.0


    # Refuse Request

    def refuse(self, message, reason):

        return reply(
            message,
            "REFUSE",
            {"reason": reason}
        )


    # =====================================
    # Send Request to Another Agent
    # =====================================

    def ask(self, message, db, receiver, action, **data):

        return bus.send(

            Message(

                sender="BookingAgent",

                receiver=receiver,

                performative="REQUEST",

                content={
                    "action": action,
                    **data
                },

                conversation_id=message.conversation_id

            ),

            db

        )


    # =====================================
    # Broadcast Notification
    # =====================================

    def notify(self, message, db, user_id, text):

        bus.broadcast(

            Message(

                sender="BookingAgent",

                receiver="*",

                performative="INFORM",

                content={
                    "user_id": user_id,
                    "message": text
                },

                conversation_id=message.conversation_id

            ),

            db

        )


    # =====================================
    # Lock Event During Transaction
    # =====================================

    def lock_event(self, db, event_id):

        db.flush()

        db.execute(

            update(Event)

            .where(Event.id == event_id)

            .values(status=Event.status)

        )

        db.expire_all()

        return db.get(Event, event_id)


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

        if action == "book":

            return self.book(message, db)

        if action == "book_hold":

            return self.book(message, db, use_hold=True)

        if action == "cancel_booking":

            return self.cancel_booking(message, db)

        if action == "cancel_event":

            return self.cancel_event(message, db)

        return self.refuse(
            message,
            "Unknown booking action"
        )


    # =====================================
    # Book Tickets
    # =====================================

    def book(self, message, db, use_hold=False):

        data = message.content

        user_id = data["user_id"]

        category_id = data["category_id"]

        quantity = data["quantity"]


        # Validate Ticket Quantity

        if quantity < 1 or quantity > self.max_tickets:

            return self.refuse(

                message,

                "You can book between 1 and 6 tickets"

            )


        # Find Ticket Category

        category = db.get(
            TicketCategory,
            category_id
        )

        if not category:

            return self.refuse(
                message,
                "Ticket category not found"
            )


        # Lock Event

        event = self.lock_event(
            db,
            category.event_id
        )

        if not event or event.status != "published":

            return self.refuse(
                message,
                "Event is not available"
            )


        # Validate Booking Time

        if self.now() >= event.start_time:

            return self.refuse(
                message,
                "Booking is closed for this event"
            )


        # =================================
        # Waitlist Reserved Ticket Booking
        # =================================

        if use_hold:

            if message.sender != "WaitlistAgent":

                return self.refuse(
                    message,
                    "Only WaitlistAgent can claim held tickets"
                )

            waitlist = db.get(
                Waitlist,
                data["waitlist_id"]
            )

            if (
                not waitlist
                or waitlist.status != "offered"
                or not waitlist.expires_at
                or waitlist.expires_at <= self.now()
                or waitlist.user_id != user_id
                or waitlist.category_id != category_id
                or waitlist.quantity != quantity
            ):

                return self.refuse(
                    message,
                    "Invalid or expired waitlist offer"
                )

            condition = TicketCategory.held >= quantity

            values = {

                "sold": TicketCategory.sold + quantity,

                "held": TicketCategory.held - quantity

            }

        else:

            # Normal Ticket Booking

            condition = (

                TicketCategory.sold
                + TicketCategory.held
                + quantity

                <= TicketCategory.quantity

            )

            values = {

                "sold": TicketCategory.sold + quantity

            }


        # =================================
        # Atomic Ticket Quantity Update
        # =================================

        result = db.execute(

            update(TicketCategory)

            .where(

                TicketCategory.id == category_id,

                condition

            )

            .values(**values)

        )

        if result.rowcount != 1:

            return self.refuse(

                message,

                "Not enough tickets available. "
                "Please join the waitlist."

            )


        # Calculate Total Price

        total = round(
            category.price * quantity,
            2
        )


        # Generate Simulated Payment Reference

        payment_reference = (
            "PAY-" + uuid4().hex.upper()
        )


        # Create Booking

        booking = Booking(

            user_id=user_id,

            category_id=category_id,

            quantity=quantity,

            total=total,

            payment_reference=payment_reference,

            status="confirmed",

            refund=0

        )

        db.add(booking)

        db.flush()


        # =================================
        # Request Tickets from TicketAgent
        # =================================

        response = self.ask(

            message,

            db,

            "TicketAgent",

            "issue",

            booking_id=booking.id,

            quantity=quantity

        )

        if response.performative != "AGREE":

            return reply(

                message,

                "FAILURE",

                {"reason": "Ticket generation failed"}

            )


        # Notify Attendee

        text = (

            f"Your booking for {event.title} "
            f"has been confirmed. "

            f"Quantity: {quantity}. "

            f"Total: LKR {total:.2f}. "

            f"Payment reference: {payment_reference}"

        )

        self.notify(

            message,

            db,

            user_id,

            text

        )


        # Return Successful Booking Response

        return reply(

            message,

            "AGREE",

            {
                "message": "Booking completed successfully",
                "booking_id": booking.id,
                "payment_reference": payment_reference,
                "total": total
            }

        )


    # =====================================
    # Cancel Individual Booking
    # =====================================

    def cancel_booking(self, message, db):

        data = message.content

        booking = db.get(
            Booking,
            data["booking_id"]
        )

        if not booking:

            return self.refuse(
                message,
                "Booking not found"
            )

        category = db.get(
            TicketCategory,
            booking.category_id
        )

        if not category:

            return self.refuse(
                message,
                "Ticket category not found"
            )

        event = self.lock_event(
            db,
            category.event_id
        )

        if not event:

            return self.refuse(
                message,
                "Event not found"
            )


        # Validate Booking Ownership

        if booking.user_id != data["user_id"]:

            return self.refuse(
                message,
                "Unauthorized booking cancellation"
            )


        # Check Booking Status

        if booking.status != "confirmed":

            return self.refuse(
                message,
                "Booking is not active"
            )

        if event.status != "published":

            return self.refuse(
                message,
                "Event is not active"
            )


        # Check Event Start Time

        if self.now() >= event.start_time:

            return self.refuse(
                message,
                "Event has already started"
            )


        # Check Whether Tickets Have Been Used

        used_ticket = db.query(Ticket).filter_by(

            booking_id=booking.id,

            status="used"

        ).first()

        if used_ticket:

            return self.refuse(

                message,

                "Booking contains a ticket "
                "that has already been used"

            )


        # Calculate Refund

        hours_remaining = (

            event.start_time - self.now()

        ).total_seconds() / 3600

        refund = round(

            booking.total
            * self.refund_rate(hours_remaining),

            2

        )


        # Cancel Booking

        booking.status = "cancelled"

        booking.refund = refund


        # Release Booked Tickets

        result = db.execute(

            update(TicketCategory)

            .where(

                TicketCategory.id == category.id,

                TicketCategory.sold >= booking.quantity

            )

            .values(

                sold=TicketCategory.sold - booking.quantity

            )

        )

        if result.rowcount != 1:

            return reply(

                message,

                "FAILURE",

                {"reason": "Ticket inventory update failed"}

            )

        db.flush()


        # Request TicketAgent to Invalidate Tickets

        response = self.ask(

            message,

            db,

            "TicketAgent",

            "invalidate",

            booking_id=booking.id

        )

        if response.performative != "AGREE":

            return reply(

                message,

                "FAILURE",

                {"reason": "Ticket invalidation failed"}

            )


        # Notify WaitlistAgent About Released Seats

        response = self.ask(

            message,

            db,

            "WaitlistAgent",

            "seats_released",

            category_id=category.id

        )

        if response.performative != "AGREE":

            return reply(

                message,

                "FAILURE",

                {"reason": "Waitlist update failed"}

            )


        # Notify User

        self.notify(

            message,

            db,

            booking.user_id,

            (
                f"Your booking for {event.title} "
                f"has been cancelled. "
                f"Refund amount: LKR {refund:.2f}"
            )

        )

        return reply(

            message,

            "AGREE",

            {
                "message": "Booking cancelled successfully",
                "booking_id": booking.id,
                "refund": refund
            }

        )


    # =====================================
    # Cancel Entire Event
    # =====================================

    def cancel_event(self, message, db):

        data = message.content

        event = self.lock_event(
            db,
            data["event_id"]
        )

        if not event:

            return self.refuse(
                message,
                "Event not found"
            )

        if event.status == "cancelled":

            return self.refuse(
                message,
                "Event is already cancelled"
            )

        if self.now() >= event.start_time:

            return self.refuse(
                message,
                "Event has already started"
            )


        # Update Event Status

        event.status = "cancelled"


        # Find Ticket Categories

        categories = db.query(
            TicketCategory
        ).filter_by(

            event_id=event.id

        ).all()

        category_ids = [

            category.id

            for category in categories

        ]


        # Find Confirmed Bookings

        bookings = db.query(Booking).filter(

            Booking.category_id.in_(category_ids),

            Booking.status == "confirmed"

        ).all()

        total_refund = 0


        # Cancel Every Booking and Give Full Refund

        for booking in bookings:

            booking.status = "cancelled"

            booking.refund = booking.total

            total_refund += booking.refund


            # Release Tickets

            result = db.execute(

                update(TicketCategory)

                .where(

                    TicketCategory.id == booking.category_id,

                    TicketCategory.sold >= booking.quantity

                )

                .values(

                    sold=TicketCategory.sold - booking.quantity

                )

            )

            if result.rowcount != 1:

                return reply(

                    message,

                    "FAILURE",

                    {"reason": "Ticket inventory update failed"}

                )

            db.flush()


            # Invalidate All Tickets

            response = self.ask(

                message,

                db,

                "TicketAgent",

                "invalidate",

                booking_id=booking.id

            )

            if response.performative != "AGREE":

                return reply(

                    message,

                    "FAILURE",

                    {"reason": "Ticket invalidation failed"}

                )


            # Notify Attendee

            self.notify(

                message,

                db,

                booking.user_id,

                (
                    f"The event {event.title} "
                    f"has been cancelled. "

                    f"You will receive a full refund of "
                    f"LKR {booking.refund:.2f}"
                )

            )

        db.flush()


        # Cancel Remaining Waitlist Entries

        response = self.ask(

            message,

            db,

            "WaitlistAgent",

            "cancel_event",

            event_id=event.id

        )

        if response.performative != "AGREE":

            return reply(

                message,

                "FAILURE",

                {"reason": "Waitlist cancellation failed"}

            )


        # Return Final Result

        return reply(

            message,

            "AGREE",

            {
                "message": "Event cancelled successfully",
                "event_id": event.id,
                "cancelled_bookings": len(bookings),
                "refund": round(total_refund, 2)
            }

        )
