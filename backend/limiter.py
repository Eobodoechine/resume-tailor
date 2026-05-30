"""
Shared slowapi rate limiter instance.

Import this module in main.py (to attach to the app) and in any router
that needs @limiter.limit() decorators — avoids circular imports.

Proxy-aware key function:
  Render sits behind a reverse proxy that APPENDS the real client IP to
  X-Forwarded-For before the request reaches the app.  The header format is:

      X-Forwarded-For: <client-supplied-entries>, <real-client-ip>

  Taking the LEFTMOST entry (as we did before) is spoofable — any client can
  set X-Forwarded-For: 1.2.3.4 and rotate through unlimited IPs to bypass
  the rate limiter.  Taking the RIGHTMOST entry is safe because Render's
  proxy controls that value; the client cannot influence it.

  For direct connections (local dev, no proxy), the header is absent and we
  fall back to REMOTE_ADDR which is the actual connecting socket address.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address as _base


def _get_real_ip(request) -> str:
    """
    Return the real client IP, safe against X-Forwarded-For spoofing.

    Render's reverse proxy appends the connecting client's IP as the
    rightmost entry in X-Forwarded-For.  Client-supplied entries are to
    the left and must never be trusted for rate-limiting purposes.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        # Rightmost entry = what the last trusted proxy (Render) appended.
        # Client-supplied spoofed IPs are to the left and are ignored.
        return forwarded.split(",")[-1].strip()
    return _base(request)


limiter = Limiter(key_func=_get_real_ip)
