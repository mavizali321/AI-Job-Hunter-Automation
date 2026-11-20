"""Authentication — phone+OTP registration, JWT tokens, user middleware."""

import hashlib
import random
import string
from datetime import datetime, timedelta, timezone

from jose import jwt, JWTError
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 72
OTP_EXPIRY_MINUTES = 10
MAX_OTP_ATTEMPTS = 5

security = HTTPBearer(auto_error=False)


def generate_otp() -> str:
    return "".join(random.choices(string.digits, k=6))


def create_access_token(user_id: int, phone: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload = {
        "sub": str(user_id),
        "phone": phone,
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    token = None

    if credentials:
        token = credentials.credentials
    else:
        token = request.cookies.get("access_token")

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    payload = decode_access_token(token)
    user_id = int(payload["sub"])

    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found or deactivated")
    if not user.is_verified:
        raise HTTPException(status_code=403, detail="Phone not verified")

    return user


def get_optional_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
) -> User | None:
    token = None
    if credentials:
        token = credentials.credentials
    else:
        token = request.cookies.get("access_token")

    if not token:
        return None

    try:
        payload = decode_access_token(token)
        user_id = int(payload["sub"])
        return db.query(User).filter(User.id == user_id, User.is_active == True).first()
    except Exception:
        return None


def register_user(db: Session, phone: str, name: str, email: str = "") -> tuple[User, str]:
    phone = phone.strip().replace(" ", "")
    if not phone.startswith("+"):
        phone = f"+{phone}"

    existing = db.query(User).filter(User.phone == phone).first()
    if existing and existing.is_verified:
        raise HTTPException(status_code=409, detail="Phone already registered")

    if existing and not existing.is_verified:
        user = existing
        user.name = name
        if email:
            user.email = email
    else:
        user = User(
            phone=phone,
            name=name,
            email=email or None,
            whatsapp_number=phone,
            is_verified=False,
        )
        db.add(user)

    otp = generate_otp()
    user.otp_code = hashlib.sha256(otp.encode()).hexdigest()
    user.otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINUTES)
    user.otp_attempts = 0
    db.commit()

    return user, otp


def verify_otp(db: Session, phone: str, otp: str) -> tuple[User, str]:
    phone = phone.strip().replace(" ", "")
    if not phone.startswith("+"):
        phone = f"+{phone}"

    user = db.query(User).filter(User.phone == phone).first()
    if not user:
        raise HTTPException(status_code=404, detail="Phone not found — register first")

    if not user.otp_code or not user.otp_expires_at:
        raise HTTPException(status_code=400, detail="No OTP pending — request a new one")

    if user.otp_attempts >= MAX_OTP_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many attempts — request a new OTP")

    now = datetime.now(timezone.utc)
    expires = user.otp_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < now:
        raise HTTPException(status_code=400, detail="OTP expired — request a new one")

    otp_hash = hashlib.sha256(otp.encode()).hexdigest()
    if otp_hash != user.otp_code:
        user.otp_attempts += 1
        db.commit()
        remaining = MAX_OTP_ATTEMPTS - user.otp_attempts
        raise HTTPException(status_code=400, detail=f"Invalid OTP — {remaining} attempts remaining")

    user.is_verified = True
    user.otp_code = None
    user.otp_expires_at = None
    user.otp_attempts = 0
    user.last_login_at = now
    db.commit()

    token = create_access_token(user.id, user.phone)
    return user, token


def resend_otp(db: Session, phone: str) -> tuple[User, str]:
    phone = phone.strip().replace(" ", "")
    if not phone.startswith("+"):
        phone = f"+{phone}"

    user = db.query(User).filter(User.phone == phone).first()
    if not user:
        raise HTTPException(status_code=404, detail="Phone not found — register first")

    otp = generate_otp()
    user.otp_code = hashlib.sha256(otp.encode()).hexdigest()
    user.otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINUTES)
    user.otp_attempts = 0
    db.commit()

    return user, otp


def login_with_otp(db: Session, phone: str) -> tuple[User, str]:
    phone = phone.strip().replace(" ", "")
    if not phone.startswith("+"):
        phone = f"+{phone}"

    user = db.query(User).filter(User.phone == phone, User.is_verified == True).first()
    if not user:
        raise HTTPException(status_code=404, detail="No verified account for this phone")

    otp = generate_otp()
    user.otp_code = hashlib.sha256(otp.encode()).hexdigest()
    user.otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINUTES)
    user.otp_attempts = 0
    db.commit()

    return user, otp
