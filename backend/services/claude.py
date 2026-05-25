"""
Claude API calls for master resume synthesis and tailoring.
"""
import anthropic
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def synthesize_master_resume(resume_texts: list[str], profile: dict) -> str:
    """
    Given a list of raw resume texts and the user's profile info,
    produce a single comprehensive master resume in structured text format.
    """
    combined = "\n\n---RESUME FILE---\n\n".join(resume_texts)
    name = profile.get("full_name", "")
    contact_block = _build_contact_block(profile)

    prompt = f"""You are an expert resume writer. Below are one or more resume files uploaded by {name}.
Your job is to synthesize them into a single, comprehensive MASTER RESUME in structured plain text.

Rules:
- Include ALL experience, skills, education, certifications, and achievements found across all files
- Remove duplicates but keep the most detailed version of each entry
- Do NOT invent or embellish anything — only use what is explicitly stated
- Organize into clear sections: SUMMARY, EXPERIENCE, SKILLS, EDUCATION, CERTIFICATIONS, PROJECTS (if present)
- Under each role: include company, title, dates, location, and all bullet points found
- Write bullet points in strong verb-led format (Managed, Built, Led, Drove, etc.)
- Output plain text only — no markdown headers with #, no asterisks for bullets, use ALL CAPS for section headers

Contact info to include at the top:
{contact_block}

Resume files to synthesize:
{combined}

Output the complete master resume now:"""

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text


def tailor_resume(master_resume: str, job_description: str, profile: dict, job_title: str = "", company: str = "") -> str:
    """
    Given a master resume and a job description,
    produce a tailored version that highlights the most relevant experience.
    """
    name = profile.get("full_name", "")
    contact_block = _build_contact_block(profile)

    target = f"{job_title} at {company}" if job_title and company else "the role below"

    prompt = f"""You are an expert resume writer and career strategist. Your task is to tailor {name}'s master resume for {target}.

Rules:
- Select and emphasize the experience, skills, and achievements MOST relevant to the job description
- Reorder bullet points within each role to lead with the most relevant ones
- Adjust the summary to directly address what the employer is looking for
- Do NOT fabricate, invent, or add anything not in the master resume
- Do NOT remove entire roles — keep all jobs but trim less-relevant bullets if needed
- Match terminology from the job description naturally (don't keyword-stuff)
- Output plain text only — no markdown, use ALL CAPS for section headers
- Keep it to one page worth of content (approximately 600-750 words of body text)

Contact info to include at the top:
{contact_block}

MASTER RESUME:
{master_resume}

JOB DESCRIPTION:
{job_description}

Output the tailored resume now:"""

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text


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
