from datetime import datetime, timedelta
from sqlalchemy import update
from models import Venue, Event, VenueHire, now
from agents.message_bus import reply


class VenueAgent:

    def __init__(self):
        self.buffer = timedelta(minutes=30)
        self.auto_approval_limit = 100

    def available(self, db, venue_id, start, end):

        events = db.query(Event).filter(
            Event.venue_id == venue_id,
            Event.status == "published",
            Event.start_time < end + self.buffer,
            Event.end_time > start - self.buffer
        ).first()

        hires = db.query(VenueHire).filter(
            VenueHire.venue_id == venue_id,
            VenueHire.status.in_(["confirmed", "pending"]),
            VenueHire.start_time < end + self.buffer,
            VenueHire.end_time > start - self.buffer
        ).first()

        return events is None and hires is None

    def receive(self, message, db):

        if message.performative != "REQUEST":
            return reply(message, "INFORM", {})

        data = message.content

        venue = db.query(Venue).filter_by(
            id=data["venue_id"]
        ).first()

        if not venue:
            return reply(message, "REFUSE", {
                "reason": "Venue not found"
            })

        start = datetime.fromisoformat(data["start"])
        end = datetime.fromisoformat(data["end"])

        if start < now() or end <= start:
            return reply(message, "REFUSE", {
                "reason": "Invalid date or time"
            })

        # Serialize scheduling changes for this venue
        db.execute(
            update(Venue)
            .where(Venue.id == venue.id)
            .values(capacity=Venue.capacity)
        )

        if not self.available(db, venue.id, start, end):

            alternatives = []

            for hours in range(1, 49):

                new_start = start + timedelta(hours=hours)
                new_end = end + timedelta(hours=hours)

                if self.available(
                    db, venue.id, new_start, new_end
                ):
                    alternatives.append({
                        "venue_id": venue.id,
                        "start": new_start.isoformat(),
                        "end": new_end.isoformat()
                    })

                    if len(alternatives) == 3:
                        break

            if not alternatives:
                return reply(message, "REFUSE", {
                    "reason": "No available nearby time slots"
                })

            return reply(message, "PROPOSE", {
                "reason": "Venue time clash",
                "alternatives": alternatives
            })

        if data["action"] == "event_slot":

            if data["quantity"] > venue.capacity:
                return reply(message, "REFUSE", {
                    "reason": "Venue capacity exceeded"
                })

            return reply(message, "AGREE", {
                "reason": "Time slot available"
            })

        if data["action"] == "hire":

            if not venue.private_hire:
                return reply(message, "REFUSE", {
                    "reason": "Private hire not allowed"
                })

            guests = data["guest_count"]

            if guests < 1 or guests > venue.capacity:
                return reply(message, "REFUSE", {
                    "reason": "Invalid guest count"
                })

            duration = (end - start).total_seconds() / 3600

            price = round(duration * venue.hourly_rate, 2)

            status = (
                "confirmed"
                if guests <= self.auto_approval_limit
                else "pending"
            )

            hire = VenueHire(
                user_id=data["user_id"],
                venue_id=venue.id,
                start_time=start,
                end_time=end,
                purpose=data["purpose"],
                guest_count=guests,
                price=price,
                status=status
            )

            db.add(hire)
            db.flush()

            return reply(message, "AGREE", {
                "hire_id": hire.id,
                "status": status,
                "price": price
            })

        return reply(message, "REFUSE", {
            "reason": "Unknown action"
        })