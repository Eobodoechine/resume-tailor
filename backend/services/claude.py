"""
Claude API calls for master resume synthesis and tailoring.
"""
import logging
import time
import traceback
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

    prompt = f"""You are an expert resume writer. Below are one or more resume files uploaded by {name}.
Your job is to synthesize them into a single, comprehensive MASTER RESUME in structured plain text.

CONTENT RULES:
- Include ALL experience, skills, education, certifications, and achievements found across all files
- Remove duplicates but keep the most detailed version of each entry
- Do NOT invent or embellish anything — only use what is explicitly stated
- Write bullet points in strong verb-led format (Managed, Built, Led, Drove, etc.)
- PRESERVE EXACT TOOL AND SYSTEM NAMES as they appear in the source files — do not normalize,
  abbreviate, or paraphrase. ATS systems match on exact strings.
  When a tool name appears multiple times across files, keep the most complete version.

STRICT OUTPUT FORMAT:

1. Header (first lines, before any section):
{contact_block}

2. Section headers: EXACTLY these words in ALL CAPS on their own line:
   SUMMARY, EXPERIENCE, SKILLS, EDUCATION, CERTIFICATIONS, PROJECTS (only if present)

3. EXPERIENCE section:
   - Each role header: Job Title | Company Name | Month Year – Month Year  (exactly 2 pipe characters)
   - NO sub-section headers inside a role — only bullet points
   - Every bullet starts with "•"

4. SKILLS section:
   - Each category on ONE line: Category Name: item1, item2, item3
   - Do NOT write multi-line paragraphs for skills

5. EDUCATION:
   - Only actual degrees from the resume: Degree | School | Year
   - If no formal degree exists in the source material, omit EDUCATION entirely

Contact info to include at the top:
{contact_block}

Resume files to synthesize:
{combined}

Output the complete master resume now:"""

    t0 = time.monotonic()
    try:
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8000,
            messages=[{"role": "user", "content": prompt}],
            timeout=API_TIMEOUT,
        )
    except Exception as e:
        logger.error(
            "synthesize: pass 1 API call FAILED  model=%s  error=%s\n%s",
            CLAUDE_MODEL, e, traceback.format_exc(),
        )
        raise

    try:
        text = message.content[0].text
    except (IndexError, AttributeError) as e:
        logger.error(
            "synthesize: pass 1 response has no text content  "
            "content_len=%d  error=%s",
            len(message.content) if message.content else 0, e,
        )
        raise

    logger.info(
        "claude[synthesize] input=%d output=%d ms=%d chars=%d",
        message.usage.input_tokens,
        message.usage.output_tokens,
        int((time.monotonic() - t0) * 1000),
        len(text),
    )

    # ── Continuation loop — fires only when truncated, up to 4 extra passes ──
    # If output_tokens hits the limit the response was cut mid-resume.
    # Each pass sends the accumulated text as an assistant turn so Claude
    # resumes from the exact character it stopped at.
    # Before appending, trim any incomplete last line at the seam so Claude
    # doesn't start mid-word and create a duplicate partial line.
    MAX_CONTINUATIONS = 4
    continuation = None
    for pass_num in range(1, MAX_CONTINUATIONS + 1):
        last_msg = continuation if continuation is not None else message
        if last_msg.usage.output_tokens < 7900:
            break   # clean stop — resume is complete

        logger.warning(
            "synthesize: pass %d hit max_tokens — firing continuation %d/%d  "
            "output_tokens=%d  accumulated_chars=%d  tail=%r",
            pass_num, pass_num, MAX_CONTINUATIONS,
            last_msg.usage.output_tokens, len(text), text[-100:],
        )

        # Trim the last line if it looks incomplete (cut mid-word/mid-sentence).
        # A complete line ends with punctuation or is a section/role header.
        lines = text.rstrip().splitlines()
        if lines:
            last_line = lines[-1].rstrip()
            looks_complete = (
                last_line.endswith((".", ",", ")", "|", "–", "-"))
                or last_line.isupper()                      # section header e.g. EXPERIENCE
                or last_line.count("|") >= 2                # role header
                # NOTE: do NOT add startswith("•") here — a bullet cut mid-word
                # is NOT complete; trimming it lets Claude rewrite it cleanly.
            )
            if not looks_complete:
                text = "\n".join(lines[:-1])
                logger.info(
                    "synthesize: cont%d trimmed incomplete seam line: %r",
                    pass_num, last_line,
                )

        t_cont = time.monotonic()
        try:
            continuation = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=8000,
                messages=[
                    {"role": "user",      "content": prompt},
                    {"role": "assistant", "content": text},
                    {"role": "user",      "content": "Continue exactly where you stopped. Do not repeat anything already written."},
                ],
                timeout=API_TIMEOUT,
            )
        except Exception as e:
            logger.error(
                "synthesize: continuation %d API call FAILED  model=%s  "
                "accumulated_chars=%d  error=%s\n%s",
                pass_num, CLAUDE_MODEL, len(text), e, traceback.format_exc(),
            )
            break   # return what we have rather than crashing

        try:
            cont_text = continuation.content[0].text
        except (IndexError, AttributeError) as e:
            logger.error(
                "synthesize: continuation %d response has no text content  "
                "content_len=%d  error=%s",
                pass_num,
                len(continuation.content) if continuation.content else 0, e,
            )
            break

        logger.info(
            "claude[synthesize:cont%d] input=%d output=%d ms=%d chars=%d",
            pass_num,
            continuation.usage.input_tokens,
            continuation.usage.output_tokens,
            int((time.monotonic() - t_cont) * 1000),
            len(cont_text),
        )
        text = text + "\n" + cont_text.lstrip()

    else:
        # Loop exhausted all continuations and was still truncated
        logger.error(
            "synthesize: hit max continuations (%d) — resume may be incomplete  "
            "final_chars=%d",
            MAX_CONTINUATIONS, len(text),
        )

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
    if len(role_lines) == 1:
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
