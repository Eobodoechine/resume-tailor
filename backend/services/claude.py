"""
Claude API calls for master resume synthesis and tailoring.
"""
import logging
import time
import warnings
import anthropic
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

logger = logging.getLogger(__name__)

# Sync client for non-streaming calls; kept for backward compat in legacy callers
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
# Async client for streaming endpoints — never blocks the event loop (TD-17)
async_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

API_TIMEOUT        = 60.0          # seconds — raises anthropic.APITimeoutError on breach (TD-01)
MAX_SYNTHESIS_CHARS = 150_000       # ~37k tokens — leaves room for prompt + output (TD-02)


def synthesize_master_resume(resume_texts: list[str], profile: dict) -> str:
    """
    Given a list of raw resume texts and the user's profile info,
    produce a single comprehensive master resume in structured text format.
    """
    # Cap total input to prevent context overflow (TD-02)
    running = 0
    capped: list[str] = []
    for t in resume_texts:
        if running + len(t) > MAX_SYNTHESIS_CHARS:
            logger.warning(
                "synthesize: capped at %d/%d files (%d chars) — "
                "remaining files omitted to stay within context limit",
                len(capped), len(resume_texts), running,
            )
            break
        capped.append(t)
        running += len(t)
    if not capped:               # single file exceeds limit — truncate it hard
        capped = [resume_texts[0][:MAX_SYNTHESIS_CHARS]]

    combined = "\n\n---RESUME FILE---\n\n".join(capped)
    name = profile.get("full_name", "")
    contact_block = _build_contact_block(profile)

    prompt = f"""You are an expert resume writer. Below are one or more resume files uploaded by {name}.
Your job is to synthesize them into a single, comprehensive MASTER RESUME in structured plain text.

CONTENT RULES:
- Include ALL experience, skills, education, certifications, and achievements found across all files
- Remove duplicates but keep the most detailed version of each entry
- Do NOT invent or embellish anything — only use what is explicitly stated
- Write bullet points in strong verb-led format (Managed, Built, Led, Drove, etc.)

STRICT OUTPUT FORMAT:

1. Header (first lines, before any section):
{contact_block}

2. Section headers: EXACTLY these words in ALL CAPS on their own line:
   SUMMARY, EXPERIENCE, SKILLS, EDUCATION, CERTIFICATIONS, PROJECTS (only if present)

3. EXPERIENCE section:
   - Each role header: Job Title | Company Name | Month Year – Month Year  (exactly 2 pipe characters)
   - NO sub-section headers inside a role — only bullet points
   - Every bullet starts with "•"

4. SKILLS section — CRITICAL:
   - Each category on ONE line: Category Name: item1, item2, item3
   - Example: Systems & Tools: CoStar, Oracle EBS, Coupa, Power Automate
   - Do NOT write multi-line paragraphs for skills

5. EDUCATION:
   - Only actual degrees from the resume: Degree | School | Year
   - If no formal degree exists in the source material, omit EDUCATION entirely

Contact info to include at the top:
{contact_block}

Resume files to synthesize:
{combined}

Output the complete master resume now, following the STRICT OUTPUT FORMAT above:"""

    t0 = time.monotonic()
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
        timeout=API_TIMEOUT,
    )
    logger.info(
        "claude[synthesize] email=%s input=%d output=%d ms=%d",
        profile.get("email", "?"),
        message.usage.input_tokens,
        message.usage.output_tokens,
        int((time.monotonic() - t0) * 1000),
    )
    return message.content[0].text


def _build_tailor_prompt(
    name: str,
    contact_block: str,
    target: str,
    master_resume: str,
    job_description: str,
    max_roles: int = 3,
) -> str:
    """
    Single source of truth for the tailoring prompt.
    Called by both tailor_resume() and stream_tailor_resume*() so they
    can never silently drift apart.

    max_roles controls how many EXPERIENCE roles to include.  Default is 3
    (keeps the output to one page).  Callers can raise it if the user asks
    for more roles explicitly.
    """
    return f"""You are an expert resume writer and career strategist. Your task is to tailor {name}'s master resume for {target}.

CONTENT RULES:
- Select and emphasize the experience, skills, and achievements MOST relevant to the job description
- Reorder bullet points within each role to lead with the most relevant ones
- Adjust the summary to directly address what the employer is looking for
- Do NOT fabricate, invent, or add anything not in the master resume
- Include ONLY the {max_roles} most relevant roles in the EXPERIENCE section — pick the jobs that best match the job description and omit the rest
- Within each included role, trim less-relevant bullets so the overall resume fits one page
- Match terminology from the job description naturally (don't keyword-stuff)
- Keep it to one page worth of content (approximately 600-750 words of body text)

STRICT OUTPUT FORMAT — follow exactly or the PDF renderer will break:

1. HEADER (first 1-2 lines, before any section):
{contact_block}

2. Section headers must be EXACTLY these words in ALL CAPS on their own line:
   SUMMARY
   EXPERIENCE
   SKILLS
   EDUCATION
   CERTIFICATIONS  (only if present in master)

3. EXPERIENCE section rules:
   - Each role header MUST use exactly this format with TWO pipe characters:
     Job Title | Company Name | Month Year – Month Year
     Example: Property Tax Specialist | United Parcel Service (UPS) | June 2022 – Present
   - NO sub-section headers (like "WORKFLOW DISCOVERY" or "KEY ACHIEVEMENTS") — only bullet points
   - Every bullet point MUST start with "•" character
   - Do not use "-" or "*" for bullets

4. SKILLS section rules — CRITICAL:
   - Each skill category goes on ONE line using this EXACT format:
     Category Name: item1, item2, item3, item4
   - Example:
     Systems & Tools: CoStar, Oracle EBS, Coupa, Power Automate, SharePoint
     Process Skills: Workflow Discovery, Stakeholder Communication, Process Automation
   - Do NOT write multi-line skill paragraphs
   - Do NOT use ALL CAPS for the category names in this section

5. EDUCATION section rules:
   - Copy ONLY actual degrees/certifications from the master resume
   - Each entry on its own line: Degree | School | Year
   - If no formal degree is listed in the master resume, omit the EDUCATION section entirely

MASTER RESUME:
{master_resume}

JOB DESCRIPTION:
{job_description}

Output the tailored resume now, following the STRICT OUTPUT FORMAT above:"""


def tailor_resume(master_resume: str, job_description: str, profile: dict, job_title: str = "", company: str = "", max_roles: int = 3) -> str:
    """
    Given a master resume and a job description,
    produce a tailored version that highlights the most relevant experience.

    max_roles: how many EXPERIENCE roles to include (default 3 for one-page fit).
    """
    name = profile.get("full_name", "")
    contact_block = _build_contact_block(profile)
    target = f"{job_title} at {company}" if job_title and company else "the role below"
    prompt = _build_tailor_prompt(name, contact_block, target, master_resume, job_description, max_roles=max_roles)

    t0 = time.monotonic()
    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
        timeout=API_TIMEOUT,
    )
    logger.info(
        "claude[tailor] email=%s input=%d output=%d ms=%d",
        profile.get("email", "?"),
        message.usage.input_tokens,
        message.usage.output_tokens,
        int((time.monotonic() - t0) * 1000),
    )
    return message.content[0].text


def stream_tailor_resume(
    master_resume: str,
    job_description: str,
    profile: dict,
    job_title: str = "",
    company: str = "",
    max_roles: int = 3,
):
    """
    Sync streaming generator — kept for backward compatibility.
    Prefer stream_tailor_resume_async() in async routes to avoid blocking
    the event loop.

    Yields raw text strings — the caller wraps them in SSE framing.
    Uses the same prompt as tailor_resume() via _build_tailor_prompt().

    .. deprecated::
        Use stream_tailor_resume_async() in async routes instead.
    """
    warnings.warn(
        "stream_tailor_resume() blocks the event loop. "
        "Use stream_tailor_resume_async() in async routes instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    name = profile.get("full_name", "")
    contact_block = _build_contact_block(profile)
    target = f"{job_title} at {company}" if job_title and company else "the role below"
    prompt = _build_tailor_prompt(name, contact_block, target, master_resume, job_description, max_roles=max_roles)

    with client.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
        timeout=API_TIMEOUT,
    ) as stream:
        for text in stream.text_stream:
            yield text


async def stream_tailor_resume_async(
    master_resume: str,
    job_description: str,
    profile: dict,
    job_title: str = "",
    company: str = "",
    max_roles: int = 3,
):
    """
    Async streaming generator — use in async routes so the event loop is
    never blocked waiting on the Anthropic network call (TD-17).

    Yields raw text strings — the caller wraps them in SSE framing.
    """
    name = profile.get("full_name", "")
    contact_block = _build_contact_block(profile)
    target = f"{job_title} at {company}" if job_title and company else "the role below"
    prompt = _build_tailor_prompt(name, contact_block, target, master_resume, job_description, max_roles=max_roles)

    async with async_client.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
        timeout=API_TIMEOUT,
    ) as stream:
        async for text in stream.text_stream:
            yield text


def _build_contact_block(profile: dict) -> str:
    parts = [profile.get("full_name", "")]
    if profile.get("email"):
        parts.append(profile["email"])
    if profile.get("phone"):
        parts.append(profile["phone"])
    if profile.get("location"):
        parts.append(profile["location"])
    if profile.get("linkedin_url"):
        parts.append(profile["linkedin_url"])
    if profile.get("website"):
        parts.append(profile["website"])
    return " | ".join(p for p in parts if p)
