
from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from models import Venue, Event, VenueHire, TicketCategory

from .message_bus import Message, bus, reply


class VenueAgent:

    # Agent Rules

    buffer = timedelta(minutes=30)

    auto_limit = 100


    # Current UTC Time

    def now(self):

        return datetime.now(timezone.utc).replace(tzinfo=None)


    # Convert Date and Time

    def parse_time(self, value):

        result = datetime.fromisoformat(value)

        if result.tzinfo is not None:

            result = result.astimezone(
                timezone.utc
            ).replace(tzinfo=None)

        return result


    # Refuse Request

    def refuse(self, message, reason):

        return reply(
            message,
            "REFUSE",
            {"reason": reason}
        )


    # Send Notification

    def notify(self, message, db, user_id, text):

        bus.broadcast(

            Message(

                sender="VenueAgent",

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
    # Check Venue Availability
    # =====================================

    def available(
        self,
        db,
        venue_id,
        start,
        end,
        exclude_event=None,
        exclude_hire=None
    ):

        events = db.query(Event).filter(

            Event.venue_id == venue_id,

            Event.status == "published",

            Event.start_time < end + self.buffer,

            Event.end_time > start - self.buffer

        )

        hires = db.query(VenueHire).filter(

            VenueHire.venue_id == venue_id,

            VenueHire.status.in_([
                "confirmed",
                "pending"
            ]),

            VenueHire.start_time < end + self.buffer,

            VenueHire.end_time > start - self.buffer

        )

        if exclude_event is not None:

            events = events.filter(
                Event.id != exclude_event
            )

        if exclude_hire is not None:

            hires = hires.filter(
                VenueHire.id != exclude_hire
            )

        return (
            events.first() is None
            and hires.first() is None
        )


    # =====================================
    # Suggest Alternative Time Slots
    # =====================================

    def alternatives(self, db, venue_id, start, end):

        suggestions = []

        for hour in range(1, 169):

            new_start = start + timedelta(hours=hour)

            new_end = end + timedelta(hours=hour)

            if self.available(
                db,
                venue_id,
                new_start,
                new_end
            ):

                suggestions.append({

                    "start": new_start.isoformat() + "Z",

                    "end": new_end.isoformat() + "Z"

                })

            if len(suggestions) == 3:

                break

        return suggestions


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

        if action == "event_slot":

            return self.event_slot(message, db)

        if action == "create_event":

            return self.create_event(message, db)

        if action == "publish_event":

            return self.publish_event(message, db)

        if action == "edit_venue":

            return self.edit_venue(message, db)

        if action == "hire":

            return self.hire(message, db)

        if action == "hire_decision":

            return self.hire_decision(message, db)

        return self.refuse(
            message,
            "Unknown venue action"
        )


    # =====================================
    # Lock Venue During a Transaction
    # =====================================

    def lock_venue(self, db, venue_id):

        db.execute(

            update(Venue)

            .where(Venue.id == venue_id)

            .values(capacity=Venue.capacity)

        )

        db.expire_all()

        return db.get(Venue, venue_id)


    # =====================================
    # Validate Event Time Slot
    # =====================================

    def event_slot(self, message, db):

        data = message.content

        venue = self.lock_venue(
            db,
            data["venue_id"]
        )

        if not venue:

            return self.refuse(
                message,
                "Venue not found"
            )

        start = self.parse_time(data["start"])

        end = self.parse_time(data["end"])

        quantity = data["quantity"]

        if start <= self.now() or end <= start:

            return self.refuse(
                message,
                "Invalid event date or time"
            )

        if quantity <= 0:

            return self.refuse(
                message,
                "Ticket quantity must be positive"
            )

        if quantity > venue.capacity:

            return self.refuse(
                message,
                "Ticket quantity exceeds venue capacity"
            )

        if not self.available(
            db,
            venue.id,
            start,
            end,
            exclude_event=data.get("exclude_event")
        ):

            options = self.alternatives(
                db,
                venue.id,
                start,
                end
            )

            if options:

                return reply(

                    message,

                    "PROPOSE",

                    {
                        "reason": "Venue is unavailable",
                        "alternatives": options
                    }

                )

            return self.refuse(
                message,
                "No alternative time slots available"
            )

        return reply(

            message,

            "AGREE",

            {"message": "Venue time slot is available"}

        )


    # =====================================
    # Create Event
    # =====================================

    def create_event(self, message, db):

        data = message.content

        venue = self.lock_venue(
            db,
            data["venue_id"]
        )

        if not venue:

            return self.refuse(
                message,
                "Venue not found"
            )

        start = self.parse_time(data["start"])

        end = self.parse_time(data["end"])

        categories = data.get("ticket_categories", [])

        status = data.get("status", "draft")

        if status not in ["draft", "published"]:

            return self.refuse(
                message,
                "Invalid event status"
            )

        if start <= self.now() or end <= start:

            return self.refuse(
                message,
                "Invalid event date or time"
            )

        if not categories:

            return self.refuse(
                message,
                "At least one ticket category is required"
            )

        for category in categories:

            if (
                not category["name"].strip()
                or category["quantity"] <= 0
                or category["price"] < 0
            ):

                return self.refuse(
                    message,
                    "Invalid ticket category details"
                )

        total_quantity = sum(

            category["quantity"]

            for category in categories

        )

        if total_quantity > venue.capacity:

            return self.refuse(
                message,
                "Total tickets exceed venue capacity"
            )


        # Check availability for published events

        if status == "published":

            if not self.available(
                db,
                venue.id,
                start,
                end
            ):

                options = self.alternatives(
                    db,
                    venue.id,
                    start,
                    end
                )

                if options:

                    return reply(

                        message,

                        "PROPOSE",

                        {
                            "reason": "Venue time conflict",
                            "alternatives": options
                        }

                    )

                return self.refuse(
                    message,
                    "Venue is unavailable"
                )


        # Save Event

        event = Event(

            title=data["title"],

            category=data["category"],

            venue_id=venue.id,

            start_time=start,

            end_time=end,

            description=data["description"],

            status=status

        )

        db.add(event)

        db.flush()


        # Save Ticket Categories

        for category in categories:

            ticket_category = TicketCategory(

                event_id=event.id,

                name=category["name"],

                price=category["price"],

                quantity=category["quantity"],

                sold=0,

                held=0

            )

            db.add(ticket_category)

        return reply(

            message,

            "AGREE",

            {
                "message": "Event created successfully",
                "event_id": event.id,
                "status": event.status
            }

        )


    # =====================================
    # Publish Draft Event
    # =====================================

    def publish_event(self, message, db):

        data = message.content

        event = db.get(Event, data["event_id"])

        if not event:

            return self.refuse(
                message,
                "Event not found"
            )

        venue = self.lock_venue(
            db,
            event.venue_id
        )

        if not venue:

            return self.refuse(
                message,
                "Venue not found"
            )

        if event.status != "draft":

            return self.refuse(
                message,
                "Only draft events can be published"
            )

        if (
            event.start_time <= self.now()
            or event.end_time <= event.start_time
        ):

            return self.refuse(
                message,
                "Invalid event date or time"
            )

        categories = db.query(TicketCategory).filter_by(

            event_id=event.id

        ).all()

        total_quantity = sum(

            category.quantity

            for category in categories

        )

        if total_quantity <= 0:

            return self.refuse(
                message,
                "No tickets configured"
            )

        if total_quantity > venue.capacity:

            return self.refuse(
                message,
                "Tickets exceed venue capacity"
            )

        if not self.available(
            db,
            venue.id,
            event.start_time,
            event.end_time,
            exclude_event=event.id
        ):

            options = self.alternatives(

                db,

                venue.id,

                event.start_time,

                event.end_time

            )

            if options:

                return reply(

                    message,

                    "PROPOSE",

                    {
                        "reason": "Venue scheduling conflict",
                        "alternatives": options
                    }

                )

            return self.refuse(
                message,
                "Venue is unavailable"
            )

        event.status = "published"

        return reply(

            message,

            "AGREE",

            {
                "message": "Event published successfully",
                "event_id": event.id
            }

        )


    # =====================================
    # Edit Venue
    # =====================================

    def edit_venue(self, message, db):

        data = message.content

        venue = self.lock_venue(
            db,
            data["venue_id"]
        )

        if not venue:

            return self.refuse(
                message,
                "Venue not found"
            )

        capacity = data["capacity"]

        if capacity <= 0:

            return self.refuse(
                message,
                "Invalid venue capacity"
            )

        if data["hourly_rate"] < 0:

            return self.refuse(
                message,
                "Invalid hourly rate"
            )


        # Check Existing Events

        events = db.query(Event).filter(

            Event.venue_id == venue.id,

            Event.status.in_([
                "draft",
                "published"
            ]),

            Event.end_time > self.now()

        ).all()

        for event in events:

            categories = db.query(
                TicketCategory
            ).filter_by(
                event_id=event.id
            ).all()

            total = sum(
                category.quantity
                for category in categories
            )

            if total > capacity:

                return self.refuse(

                    message,

                    "New capacity is below existing "
                    "event ticket allocation"

                )


        # Check Existing Private Hires

        conflicting_hire = db.query(VenueHire).filter(

            VenueHire.venue_id == venue.id,

            VenueHire.status.in_([
                "pending",
                "confirmed"
            ]),

            VenueHire.end_time > self.now(),

            VenueHire.guest_count > capacity

        ).first()

        if conflicting_hire:

            return self.refuse(

                message,

                "New capacity is below existing "
                "private hire guest count"

            )


        # Update Venue

        venue.name = data["name"]

        venue.location = data["location"]

        venue.capacity = capacity

        venue.description = data["description"]

        venue.image = data.get("image", "")

        venue.hourly_rate = data["hourly_rate"]

        venue.private_hire = data["private_hire"]

        return reply(

            message,

            "AGREE",

            {"message": "Venue updated successfully"}

        )


    # =====================================
    # Private Venue Hire
    # =====================================

    def hire(self, message, db):

        data = message.content

        venue = self.lock_venue(
            db,
            data["venue_id"]
        )

        if not venue:

            return self.refuse(
                message,
                "Venue not found"
            )

        if not venue.private_hire:

            return self.refuse(
                message,
                "Venue is not available for private hire"
            )

        start = self.parse_time(data["start"])

        end = self.parse_time(data["end"])

        guest_count = data["guest_count"]

        if start <= self.now() or end <= start:

            return self.refuse(
                message,
                "Invalid hire date or time"
            )

        if guest_count <= 0:

            return self.refuse(
                message,
                "Guest count must be positive"
            )

        if guest_count > venue.capacity:

            return self.refuse(
                message,
                "Guest count exceeds venue capacity"
            )

        if not data["purpose"].strip():

            return self.refuse(
                message,
                "Hire purpose is required"
            )


        # Check Availability

        if not self.available(
            db,
            venue.id,
            start,
            end
        ):

            options = self.alternatives(
                db,
                venue.id,
                start,
                end
            )

            if options:

                return reply(

                    message,

                    "PROPOSE",

                    {
                        "reason": "Venue is already reserved",
                        "alternatives": options
                    }

                )

            return self.refuse(
                message,
                "No alternative time slots available"
            )


        # Calculate Hire Price

        duration = (
            end - start
        ).total_seconds() / 3600

        price = round(
            duration * venue.hourly_rate,
            2
        )


        # Autonomous Decision

        if guest_count <= self.auto_limit:

            status = "confirmed"

        else:

            status = "pending"


        # Save Venue Hire

        hire = VenueHire(

            user_id=data["user_id"],

            venue_id=venue.id,

            start_time=start,

            end_time=end,

            purpose=data["purpose"],

            guest_count=guest_count,

            price=price,

            status=status

        )

        db.add(hire)

        db.flush()


        # Notify User

        if status == "confirmed":

            text = (
                f"Your private hire request for "
                f"{venue.name} has been confirmed. "
                f"Total price: LKR {price:.2f}"
            )

        else:

            text = (
                f"Your private hire request for "
                f"{venue.name} is waiting for "
                f"administrator approval."
            )

        self.notify(

            message,

            db,

            data["user_id"],

            text

        )

        return reply(

            message,

            "AGREE",

            {
                "message": text,
                "hire_id": hire.id,
                "price": price,
                "status": status
            }

        )


    # =====================================
    # Administrator Hire Decision
    # =====================================

    def hire_decision(self, message, db):

        data = message.content

        hire = db.get(
            VenueHire,
            data["hire_id"]
        )

        if not hire:

            return self.refuse(
                message,
                "Hire request not found"
            )

        venue = self.lock_venue(
            db,
            hire.venue_id
        )

        if not venue:

            return self.refuse(
                message,
                "Venue not found"
            )

        if hire.status != "pending":

            return self.refuse(
                message,
                "Only pending requests can be processed"
            )

        decision = data.get("decision")

        if decision == "approve":

            if (
                hire.start_time <= self.now()
                or hire.end_time <= hire.start_time
            ):

                return self.refuse(
                    message,
                    "Hire time is no longer valid"
                )

            if hire.guest_count > venue.capacity:

                return self.refuse(
                    message,
                    "Guest count exceeds venue capacity"
                )

            if not self.available(

                db,

                hire.venue_id,

                hire.start_time,

                hire.end_time,

                exclude_hire=hire.id

            ):

                return self.refuse(
                    message,
                    "Venue is no longer available"
                )

            hire.status = "confirmed"

            text = (
                "Your private venue hire request "
                "has been approved."
            )

        elif decision == "reject":

            hire.status = "rejected"

            text = (
                "Your private venue hire request "
                "has been rejected."
            )

        else:

            return self.refuse(
                message,
                "Invalid administrator decision"
            )


        # Notify User

        self.notify(

            message,

            db,

            hire.user_id,

            text

        )

        return reply(

            message,

            "AGREE",

            {
                "message": text,
                "hire_id": hire.id,
                "status": hire.status
            }

        )
