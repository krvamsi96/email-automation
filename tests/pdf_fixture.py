"""
Minimal PDF writer for tests: text pages with an optional document title, so PDF
loading and cleaning can be tested without binary fixture files.
"""

from pathlib import Path
from typing import List, Optional


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(path: Path, pages: List[List[str]], title: Optional[str] = None) -> Path:
    """Writes a PDF with one text line per list item (Helvetica, US Letter)."""
    objects: List[bytes] = []

    def add(body: str) -> int:
        objects.append(body.encode("latin-1"))
        return len(objects)

    catalog = add("<< /Type /Catalog /Pages 2 0 R >>")
    pages_obj = add("")  # filled in once the page ids are known
    font = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids = []
    for lines in pages:
        text = " T* ".join(f"({_escape(line)}) Tj" for line in lines)
        stream = f"BT /F1 11 Tf 14 TL 72 740 Td {text} ET"
        content = add(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        page_ids.append(add(
            f"<< /Type /Page /Parent {pages_obj} 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>"
        ))
    objects[pages_obj - 1] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {len(page_ids)} >>"
    ).encode("latin-1")
    info = add(f"<< /Title ({_escape(title)}) >>") if title else None

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    trailer = f"<< /Size {len(objects) + 1} /Root {catalog} 0 R" + (f" /Info {info} 0 R" if info else "") + " >>"
    out += f"trailer\n{trailer}\nstartxref\n{xref}\n%%EOF\n".encode()
    Path(path).write_bytes(bytes(out))
    return Path(path)
