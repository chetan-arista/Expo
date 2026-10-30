"""Builds ARACHNIX-Feature-Guide.pdf (version 2.0): features shared with Screaming Frog, new in ARACHNIX, new in 2.0.

    python tools/make_feature_guide.py .

The tab and check counts are read from the registry when the PDF is built."""
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

REPO = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
sys.path.insert(0, str(REPO))
OUT = REPO / "ARACHNIX-Feature-Guide.pdf"
LOGO = REPO / "web" / "static" / "brand" / "mark-512.png"

pdfmetrics.registerFont(TTFont("UI", r"C:\Windows\Fonts\segoeui.ttf"))
pdfmetrics.registerFont(TTFont("UI-Bold", r"C:\Windows\Fonts\segoeuib.ttf"))
pdfmetrics.registerFontFamily("UI", normal="UI", bold="UI-Bold", italic="UI", boldItalic="UI-Bold")

GREEN = colors.HexColor("#1E6B65")
GREEN_DARK = colors.HexColor("#0B4E48")
INK = colors.HexColor("#1E2823")
MUTED = colors.HexColor("#5A675F")
CANVAS = colors.HexColor("#F1F6F4")
LINE = colors.HexColor("#DCE3DE")
NEW_BG = colors.HexColor("#DDF1E8")
SOON_BG = colors.HexColor("#FFF6E5")
AMBER = colors.HexColor("#8A5A00")

title = ParagraphStyle("title", fontName="UI-Bold", fontSize=22, leading=26, textColor=INK)
subtitle = ParagraphStyle("sub", fontName="UI", fontSize=10.5, leading=14, textColor=MUTED)
h1 = ParagraphStyle("h1", fontName="UI-Bold", fontSize=15, leading=19, textColor=GREEN_DARK, spaceBefore=10, spaceAfter=2)
h1note = ParagraphStyle("h1note", fontName="UI", fontSize=9, leading=12, textColor=MUTED, spaceAfter=6)
h2 = ParagraphStyle("h2", fontName="UI-Bold", fontSize=10.5, leading=14, textColor=GREEN, spaceBefore=8, spaceAfter=4)
cell_name = ParagraphStyle("name", fontName="UI-Bold", fontSize=9, leading=11.5, textColor=INK)
cell_text = ParagraphStyle("text", fontName="UI", fontSize=8.8, leading=11.5, textColor=INK, alignment=TA_LEFT)
stat_num = ParagraphStyle("statn", fontName="UI-Bold", fontSize=17, leading=20, textColor=GREEN_DARK)
stat_lbl = ParagraphStyle("statl", fontName="UI", fontSize=8, leading=10, textColor=MUTED)
note = ParagraphStyle("note", fontName="UI", fontSize=8, leading=10.5, textColor=MUTED)

PAGE_W = A4[0] - 36 * mm


def bullets(items):
    return Paragraph("<br/>".join(f"\u2022&nbsp;{t}" for t in items), cell_text)


def feature_table(rows, bg=None):
    data = [[Paragraph(name, cell_name), bullets(points)] for name, points in rows]
    t = Table(data, colWidths=[44 * mm, PAGE_W - 44 * mm])
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, LINE),
        ("BOX", (0, 0), (-1, -1), 0.6, LINE),
    ]
    if bg is not None:
        style.append(("BACKGROUND", (0, 0), (0, -1), bg))
    else:
        style.append(("BACKGROUND", (0, 0), (0, -1), CANVAS))
    t.setStyle(TableStyle(style))
    return t


def group(heading, rows, bg=None):
    return [Paragraph(heading, h2), feature_table(rows, bg)]


# ---------------------------------------------------------------- content
SHARED = [
    ("Crawling", [
        ("Spider crawl", ["Follows every internal link from your start URL, the way a search engine does.",
                          "Lists every page, redirect, error, image, script and file it finds."]),
        ("List mode", ["Checks only the URLs you paste or upload, without following links."]),
        ("SERP mode", ["Checks a list of titles and descriptions for length and pixel width, without crawling."]),
        ("JavaScript rendering", ["Opens pages in Chrome to see the links and content that JavaScript adds."]),
        ("Crawl settings", ["Speed, parallel requests, depth, URL limits, include/exclude rules.",
                            "User-agent presets, custom request headers and proxy."]),
        ("Crawl scope", ["Stay on one subdomain, one folder, all subdomains, or a single URL."]),
        ("robots.txt and sitemaps", ["Obeys robots.txt with Google's matching rules.",
                                     "Reads XML sitemaps to find and check the pages they list."]),
        ("URL rewriting", ["Removes tracking parameters or rewrites URLs (regex) before they are crawled."]),
        ("Logged-in crawls", ["Crawls pages behind a login: email and password, pasted cookies or a header token."]),
    ]),
    ("Audit tabs (what each one finds)", [
        ("Internal / External", ["Every internal URL with its SEO data; every outbound link and its status."]),
        ("Response Codes", ["Working, redirected, broken (4xx), server-error (5xx) and unreachable URLs, and redirect chains."]),
        ("Page Titles / Meta Description", ["Missing, duplicate, too long or too short (in characters and pixels)."]),
        ("H1 / H2", ["Missing, duplicate, multiple or overlong headings."]),
        ("Content", ["Near-duplicate pages, thin pages and readability."]),
        ("Images", ["Missing alt text, oversized files and missing size attributes."]),
        ("Canonicals / Directives", ["Canonical tag problems; noindex, nofollow and other robots instructions."]),
        ("Hreflang / Pagination", ["Language-version errors; broken paginated series."]),
        ("Structured Data", ["Schema errors and missing properties that block Google rich results."]),
        ("Security", ["HTTP pages, mixed content and missing security headers."]),
        ("URL / Links", ["Messy URLs (long, uppercase, parameters); link problems such as broken #bookmarks."]),
        ("JavaScript / Mobile / Accessibility", ["Content that only appears after rendering; small tap targets and fonts; "
                                                 "accessibility issues such as missing labels."]),
        ("Sitemaps", ["Pages missing from the sitemap, and sitemap entries that should not be there."]),
        ("Custom Search / Extraction / JavaScript", ["Find any text, pull any data (CSS, XPath, regex) or run your own "
                                                     "script on every page."]),
        ("Validation", ["HTML structure problems that can confuse search engines."]),
    ]),
    ("Analysis and views", [
        ("Issues overview", ["Every issue with its priority, what it means and how to fix it."]),
        ("URL details pane", ["For any URL: inlinks, outlinks, images, resources, search-result preview, "
                              "headers, cookies, source and rendered page."]),
        ("Crawl Analysis", ["Link Score (internal link strength) and near-duplicates, calculated after the crawl."]),
        ("Segments", ["Group URLs into sections (blog, products...) and filter every tab by section."]),
        ("Visualisations", ["Crawl and directory tree graphs, force-directed diagrams and word clouds."]),
        ("Compare mode", ["Compares two crawls: added, new, removed and missing URLs for each filter."]),
    ]),
    ("Saving, automation and output", [
        ("Saved crawls", ["Every crawl is saved; open, rename, lock, export or import it later."]),
        ("Scheduling", ["Runs crawls automatically (daily, weekly, monthly) and emails when they finish."]),
        ("Profiles", ["Save a crawl configuration once and reuse it."]),
        ("Bulk Export and Reports", ["One-click exports (all inlinks, all issues, redirect chains...) and 30 ready-made reports."]),
        ("Sitemap generator", ["Builds XML and image sitemaps from the crawl."]),
        ("Excel and CSV", ["Any tab, filter or view exported as Excel or CSV."]),
    ]),
    ("Google connections (optional)", [
        ("Search Console, GA4, PageSpeed Insights", ["Connect if you want: adds clicks, impressions, traffic and "
                                                     "Core Web Vitals to every URL."]),
    ]),
]

NEW = [
    ("Built for the team", [
        ("Runs in the browser", ["No installs or licences: anyone on the office network opens one link.",
                                 "Can also be hosted online, protected by a team access key."]),
        ("Visual-first dashboard", ["Health score, charts and tiles that always add up, in plain language."]),
        ("Passed / Failed / Not checked", ["Every check says honestly whether it passed, failed or did not run, "
                                           "never a misleading zero."]),
        ("Multi-select filters", ["Combine several filters at once, not one at a time.",
                                  "Any (the default) shows URLs in any of them; All only URLs in every one."]),
        ("Honest partial crawls", ["When a limit stops the crawl, it says exactly what was left out."]),
    ]),
    ("Clearer at a glance", [
        ("Why pages didn't load", ["A short link next to the health score (for example “2× 404”) counts the pages "
                                   "that didn't load, by reason.", "One click lists exactly those pages in the grid."]),
        ("Issues tile", ["Issues, warnings and opportunities side by side, each with its own coloured marker."]),
        ("Readable times", ["Crawl times read 42s, 4:22 or 1:04:22 instead of long decimals."]),
        ("Clear means clear", ["Clear empties the address box and the site name at the top right too, "
                               "ready for the next site (the crawl stays saved)."]),
    ]),
    ("Proof and confirmation", [
        ("Pinpointed evidence", ["Screenshots with the broken link or SEO problem boxed on the page.",
                                 "Includes its position and HTML, so developers find it fast."]),
        ("Validation agent", ["Re-checks every reported error live, 3 times, before you report it.",
                              "Labels each one confirmed, intermittent or already fixed."]),
    ]),
    ("From problem to fix", [
        ("Fix plan", ["Groups issues by root cause (template, plugin, server, content) and ranks them by impact.",
                      "Gives fix steps for the site's CMS."]),
        ("Ready-to-import fix files", ["Redirect and meta fixes for Yoast, Rank Math, Shopify, .htaccess and Next.js."]),
        ("Developer tickets", ["Exports issues as Jira, CSV or Markdown tickets, with screenshots."]),
        ("Website Audit Report (PDF)", ["A short, client-ready PDF report with evidence, validation and fix plan."]),
    ]),
    ("Insight", [
        ("Content insights", ["Finds pages whose URL promises one thing while the page delivers another, "
                              "plus thin and off-topic pages."]),
        ("Business insights (AI)", ["Company profile, offerings, competitors, contacts and tech stack, "
                                    "each with its source, as its own PDF."]),
        ("Free AI built in", ["Uses free Gemini or Groq tiers; works fully without AI too.",
                              "Text from logged-in pages is never sent to free cloud AI."]),
        ("Extra audit tabs", ["AI Crawlers (which AI bots may read the site), Robots.txt audit, Hygiene, "
                              "Parameters, Performance, Social and Third Parties."]),
    ]),
    ("Safer logged-in crawls", [
        ("Login guard", ["Re-requests members-only pages while logged out to catch pages leaking to the public."]),
        ("Session safety", ["Never opens sign-out or data-changing links; renews an expired session automatically.",
                            "A live browser window lets any team member pass CAPTCHA, 2FA or SSO themselves."]),
    ]),
    ("Sign-in help and site map", [
        ("Paste cookies, guided", ["Step-by-step help to copy your session from Chrome, Edge or Firefox.",
                                   "Accepts any format and tests the cookies before the crawl."]),
        ("Bot-check guidance", ["Explains clearly when a site's sign-in uses a bot check, and the ways to proceed."]),
        ("Find subdomains", ["Finds your domain's other hosts (e.g. admin.site.com) and explains why each was or "
                             "wasn't crawled."]),
        ("Drill-down site graph", ["Collapsible tree: domain, subdomains, folders and pages, with counts and status colours."]),
        ("Graphs you can share", ["Every site graph and tree marks the start page with a pulsing teal node on a faint dot grid.",
                                  "Download any of them as it is drawn now: PNG image or SVG (sharp at any size)."]),
        ("New look", ["Teal palette with an easy-on-the-eyes dark mode, clear screen areas, and an animated "
                      "ARACHNIX logo that stays still when animations are turned down.",
                      "The same logo shows while a login test, business research or a graph is loading."]),
    ]),
    ("Smoother reports and pages", [
        ("Business report, reopened", ["A report already made for the site opens straight away (Open business report); "
                                       "Regenerate researches it again.", "A close (×) button hides the panel."]),
        ("Render on demand", ["Render this page now counts the seconds while Chrome works; "
                              "Take a screenshot now adds a missing screenshot."]),
        ("Safe downloads on plain HTTP", ["A slow PDF or Excel file waits behind a Save button, so the browser does not drop it.",
                                          "If Chrome or Edge warns, choose Keep."]),
    ]),
    ("Tracking over time", [
        ("Issue tracking between crawls", ["Shows which issues are new, fixed or have come back (regressed)."]),
        ("Dismiss accepted issues", ["Hide issues the team has accepted, per site, so reports stay focused."]),
    ]),
]

NEW20 = [
    ("AI agents (MCP)", ["Claude, Cursor and VS Code can read your crawls through ARACHNIX's own MCP endpoint, with "
                         "their own tokens; they never become admin and never crawl private addresses unless allowed."]),
    ("Ask ARACHNIX", ["A chat about the crawl on screen that uses ARACHNIX's own data; anything that changes something "
                      "waits for your Allow."]),
    ("Connections hub", ["Slack, Teams, Discord, webhooks, Jira, GitHub Issues, IndexNow, LanguageTool, Green Web, "
                         "Moz / Majestic / Ahrefs and Google Sheets in one place; crawl summaries sent after every crawl."]),
    ("Browser extension", ["The ARACHNIX Companion hands your signed-in Chrome or Edge session to a crawl in one click."]),
    ("AI tab, spelling and grammar", ["Your own AI prompts on every page; spelling with a site dictionary "
                                      "(add a word in one click) and grammar through LanguageTool."]),
    ("Structured data, checked properly", ["Every JSON-LD item checked against the schema.org vocabulary, down to "
                                           "nested values such as offers.price."]),
    ("Carbon rating", ["CO2 per page view and a rating from A+ to F (Sustainable Web Design model)."]),
    ("Log file analysis", ["Upload a server log: which pages Googlebot, Bingbot and AI bots really request, fake bots "
                           "unmasked, and URLs bots ask for that the crawl never found. Counts only, no IP addresses."]),
    ("Preferences and Spider options", ["Your own thresholds (filter names follow them), follow switches, resource "
                                        "types, crawl limits, a custom robots.txt with a tester."]),
    ("More reports", ["Crawl Path, Small Tap Targets, PageSpeed Opportunities, PDF properties and text, trend history "
                      "per site, white-label PDFs with your client's logo."]),
]

LATER = [
    ("Still to come", ["Scheduler step that fetches Google data before the exports; point-and-click extractor; "
                       "Compare additions; tree-graph metric picker; mobile SERP preview."]),
]


from crawler.seo import registry as _registry          # noqa: E402
N_TABS = len(_registry.tabs())
N_CHECKS = sum(1 for t in _registry.tabs() for f in t.filters if f.issue_type)


def stats_row():
    cells = [
        [Paragraph(str(N_TABS), stat_num), Paragraph("audit tabs", stat_lbl)],
        [Paragraph(f"{N_CHECKS}", stat_num), Paragraph("issue checks", stat_lbl)],
        [Paragraph("29", stat_num), Paragraph("Screaming Frog tabs matched", stat_lbl)],
        [Paragraph("0", stat_num), Paragraph("licences needed (runs in the browser)", stat_lbl)],
    ]
    t = Table([[c for c in cells]], colWidths=[PAGE_W / 4] * 4)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CANVAS),
        ("LINEAFTER", (0, 0), (-2, -1), 0.6, colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


def header():
    logo = Image(str(LOGO), width=15 * mm, height=15 * mm)
    text = [Paragraph("ARACHNIX 2.0 Feature Guide", title),
            Paragraph("What ARACHNIX shares with Screaming Frog, what it adds, and what is new in 2.0.", subtitle)]
    t = Table([[logo, text]], colWidths=[19 * mm, PAGE_W - 19 * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    return t


def on_page(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.6)
    canvas.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
    canvas.setFont("UI", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(18 * mm, 9.5 * mm, "ARACHNIX 2.0  \u00b7  Arista Systems  \u00b7  Feature guide  \u00b7  29 Oct 2026")
    canvas.drawRightString(A4[0] - 18 * mm, 9.5 * mm, f"Page {doc.page}")
    canvas.restoreState()


story = [header(), Spacer(1, 8), stats_row(), Spacer(1, 4)]

story.append(Paragraph("1. Same as Screaming Frog", h1))
story.append(Paragraph("The important Screaming Frog features, and what they do in ARACHNIX.", h1note))
for heading, rows in SHARED:
    story.extend(group(heading, rows))

story.append(Paragraph("2. New in ARACHNIX (not in Screaming Frog)", h1))
story.append(Paragraph("Features you only get in ARACHNIX.", h1note))
for heading, rows in NEW:
    story.extend(group(heading, rows, NEW_BG))

story.append(Paragraph("3. New in ARACHNIX 2.0", h1))
story.append(Paragraph("Delivered on 29 October 2026 and switched on by default (ARACHNIX_V2_FEATURES=0 hides them).", h1note))
story.append(feature_table(NEW20, NEW_BG))
story.append(Spacer(1, 6))
story.append(feature_table(LATER, SOON_BG))

doc = SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                        topMargin=16 * mm, bottomMargin=20 * mm,
                        title="ARACHNIX 2.0 Feature Guide", author="Arista Systems",
                        subject="ARACHNIX features compared with Screaming Frog")
doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
print("wrote", OUT)
