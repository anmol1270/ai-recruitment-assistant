"""
Authentication module — Google OAuth2 + JWT session tokens.
"""

from __future__ import annotations

import httpx
import structlog
from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import jwt, JWTError
from fastapi import HTTPException, Request, Response

log = structlog.get_logger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

JWT_ALGORITHM = "HS256"
JWT_EXPIRE_DAYS = 30
COOKIE_NAME = "session_token"


class AuthManager:
    """Handles Google OAuth flow and JWT session management."""

    def __init__(
        self,
        google_client_id: str,
        google_client_secret: str,
        jwt_secret: str,
        base_url: str,
    ):
        self.google_client_id = google_client_id
        self.google_client_secret = google_client_secret
        self.jwt_secret = jwt_secret
        self.base_url = base_url.rstrip("/")
        self.redirect_uri = f"{self.base_url}/auth/callback"

    def get_login_url(self, state: str = "") -> str:
        """Generate Google OAuth login URL."""
        params = {
            "client_id": self.google_client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "offline",
            "prompt": "consent",
        }
        if state:
            params["state"] = state
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{GOOGLE_AUTH_URL}?{qs}"

    async def exchange_code(self, code: str) -> dict:
        """Exchange authorization code for user info."""
        async with httpx.AsyncClient() as client:
            # Exchange code for tokens
            resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": self.google_client_id,
                    "client_secret": self.google_client_secret,
                    "redirect_uri": self.redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            if resp.status_code != 200:
                log.error("google_token_exchange_failed", status=resp.status_code, body=resp.text)
                raise HTTPException(status_code=401, detail="Failed to authenticate with Google")

            tokens = resp.json()
            access_token = tokens["access_token"]

            # Fetch user info
            resp = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=401, detail="Failed to get user info")

            return resp.json()

    def create_session_token(self, user_id: int, email: str) -> str:
        """Create a JWT session token."""
        payload = {
            "sub": str(user_id),
            "email": email,
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(days=JWT_EXPIRE_DAYS),
        }
        return jwt.encode(payload, self.jwt_secret, algorithm=JWT_ALGORITHM)

    def verify_session_token(self, token: str) -> Optional[dict]:
        """Verify and decode JWT. Returns payload or None."""
        try:
            payload = jwt.decode(token, self.jwt_secret, algorithms=[JWT_ALGORITHM])
            return payload
        except JWTError:
            return None

    def set_session_cookie(self, response: Response, token: str) -> None:
        """Set session cookie on response."""
        response.set_cookie(
            key=COOKIE_NAME,
            value=token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=JWT_EXPIRE_DAYS * 86400,
            path="/",
        )

    def clear_session_cookie(self, response: Response) -> None:
        """Clear session cookie."""
        response.delete_cookie(key=COOKIE_NAME, path="/")

    def get_current_user_id(self, request: Request) -> Optional[int]:
        """Extract user ID from session cookie. Returns None if not authenticated."""
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return None
        payload = self.verify_session_token(token)
        if not payload:
            return None
        try:
            return int(payload["sub"])
        except (KeyError, ValueError):
            return None

    def require_auth(self, request: Request) -> int:
        """Extract user ID or raise 401."""
        user_id = self.get_current_user_id(request)
        if user_id is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return user_id
