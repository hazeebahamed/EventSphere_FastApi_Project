from datetime import timedelta
from models import now
from agents.booking_agent import BookingAgent
from agents.venue_agent import VenueAgent


def test_full_refund():

    agent = BookingAgent()

    start = now() + timedelta(hours=72)

    assert agent.refund_rate(start) == 1.0


def test_half_refund():

    agent = BookingAgent()

    start = now() + timedelta(hours=36)

    assert agent.refund_rate(start) == 0.5


def test_no_refund():

    agent = BookingAgent()

    start = now() + timedelta(hours=12)

    assert agent.refund_rate(start) == 0.0


def test_venue_buffer():

    agent = VenueAgent()

    assert agent.buffer == timedelta(minutes=30)