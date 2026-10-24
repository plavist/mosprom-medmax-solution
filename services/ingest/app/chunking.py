import re


def chunk_text(text: str, size: int = 900, overlap: int = 150) -> list[str]:
    """Режем по абзацам/предложениям до ~size символов с перекрытием."""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    units: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if len(para) <= size:
            units.append(para)
        else:
            units.extend(s.strip() for s in re.split(r"(?<=[.!?;])\s+|\n", para) if s.strip())
    chunks: list[str] = []
    cur = ""
    for u in units:
        while len(u) > size:  # слишком длинная единица без знаков препинания
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(u[:size])
            u = u[size - overlap:]
        if cur and len(cur) + len(u) + 1 > size:
            chunks.append(cur)
            cur = cur[-overlap:].lstrip() + " " + u if overlap else u
            cur = cur.strip()
            if len(cur) > size:
                cur = u
        else:
            cur = f"{cur}\n{u}".strip() if cur else u
    if cur:
        chunks.append(cur)
    return chunks
