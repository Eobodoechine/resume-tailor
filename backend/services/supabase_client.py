from supabase import create_client, Client
from config import SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_KEY

# Module-level singleton for the admin (service-role) client.
# Initialized once at import time — safe to share across threads because
# supabase-py's httpx transport is thread-safe for concurrent reads and
# we never call .auth.set_session() on this client.
# No lru_cache needed: a module global is simpler and avoids the lru_cache
# thread-safety footgun where the cache lock and internal client state
# could interact under high concurrency.
_admin_client: Client | None = None

# Anon client — for unauthenticated auth calls (e.g. sign_in_with_otp)
def get_anon_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

# Standard client (respects RLS — use for user-scoped operations)
def get_client(user_token: str) -> Client:
    """
    Return a Supabase client that forwards the user's JWT to PostgREST,
    so RLS policies like `auth.uid() = user_id` are enforced.
    Use this for all SELECT/INSERT/UPDATE/DELETE operations scoped to a
    single user. Reserve get_admin_client() for truly cross-user ops.
    Using .postgrest.auth() avoids the empty-refresh-token issue with
    client.auth.set_session() in supabase-py 2.x.

    A fresh client is created per request because it carries per-user JWT state.
    """
    client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    client.postgrest.auth(user_token)
    return client

# Admin client (bypasses RLS — use only for admin operations)
def get_admin_client() -> Client:
    """
    Returns the shared admin Supabase client (service role key — bypasses RLS).
    The client is a module-level singleton created on first call and reused
    for the lifetime of the process. Do NOT call .auth.set_session() on it.

    Key-rotation caveat: if SUPABASE_SERVICE_KEY is rotated while the process
    is running, the cached client will keep using the old key until the process
    is restarted. Rotate keys during a deploy rather than in-flight.
    """
    global _admin_client
    if _admin_client is None:
        _admin_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _admin_client

# Verify a bearer token and return the user object
def get_user_from_token(token: str):
    try:
        admin = get_admin_client()
        result = admin.auth.get_user(token)
        return result.user if result else None
    except Exception:
        # Malformed JWT, expired token, or network error — treat as unauthenticated
        return None
