
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from models import (
    Waitlist,
    TicketCategory,
    Event
)

from .message_bus import Message, bus, reply


class WaitlistAgent:

    # Agent Rules

    hold_minutes = 10

    max_tickets = 6


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
    # Send Request to Another Agent
    # =====================================

    def ask(self, message, db, receiver, action, **data):

        return bus.send(

            Message(

                sender="WaitlistAgent",

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
    # Notify Attendee
    # =====================================

    def notify(self, message, db, user_id, text):

        bus.broadcast(

            Message(

                sender="WaitlistAgent",

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

        if action == "join":

            return self.join(message, db)

        if action == "seats_released":

            return self.offer_next(message, db)

        if action == "claim":

            return self.claim(message, db)

        if action == "expiry_sweep":

            return self.expire_holds(message, db)

        if action == "cancel_event":

            return self.cancel_event(message, db)

        return self.refuse(

            message,

            "Unknown waitlist action"

        )


    # =====================================
    # Join Waitlist
    # =====================================

    def join(self, message, db):

        data = message.content

        user_id = data["user_id"]

        category_id = data["category_id"]

        quantity = data.get("quantity", 1)


        # Validate Quantity

        if quantity < 1 or quantity > self.max_tickets:

            return self.refuse(

                message,

                "You can request between 1 and 6 tickets"

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

        if self.now() >= event.start_time:

            return self.refuse(
                message,
                "Event booking has closed"
            )

        if quantity > category.quantity:

            return self.refuse(
                message,
                "Requested quantity exceeds ticket capacity"
            )


        # Prevent Duplicate Waitlist Entries

        existing = db.query(Waitlist).filter(

            Waitlist.user_id == user_id,

            Waitlist.category_id == category_id,

            Waitlist.status.in_([
                "waiting",
                "offered"
            ])

        ).first()

        if existing:

            return self.refuse(

                message,

                "You are already on this waitlist"

            )


        # Check Available Tickets

        available = (

            category.quantity
            - category.sold
            - category.held

        )

        if available >= quantity:

            return reply(

                message,

                "PROPOSE",

                {
                    "reason": "Tickets are currently available",
                    "alternatives": [
                        f"Book directly from /events/{event.id}"
                    ]
                }

            )


        # Create Waitlist Entry

        entry = Waitlist(

            user_id=user_id,

            category_id=category_id,

            quantity=quantity,

            status="waiting",

            created_at=self.now(),

            expires_at=None

        )

        db.add(entry)

        db.flush()


        # Notify User

        self.notify(

            message,

            db,

            user_id,

            (
                f"You have joined the waitlist for "
                f"{event.title}. You will be notified "
                f"when tickets become available."
            )

        )

        return reply(

            message,

            "AGREE",

            {
                "message": "Successfully joined the waitlist",
                "waitlist_id": entry.id,
                "status": "waiting"
            }

        )


    # =====================================
    # Offer Available Tickets
    # =====================================

    def offer_next(self, message, db):

        category_id = message.content["category_id"]

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

        if (
            not event
            or event.status != "published"
            or self.now() >= event.start_time
        ):

            return reply(

                message,

                "AGREE",

                {"message": "No new offers required"}

            )


        # Find Waiting Attendees
        # First-Come, First-Served Priority

        entries = db.query(Waitlist).filter_by(

            category_id=category_id,

            status="waiting"

        ).order_by(

            Waitlist.created_at.asc(),

            Waitlist.id.asc()

        ).all()

        offered_count = 0


        # Offer Tickets to Eligible Attendees

        for entry in entries:

            available = (

                category.quantity
                - category.sold
                - category.held

            )

            if available < entry.quantity:

                continue


            # Reserve Tickets Atomically

            result = db.execute(

                update(TicketCategory)

                .where(

                    TicketCategory.id == category_id,

                    (
                        TicketCategory.sold
                        + TicketCategory.held
                        + entry.quantity
                        <= TicketCategory.quantity
                    )

                )

                .values(

                    held=(
                        TicketCategory.held
                        + entry.quantity
                    )

                )

            )

            if result.rowcount != 1:

                break


            # Create Temporary Reservation

            entry.status = "offered"

            entry.expires_at = min(

                self.now() + timedelta(
                    minutes=self.hold_minutes
                ),

                event.start_time

            )

            db.flush()

            db.refresh(category)

            offered_count += 1


            # Notify Attendee

            self.notify(

                message,

                db,

                entry.user_id,

                (
                    f"Good news! {entry.quantity} ticket(s) "
                    f"for {event.title} are reserved for you. "
                    f"Claim them from My Bookings before "
                    f"the offer expires."
                )

            )

        return reply(

            message,

            "AGREE",

            {
                "message": "Waitlist processed",
                "offered_count": offered_count
            }

        )


    # =====================================
    # Claim Reserved Tickets
    # =====================================

    def claim(self, message, db):

        data = message.content

        entry = db.get(
            Waitlist,
            data["waitlist_id"]
        )

        if not entry:

            return self.refuse(
                message,
                "Waitlist entry not found"
            )


        # Find Ticket Category

        category = db.get(
            TicketCategory,
            entry.category_id
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


        # Validate Ownership

        if entry.user_id != data["user_id"]:

            return self.refuse(
                message,
                "Unauthorized waitlist claim"
            )


        # Validate Offer Status

        if entry.status != "offered":

            return self.refuse(
                message,
                "No active ticket offer"
            )


        # Validate Expiration Time

        if (
            not entry.expires_at
            or entry.expires_at <= self.now()
        ):

            return self.refuse(
                message,
                "Your ticket offer has expired"
            )


        # Validate Event

        if (
            not event
            or event.status != "published"
            or self.now() >= event.start_time
        ):

            return self.refuse(
                message,
                "Event is no longer available"
            )


        # Ask BookingAgent to Complete Booking

        response = self.ask(

            message,

            db,

            "BookingAgent",

            "book_hold",

            user_id=entry.user_id,

            category_id=entry.category_id,

            quantity=entry.quantity,

            waitlist_id=entry.id

        )

        if response.performative != "AGREE":

            return reply(

                message,

                "FAILURE",

                {
                    "reason": response.content.get(
                        "reason",
                        "Waitlist booking failed"
                    )
                }

            )


        # Mark Waitlist Entry as Claimed

        entry.status = "claimed"

        entry.expires_at = None

        db.flush()

        return reply(

            message,

            "AGREE",

            {
                "message": "Reserved tickets claimed successfully",

                "booking_id": response.content.get(
                    "booking_id"
                ),

                "payment_reference": response.content.get(
                    "payment_reference"
                ),

                "total": response.content.get("total")
            }

        )


    # =====================================
    # Automatically Release Expired Holds
    # =====================================

    def expire_holds(self, message, db):

        current_time = self.now()


        # Find Expired Offers

        entries = db.query(Waitlist).filter(

            Waitlist.status == "offered",

            Waitlist.expires_at <= current_time

        ).order_by(

            Waitlist.id.asc()

        ).all()

        entry_ids = [
            entry.id
            for entry in entries
        ]

        affected_categories = set()

        expired_count = 0


        for entry_id in entry_ids:

            entry = db.get(
                Waitlist,
                entry_id
            )

            category = db.get(
                TicketCategory,
                entry.category_id
            )

            if not category:

                continue


            # Lock Event

            event = self.lock_event(
                db,
                category.event_id
            )


            # Recheck After Locking

            if (
                entry.status != "offered"
                or not entry.expires_at
                or entry.expires_at > self.now()
            ):

                continue


            # Release Held Ticket Quantity

            result = db.execute(

                update(TicketCategory)

                .where(

                    TicketCategory.id == category.id,

                    TicketCategory.held >= entry.quantity

                )

                .values(

                    held=(
                        TicketCategory.held
                        - entry.quantity
                    )

                )

            )

            if result.rowcount != 1:

                return reply(

                    message,

                    "FAILURE",

                    {
                        "reason": "Unable to release expired hold"
                    }

                )


            # Update Waitlist Status

            entry.status = "expired"

            affected_categories.add(
                entry.category_id
            )

            expired_count += 1

            db.flush()


            # Notify Previous Attendee

            self.notify(

                message,

                db,

                entry.user_id,

                (
                    "Your waitlist ticket reservation "
                    "has expired. The tickets have "
                    "been released."
                )

            )


        # Offer Released Tickets to Next Attendee

        for category_id in sorted(affected_categories):

            response = bus.send(

                Message(

                    sender="WaitlistAgent",

                    receiver="WaitlistAgent",

                    performative="REQUEST",

                    content={

                        "action": "seats_released",

                        "category_id": category_id

                    },

                    conversation_id=message.conversation_id

                ),

                db

            )

            if response.performative != "AGREE":

                return reply(

                    message,

                    "FAILURE",

                    {
                        "reason": "Unable to reoffer tickets"
                    }

                )

        return reply(

            message,

            "AGREE",

            {
                "message": "Expired holds processed",
                "expired_count": expired_count
            }

        )


    # =====================================
    # Cancel Waitlist When Event Is Cancelled
    # =====================================

    def cancel_event(self, message, db):

        event_id = message.content["event_id"]

        event = self.lock_event(
            db,
            event_id
        )

        if not event:

            return self.refuse(
                message,
                "Event not found"
            )

        if event.status != "cancelled":

            return self.refuse(
                message,
                "Event has not been cancelled"
            )


        # Find Event Ticket Categories

        categories = db.query(
            TicketCategory
        ).filter_by(

            event_id=event_id

        ).all()

        category_ids = [

            category.id

            for category in categories

        ]


        # Find Active Waitlist Entries

        entries = db.query(Waitlist).filter(

            Waitlist.category_id.in_(category_ids),

            Waitlist.status.in_([
                "waiting",
                "offered"
            ])

        ).all()


        # Cancel All Active Entries

        for entry in entries:

            entry.status = "cancelled"

            entry.expires_at = None

            self.notify(

                message,

                db,

                entry.user_id,

                (
                    f"Your waitlist entry for {event.title} "
                    f"has been cancelled because the "
                    f"event was cancelled."
                )

            )


        # Release All Remaining Held Tickets

        for category in categories:

            category.held = 0

        db.flush()

        return reply(

            message,

            "AGREE",

            {
                "message": "Event waitlist cancelled",

                "cancelled_entries": len(entries)
            }

        )
