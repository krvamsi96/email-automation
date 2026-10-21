"""
Correspondence-aware segmentation of documents that contain e-mail messages.

Printed or exported e-mail threads (PDF, TXT, DOCX) put many messages into one
document: each with a header (sender, date, recipients, subject), usually in
reverse chronological order, and often with every message repeated as a quoted
copy further down. Splitting such a document at fixed character offsets mixes
the end of one message with the start of another, separates a reply from the
request it answers and fills chunks with recipient address lists.

This module finds the message boundaries from the header formats themselves
(no document-specific rules), so each message can be indexed on its own with
its sender, date and subject as metadata, and repeated copies can be dropped.

Recognised header styles:
  * RFC 822 / Outlook blocks:  From: ... / Sent: or Date: ... / To: / Cc: / Subject:
  * web-mail print views:      Name < address >  followed by a date line, then To "..." / Cc "..."
  * reply lead-ins:            On <date>, <name> wrote:
  * separators:                ---- Forwarded message ---- / -----Original Message-----
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

FIELD_RE = re.compile(r"^(from|to|cc|bcc|date|sent|subject|reply-to)\s*:\s*(.*)$", re.IGNORECASE)
# Raw MIME headers that appear inside delivery reports and forwarded originals
MIME_FIELD_RE = re.compile(
    r"^(message-id|in-reply-to|references|content-type|content-transfer-encoding|mime-version|"
    r"user-agent|return-path|received|x-[\w-]+|boundary)\s*[:=]", re.IGNORECASE)
# Recipient lines in web-mail print views: To "Name"<addr>, Cc "Name"<addr>
PRINT_FIELD_RE = re.compile(r'^(to|cc|bcc)\s+["<]', re.IGNORECASE)
ADDRESS_LINE_RE = re.compile(
    r"^(?P<name>[^<>\n]{0,120}?)\s*<\s*(?P<addr>[\w.+'-]+@[\w-]+(?:\.[\w-]+)+)\s*>\s*$")
EMAIL_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
SEPARATOR_RE = re.compile(
    r"^\W*(forwarded message|original message|begin forwarded message)\W*$", re.IGNORECASE)
ON_WROTE_RE = re.compile(r"^on\s+(?P<when>.{6,80}?),?\s+(?P<who>[^,]{2,120}?)\s+wrote:\s*$", re.IGNORECASE)

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
DATE_RE = re.compile(
    r"(?:(?P<d1>\d{1,2})\s+(?P<m1>[a-z]{3,9})\.?,?\s+(?P<y1>\d{4})"          # 27 Aug 2026
    r"|(?P<m2>[a-z]{3,9})\.?\s+(?P<d2>\d{1,2}),?\s+(?P<y2>\d{4}))"           # Aug 27, 2026
    r"(?:\s*(?:at\s+)?(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?\s*(?P<ap>[ap]\.?m\.?)?)?",
    re.IGNORECASE)
MAX_DATE_LINE = 70


@dataclass
class Message:
    """One e-mail message found in a document."""
    sender: str = ""
    date: str = ""
    subject: str = ""
    timestamp: str = ""          # sortable "YYYY-MM-DDTHH:MM:SS" (time as written), "" when unknown
    lines: List[Tuple[str, int]] = field(default_factory=list)   # (line, offset in document text)

    @property
    def body(self) -> str:
        return "\n".join(text for text, _ in self.lines).strip()

    @property
    def offset(self) -> int:
        return self.lines[0][1] if self.lines else 0


def parse_date(text: str) -> str:
    """Sortable timestamp from a header date ("Thu, 27 Aug 2026 9:27:54 AM +0530"), or ""."""
    m = DATE_RE.search(text or "")
    if not m:
        return ""
    day, month, year = (m["d1"], m["m1"], m["y1"]) if m["d1"] else (m["d2"], m["m2"], m["y2"])
    month_no = MONTHS.get(month[:3].lower())
    if not month_no:
        return ""
    hour, minute, second = int(m["h"] or 0), int(m["mi"] or 0), int(m["s"] or 0)
    ampm = (m["ap"] or "").replace(".", "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    try:
        return datetime(int(year), month_no, int(day), hour, minute, second).isoformat()
    except ValueError:
        return ""


def _is_date_line(line: str) -> bool:
    return len(line) <= MAX_DATE_LINE and bool(parse_date(line)) and not EMAIL_RE.search(line)


def is_address_line(line: str) -> bool:
    """A wrapped recipient-list line: addresses and quoted names with hardly any other words."""
    if "@" not in line and not line.lstrip().startswith(('"', "<")) and not line.rstrip().endswith(('"', ">")):
        return False
    rest = EMAIL_RE.sub(" ", re.sub(r"<[^>]*>?", " ", line))
    # A quoted name may be wrapped onto this line ('Kotaru IAS"<addr>') or onto the next ('"Om Pradyumana').
    candidates = [rest]
    if rest.count('"') % 2:
        candidates += [re.sub(r'^[^"]*"', " ", rest), re.sub(r'"[^"]*$', " ", rest)]

    def words(text: str) -> int:
        text = re.sub(r'"[^"]*"', " ", text).replace('"', " ")
        return len([w for w in text.split() if re.search(r"[^\W\d_]", w)])

    return min(words(c) for c in candidates) <= 4


def _name(sender: str) -> str:
    """Display name from 'Name <addr>' / '"Name"<addr>' / 'addr'."""
    sender = sender.strip().strip(",;")
    m = re.match(r'^\s*"?([^"<]*?)"?\s*<\s*([^>]+?)\s*>?\s*$', sender)
    if m:
        return (m.group(1).strip() or m.group(2).strip())[:120]
    return sender[:120]


class _Parser:
    def __init__(self, text: str, title: str = ""):
        self.lines: List[Tuple[str, int]] = []
        offset = 0
        for raw in text.split("\n"):
            self.lines.append((raw.strip(), offset))
            offset += len(raw) + 1
        self.title_words = set(re.findall(r"\w+", (title or "").lower()))
        self.messages: List[Message] = []
        self.headers = 0

    def _next(self, i: int) -> int:
        """Index of the next non-blank line at or after i (len(lines) when none)."""
        while i < len(self.lines) and not self.lines[i][0]:
            i += 1
        return i

    def _subject_continues(self, line: str) -> bool:
        """A wrapped subject line: all of its words occur in the document title."""
        words = re.findall(r"\w+", line.lower())
        return bool(words) and len(line) <= 120 and all(w in self.title_words for w in words)

    def _ends_header(self, line: str, k: int) -> bool:
        """A short wrapped subject tail that is followed by more header fields."""
        n = self._next(k + 1)
        return (len(line.split()) <= 6 and n < len(self.lines)
                and bool(MIME_FIELD_RE.match(self.lines[n][0]) or FIELD_RE.match(self.lines[n][0])))

    def _rfc_header(self, i: int) -> Optional[Tuple[Message, int]]:
        """From: ... block (at least two header fields). Returns (message, index after header)."""
        m = FIELD_RE.match(self.lines[i][0])
        if not m or m.group(1).lower() != "from":
            return None
        msg, fields, last = Message(sender=_name(m.group(2))), 1, "from"
        j = i + 1
        while True:
            k = self._next(j)
            if k >= len(self.lines):
                break
            line = self.lines[k][0]
            fm = FIELD_RE.match(line)
            if fm:
                if fm.group(1).lower() == "from":
                    break  # the next message's header
                last, value = fm.group(1).lower(), fm.group(2).strip()
                fields += 1
                if last in ("date", "sent"):
                    msg.date = value
                elif last == "subject":
                    msg.subject = value
            elif MIME_FIELD_RE.match(line):
                last = "mime"
            elif last in ("from", "to", "cc", "bcc", "reply-to") and is_address_line(line):
                pass
            elif last == "subject" and (self._subject_continues(line) or self._ends_header(line, k)):
                msg.subject = f"{msg.subject} {line}".strip()
            elif last == "mime" and (line.startswith(("boundary", "<")) or EMAIL_RE.fullmatch(line.strip("<>"))):
                pass
            else:
                break
            j = k + 1
        if fields < 2:
            return None
        return msg, j

    def _print_header(self, i: int) -> Optional[Tuple[Message, int]]:
        """'Name < address >' followed by a date line (web-mail print view)."""
        line = self.lines[i][0]
        k = self._next(i + 1)
        if k < len(self.lines) and self.lines[k][0] == ">":  # address wrapped before its closing bracket
            line, k = line + " >", self._next(k + 1)
        if not ADDRESS_LINE_RE.match(line) or k >= len(self.lines) or not _is_date_line(self.lines[k][0]):
            return None
        msg = Message(sender=_name(line), date=self.lines[k][0])
        j, last = k + 1, ""
        while True:
            n = self._next(j)
            if n >= len(self.lines):
                break
            nxt = self.lines[n][0]
            fm = FIELD_RE.match(nxt)
            if PRINT_FIELD_RE.match(nxt) or (fm and fm.group(1).lower() in ("to", "cc", "bcc")):
                last = "to"
            elif last and is_address_line(nxt):
                pass
            else:
                break
            j = n + 1
        return msg, j

    def parse(self) -> List[Message]:
        current = Message()
        i = 0
        while i < len(self.lines):
            line = self.lines[i][0]
            if SEPARATOR_RE.match(line):
                i += 1
                continue
            header = None
            if line:
                header = self._rfc_header(i) or self._print_header(i)
                if header is None:
                    w = ON_WROTE_RE.match(line)
                    if w and parse_date(w["when"]):
                        header = (Message(sender=_name(w["who"]), date=w["when"]), i + 1)
            if header:
                self.messages.append(current)
                current, i = header
                current.timestamp = parse_date(current.date)
                self.headers += 1
                continue
            current.lines.append(self.lines[i])
            i += 1
        self.messages.append(current)
        return self.messages


def split_messages(text: str, title: str = "") -> List[Message]:
    """
    Messages found in a document, in document order, with empty ones removed.
    Returns [] when the document does not look like correspondence (no message
    headers), so callers can fall back to plain text chunking.
    """
    parser = _Parser(text, title)
    messages = parser.parse()
    if parser.headers == 0:
        return []
    kept = []
    for msg in messages:
        # Raw MIME headers (delivery reports), bare address lines and separator rules carry no content.
        msg.lines = [(t, o) for t, o in msg.lines
                     if not MIME_FIELD_RE.match(t) and not (ADDRESS_LINE_RE.match(t) or EMAIL_RE.fullmatch(t))
                     and (not t or re.search(r"\w", t))]
        while msg.lines and not msg.lines[0][0]:
            msg.lines.pop(0)
        words = re.findall(r"\w+", EMAIL_RE.sub(" ", msg.body).lower())
        if len(words) < 3:
            continue
        if not msg.sender and all(w in parser.title_words for w in words):
            continue  # print-view preamble: mailbox name and the thread subject
        kept.append(msg)
    return kept


def header_line(sender: str = "", date: str = "", subject: str = "") -> str:
    """One-line description of a message, e.g. 'From: X | Date: Y | Subject: Z'."""
    parts = [f"From: {sender}" if sender else "", f"Date: {date}" if date else "",
             f"Subject: {subject}" if subject else ""]
    return " | ".join(p for p in parts if p)
