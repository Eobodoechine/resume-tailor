"""
Shared slowapi rate limiter instance.

Import this module in main.py (to attach to the app) and in any router
that needs @limiter.limit() decorators — avoids circular imports.

Proxy-aware key function:
  Render (and most PaaS hosts) sit behind a reverse proxy and set
  X-Forwarded-For with the real client IP.  Using the raw REMOTE_ADDR
  would key every request on the proxy's internal IP, making the rate
  limit useless.  We read the leftmost (client) IP from X-Forwarded-For
  instead, falling back to REMOTE_ADDR for direct connections.
"""
from slowapi import Limiter
from slowapi.util import get_remote_address as _base


def _get_real_ip(request) -> str:
    """Return the client IP, honouring X-Forwarded-For set by the proxy."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        # Header may be "client, proxy1, proxy2" — leftmost is the origin.
        return forwarded.split(",")[0].strip()
    return _base(request)


limiter = Limiter(key_func=_get_real_ip)
