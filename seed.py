import os

from database import Base, engine, SessionLocal
from models import User
from auth import hash_password

Base.metadata.create_all(bind=engine)

accounts = [
    ("Administrator", "admin@example.com", "admin"),
    ("Gate Staff", "staff@example.com", "staff"),
    ("Attendee", "student@example.com", "attendee")
]

with SessionLocal() as db:

    for name, email, role in accounts:

        password = os.environ[
            role.upper() + "_DEMO_PASSWORD"
        ]

        existing = db.query(User).filter_by(
            email=email
        ).first()

        if existing is None:

            db.add(User(
                name=name,
                email=email,
                password_hash=hash_password(password),
                role=role
            ))

    db.commit()

print("Demo accounts created")