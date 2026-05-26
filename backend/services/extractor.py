"""
Extract plain text from PDF and DOCX resume files.
"""
import io
import pdfplumber
import docx


def extract_text(file_bytes: bytes, filename: str) -> str:
    ext = filename.lower().rsplit(".", 1)[-1]
    if ext == "pdf":
        return _extract_pdf(file_bytes)
    elif ext in ("docx", "doc"):
        return _extract_docx(file_bytes)
    else:
        raise ValueError(f"Unsupported file type: {ext}")


def _extract_pdf(file_bytes: bytes) -> str:
    text_parts = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    return "\n\n".join(text_parts)


def _extract_docx(file_bytes: bytes) -> str:
    """
    Extract text from a DOCX file, including paragraphs AND tables.

    A huge fraction of real-world resumes are built in two-column Word
    tables — extracting only doc.paragraphs would silently drop that
    content. We also pull text from section headers/footers in case the
    candidate put their contact info there.
    """
    doc = docx.Document(io.BytesIO(file_bytes))
    parts: list[str] = []

    for p in doc.paragraphs:
        text = p.text.strip()
        if text:
            parts.append(text)

    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                cell_text = cell.text.strip()
                if cell_text:
                    parts.append(cell_text)

    for section in doc.sections:
        for source in (section.header, section.footer):
            for p in source.paragraphs:
                text = p.text.strip()
                if text:
                    parts.append(text)

    return "\n".join(parts)
