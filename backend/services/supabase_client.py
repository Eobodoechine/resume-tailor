from supabase import create_client, Client
from config import SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_KEY

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
    """
    client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    client.postgrest.auth(user_token)
    return client

# Admin client (bypasses RLS — use only for admin operations)
def get_admin_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# Verify a bearer token and return the user object
def get_user_from_token(token: str):
    try:
        admin = get_admin_client()
        result = admin.auth.get_user(token)
        return result.user if result else None
    except Exception:
        # Malformed JWT, expired token, or network error — treat as unauthenticated
        return None
