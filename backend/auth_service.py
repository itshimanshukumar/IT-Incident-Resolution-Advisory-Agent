import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List
import jwt
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError
from fastapi import Header, HTTPException, Depends, status

from backend.config import settings
from backend.database import get_connection, hash_password, verify_password_hash, log_audit
from backend.models import UserResponse, LoginRequest

# In-memory login attempt throttling tracker
class LoginRateLimiter:
    def __init__(self, max_attempts: int = 5, window_seconds: int = 300):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._failures: Dict[str, List[float]] = {}

    def is_rate_limited(self, identifier: str) -> bool:
        now = time.time()
        cutoff = now - self.window_seconds
        attempts = [t for t in self._failures.get(identifier, []) if t > cutoff]
        self._failures[identifier] = attempts
        return len(attempts) >= self.max_attempts

    def record_failure(self, identifier: str):
        now = time.time()
        cutoff = now - self.window_seconds
        attempts = [t for t in self._failures.get(identifier, []) if t > cutoff]
        attempts.append(now)
        self._failures[identifier] = attempts

    def reset(self, identifier: str):
        self._failures.pop(identifier, None)

login_limiter = LoginRateLimiter(max_attempts=5, window_seconds=300)

def create_access_token(user_id: str, role: str, name: str = "", department: str = "", location: str = "") -> str:
    """Generate a signed JWT token containing verified user principal claims."""
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=settings.SESSION_EXPIRE_MINUTES)
    payload = {
        "sub": user_id,
        "role": role,
        "name": name,
        "department": department,
        "location": location,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": str(uuid.uuid4())
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)

def decode_access_token(token: str) -> dict:
    """Verify and decode a signed JWT token."""
    return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])

def authenticate_user(login_data: LoginRequest, db_path=None) -> Optional[UserResponse]:
    """
    Validates user credentials with Argon2id password hashing, applies rate limiting,
    enforces portal role separation, performs safe hash migration, and returns UserResponse with JWT token.
    """
    emp_id = login_data.employee_id.strip()
    pwd = login_data.password.strip()
    portal = login_data.portal.strip().lower()

    if login_limiter.is_rate_limited(emp_id):
        log_audit(emp_id, "login_throttled", "auth", "FAILED", f"User {emp_id} exceeded maximum login attempts", db_path)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Please wait 5 minutes before trying again."
        )

    conn = get_connection(db_path)
    cur = conn.cursor()
    cur.execute("SELECT id, password_hash, name, department, location, role FROM users WHERE id = ?", (emp_id,))
    user = cur.fetchone()

    # User existence failure (generic audit log and failure)
    if not user:
        login_limiter.record_failure(emp_id)
        conn.close()
        log_audit(emp_id, "login_failed", "auth", "FAILED", "Invalid credentials", db_path)
        return None

    # Verify password hash (Argon2id or legacy fallback)
    stored_hash = user["password_hash"]
    if not verify_password_hash(pwd, stored_hash):
        login_limiter.record_failure(emp_id)
        conn.close()
        log_audit(emp_id, "login_failed", "auth", "FAILED", "Invalid credentials", db_path)
        return None

    # Safe transparent migration: If hash is legacy SHA-256, upgrade to Argon2id in database
    if not stored_hash.startswith("$argon2"):
        new_argon2 = hash_password(pwd)
        cur.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_argon2, emp_id))
        conn.commit()

    conn.close()

    # Enforce portal role separation
    is_it_portal = (portal == "it")
    is_it_user = (user["role"].lower() == "it")
    if is_it_portal != is_it_user:
        login_limiter.record_failure(emp_id)
        log_audit(emp_id, "login_denied", "auth", "FAILED", f"Unauthorized portal role access attempt to {portal}", db_path)
        return None

    # Success: reset failed attempt counter
    login_limiter.reset(emp_id)
    log_audit(emp_id, "login_success", "auth", "SUCCESS", f"User signed into {portal} portal", db_path)

    # Issue signed session JWT
    token = create_access_token(
        user_id=user["id"],
        role=user["role"],
        name=user["name"],
        department=user["department"],
        location=user["location"]
    )

    return UserResponse(
        id=user["id"],
        name=user["name"],
        department=user["department"],
        location=user["location"],
        role=user["role"],
        access_token=token,
        token_type="bearer"
    )

# ----------------- FastApi Auth Dependencies -----------------

def get_current_user_optional(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    x_auth_token: Optional[str] = Header(None, alias="x-auth-token")
) -> Optional[UserResponse]:
    """
    Extracts and verifies JWT token if present. Returns UserResponse or None.
    Raises 401 if a token was supplied but is expired, invalid, or tampered.
    """
    token = None
    if authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
        elif len(parts) == 1:
            token = parts[0].strip()
    elif x_auth_token:
        token = x_auth_token.strip()

    if not token:
        return None

    try:
        payload = decode_access_token(token)
        return UserResponse(
            id=payload["sub"],
            name=payload.get("name", ""),
            department=payload.get("department", ""),
            location=payload.get("location", ""),
            role=payload.get("role", "employee"),
            access_token=token,
            token_type="bearer"
        )
    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Please log in again."
        )
    except InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or tampered authentication token."
        )

def get_authenticated_user(
    user: Optional[UserResponse] = Depends(get_current_user_optional)
) -> UserResponse:
    """Requires verified authentication. Returns UserResponse or raises 401."""
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please provide a valid Bearer token."
        )
    return user

def require_it_role(
    user: UserResponse = Depends(get_authenticated_user)
) -> UserResponse:
    """Requires IT staff role. Raises 401 if not authenticated, 403 if not IT."""
    if user.role.lower() != "it":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: IT department administrative authorization required."
        )
    return user

def require_employee_or_it(
    user: UserResponse = Depends(get_authenticated_user)
) -> UserResponse:
    """Requires employee or IT staff role."""
    if user.role.lower() not in ("employee", "it"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access forbidden: Insufficient privileges."
        )
    return user
