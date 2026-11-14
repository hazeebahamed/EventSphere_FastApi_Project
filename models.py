from sqlalchemy import (
    Column, Integer, String, Float,
    Boolean, DateTime, ForeignKey, Text
)
from datetime import datetime, timezone
from database import Base


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    email = Column(String(150), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(String(20), default="attendee")


class Venue(Base):
    __tablename__ = "venues"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    location = Column(String(200))
    capacity = Column(Integer, nullable=False)
    description = Column(Text)
    image = Column(String(500))
    hourly_rate = Column(Float, default=0)
    private_hire = Column(Boolean, default=True)


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True)
    title = Column(String(150), nullable=False)
    category = Column(String(100))
    venue_id = Column(Integer, ForeignKey("venues.id"))
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    description = Column(Text)
    status = Column(String(20), default="draft")


class TicketCategory(Base):
    __tablename__ = "ticket_categories"

    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey("events.id"))
    name = Column(String(100))
    price = Column(Float)
    quantity = Column(Integer)
    sold = Column(Integer, default=0)
    held = Column(Integer, default=0)


class Booking(Base):
    __tablename__ = "bookings"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    category_id = Column(
        Integer, ForeignKey("ticket_categories.id")
    )
    quantity = Column(Integer)
    total = Column(Float)
    payment_reference = Column(String(100), unique=True)
    status = Column(String(20), default="confirmed")
    refund = Column(Float, default=0)
    created_at = Column(DateTime, default=now)


class Ticket(Base):
    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True)
    booking_id = Column(Integer, ForeignKey("bookings.id"))
    code = Column(String(200), unique=True)
    status = Column(String(20), default="valid")
    checked_in_at = Column(DateTime, nullable=True)
    scan_count = Column(Integer, default=0)


class VenueHire(Base):
    __tablename__ = "venue_hires"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    venue_id = Column(Integer, ForeignKey("venues.id"))
    start_time = Column(DateTime)
    end_time = Column(DateTime)
    purpose = Column(Text)
    guest_count = Column(Integer)
    price = Column(Float)
    status = Column(String(20), default="pending")


class Waitlist(Base):
    __tablename__ = "waitlists"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    category_id = Column(
        Integer, ForeignKey("ticket_categories.id")
    )
    quantity = Column(Integer, default=1)
    status = Column(String(20), default="waiting")
    created_at = Column(DateTime, default=now)
    expires_at = Column(DateTime, nullable=True)


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    message = Column(Text)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=now)


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id = Column(Integer, primary_key=True)
    sender = Column(String(100))
    receiver = Column(String(100))
    performative = Column(String(20))
    content = Column(Text)
    conversation_id = Column(String(100))
    created_at = Column(DateTime, default=now)