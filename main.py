import os
import asyncio

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from database import Base, engine, get_db, SessionLocal
from models import User, Event, TicketCategory
from auth import (
    hash_password,
    verify_password,
    require_role,
    create_csrf_token,
    verify_csrf
)

from agents.message_bus import Message, bus
from agents.venue_agent import VenueAgent
from agents.booking_agent import BookingAgent
from agents.ticket_agent import TicketAgent
from agents.waitlist_agent import WaitlistAgent
from agents.notification_agent import NotificationAgent


bus.register("VenueAgent", VenueAgent())
bus.register("BookingAgent", BookingAgent())
bus.register("TicketAgent", TicketAgent())
bus.register("WaitlistAgent", WaitlistAgent())
bus.register("NotificationAgent", NotificationAgent())


def run_sweep():

    with SessionLocal() as db:

        try:
            bus.send(
                Message(
                    sender="Scheduler",
                    receiver="WaitlistAgent",
                    performative="REQUEST",
                    content={"action": "expiry_sweep"}
                ),
                db
            )

            db.commit()

        except Exception:
            db.rollback()
            raise


async def waitlist_timer():

    while True:

        await asyncio.sleep(30)

        try:
            await asyncio.to_thread(run_sweep)

        except Exception:
            import logging
            logging.exception("Waitlist sweep failed")


@asynccontextmanager
async def lifespan(app):

    Base.metadata.create_all(bind=engine)

    task = asyncio.create_task(waitlist_timer())

    yield

    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="EventSphere",
    lifespan=lifespan
)

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ["SECRET_KEY"],
    https_only=os.getenv("PRODUCTION") == "true",
    same_site="lax"
)

templates = Jinja2Templates(
    directory="templates"
)


@app.get("/")
def home(
    request: Request,
    q: str = "",
    category: str = "",
    db=Depends(get_db)
):

    query = db.query(Event).filter_by(
        status="published"
    )

    if q:
        query = query.filter(
            Event.title.ilike(f"%{q}%")
        )

    if category:
        query = query.filter_by(category=category)

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"events": query.all()}
    )


@app.get("/register")
def register_page(request: Request):

    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={
            "csrf_token": create_csrf_token(request)
        }
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

    email = email.strip().lower()

    if db.query(User).filter_by(email=email).first():
        raise HTTPException(400, "Email already registered")

    if len(password) < 8:
        raise HTTPException(400, "Password too short")

    user = User(
        name=name,
        email=email,
        password_hash=hash_password(password),
        role="attendee"
    )

    db.add(user)
    db.commit()

    return RedirectResponse("/login", status_code=303)


@app.get("/login")
def login_page(request: Request):

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "csrf_token": create_csrf_token(request)
        }
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
        password, user.password_hash
    ):
        raise HTTPException(401, "Invalid credentials")

    request.session.clear()

    request.session["user_id"] = user.id
    request.session["role"] = user.role

    if user.role == "admin":
        target = "/admin"
    elif user.role == "staff":
        target = "/scanner"
    else:
        target = "/"

    return RedirectResponse(target, status_code=303)


@app.get("/events/{event_id}")
def event_page(
    event_id: int,
    request: Request,
    db=Depends(get_db)
):

    event = db.get(Event, event_id)

    if not event or event.status != "published":
        raise HTTPException(404, "Event not found")

    categories = db.query(TicketCategory).filter_by(
        event_id=event_id
    ).all()

    return templates.TemplateResponse(
        request=request,
        name="event.html",
        context={
            "event": event,
            "categories": categories,
            "csrf_token": create_csrf_token(request)
        }
    )


@app.post("/book")
def book(
    request: Request,
    category_id: int = Form(...),
    quantity: int = Form(...),
    csrf_token: str = Form(...),
    db=Depends(get_db)
):

    user_id = require_role(request, "attendee")

    verify_csrf(request, csrf_token)

    response = bus.send(
        Message(
            sender="WebAPI",
            receiver="BookingAgent",
            performative="REQUEST",
            content={
                "action": "book",
                "user_id": user_id,
                "category_id": category_id,
                "quantity": quantity
            }
        ),
        db
    )

    db.commit()

    return response.model_dump()