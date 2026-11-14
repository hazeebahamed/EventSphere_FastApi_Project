from pwdlib import PasswordHash
from fastapi import HTTPException
import secrets

password_hasher = PasswordHash.recommended()


def hash_password(password):

    return password_hasher.hash(password)


def verify_password(password, hashed_password):

    return password_hasher.verify(
        password, hashed_password
    )


def require_role(request, role):

    if request.session.get("role") != role:

        raise HTTPException(
            status_code=403,
            detail="Access denied"
        )

    return request.session.get("user_id")


def create_csrf_token(request):

    if "csrf_token" not in request.session:

        request.session["csrf_token"] = (
            secrets.token_urlsafe(32)
        )

    return request.session["csrf_token"]


def verify_csrf(request, token):

    expected = request.session.get("csrf_token", "")

    if not expected or not secrets.compare_digest(
        expected, token
    ):

        raise HTTPException(
            status_code=403,
            detail="Invalid CSRF token"
        )