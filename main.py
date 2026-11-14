
# ============================================================
# EventSphere - IT4123 Agent Based Computing Practical
# ICA 02 Assignment
# main.py
# ============================================================

# ==================== IMPORTS ====================

import os
import io
import asyncio
import logging
import qrcode

from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    FastAPI,
    Request,
    Form,
    Depends,
    HTTPException
)

from fastapi.responses import (
    RedirectResponse,
    StreamingResponse
)

from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from starlette.middleware.sessions import SessionMiddleware

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from database import Base, engine, get_db, SessionLocal

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

from auth import (
    hash_password,
    verify_password,
    require_role,
    create_csrf_token,
    verify_csrf
)

# Import all five agents

from agents.message_bus import Message, bus
from agents.venue_agent import VenueAgent
from agents.booking_agent import BookingAgent
from agents.ticket_agent import TicketAgent
from agents.waitlist_agent import WaitlistAgent
from agents.notification_agent import NotificationAgent


# ==================== REGISTER AGENTS ====================

bus.register("VenueAgent", VenueAgent())
bus.register("BookingAgent", BookingAgent())
bus.register("TicketAgent", TicketAgent())
bus.register("WaitlistAgent", WaitlistAgent())
bus.register("NotificationAgent", NotificationAgent())


# ==================== BACKGROUND TIMER ====================

def run_waitlist_sweep():

    with SessionLocal() as db:

        try:

            message = Message(
                sender="Scheduler",
                receiver="WaitlistAgent",
                performative="REQUEST",
                content={
                    "action": "expiry_sweep"
                }
            )

            bus.send(message, db)

            db.commit()

        except Exception:

            db.rollback()

            logging.exception(
                "Waitlist expiry sweep failed"
            )


async def waitlist_timer():

    while True:

        await asyncio.sleep(30)

        await asyncio.to_thread(
            run_waitlist_sweep
        )


# ==================== APPLICATION LIFESPAN ====================

@asynccontextmanager
async def lifespan(app: FastAPI):

    Base.metadata.create_all(bind=engine)

    task = asyncio.create_task(
        waitlist_timer()
    )

    print("EventSphere started successfully")
    print("All five agents are running")

    try:

        yield

    finally:

        task.cancel()

        try:

            await task

        except asyncio.CancelledError:

            pass

        print("EventSphere stopped")


# ==================== CREATE APPLICATION ====================

app = FastAPI(
    title="EventSphere",
    description="Agent Based Event Booking System",
    version="1.0.0",
    lifespan=lifespan
)


# ==================== SESSION CONFIGURATION ====================

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ["SECRET_KEY"],
    https_only=os.getenv("PRODUCTION") == "true",
    same_site="lax",
    max_age=86400
)


# ==================== FRONTEND CONFIGURATION ====================

BASE_DIR = Path(__file__).resolve().parent

app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "static")),
    name="static"
)

templates = Jinja2Templates(
    directory=str(BASE_DIR / "templates")
)


# ==================== HELPER FUNCTIONS ====================

def render_page(request, template_name, **context):

    context["csrf_token"] = create_csrf_token(request)
    context["current_user_id"] = request.session.get("user_id")
    context["current_role"] = request.session.get("role")
    context["current_name"] = request.session.get("name")

    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context=context
    )


def send_request(db, agent, action, **content):

    try:

        message = Message(
            sender="WebAPI",
            receiver=agent,
            performative="REQUEST",
            content={
                "action": action,
                **content
            }
        )

        response = bus.send(message, db)

        if response.performative == "FAILURE":

            db.rollback()

        else:

            db.commit()

        return response

    except Exception:

        db.rollback()

        logging.exception("Agent request failed")

        raise HTTPException(
            status_code=500,
            detail="Agent request failed"
        )


def redirect(url):

    return RedirectResponse(
        url=url,
        status_code=303
    )


def parse_datetime(value):

    try:

        result = datetime.fromisoformat(value)

        if result.tzinfo is not None:

            result = result.astimezone(
                timezone.utc
            ).replace(tzinfo=None)

        return result

    except ValueError:

        raise HTTPException(
            status_code=400,
            detail="Invalid date or time"
        )


# ============================================================
# SECTION 01 - HOME PAGE
# ============================================================

@app.get("/")
def home(
    request: Request,
    q: str = "",
    category: str = "",
    db=Depends(get_db)
):

    query = db.query(Event).filter(
        Event.status == "published"
    )

    if q.strip():

        query = query.filter(
            Event.title.ilike(f"%{q.strip()}%")
        )

    if category:

        query = query.filter(
            Event.category == category
        )

    events = query.order_by(
        Event.start_time.asc()
    ).all()

    categories = db.query(
        Event.category
    ).filter(
        Event.status == "published"
    ).distinct().all()

    venues = {
        venue.id: venue
        for venue in db.query(Venue).all()
    }

    return render_page(
        request,
        "index.html",
        events=events,
        venues=venues,
        categories=[item[0] for item in categories],
        q=q,
        selected_category=category
    )


# ============================================================
# SECTION 02 - USER REGISTRATION
# ============================================================

@app.get("/register")
def register_page(request: Request):

    return render_page(
        request,
        "register.html"
    )


@app.post("/register")
def register(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    db=Depends(get_db)
):

    verify_csrf(request, csrf_token)

    name = name.strip()
    email = email.strip().lower()

    if not name or "@" not in email:

        raise HTTPException(
            400,
            "Invalid name or email"
        )

    if len(password) < 8:

        return render_page(
            request,
            "register.html",
            error="Password must contain at least 8 characters"
        )

    existing_user = db.query(User).filter_by(
        email=email
    ).first()

    if existing_user:

        return render_page(
            request,
            "register.html",
            error="Email is already registered"
        )

    user = User(
        name=name,
        email=email,
        password_hash=hash_password(password),
        role="attendee"
    )

    try:

        db.add(user)
        db.commit()

    except IntegrityError:

        db.rollback()

        raise HTTPException(
            400,
            "Email is already registered"
        )

    return redirect("/login")


# ============================================================
# SECTION 03 - USER LOGIN AND LOGOUT
# ============================================================

@app.get("/login")
def login_page(request: Request):

    return render_page(
        request,
        "login.html"
    )


@app.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    db=Depends(get_db)
):

    verify_csrf(request, csrf_token)

    user = db.query(User).filter_by(
        email=email.strip().lower()
    ).first()

    if not user or not verify_password(
        password,
        user.password_hash
    ):

        return render_page(
            request,
            "login.html",
            error="Invalid email or password"
        )

    request.session.clear()

    request.session["user_id"] = user.id
    request.session["name"] = user.name
    request.session["role"] = user.role

    create_csrf_token(request)

    if user.role == "admin":

        return redirect("/admin")

    elif user.role == "staff":

        return redirect("/scanner")

    return redirect("/")


@app.post("/logout")
def logout(
    request: Request,
    csrf_token: str = Form(...)
):

    verify_csrf(request, csrf_token)

    request.session.clear()

    return redirect("/login")


# ============================================================
# SECTION 04 - ADMIN DASHBOARD
# ============================================================

@app.get("/admin")
def admin_dashboard(
    request: Request,
    db=Depends(get_db)
):

    require_role(request, "admin")

    venues = db.query(Venue).all()
    events = db.query(Event).all()

    hires = db.query(VenueHire).order_by(
        VenueHire.id.desc()
    ).all()

    logs = db.query(AgentMessage).order_by(
        AgentMessage.id.desc()
    ).limit(200).all()

    total_users = db.query(User).filter_by(
        role="attendee"
    ).count()

    total_tickets = db.query(
        func.sum(Booking.quantity)
    ).filter(
        Booking.status == "confirmed"
    ).scalar() or 0

    gross_revenue = db.query(
        func.sum(Booking.total)
    ).scalar() or 0

    total_refunds = db.query(
        func.sum(Booking.refund)
    ).scalar() or 0

    total_revenue = gross_revenue - total_refunds

    total_checkins = db.query(Ticket).filter_by(
        status="used"
    ).count()

    event_statistics = []

    for event in events:

        venue = db.get(Venue, event.venue_id)

        categories = db.query(
            TicketCategory
        ).filter_by(
            event_id=event.id
        ).all()

        category_ids = [
            category.id for category in categories
        ]

        sold = sum(
            category.sold for category in categories
        )

        capacity = venue.capacity if venue else 0

        ticket_capacity = sum(
            category.quantity for category in categories
        )

        revenue = db.query(
            func.sum(Booking.total)
        ).filter(
            Booking.category_id.in_(category_ids)
        ).scalar() or 0

        refunds = db.query(
            func.sum(Booking.refund)
        ).filter(
            Booking.category_id.in_(category_ids)
        ).scalar() or 0

        checkins = db.query(Ticket).join(
            Booking,
            Ticket.booking_id == Booking.id
        ).filter(
            Booking.category_id.in_(category_ids),
            Ticket.status == "used"
        ).count()

        event_statistics.append({
            "id": event.id,
            "title": event.title,
            "status": event.status,
            "capacity": capacity,
            "ticket_capacity": ticket_capacity,
            "sold": sold,
            "revenue": revenue - refunds,
            "checkins": checkins
        })

    return render_page(
        request,
        "admin.html",
        venues=venues,
        events=events,
        hires=hires,
        logs=logs,
        total_users=total_users,
        total_tickets=total_tickets,
        total_revenue=total_revenue,
        total_checkins=total_checkins,
        total_events=len(events),
        total_venues=len(venues),
        event_statistics=event_statistics
    )


# ============================================================
# SECTION 05 - VENUE MANAGEMENT
# ============================================================

@app.post("/admin/venues")
def create_venue(
    request: Request,
    name: str = Form(...),
    location: str = Form(...),
    capacity: int = Form(...),
    description: str = Form(...),
    hourly_rate: float = Form(...),
    csrf_token: str = Form(...),
    image: str = Form(""),
    private_hire: bool = Form(False),
    db=Depends(get_db)
):

    require_role(request, "admin")

    verify_csrf(request, csrf_token)

    if capacity <= 0 or hourly_rate < 0:

        raise HTTPException(
            400,
            "Invalid capacity or hourly rate"
        )

    venue = Venue(
        name=name,
        location=location,
        capacity=capacity,
        description=description,
        image=image,
        hourly_rate=hourly_rate,
        private_hire=private_hire
    )

    db.add(venue)
    db.commit()

    return redirect("/admin")


@app.post("/admin/venues/{venue_id}/edit")
def edit_venue(
    venue_id: int,
    request: Request,
    name: str = Form(...),
    location: str = Form(...),
    capacity: int = Form(...),
    description: str = Form(...),
    hourly_rate: float = Form(...),
    csrf_token: str = Form(...),
    image: str = Form(""),
    private_hire: bool = Form(False),
    db=Depends(get_db)
):

    require_role(request, "admin")

    verify_csrf(request, csrf_token)

    if capacity <= 0 or hourly_rate < 0:

        raise HTTPException(
            400,
            "Invalid capacity or hourly rate"
        )

    response = send_request(
        db,
        "VenueAgent",
        "edit_venue",
        venue_id=venue_id,
        name=name,
        location=location,
        capacity=capacity,
        description=description,
        hourly_rate=hourly_rate,
        image=image,
        private_hire=private_hire
    )

    if response.performative == "AGREE":

        return redirect("/admin")

    return render_page(
        request,
        "result.html",
        result=response.model_dump()
    )


# ============================================================
# SECTION 06 - CREATE EVENTS
# ============================================================

@app.post("/admin/events")
def create_event(
    request: Request,
    title: str = Form(...),
    category: str = Form(...),
    venue_id: int = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    description: str = Form(...),
    ticket_name: list[str] = Form(...),
    ticket_price: list[float] = Form(...),
    ticket_quantity: list[int] = Form(...),
    csrf_token: str = Form(...),
    status: str = Form("draft"),
    db=Depends(get_db)
):

    require_role(request, "admin")

    verify_csrf(request, csrf_token)

    start = parse_datetime(start_time)
    end = parse_datetime(end_time)

    if end <= start:

        raise HTTPException(
            400,
            "End time must be after start time"
        )

    if status not in ["draft", "published"]:

        raise HTTPException(
            400,
            "Invalid event status"
        )

    if not (
        len(ticket_name)
        == len(ticket_price)
        == len(ticket_quantity)
    ):

        raise HTTPException(
            400,
            "Invalid ticket categories"
        )

    if not ticket_name:

        raise HTTPException(
            400,
            "At least one ticket category is required"
        )

    categories = []

    for name, price, quantity in zip(
        ticket_name,
        ticket_price,
        ticket_quantity
    ):

        if not name.strip() or price < 0 or quantity <= 0:

            raise HTTPException(
                400,
                "Invalid ticket category"
            )

        categories.append({
            "name": name,
            "price": price,
            "quantity": quantity
        })

    response = send_request(
        db,
        "VenueAgent",
        "create_event",
        title=title,
        category=category,
        venue_id=venue_id,
        start=start.isoformat(),
        end=end.isoformat(),
        description=description,
        ticket_categories=categories,
        quantity=sum(ticket_quantity),
        status=status
    )

    if response.performative == "AGREE":

        return redirect("/admin")

    return render_page(
        request,
        "result.html",
        result=response.model_dump()
    )


# ============================================================
# SECTION 07 - PUBLISH AND CANCEL EVENTS
# ============================================================

@app.post("/admin/events/{event_id}/publish")
def publish_event(
    event_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db=Depends(get_db)
):

    require_role(request, "admin")

    verify_csrf(request, csrf_token)

    response = send_request(
        db,
        "VenueAgent",
        "publish_event",
        event_id=event_id
    )

    if response.performative == "AGREE":

        return redirect("/admin")

    return render_page(
        request,
        "result.html",
        result=response.model_dump()
    )


@app.post("/admin/events/{event_id}/cancel")
def cancel_event(
    event_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db=Depends(get_db)
):

    require_role(request, "admin")

    verify_csrf(request, csrf_token)

    response = send_request(
        db,
        "BookingAgent",
        "cancel_event",
        event_id=event_id
    )

    if response.performative == "AGREE":

        return redirect("/admin")

    return render_page(
        request,
        "result.html",
        result=response.model_dump()
    )


# ============================================================
# SECTION 08 - EVENT DETAILS
# ============================================================

@app.get("/events/{event_id}")
def event_details(
    event_id: int,
    request: Request,
    db=Depends(get_db)
):

    event = db.get(Event, event_id)

    if not event or event.status != "published":

        raise HTTPException(
            404,
            "Event not found"
        )

    venue = db.get(Venue, event.venue_id)

    categories = db.query(
        TicketCategory
    ).filter_by(
        event_id=event_id
    ).all()

    return render_page(
        request,
        "event.html",
        event=event,
        venue=venue,
        categories=categories
    )


# ============================================================
# SECTION 09 - TICKET BOOKING
# ============================================================

@app.post("/book")
def book_tickets(
    request: Request,
    category_id: int = Form(...),
    quantity: int = Form(...),
    csrf_token: str = Form(...),
    db=Depends(get_db)
):

    user_id = require_role(
        request,
        "attendee"
    )

    verify_csrf(request, csrf_token)

    response = send_request(
        db,
        "BookingAgent",
        "book",
        user_id=user_id,
        category_id=category_id,
        quantity=quantity
    )

    if response.performative == "AGREE":

        return redirect("/bookings")

    return render_page(
        request,
        "result.html",
        result=response.model_dump()
    )


# ============================================================
# SECTION 10 - BOOKING HISTORY
# ============================================================

@app.get("/bookings")
def booking_history(
    request: Request,
    db=Depends(get_db)
):

    user_id = require_role(
        request,
        "attendee"
    )

    bookings = db.query(Booking).filter_by(
        user_id=user_id
    ).order_by(
        Booking.id.desc()
    ).all()

    booking_details = []

    for booking in bookings:

        category = db.get(
            TicketCategory,
            booking.category_id
        )

        event = db.get(
            Event,
            category.event_id
        )

        tickets = db.query(Ticket).filter_by(
            booking_id=booking.id
        ).all()

        booking_details.append({
            "booking": booking,
            "category": category,
            "event": event,
            "tickets": tickets
        })

    waitlists = db.query(Waitlist).filter_by(
        user_id=user_id
    ).order_by(
        Waitlist.id.desc()
    ).all()

    hires = db.query(VenueHire).filter_by(
        user_id=user_id
    ).order_by(
        VenueHire.id.desc()
    ).all()

    return render_page(
        request,
        "bookings.html",
        bookings=booking_details,
        waitlists=waitlists,
        hires=hires
    )


# ============================================================
# SECTION 11 - CANCEL BOOKING
# ============================================================

@app.post("/bookings/{booking_id}/cancel")
def cancel_booking(
    booking_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db=Depends(get_db)
):

    user_id = require_role(
        request,
        "attendee"
    )

    verify_csrf(request, csrf_token)

    booking = db.get(Booking, booking_id)

    if not booking:

        raise HTTPException(
            404,
            "Booking not found"
        )

    if booking.user_id != user_id:

        raise HTTPException(
            403,
            "Access denied"
        )

    response = send_request(
        db,
        "BookingAgent",
        "cancel_booking",
        booking_id=booking_id,
        user_id=user_id
    )

    if response.performative == "AGREE":

        return redirect("/bookings")

    return render_page(
        request,
        "result.html",
        result=response.model_dump()
    )


# ============================================================
# SECTION 12 - DIGITAL QR CODE GENERATION
# ============================================================

@app.get("/tickets/{ticket_id}/qr")
def generate_ticket_qr(
    ticket_id: int,
    request: Request,
    db=Depends(get_db)
):

    user_id = require_role(
        request,
        "attendee"
    )

    ticket = db.get(Ticket, ticket_id)

    if not ticket:

        raise HTTPException(
            404,
            "Ticket not found"
        )

    booking = db.get(
        Booking,
        ticket.booking_id
    )

    if booking.user_id != user_id:

        raise HTTPException(
            403,
            "Access denied"
        )

    if booking.status != "confirmed":

        raise HTTPException(
            400,
            "Booking is not active"
        )

    qr_image = qrcode.make(ticket.code)

    buffer = io.BytesIO()

    qr_image.save(
        buffer,
        format="PNG"
    )

    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store"
        }
    )


# ============================================================
# SECTION 13 - STAFF SCANNER
# ============================================================

@app.get("/scanner")
def scanner_page(
    request: Request,
    db=Depends(get_db)
):

    require_role(request, "staff")

    events = db.query(Event).filter_by(
        status="published"
    ).all()

    return render_page(
        request,
        "scanner.html",
        events=events
    )


@app.post("/scan")
def scan_ticket(
    request: Request,
    event_id: int = Form(...),
    code: str = Form(...),
    csrf_token: str = Form(...),
    db=Depends(get_db)
):

    staff_id = require_role(
        request,
        "staff"
    )

    verify_csrf(request, csrf_token)

    response = send_request(
        db,
        "TicketAgent",
        "scan",
        event_id=event_id,
        code=code.strip(),
        staff_id=staff_id
    )

    return response.model_dump()


# ============================================================
# SECTION 14 - PRIVATE VENUE HIRE PAGE
# ============================================================

@app.get("/venues")
def private_venues(
    request: Request,
    db=Depends(get_db)
):

    venues = db.query(Venue).filter_by(
        private_hire=True
    ).all()

    return render_page(
        request,
        "venues.html",
        venues=venues
    )


# ============================================================
# SECTION 15 - REQUEST PRIVATE VENUE HIRE
# ============================================================

@app.post("/hire")
def request_venue_hire(
    request: Request,
    venue_id: int = Form(...),
    start_time: str = Form(...),
    end_time: str = Form(...),
    purpose: str = Form(...),
    guest_count: int = Form(...),
    csrf_token: str = Form(...),
    db=Depends(get_db)
):

    user_id = require_role(
        request,
        "attendee"
    )

    verify_csrf(request, csrf_token)

    start = parse_datetime(start_time)
    end = parse_datetime(end_time)

    response = send_request(
        db,
        "VenueAgent",
        "hire",
        user_id=user_id,
        venue_id=venue_id,
        start=start.isoformat(),
        end=end.isoformat(),
        purpose=purpose,
        guest_count=guest_count
    )

    return render_page(
        request,
        "result.html",
        result=response.model_dump()
    )


# ============================================================
# SECTION 16 - APPROVE OR REJECT VENUE HIRE
# ============================================================

@app.post("/admin/hires/{hire_id}/decision")
def decide_venue_hire(
    hire_id: int,
    request: Request,
    decision: str = Form(...),
    csrf_token: str = Form(...),
    db=Depends(get_db)
):

    admin_id = require_role(
        request,
        "admin"
    )

    verify_csrf(request, csrf_token)

    if decision not in ["approve", "reject"]:

        raise HTTPException(
            400,
            "Invalid decision"
        )

    response = send_request(
        db,
        "VenueAgent",
        "hire_decision",
        hire_id=hire_id,
        decision=decision,
        admin_id=admin_id
    )

    if response.performative == "AGREE":

        return redirect("/admin")

    return render_page(
        request,
        "result.html",
        result=response.model_dump()
    )


# ============================================================
# SECTION 17 - JOIN WAITLIST
# ============================================================

@app.post("/waitlist/join")
def join_waitlist(
    request: Request,
    category_id: int = Form(...),
    csrf_token: str = Form(...),
    quantity: int = Form(1),
    db=Depends(get_db)
):

    user_id = require_role(
        request,
        "attendee"
    )

    verify_csrf(request, csrf_token)

    if quantity < 1 or quantity > 6:

        raise HTTPException(
            400,
            "Quantity must be between 1 and 6"
        )

    category = db.get(
        TicketCategory,
        category_id
    )

    if not category:

        raise HTTPException(
            404,
            "Ticket category not found"
        )

    response = send_request(
        db,
        "WaitlistAgent",
        "join",
        user_id=user_id,
        category_id=category_id,
        quantity=quantity
    )

    if response.performative == "AGREE":

        return redirect("/bookings")

    return render_page(
        request,
        "result.html",
        result=response.model_dump()
    )


# ============================================================
# SECTION 18 - CLAIM WAITLIST OFFER
# ============================================================

@app.post("/waitlist/claim")
def claim_waitlist(
    request: Request,
    waitlist_id: int = Form(...),
    csrf_token: str = Form(...),
    db=Depends(get_db)
):

    user_id = require_role(
        request,
        "attendee"
    )

    verify_csrf(request, csrf_token)

    entry = db.get(
        Waitlist,
        waitlist_id
    )

    if not entry:

        raise HTTPException(
            404,
            "Waitlist entry not found"
        )

    if entry.user_id != user_id:

        raise HTTPException(
            403,
            "Access denied"
        )

    response = send_request(
        db,
        "WaitlistAgent",
        "claim",
        waitlist_id=waitlist_id,
        user_id=user_id
    )

    if response.performative == "AGREE":

        return redirect("/bookings")

    return render_page(
        request,
        "result.html",
        result=response.model_dump()
    )


# ============================================================
# SECTION 19 - USER NOTIFICATIONS
# ============================================================

@app.get("/notifications")
def notification_page(
    request: Request,
    db=Depends(get_db)
):

    user_id = request.session.get(
        "user_id"
    )

    if not user_id:

        return redirect("/login")

    notifications = db.query(
        Notification
    ).filter_by(
        user_id=user_id
    ).order_by(
        Notification.id.desc()
    ).all()

    return render_page(
        request,
        "notifications.html",
        notifications=notifications
    )


@app.post("/notifications/{notification_id}/read")
def read_notification(
    notification_id: int,
    request: Request,
    csrf_token: str = Form(...),
    db=Depends(get_db)
):

    verify_csrf(request, csrf_token)

    user_id = request.session.get(
        "user_id"
    )

    if not user_id:

        raise HTTPException(
            401,
            "Login required"
        )

    notification = db.get(
        Notification,
        notification_id
    )

    if not notification:

        raise HTTPException(
            404,
            "Notification not found"
        )

    if notification.user_id != user_id:

        raise HTTPException(
            403,
            "Access denied"
        )

    notification.is_read = True

    db.commit()

    return redirect("/notifications")


# ============================================================
# SECTION 20 - AGENT MESSAGE LOGS
# ============================================================

@app.get("/admin/logs")
def agent_logs(
    request: Request,
    conversation_id: str = "",
    db=Depends(get_db)
):

    require_role(request, "admin")

    query = db.query(AgentMessage)

    if conversation_id:

        query = query.filter_by(
            conversation_id=conversation_id
        )

    logs = query.order_by(
        AgentMessage.id.desc()
    ).limit(500).all()

    return render_page(
        request,
        "logs.html",
        logs=logs,
        conversation_id=conversation_id
    )


# ============================================================
# SECTION 21 - HEALTH CHECK
# ============================================================

@app.get("/health")
def health_check():

    return {
        "status": "running",
        "application": "EventSphere",
        "agents": [
            "VenueAgent",
            "BookingAgent",
            "TicketAgent",
            "WaitlistAgent",
            "NotificationAgent"
        ]
    }


# ============================================================
# SECTION 22 - RUN APPLICATION
# ============================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000"))
    )
