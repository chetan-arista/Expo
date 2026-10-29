"""
PDF files (P21): the bulk export "All PDF Text" (one .txt per PDF read in the crawl, from the crawl's source store)
and the "pdf" details section (crawler/seo/pdf_meta.py registers it when imported here). No tab of its own: the
PDF properties are columns of the Internal tab (filter PDF).
"""
from .. import pdf_meta
from .internal import internal_rows


def _pdfs(site):
    return [r for r in internal_rows(site) if isinstance((getattr(r, "seo", None) or {}).get("pdf"), dict)]


def _store(crawler):
    store = getattr(crawler, "source_store", None)
    if store is None:
        try:
            from ...source_store import SourceStore
            store = SourceStore.open(getattr(crawler, "source_dir", "") or "")
        except Exception:
            store = None
    return store


def _files(site, crawler):
    store = _store(crawler)
    if store is None:
        return [], "The text of PDFs is kept only when page source storage is on (Settings > Storage)."
    out, used = [], set()
    for r in _pdfs(site):
        text = store.get(r.url + pdf_meta.TEXT_KEY_SUFFIX)
        if not text:
            continue
        base = (r.url.rstrip("/").rsplit("/", 1)[-1] or "document").rsplit(".", 1)[0][:80] or "document"
        name, n = f"{base}.txt", 2
        while name in used:
            name, n = f"{base} ({n}).txt", n + 1
        used.add(name)
        out.append((name, (f"Source: {r.url}\n\n" + text).encode("utf-8")))
    return out, "" if out else "No PDF text was read in this crawl."


def _not_checked(site) -> str:
    if not _pdfs(site):
        return "No PDF file was read in this crawl."
    return ""


def _register():
    try:
        from ...reports import register_bulk_export
        register_bulk_export("all_pdf_text", "All PDF Text", files=_files, not_checked=_not_checked,
                             description="The text of every PDF the crawl read, one .txt file each (zip).")
    except Exception:
        pass


_register()
