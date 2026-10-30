"""
Document title of a PDF file, read from its bytes without a PDF library. Screaming Frog shows it as
the PDF's "Title 1" in the Internal tab. Looks in the Info dictionary the trailer points at (plain or
inside a compressed object stream), then in the XMP metadata (<dc:title>). Returns "" when neither
has a title or the file cannot be read.
"""
import re
import zlib
from typing import Optional

_INFO_REF = re.compile(rb"/Info\s+(\d+)\s+(\d+)\s+R")
_TITLE_KEY = re.compile(rb"/Title\s*")
_OBJSTM = re.compile(rb"/Type\s*/ObjStm")
_STREAM = re.compile(rb"stream\r?\n")
_XMP_TITLE = re.compile(rb"<dc:title>.*?<rdf:li[^>]*>(.*?)</rdf:li>", re.S)
_ESCAPES = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b", ord("f"): b"\f",
            ord("("): b"(", ord(")"): b")", ord("\\"): b"\\"}


def _literal(data: bytes, i: int) -> Optional[bytes]:
    """Bytes of the PDF literal string starting at data[i] == '(' (nested parentheses and escapes)."""
    out, depth, i = bytearray(), 1, i + 1
    while i < len(data):
        c = data[i]
        if c == 0x5C:  # backslash
            i += 1
            if i >= len(data):
                break
            nxt = data[i]
            if nxt in _ESCAPES:
                out += _ESCAPES[nxt]
            elif 0x30 <= nxt <= 0x37:  # octal \ddd
                j = i
                while j < min(i + 3, len(data)) and 0x30 <= data[j] <= 0x37:
                    j += 1
                out.append(int(data[i:j], 8) & 0xFF)
                i = j - 1
            elif nxt in (0x0A, 0x0D):  # line continuation
                if nxt == 0x0D and i + 1 < len(data) and data[i + 1] == 0x0A:
                    i += 1
            else:
                out.append(nxt)
        elif c == 0x28:
            depth += 1
            out.append(c)
        elif c == 0x29:
            depth -= 1
            if depth == 0:
                return bytes(out)
            out.append(c)
        else:
            out.append(c)
        i += 1
    return None


def _decode(raw: bytes) -> str:
    if raw[:2] == b"\xfe\xff":
        text = raw[2:].decode("utf-16-be", "replace")
    elif raw[:3] == b"\xef\xbb\xbf":
        text = raw[3:].decode("utf-8", "replace")
    else:
        text = raw.decode("latin-1")  # PDFDocEncoding matches Latin-1 for printable text
    return " ".join(text.replace("\x00", "").split())


def _title_in(obj: bytes) -> str:
    """Value of /Title in a dictionary's bytes ("" when absent)."""
    m = _TITLE_KEY.search(obj)
    if not m:
        return ""
    i = m.end()
    if obj[i:i + 1] == b"(":
        raw = _literal(obj, i)
        return _decode(raw) if raw is not None else ""
    if obj[i:i + 1] == b"<":
        end = obj.find(b">", i)
        hexs = re.sub(rb"\s", b"", obj[i + 1:end]) if end > 0 else b""
        try:
            return _decode(bytes.fromhex((hexs + b"0" * (len(hexs) % 2)).decode("ascii")))
        except ValueError:
            return ""
    return ""


def _plain_object(data: bytes, num: int, gen: int) -> Optional[bytes]:
    """Last (newest) uncompressed 'num gen obj ... endobj' body."""
    starts = [m.end() for m in re.finditer(rb"(?<!\d)%d\s+%d\s+obj" % (num, gen), data)]
    if not starts:
        return None
    end = data.find(b"endobj", starts[-1])
    return data[starts[-1]:end if end > 0 else len(data)]


def _stream_object(data: bytes, num: int) -> Optional[bytes]:
    """Object `num` from a compressed object stream (/Type /ObjStm, FlateDecode)."""
    for m in _OBJSTM.finditer(data):
        head_start = data.rfind(b"obj", 0, m.start())
        s = _STREAM.search(data, m.end())
        if head_start < 0 or not s:
            continue
        head = data[head_start:s.start()]
        first = re.search(rb"/First\s+(\d+)", head)
        count = re.search(rb"/N\s+(\d+)", head)
        end = data.find(b"endstream", s.end())
        if not first or not count or end < 0:
            continue
        try:
            body = zlib.decompress(data[s.end():end])
        except zlib.error:
            try:
                body = zlib.decompressobj().decompress(data[s.end():end])
            except zlib.error:
                continue
        nums = [int(x) for x in body[:int(first.group(1))].split()]
        pairs = list(zip(nums[0::2], nums[1::2]))[:int(count.group(1))]
        for k, (obj_num, offset) in enumerate(pairs):
            if obj_num == num:
                start = int(first.group(1)) + offset
                stop = int(first.group(1)) + pairs[k + 1][1] if k + 1 < len(pairs) else len(body)
                return body[start:stop]
    return None


def pdf_title(data: bytes) -> str:
    """The document title stored in a PDF ("" when there is none)."""
    if not data or not data.lstrip()[:5] == b"%PDF-":
        return ""
    try:
        refs = _INFO_REF.findall(data)
        if refs:
            num, gen = int(refs[-1][0]), int(refs[-1][1])
            info = _plain_object(data, num, gen) or _stream_object(data, num)
            title = _title_in(info or b"")
            if title:
                return title
        m = _XMP_TITLE.search(data)
        if m:
            text = " ".join(m.group(1).decode("utf-8", "replace").split())   # XMP is UTF-8 XML
            return text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")
    except Exception:
        return ""
    return ""


# ---------------------------------------------------------------------------------------------
# PDF properties and text (P21): pypdf (BSD-3), optional; pdf_title above stays the title source
# ---------------------------------------------------------------------------------------------
PDF_TEXT_MAX_CHARS = 2_000_000      # text kept of one PDF (word count and the all_pdf_text export)
PDF_TEXT_MAX_PAGES = 300
PDF_TEXT_SECONDS = 8.0               # text extraction stops after this (between pages)
TEXT_KEY_SUFFIX = "#pdf-text"        # the PDF's text sits in the crawl's source store under url + this
PROPS = ("title", "author", "subject", "keywords", "creator", "producer")


def _meta_text(value) -> str:
    return " ".join(str(value or "").split())[:500]


def _date(meta, attr: str) -> str:
    try:
        d = getattr(meta, attr, None)
        return d.isoformat() if d is not None else ""
    except Exception:
        return ""


def pdf_info(data: bytes):
    """({"read", "reason", "title", "author", "subject", "keywords", "creator", "producer", "pages", "created",
    "modified", "words", "encrypted", "text_capped"}, text) of a PDF's bytes. Never raises: a broken or encrypted
    PDF gives read False and a plain reason; without pypdf only the title (pdf_title) is read."""
    import io
    import time
    info = {"read": False, "reason": "", "title": pdf_title(data), "author": "", "subject": "", "keywords": "",
            "creator": "", "producer": "", "pages": None, "created": "", "modified": "", "words": None,
            "encrypted": False, "text_capped": False}
    if not data or not data.lstrip()[:5] == b"%PDF-":
        info["reason"] = "The file is not a PDF document (it does not start with %PDF)."
        return info, ""
    try:
        import logging
        logging.getLogger("pypdf").setLevel(logging.ERROR)     # its "EOF marker not found" notes stay quiet
        from pypdf import PdfReader
    except ImportError:
        info["reason"] = "The PDF reader (pypdf) is not installed on this server: only the title was read."
        return info, ""
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            info["encrypted"] = True
            try:
                opened = bool(reader.decrypt(""))       # many PDFs only restrict printing / copying
            except Exception:
                opened = False
            if not opened:
                info["reason"] = "The PDF is password-protected, so its text and properties cannot be read."
                return info, ""
        meta = reader.metadata
        if meta is not None:
            for key in PROPS:
                value = _meta_text(getattr(meta, key, "") if key != "keywords" else meta.get("/Keywords", ""))
                if value and not (key == "title" and info["title"]):
                    info[key] = value
            info["created"], info["modified"] = _date(meta, "creation_date"), _date(meta, "modification_date")
        pages = reader.pages
        info["pages"] = len(pages)
    except Exception as e:
        info["reason"] = f"The PDF could not be read (it may be broken: {type(e).__name__})."
        return info, ""
    parts, size, started = [], 0, time.time()
    for i, pg in enumerate(pages):
        if i >= PDF_TEXT_MAX_PAGES or size >= PDF_TEXT_MAX_CHARS or time.time() - started > PDF_TEXT_SECONDS:
            info["text_capped"] = True
            break
        try:
            t = pg.extract_text() or ""
        except Exception:
            continue
        parts.append(t)
        size += len(t)
    text = "\n".join(parts)[:PDF_TEXT_MAX_CHARS]
    info["words"] = len(text.split())
    info["read"] = True
    if info["words"] == 0 and info["pages"]:
        info["reason"] = "The PDF has no text layer (scanned pages): its words cannot be counted."
    return info, text


def _details(site, url: str):
    r = (getattr(site, "by_url", None) or {}).get(url)
    pdf = (getattr(r, "seo", None) or {}).get("pdf") if r is not None else None
    if not isinstance(pdf, dict):
        ctype = str(getattr(r, "content_type", "") or "").lower() if r is not None else ""
        if r is not None and "application/pdf" in ctype:
            return {"not_checked": "The properties of this PDF were not read (run a new crawl to read them)."}
        return None
    return dict(pdf)


try:
    from .registry import register_details
    register_details("pdf", _details)
except Exception:          # tooling that loads this module alone
    pass
