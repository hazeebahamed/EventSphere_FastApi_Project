
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base

from models import (
    User,
    Venue,
    Event,
    TicketCategory,
    Booking,
    Ticket,
    VenueHire,
    Waitlist,
    Notification,
    AgentMessage
)

from agents.message_bus import Message, bus

from agents.venue_agent import VenueAgent
from agents.booking_agent import BookingAgent
from agents.ticket_agent import TicketAgent
from agents.waitlist_agent import WaitlistAgent
from agents.notification_agent import NotificationAgent


# =========================================
# Test Database Setup
# =========================================

@pytest.fixture
def session_factory(tmp_path):

    engine = create_engine(

        f"sqlite:///{tmp_path / 'test.db'}",

        connect_args={
            "check_same_thread": False,
            "timeout": 30
        }

    )

    Base.metadata.create_all(engine)

    TestSession = sessionmaker(

        bind=engine,

        autoflush=False,

        autocommit=False

    )

    # Register Agents

    bus.register("VenueAgent", VenueAgent())

    bus.register("BookingAgent", BookingAgent())

    bus.register("TicketAgent", TicketAgent())

    bus.register("WaitlistAgent", WaitlistAgent())

    bus.register("NotificationAgent", NotificationAgent())

    yield TestSession

    engine.dispose()


@pytest.fixture
def db(session_factory):

    session = session_factory()

    try:

        yield session

    finally:

        session.close()


# =========================================
# Helper Functions
# =========================================

def now():

    return datetime.now(timezone.utc).replace(
        tzinfo=None
    )


def send(db, receiver, action, **data):

    message = Message(

        sender="TestClient",

        receiver=receiver,

        performative="REQUEST",

        content={
            "action": action,
            **data
        }

    )

    response = bus.send(message, db)

    if response.performative == "FAILURE":

        db.rollback()

    else:

        db.commit()

    return response


def create_user(db, number):

    user = User(

        name=f"Test User {number}",

        email=f"user{number}@example.com",

        password_hash="test-only",

        role="attendee"

    )

    db.add(user)

    db.flush()

    return user


# =========================================
# Sample Test Data
# =========================================

@pytest.fixture
def sample(db):

    user1 = create_user(db, 1)

    user2 = create_user(db, 2)

    user3 = create_user(db, 3)

    venue = Venue(

        name="Test Auditorium",

        location="Kandy",

        capacity=200,

        description="Test venue",

        image="",

        hourly_rate=2500,

        private_hire=True

    )

    db.add(venue)

    db.flush()

    event = Event(

        title="Music Night",

        category="Music",

        venue_id=venue.id,

        start_time=now() + timedelta(days=3),

        end_time=now() + timedelta(
            days=3,
            hours=3
        ),

        description="Test music event",

        status="published"

    )

    db.add(event)

    db.flush()

    category = TicketCategory(

        event_id=event.id,

        name="General",

        price=1000,

        quantity=5,

        sold=0,

        held=0

    )

    db.add(category)

    db.commit()

    return {

        "user1": user1,

        "user2": user2,

        "user3": user3,

        "venue": venue,

        "event": event,

        "category": category

    }


def book(db, sample, quantity=1, user="user1"):

    return send(

        db,

        "BookingAgent",

        "book",

        user_id=sample[user].id,

        category_id=sample["category"].id,

        quantity=quantity

    )


def open_checkin(db, sample):

    event = sample["event"]

    event.start_time = now() + timedelta(
        minutes=30
    )

    event.end_time = now() + timedelta(
        hours=3
    )

    db.commit()


# =========================================
# TEST 01 - Successful Booking
# =========================================

def test_successful_booking(db, sample):

    response = book(db, sample, quantity=2)

    assert response.performative == "AGREE"

    booking = db.query(Booking).first()

    assert booking.quantity == 2

    assert booking.total == 2000

    assert booking.status == "confirmed"

    assert booking.payment_reference.startswith(
        "PAY-"
    )


# =========================================
# TEST 02 - Prevent Overbooking
# =========================================

def test_prevent_overbooking(db, sample):

    first = book(db, sample, quantity=4)

    second = book(

        db,

        sample,

        quantity=2,

        user="user2"

    )

    assert first.performative == "AGREE"

    assert second.performative == "REFUSE"

    db.refresh(sample["category"])

    assert sample["category"].sold == 4


# =========================================
# TEST 03 - Concurrent Overbooking
# =========================================

def test_concurrent_overbooking(
    db,
    sample,
    session_factory
):

    category_id = sample["category"].id

    user_ids = [

        sample["user1"].id,

        sample["user2"].id

    ]

    db.commit()

    barrier = Barrier(2)

    def worker(user_id):

        session = session_factory()

        try:

            barrier.wait(timeout=10)

            response = send(

                session,

                "BookingAgent",

                "book",

                user_id=user_id,

                category_id=category_id,

                quantity=4

            )

            return response.performative

        finally:

            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:

        results = list(
            pool.map(worker, user_ids)
        )

    assert results.count("AGREE") == 1

    assert results.count("REFUSE") == 1

    db.expire_all()

    category = db.get(
        TicketCategory,
        category_id
    )

    assert category.sold == 4


# =========================================
# TEST 04 - Invalid Ticket Quantity
# =========================================

def test_invalid_ticket_quantity(db, sample):

    response = book(db, sample, quantity=0)

    assert response.performative == "REFUSE"

    assert db.query(Booking).count() == 0


# =========================================
# TEST 05 - Unique Ticket Generation
# =========================================

def test_unique_ticket_generation(db, sample):

    response = book(db, sample, quantity=3)

    assert response.performative == "AGREE"

    tickets = db.query(Ticket).all()

    codes = [ticket.code for ticket in tickets]

    assert len(tickets) == 3

    assert len(set(codes)) == 3

    assert all(
        ticket.status == "valid"
        for ticket in tickets
    )


# =========================================
# TEST 06 - Double Check-in Prevention
# =========================================

def test_double_checkin(db, sample):

    assert book(db, sample).performative == "AGREE"

    open_checkin(db, sample)

    ticket = db.query(Ticket).first()

    first = send(

        db,

        "TicketAgent",

        "scan",

        code=ticket.code,

        event_id=sample["event"].id

    )

    second = send(

        db,

        "TicketAgent",

        "scan",

        code=ticket.code,

        event_id=sample["event"].id

    )

    assert first.performative == "AGREE"

    assert second.performative == "REFUSE"

    db.refresh(ticket)

    assert ticket.status == "used"

    assert ticket.scan_count == 2


# =========================================
# TEST 07 - Wrong Event QR Code
# =========================================

def test_wrong_event_ticket(db, sample):

    assert book(db, sample).performative == "AGREE"

    open_checkin(db, sample)

    ticket = db.query(Ticket).first()

    response = send(

        db,

        "TicketAgent",

        "scan",

        code=ticket.code,

        event_id=99999

    )

    assert response.performative == "REFUSE"

    db.refresh(ticket)

    assert ticket.status == "valid"


# =========================================
# TEST 08 - Check-in Too Early
# =========================================

def test_early_checkin(db, sample):

    assert book(db, sample).performative == "AGREE"

    ticket = db.query(Ticket).first()

    response = send(

        db,

        "TicketAgent",

        "scan",

        code=ticket.code,

        event_id=sample["event"].id

    )

    assert response.performative == "REFUSE"

    assert ticket.status == "valid"


# =========================================
# TEST 09 - Refund Policy
# =========================================

def test_refund_policy():

    agent = BookingAgent()

    assert agent.refund_rate(72) == 1.0

    assert agent.refund_rate(48) == 1.0

    assert agent.refund_rate(36) == 0.5

    assert agent.refund_rate(24) == 0.5

    assert agent.refund_rate(12) == 0.0


# =========================================
# TEST 10 - Booking Cancellation
# =========================================

def test_booking_cancellation(db, sample):

    response = book(db, sample, quantity=2)

    assert response.performative == "AGREE"

    booking_id = response.content["booking_id"]

    result = send(

        db,

        "BookingAgent",

        "cancel_booking",

        booking_id=booking_id,

        user_id=sample["user1"].id

    )

    assert result.performative == "AGREE"

    booking = db.get(Booking, booking_id)

    assert booking.status == "cancelled"

    assert booking.refund == 2000

    db.refresh(sample["category"])

    assert sample["category"].sold == 0

    tickets = db.query(Ticket).filter_by(

        booking_id=booking_id

    ).all()

    assert all(

        ticket.status == "cancelled"

        for ticket in tickets

    )


# =========================================
# TEST 11 - Event Cancellation Full Refund
# =========================================

def test_event_cancellation(db, sample):

    response = book(db, sample, quantity=2)

    assert response.performative == "AGREE"

    result = send(

        db,

        "BookingAgent",

        "cancel_event",

        event_id=sample["event"].id

    )

    assert result.performative == "AGREE"

    db.refresh(sample["event"])

    assert sample["event"].status == "cancelled"

    booking = db.query(Booking).first()

    assert booking.status == "cancelled"

    assert booking.refund == booking.total


# =========================================
# TEST 12 - Venue Overlap Prevention
# =========================================

def test_venue_overlap(db, sample):

    event = sample["event"]

    response = send(

        db,

        "VenueAgent",

        "hire",

        user_id=sample["user1"].id,

        venue_id=sample["venue"].id,

        start=event.start_time.isoformat(),

        end=event.end_time.isoformat(),

        purpose="Private party",

        guest_count=50

    )

    assert response.performative == "PROPOSE"

    assert len(response.content["alternatives"]) > 0

    assert db.query(VenueHire).count() == 0


# =========================================
# TEST 13 - Venue Capacity Validation
# =========================================

def test_venue_capacity(db, sample):

    start = now() + timedelta(days=7)

    end = start + timedelta(hours=2)

    response = send(

        db,

        "VenueAgent",

        "hire",

        user_id=sample["user1"].id,

        venue_id=sample["venue"].id,

        start=start.isoformat(),

        end=end.isoformat(),

        purpose="Private party",

        guest_count=300

    )

    assert response.performative == "REFUSE"


# =========================================
# TEST 14 - Automatic Venue Confirmation
# =========================================

def test_automatic_venue_confirmation(db, sample):

    start = now() + timedelta(days=7)

    end = start + timedelta(hours=4)

    response = send(

        db,

        "VenueAgent",

        "hire",

        user_id=sample["user1"].id,

        venue_id=sample["venue"].id,

        start=start.isoformat(),

        end=end.isoformat(),

        purpose="Birthday party",

        guest_count=50

    )

    assert response.performative == "AGREE"

    assert response.content["status"] == "confirmed"

    assert response.content["price"] == 10000

    hire = db.query(VenueHire).first()

    assert hire.status == "confirmed"


# =========================================
# TEST 15 - Administrator Escalation
# =========================================

def test_venue_escalation(db, sample):

    start = now() + timedelta(days=7)

    end = start + timedelta(hours=4)

    response = send(

        db,

        "VenueAgent",

        "hire",

        user_id=sample["user1"].id,

        venue_id=sample["venue"].id,

        start=start.isoformat(),

        end=end.isoformat(),

        purpose="Wedding",

        guest_count=150

    )

    assert response.performative == "AGREE"

    assert response.content["status"] == "pending"

    decision = send(

        db,

        "VenueAgent",

        "hire_decision",

        hire_id=response.content["hire_id"],

        decision="approve"

    )

    assert decision.performative == "AGREE"

    assert decision.content["status"] == "confirmed"


# =========================================
# TEST 16 - Join Waitlist
# =========================================

def test_join_waitlist(db, sample):

    assert book(

        db,

        sample,

        quantity=5

    ).performative == "AGREE"

    response = send(

        db,

        "WaitlistAgent",

        "join",

        user_id=sample["user2"].id,

        category_id=sample["category"].id,

        quantity=1

    )

    assert response.performative == "AGREE"

    entry = db.query(Waitlist).first()

    assert entry.status == "waiting"


# =========================================
# TEST 17 - Waitlist Offer and Claim
# =========================================

def test_waitlist_offer_and_claim(db, sample):

    response = book(db, sample, quantity=5)

    booking_id = response.content["booking_id"]

    joined = send(

        db,

        "WaitlistAgent",

        "join",

        user_id=sample["user2"].id,

        category_id=sample["category"].id,

        quantity=2

    )

    assert joined.performative == "AGREE"

    cancelled = send(

        db,

        "BookingAgent",

        "cancel_booking",

        booking_id=booking_id,

        user_id=sample["user1"].id

    )

    assert cancelled.performative == "AGREE"

    entry = db.query(Waitlist).first()

    assert entry.status == "offered"

    db.refresh(sample["category"])

    assert sample["category"].held == 2

    claimed = send(

        db,

        "WaitlistAgent",

        "claim",

        waitlist_id=entry.id,

        user_id=sample["user2"].id

    )

    assert claimed.performative == "AGREE"

    db.refresh(entry)

    db.refresh(sample["category"])

    assert entry.status == "claimed"

    assert sample["category"].held == 0

    assert sample["category"].sold == 2


# =========================================
# TEST 18 - Waitlist Expiry and Reoffer
# =========================================

def test_waitlist_expiry_and_reoffer(db, sample):

    response = book(db, sample, quantity=5)

    booking_id = response.content["booking_id"]

    for user in ["user2", "user3"]:

        result = send(

            db,

            "WaitlistAgent",

            "join",

            user_id=sample[user].id,

            category_id=sample["category"].id,

            quantity=5

        )

        assert result.performative == "AGREE"

    cancelled = send(

        db,

        "BookingAgent",

        "cancel_booking",

        booking_id=booking_id,

        user_id=sample["user1"].id

    )

    assert cancelled.performative == "AGREE"

    first = db.query(Waitlist).filter_by(

        user_id=sample["user2"].id

    ).first()

    second = db.query(Waitlist).filter_by(

        user_id=sample["user3"].id

    ).first()

    assert first.status == "offered"

    assert second.status == "waiting"

    first.expires_at = now() - timedelta(minutes=1)

    db.commit()

    response = send(

        db,

        "WaitlistAgent",

        "expiry_sweep"

    )

    assert response.performative == "AGREE"

    db.refresh(first)

    db.refresh(second)

    assert first.status == "expired"

    assert second.status == "offered"

    db.refresh(sample["category"])

    assert sample["category"].held == 5


# =========================================
# TEST 19 - Notification Creation
# =========================================

def test_notification_creation(db, sample):

    response = book(db, sample)

    assert response.performative == "AGREE"

    notification = db.query(Notification).filter_by(

        user_id=sample["user1"].id

    ).first()

    assert notification is not None

    assert notification.is_read is False

    assert "confirmed" in notification.message.lower()


# =========================================
# TEST 20 - Agent Message Logging
# =========================================

def test_agent_message_logging(db, sample):

    response = book(db, sample)

    assert response.performative == "AGREE"

    logs = db.query(AgentMessage).all()

    assert len(logs) > 0

    assert any(

        log.receiver == "TicketAgent"

        for log in logs

    )

    assert any(

        log.receiver == "NotificationAgent"

        for log in logs

    )

    assert any(

        log.performative == "REQUEST"

        for log in logs

    )

    assert any(

        log.performative == "AGREE"

        for log in logs

    )
