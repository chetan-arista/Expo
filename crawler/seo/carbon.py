"""
Carbon rating (P18): grams of CO2e per page view and a rating from A+ to F, from the bytes a page view transfers.

Model: the Sustainable Web Design model, version 4 (sustainablewebdesign.org; the same model CO2.js publishes under
Apache-2.0). This is our own implementation of the published figures; no code is copied.
  energy per GB transferred (kWh/GB)   operational   embodied
    data centre                        0.055         0.012
    network                            0.059         0.013
    user device                        0.080         0.081
  grid intensity: 494 g CO2e/kWh (global average); a data centre on green hosting uses 50 g/kWh for its
  operational part (the Green Web Foundation answer, connectors/green_web.py; not looked up -> grid average).
  Every page view counts as a first visit (version 4 drops the cache assumptions).
  ratings (g per view): A+ <= 0.040, A <= 0.079, B <= 0.145, C <= 0.209, D <= 0.278, E <= 0.359, F above.

Bytes of a page view: the page itself (transferred, else decoded size) plus the files it loads whose size the crawl
knows (resource_bytes_total / resource_bytes_external, filled by engine._apply_resource_weights). Files of unknown
size make the figure an "estimate (n of m files sized)". Internal bytes use the page host's green / grey factor,
files on other hosts the grid average.
"""
from typing import Any, Dict, Optional

from ..config import CARBON_RATINGS

GB = 1000 ** 3
OPERATIONAL_KWH_PER_GB = {"data_centre": 0.055, "network": 0.059, "device": 0.080}
EMBODIED_KWH_PER_GB = {"data_centre": 0.012, "network": 0.013, "device": 0.081}
GRID_G_PER_KWH = 494.0
GREEN_G_PER_KWH = 50.0
RATING_LIMITS = (("A+", 0.040), ("A", 0.079), ("B", 0.145), ("C", 0.209), ("D", 0.278), ("E", 0.359))
MODEL = "Sustainable Web Design model v4"


def grams_per_view(bytes_transferred: float, green: bool = False) -> float:
    """g CO2e for one view that transfers this many bytes; green = the data centre runs on green energy."""
    gb = max(0.0, float(bytes_transferred or 0)) / GB
    total = 0.0
    for segment, kwh in OPERATIONAL_KWH_PER_GB.items():
        intensity = GREEN_G_PER_KWH if green and segment == "data_centre" else GRID_G_PER_KWH
        total += gb * kwh * intensity
    for kwh in EMBODIED_KWH_PER_GB.values():
        total += gb * kwh * GRID_G_PER_KWH
    return total


def rating(grams: float) -> str:
    for name, limit in RATING_LIMITS:
        if grams <= limit:
            return name
    return "F"


def at_or_worse(rate: str, flag: str) -> bool:
    """True when `rate` is `flag` or a worse rating (A+ best ... F worst)."""
    try:
        return CARBON_RATINGS.index(rate) >= CARBON_RATINGS.index(flag)
    except ValueError:
        return False


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _green(site, url: str) -> Optional[bool]:
    try:
        from ..connectors.green_web import green_for
    except Exception:
        return None
    try:
        return green_for(site, url)
    except Exception:
        return None


def page_carbon(site, r) -> Optional[Dict[str, Any]]:
    """{"grams", "rating", "bytes", "files", "files_sized", "estimate", "green"} for an HTML page that loaded, or
    None for any other row."""
    from .tabs.internal import category_of, is_html_doc
    p = getattr(r, "seo", None) or {}
    if not (is_html_doc(r) and category_of(r) == "2xx" and p.get("is_html")):
        return None
    own = _int(p.get("transferred_bytes")) or _int(p.get("size_bytes"))
    internal = own + _int(p.get("resource_bytes_total"))
    external = _int(p.get("resource_bytes_external"))
    green = _green(site, getattr(r, "url", "")) if site is not None else None
    grams = grams_per_view(internal, bool(green)) + grams_per_view(external, False)
    files, sized = _int(p.get("resources_count")), _int(p.get("resources_sized"))
    return {"grams": round(grams, 3), "rating": rating(grams), "bytes": internal + external, "files": files,
            "files_sized": min(sized, files), "estimate": sized < files, "green": green}


def cached(site, r) -> Optional[Dict[str, Any]]:
    """page_carbon, computed once per row and site index (the columns and the filter read it for every row)."""
    if site is None:
        return page_carbon(None, r)
    cache = site.__dict__.setdefault("_carbon", {}) if hasattr(site, "__dict__") else {}
    key = getattr(r, "url", id(r))
    if key not in cache:
        cache[key] = page_carbon(site, r)
    return cache[key]


def why_not_checked(site) -> str:
    """Why the rating cannot be judged for this crawl ("" when it can)."""
    cfg = getattr(site, "config", None)
    if cfg is not None:
        carbon = getattr(cfg, "carbon", None)
        if isinstance(carbon, dict) and carbon.get("enabled") is False:
            return "The carbon rating was off for this crawl (Configuration > Preferences)."
        if getattr(cfg, "crawl_resources", True) is False:
            return ("Images, scripts and stylesheets were not requested (resource crawling was off), so the bytes "
                    "of a page view are unknown.")
    return ""


def _details(site, url: str) -> Optional[dict]:
    r = (getattr(site, "by_url", None) or {}).get(url)
    if r is None:
        return None
    why = why_not_checked(site)
    if why:
        return {"not_checked": why}
    c = cached(site, r)
    if c is None:
        return None
    try:
        from ..connectors.green_web import carbon_note
        source = carbon_note(site)
    except Exception:
        source = "Green hosting was not checked: the grid-average energy factor is used."
    return dict(c, model=MODEL, hosting=source,
                note=(f"Estimate: {c['files_sized']} of {c['files']} files sized" if c["estimate"] else ""))


try:
    from .registry import register_details
    register_details("carbon", _details)
except Exception:          # registry not importable (tooling that loads this module alone)
    pass
