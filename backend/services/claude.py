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
MAX_SYNTHESIS_CHARS = 400_000       # ~100k tokens — safe for Claude's 200k context window (TD-02)


def synthesize_master_resume(resume_texts: list[str], profile: dict) -> str:
    """
    Given a list of raw resume texts and the user's profile info,
    produce a single comprehensive master resume in structured text format.
    """
    # Cap total input to prevent context overflow (TD-02).
    # We use `continue` (not `break`) so one oversized file doesn't block
    # smaller files that appear later in the list.  Each oversized file is
    # hard-truncated and included rather than silently dropped, which is
    # always better than losing data entirely.
    logger.info(
        "synthesize: START  total_files=%d  budget=%d  file_sizes=%s",
        len(resume_texts), MAX_SYNTHESIS_CHARS,
        [len(t) for t in resume_texts],   # every file size up front — easiest way to spot the big one
    )
    running = 0
    capped: list[str] = []
    skipped = 0
    for i, t in enumerate(resume_texts):
        file_num = i + 1
        file_chars = len(t)
        if running + file_chars > MAX_SYNTHESIS_CHARS:
            remaining = MAX_SYNTHESIS_CHARS - running
            if remaining > 500:
                capped.append(t[:remaining])
                running = MAX_SYNTHESIS_CHARS
                logger.warning(
                    "synthesize: file %d/%d TRUNCATED  file_chars=%d  truncated_to=%d  running=%d",
                    file_num, len(resume_texts), file_chars, remaining, running,
                )
            else:
                skipped += 1
                logger.warning(
                    "synthesize: file %d/%d SKIPPED  file_chars=%d  remaining_budget=%d",
                    file_num, len(resume_texts), file_chars, remaining,
                )
            continue
        capped.append(t)
        running += file_chars
        logger.info(
            "synthesize: file %d/%d INCLUDED  file_chars=%d  running=%d",
            file_num, len(resume_texts), file_chars, running,
        )
    if skipped:
        logger.warning(
            "synthesize: %d/%d file(s) skipped — MAX_SYNTHESIS_CHARS=%d",
            skipped, len(resume_texts), MAX_SYNTHESIS_CHARS,
        )
    if not capped:
        logger.error(
            "synthesize: ALL files exceeded budget — hard-truncating first file  file_chars=%d",
            len(resume_texts[0]),
        )
        capped = [resume_texts[0][:MAX_SYNTHESIS_CHARS]]

    combined = "\n\n---RESUME FILE---\n\n".join(capped)
    logger.info(
        "synthesize: cap phase COMPLETE  files_included=%d/%d  combined_chars=%d",
        len(capped), len(resume_texts), len(combined),
    )
    name = profile.get("full_name", "")
    contact_block = _build_contact_block(profile)

    content_rules = f"""CONTENT RULES:
- The resume files may use any section names (Work History, Employment, Skills & Tools, etc.)
  Read them semantically — extract meaning, not just matching headers.
- Include ALL experience, skills, education, certifications, and achievements found across all files
- Remove duplicates but keep the most detailed version of each entry
- Do NOT invent or embellish anything — only use what is explicitly stated
- Write bullet points in strong verb-led format (Managed, Built, Led, Drove, etc.)
- PRESERVE EXACT TOOL AND SYSTEM NAMES as they appear in the source files — do not normalize,
  abbreviate, or paraphrase. ATS systems match on exact strings.
  When a tool name appears multiple times across files, keep the most complete version."""

    # ── Pass 1: compact sections (SUMMARY + SKILLS + EDUCATION) ──────────────
    # These are short — they don't need a large token budget.  Writing them
    # first in a dedicated call guarantees they are never crowded out by
    # EXPERIENCE's length.
    prompt_compact = f"""You are an expert resume writer. Below are one or more resume files uploaded by {name}.

{content_rules}

Your task for this call: extract ONLY the SUMMARY, SKILLS, and EDUCATION content.
Do NOT write any job/role entries — those will be handled separately.

OUTPUT FORMAT (write only these sections, in this order):

{contact_block}

SUMMARY
[3–4 sentence professional summary drawn from the source material]

SKILLS
[Each skill category on ONE line: Category Name: item1, item2, item3
 Do NOT write multi-line paragraphs.]

EDUCATION
[Only actual degrees: Degree | School | Year
 Omit this section entirely if no formal degree exists in the source material.]

Resume files:
{combined}

Output the header, SUMMARY, SKILLS, and EDUCATION now:"""

    t0 = time.monotonic()
    msg_compact = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt_compact}],
        timeout=API_TIMEOUT,
    )
    compact_text = msg_compact.content[0].text
    logger.info(
        "claude[synthesize:compact] input=%d output=%d ms=%d chars=%d",
        msg_compact.usage.input_tokens,
        msg_compact.usage.output_tokens,
        int((time.monotonic() - t0) * 1000),
        len(compact_text),
    )
    if msg_compact.usage.output_tokens >= 1900:
        logger.warning(
            "synthesize: compact pass hit token limit — SUMMARY/SKILLS/EDUCATION may be truncated"
            "  output_tokens=%d", msg_compact.usage.output_tokens,
        )

    # ── Pass 2: EXPERIENCE + CERTIFICATIONS + PROJECTS ────────────────────────
    # This pass gets the full 8000-token budget for the longest section.
    # We tell it what compact_text already captured so it doesn't double-count.
    prompt_experience = f"""You are an expert resume writer. Below are one or more resume files uploaded by {name}.

{content_rules}

A previous step already extracted this person's SUMMARY, SKILLS, and EDUCATION:

--- ALREADY CAPTURED ---
{compact_text}
--- END ALREADY CAPTURED ---

Your task for this call: extract ONLY the work/role EXPERIENCE content (and CERTIFICATIONS
or PROJECTS if present). Do NOT repeat the summary, skills, or education.

The source files may call this section anything — "Work History", "Employment",
"Professional Experience", "Consulting Experience", "Freelance", etc.
Extract all of it, regardless of how it is labelled.

OUTPUT FORMAT:

EXPERIENCE
[Each role on its own header line: Job Title | Company Name | Month Year – Month Year
 Then bullet points starting with "•" — the most impactful ones only, 4–6 per role.]

CERTIFICATIONS
[Only if present — omit section entirely if not]

PROJECTS
[Only if present — omit section entirely if not]

Resume files:
{combined}

Output the EXPERIENCE section (and CERTIFICATIONS / PROJECTS if present) now:"""

    t1 = time.monotonic()
    msg_exp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt_experience}],
        timeout=API_TIMEOUT,
    )
    experience_text = msg_exp.content[0].text
    logger.info(
        "claude[synthesize:experience] input=%d output=%d ms=%d chars=%d",
        msg_exp.usage.input_tokens,
        msg_exp.usage.output_tokens,
        int((time.monotonic() - t1) * 1000),
        len(experience_text),
    )
    if msg_exp.usage.output_tokens >= 7900:
        logger.warning(
            "synthesize: experience pass hit token limit — EXPERIENCE may be truncated  "
            "output_tokens=%d  tail=%r",
            msg_exp.usage.output_tokens, experience_text[-200:],
        )

    # ── Assemble in traditional resume order ──────────────────────────────────
    # compact_text  = Header + SUMMARY + SKILLS + EDUCATION
    # experience_text = EXPERIENCE + optional CERTIFICATIONS/PROJECTS
    # Traditional order: Header → SUMMARY → EXPERIENCE → SKILLS → EDUCATION → rest
    text = compact_text.rstrip() + "\n\n" + experience_text.strip()

    # Structural summary — tells you exactly what made it into the master
    sections = [h for h in ("SUMMARY", "EXPERIENCE", "SKILLS", "EDUCATION", "CERTIFICATIONS", "PROJECTS")
                if h in text]
    role_lines = [ln for ln in text.splitlines() if ln.count("|") >= 2]
    logger.info(
        "synthesize: OUTPUT STRUCTURE  sections=%s  roles=%d  chars=%d",
        sections, len(role_lines), len(text),
    )
    if "EXPERIENCE" not in text:
        logger.error(
            "synthesize: EXPERIENCE section MISSING from output — master resume likely corrupted"
        )
    elif len(role_lines) == 0:
        logger.warning(
            "synthesize: EXPERIENCE section present but 0 pipe-delimited role headers detected"
        )
    if msg_exp.usage.output_tokens < 7900 and len(role_lines) == 1:
        logger.warning(
            "synthesize: suspicious role count — roles=1  "
            "check source files for duplicates or incomplete content"
        )

    return text


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
- ROLE SELECTION — follow this priority order exactly:
    1. Always include the candidate's current or most recent role, regardless of JD relevance.
       An unexplained gap at the top of the timeline is an automatic red flag for recruiters and ATS.
    2. Select remaining roles to avoid creating gaps longer than 6 months between included positions.
       If a less-relevant role is needed to bridge a gap, include it with trimmed bullets.
    3. Within the constraint of no gaps, maximize JD relevance when choosing which roles to include.
    4. Include no more than {max_roles} roles total.
- Within each included role, trim less-relevant bullets so the overall resume fits the target length
- Match terminology from the job description naturally (don't keyword-stuff)
- TARGET LENGTH: 475–600 words of body text (excludes header and section labels). This is the
  data-backed sweet spot for interview callback rate — tighter is better. Cut weak bullets
  before expanding. Do not pad to fill space.

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

5. EDUCATION section rules — ZERO TOLERANCE FOR FABRICATION:
   - NEVER invent, infer, or guess education credentials. If a degree, school, or year does not appear
     verbatim in the MASTER RESUME text above, it MUST NOT appear in your output.
   - Copy ONLY degrees/certifications that are explicitly written in the master resume — word for word.
   - Each entry on its own line: Degree | School | Year
   - If you cannot find an explicit, written degree entry in the master resume, omit EDUCATION entirely.
   - Fabricating education (wrong school, wrong degree, invented dates) is a disqualifying error.

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
        "claude[tailor] input=%d output=%d ms=%d",
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

    logger.info("claude[stream-async] START  target=%r  prompt_chars=%d", target, len(prompt))
    t0 = time.monotonic()
    chunk_count = 0
    async with async_client.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}],
        timeout=API_TIMEOUT,
    ) as stream:
        async for text in stream.text_stream:
            chunk_count += 1
            yield text
    ms = int((time.monotonic() - t0) * 1000)
    logger.info("claude[stream-async] COMPLETE  chunks=%d  ms=%d", chunk_count, ms)


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
