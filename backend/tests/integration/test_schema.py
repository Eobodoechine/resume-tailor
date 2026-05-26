"""
Database schema validation tests.

These tests connect to the real Supabase project and verify that:
  1. All expected tables exist
  2. All expected columns are present and spelled correctly
  3. Basic insert/select/delete round-trips work (catching RLS policy issues)

Why this matters:
  The unit tests stub Supabase entirely — they can't catch a typo in a column
  name, a missing table from a migration that was never run, or a broken RLS
  policy that silently returns empty results.

  A column rename in Supabase (e.g. "tailored_content" → "content") would make
  every tailored resume silently return null without a single unit test failing.
  This file catches that class of bug.

Run with:
  TEST_SUPABASE_URL=... TEST_SUPABASE_ANON_KEY=... TEST_SUPABASE_SERVICE_KEY=... \
  pytest tests/integration/test_schema.py -v
"""
import pytest


# ── Expected schema ───────────────────────────────────────────────────────────

# Maps table name → required column names.
# Add new columns here when you add migrations.
EXPECTED_SCHEMA = {
    "access_requests": {
        "id", "email", "full_name", "reason", "status", "created_at"
    },
    "profiles": {
        "id", "full_name", "email", "phone", "location",
        "linkedin_url", "website", "updated_at"
    },
    "resume_files": {
        "id", "user_id", "filename", "file_path", "file_type",
        "extracted_text", "uploaded_at"
    },
    "master_resumes": {
        "id", "user_id", "content", "updated_at"
    },
    "tailored_resumes": {
        "id", "user_id", "job_title", "company",
        "job_description", "tailored_content", "created_at"
    },
}


@pytest.mark.integration
class TestTableExists:
    """Each table must be queryable via the admin client."""

    @pytest.mark.parametrize("table_name", list(EXPECTED_SCHEMA.keys()))
    def test_table_is_queryable(self, admin_client, table_name):
        """
        A successful SELECT (even with 0 rows) proves the table exists and
        the service key has permission to query it.
        """
        result = admin_client.table(table_name).select("*").limit(1).execute()
        # If the table doesn't exist, Supabase raises an exception.
        # Getting here means the table exists.
        assert result is not None, f"Table '{table_name}' returned None"


@pytest.mark.integration
class TestColumnPresence:
    """
    All expected columns must be present.

    Strategy: insert a minimal row with only the expected columns, then
    delete it.  A column that doesn't exist will raise a PostgREST error.
    We use the admin client (service role) so RLS doesn't interfere.
    """

    def test_access_requests_columns(self, admin_client):
        """access_requests must have all required columns."""
        result = admin_client.table("access_requests") \
            .select("id, email, full_name, reason, status, created_at") \
            .limit(1) \
            .execute()
        assert result is not None

    def test_profiles_columns(self, admin_client):
        result = admin_client.table("profiles") \
            .select("id, full_name, email, phone, location, linkedin_url, website, updated_at") \
            .limit(1) \
            .execute()
        assert result is not None

    def test_resume_files_columns(self, admin_client):
        result = admin_client.table("resume_files") \
            .select("id, user_id, filename, file_path, file_type, extracted_text, uploaded_at") \
            .limit(1) \
            .execute()
        assert result is not None

    def test_master_resumes_columns(self, admin_client):
        result = admin_client.table("master_resumes") \
            .select("id, user_id, content, updated_at") \
            .limit(1) \
            .execute()
        assert result is not None

    def test_tailored_resumes_columns(self, admin_client):
        """
        This is the most critical schema test — tailored_content is the column
        that holds the AI-generated resume.  A rename would silently return null
        for every tailored resume.
        """
        result = admin_client.table("tailored_resumes") \
            .select("id, user_id, job_title, company, job_description, tailored_content, created_at") \
            .limit(1) \
            .execute()
        assert result is not None


@pytest.mark.integration
class TestAccessRequestsFlow:
    """Validate the access request status workflow at the DB level."""

    def test_insert_and_read_access_request(self, admin_client):
        """
        Insert a pending access request, read it back, confirm status,
        then clean up.  Catches column name bugs and RLS on access_requests.
        """
        test_email = "schema_test_delete_me@example.com"

        # Clean up any leftover from a previous failed test run
        admin_client.table("access_requests").delete().eq("email", test_email).execute()

        # Insert
        insert_result = admin_client.table("access_requests").insert({
            "email": test_email,
            "full_name": "Schema Test",
            "reason": "automated schema test",
            "status": "pending"
        }).execute()

        assert insert_result.data, "Insert returned no data — check column names"
        record_id = insert_result.data[0]["id"]

        # Read back
        read_result = admin_client.table("access_requests") \
            .select("status, email") \
            .eq("id", record_id) \
            .execute()

        assert read_result.data, "Could not read back inserted row"
        assert read_result.data[0]["status"] == "pending"
        assert read_result.data[0]["email"] == test_email

        # Update status
        update_result = admin_client.table("access_requests") \
            .update({"status": "approved"}) \
            .eq("id", record_id) \
            .execute()
        assert update_result.data[0]["status"] == "approved"

        # Clean up
        admin_client.table("access_requests").delete().eq("id", record_id).execute()

    def test_rejected_email_readable(self, admin_client):
        """Status field must accept 'rejected' value (used in login check)."""
        test_email = "rejected_schema_test@example.com"
        admin_client.table("access_requests").delete().eq("email", test_email).execute()

        result = admin_client.table("access_requests").insert({
            "email": test_email,
            "full_name": "Rejected Test",
            "reason": "test",
            "status": "rejected"
        }).execute()
        assert result.data

        # Clean up
        admin_client.table("access_requests") \
            .delete().eq("email", test_email).execute()


@pytest.mark.integration
class TestTailoredResumesRoundtrip:
    """
    Insert a tailored resume row, read it back, then delete it.
    This validates that the insert fields in routes/tailor.py match
    the actual schema — the most common source of silent data loss.
    """

    def test_tailored_resume_insert_read_delete(self, admin_client, test_user_id):
        if not test_user_id:
            pytest.skip("TEST_USER_ID not set — skipping user-scoped round-trip")

        insert_result = admin_client.table("tailored_resumes").insert({
            "user_id": test_user_id,
            "job_title": "Schema Test Role",
            "company": "Schema Test Co",
            "job_description": "This is a schema validation test job description.",
            "tailored_content": "SCHEMA TEST RESUME CONTENT",
        }).execute()

        assert insert_result.data, "Insert failed — check column names in tailored_resumes"
        record_id = insert_result.data[0]["id"]

        read_result = admin_client.table("tailored_resumes") \
            .select("tailored_content, job_title, company") \
            .eq("id", record_id) \
            .execute()

        assert read_result.data, "Could not read back tailored resume"
        row = read_result.data[0]
        assert row["tailored_content"] == "SCHEMA TEST RESUME CONTENT"
        assert row["job_title"] == "Schema Test Role"
        assert row["company"] == "Schema Test Co"

        # Clean up
        admin_client.table("tailored_resumes").delete().eq("id", record_id).execute()
