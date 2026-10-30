"""
Plain-language knowledge base for error intelligence: for each family of SEO issues, why it usually
happens, how to stop it coming back, generic fix steps and platform-specific steps (WordPress, Shopify,
Magento 2, Next.js). Written for an SEO team that hands fixes to developers and content editors.

family_of(tab, filter) -> family key; FAMILIES[key] -> Family. Every registered filter maps to a family:
FILTER_FAMILY first, then FILTER_PREFIX_FAMILY (a whole family of generated filters, such as one per axe-core
rule), then the tab's family (TAB_FAMILY), then "other". Build 4 issue filters are all mapped explicitly.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

PLATFORMS = ("wordpress", "shopify", "magento", "nextjs")
PLATFORM_LABELS = {"wordpress": "WordPress", "shopify": "Shopify", "magento": "Magento 2", "nextjs": "Next.js"}

# Where a page template lives, per platform (used by template / element groups)
TEMPLATE_WHERE = {
    "wordpress": "the theme template (Appearance > Theme File Editor, a child theme, or Elementor > Templates > "
                 "Theme Builder for header/footer/single templates)",
    "shopify": "the theme code (Online Store > Themes > ... > Edit code: layout/theme.liquid, sections/ and templates/)",
    "magento": "the theme (app/design/frontend/<Vendor>/<theme>: layout XML and .phtml templates), then "
               "bin/magento setup:static-content:deploy and a cache flush",
    "nextjs": "the shared layout or page component (app/<route>/layout.tsx or page.tsx; pages/_app or _document)",
    "": "the page template your site uses for these pages (theme, layout or page-builder template)",
}
REGION_WHERE = {
    "wordpress": {"Header": "the header template (Elementor > Templates > Theme Builder > Header, or Appearance > "
                            "Customize > Header)",
                  "Footer": "the footer template (Elementor > Templates > Theme Builder > Footer, or Appearance > "
                            "Widgets for footer widgets)",
                  "Navigation menu": "Appearance > Menus", "Sidebar": "Appearance > Widgets (sidebar)",
                  "Breadcrumb": "the SEO plugin's breadcrumb settings"},
    "shopify": {"Header": "Online Store > Themes > Customize > Header (sections/header.liquid)",
                "Footer": "Online Store > Themes > Customize > Footer (sections/footer.liquid)",
                "Navigation menu": "Content > Menus (Online Store > Navigation)",
                "Sidebar": "the collection/blog sidebar section in Online Store > Themes > Customize"},
    "magento": {"Header": "the theme's header templates (Magento_Theme html/header*.phtml) or Content > Blocks",
                "Footer": "Content > Blocks (footer links block) or the theme's footer templates",
                "Navigation menu": "Catalog > Categories ('Include in Menu') or the menu extension",
                "Sidebar": "Content > Widgets / layered navigation settings"},
    "nextjs": {"Header": "the shared Header component used by app/layout.tsx",
               "Footer": "the shared Footer component used by app/layout.tsx",
               "Navigation menu": "the navigation component (or the CMS menu entry it reads)",
               "Sidebar": "the sidebar component"},
}
FILE_STEPS = {
    "wordpress": ("Media > Library: re-upload the file, or replace it in place with a media-replace plugin so the "
                  "address stays the same.",),
    "shopify": ("Content > Files: upload the file again and update the image in the product/section.",),
    "magento": ("Content > Media Gallery or Catalog > Products > Images and Videos: upload the image again.",),
    "nextjs": ("Put the file back under public/ (or the CMS asset store) or update the component's src.",),
}
REDIRECT_WHERE = {
    "wordpress": "the Redirection plugin (Tools > Redirection), Rank Math > Redirections or Yoast SEO Premium > "
                 "Redirects; for big rules use .htaccess / the Nginx config",
    "shopify": "Shopify admin: Online Store > Navigation > URL Redirects (search 'URL redirects' in the admin); "
               "bulk import via CSV",
    "magento": "Marketing > SEO & Search > URL Rewrites (Add URL Rewrite, type 301)",
    "nextjs": "redirects() in next.config.js (permanent: true) or middleware for pattern rules",
    "": "your server or CMS redirect manager (a permanent 301 redirect)",
}


@dataclass(frozen=True)
class Family:
    key: str
    category: str                 # template | plugin | cms_setting | server | content | links | config
    label: str                    # short noun phrase for titles
    why: str
    prevent: str
    fix: Tuple[str, ...]
    steps: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    effort: str = "medium"
    verify: str = "Re-crawl (or use Re-check on the sample URLs) and confirm the issue count drops to 0."


def _f(key, category, label, why, prevent, fix, steps=None, effort="medium", verify=""):
    return Family(key, category, label, why, prevent, tuple(fix), dict(steps or {}), effort,
                  verify or Family.__dataclass_fields__["verify"].default)


FAMILIES: Dict[str, Family] = {f.key: f for f in [
    _f("server_headers", "server", "security headers",
       "Security headers are added by the web server, CDN or hosting panel, not by pages. When they are missing on "
       "every URL, nobody has configured them at server level yet.",
       "Keep the header set in version-controlled server config (or the CDN rules) and add a header check to the "
       "go-live checklist for every new site or server move.",
       ["Add the missing headers once at server/CDN level so every response carries them.",
        "Start Content-Security-Policy in report-only mode, then enforce it once nothing breaks.",
        "Re-test one page and one image with the browser dev tools (Network > Headers)."],
       {"wordpress": ("Apache: add 'Header always set ...' lines to .htaccess; Nginx: add_header in the server block; "
                      "or a headers plugin / your host's panel (Kinsta, WP Engine, Cloudflare Transform Rules).",),
        "shopify": ("Shopify sets most response headers itself and they cannot be changed in the theme; leave HSTS/CSP to "
                    "Shopify or add them through a proxy/CDN in front of the store if really needed.",),
        "magento": ("Add the headers in the Nginx/Apache vhost or, on Adobe Commerce Cloud, in Fastly custom VCL "
                    "snippets.",),
        "nextjs": ("Return the headers from headers() in next.config.js (source '/:path*'), or in vercel.json / "
                   "middleware.",)},
       effort="low", verify="Open any page and any image in the browser dev tools: the headers must be present."),
    _f("https", "server", "HTTPS / mixed content",
       "Pages or the files they load are still requested over plain http://, usually old hard-coded links in "
       "content or a theme setting that stores the site address with http.",
       "Store the site address with https:// in the CMS settings, force HTTPS at server level and search the "
       "database for 'http://' after every migration.",
       ["Force HTTPS with a permanent redirect at server level.",
        "Search and replace http://yourdomain with https://yourdomain in the database / theme settings.",
        "Load external files (fonts, scripts, images) over https:// only."],
       {"wordpress": ("Settings > General: both addresses with https://; run 'Better Search Replace' for old http "
                      "links; Really Simple SSL can fix leftover mixed content.",),
        "shopify": ("Shopify serves https automatically; fix http:// links typed into theme settings, sections or "
                    "product descriptions.",),
        "magento": ("Stores > Configuration > General > Web: Base URLs (Secure) with https and 'Use Secure URLs on "
                    "Storefront' = Yes.",),
        "nextjs": ("Use https:// for every asset URL in code and environment variables; enforce HTTPS at the host.",)},
       effort="medium"),
    _f("content_type", "server", "wrong content type",
       "The server labels a file with the wrong MIME type, usually a missing type mapping on the server or CDN.",
       "Keep the server's MIME type list current when new file types (webp, avif, woff2) are introduced.",
       ["Add the correct MIME type mapping for the file extension on the server or CDN."], effort="low"),
    _f("cross_origin", "template", "unsafe new-tab links",
       "Links that open external sites in a new tab without rel=\"noopener\" usually come from a menu, footer or "
       "social-icon widget that adds target=\"_blank\".",
       "Let the theme add rel=\"noopener noreferrer\" automatically to every target=\"_blank\" link.",
       ["Add rel=\"noopener noreferrer\" to the new-tab links (once in the widget/template for repeated links)."],
       effort="low"),
    _f("server_errors", "server", "server errors / timeouts",
       "The server crashed or did not answer: an overloaded host, a PHP/plugin fatal error, a timeout behind the "
       "CDN, or a firewall that blocks crawlers.",
       "Monitor uptime and error logs; load-test after big plugin or theme updates; allow-list your own crawler "
       "and search engines in the firewall.",
       ["Re-check the URLs (Validate) to see if the error is constant or temporary.",
        "Read the server error log at the time of the crawl for the exact error.",
        "Fix the failing plugin/code or raise server resources; ask the host about rate limits."],
       {"wordpress": ("Turn on WP_DEBUG_LOG on staging and reproduce; disable recently updated plugins one by one.",),
        "magento": ("Check var/log/exception.log and system.log; reindex and flush cache.",),
        "nextjs": ("Check the function/server logs (e.g. Vercel > Logs) for the failing route.",)},
       effort="medium"),
    _f("broken_internal", "links", "broken internal links",
       "Pages were deleted, renamed or moved and the links pointing to them were not updated (and no redirect was "
       "added). Images and files break the same way when they are deleted from the media library.",
       "Whenever a page or file is renamed or removed, add a 301 redirect in the same step and run a broken-link "
       "check before publishing big content changes.",
       ["Redirect each removed URL to the best live replacement (301).",
        "Update the links on the pages that still point to the old URL.",
        "Restore deleted images/files or remove them from the pages."],
       {"wordpress": ("Redirects: Tools > Redirection (or Rank Math > Redirections). Update links with 'Better Search "
                      "Replace'.",),
        "shopify": ("Online Store > Navigation > URL Redirects; fix links in menus (Content > Menus) and rich text.",),
        "magento": ("Marketing > SEO & Search > URL Rewrites; update CMS blocks/pages that link to old URLs.",),
        "nextjs": ("Add the old paths to redirects() in next.config.js; update hard-coded links in components/CMS.",)},
       effort="medium"),
    _f("redirects", "links", "links to redirects",
       "Internal links still use old addresses that now redirect (renamed pages, http to https, missing trailing "
       "slash). Every click and crawl takes an extra hop.",
       "When a URL changes, update internal links in the same task, not only the redirect.",
       ["Replace the old URL with the final URL in menus, templates and content (a database search-and-replace "
        "handles many at once)."],
       {"wordpress": ("Tools > Better Search Replace: old URL -> new URL (dry run first).",),
        "shopify": ("Update menu items (Content > Menus) and links in descriptions to the final URL.",),
        "magento": ("Update links in CMS pages/blocks and menus; keep the URL Rewrite for outside links.",),
        "nextjs": ("Update the href values in components / CMS entries to the final path.",)},
       effort="low"),
    _f("redirect_chains", "config", "redirect chains / loops",
       "Redirect rules were stacked over time (old -> older -> new) or two rules point at each other.",
       "When adding a redirect, point it at the final URL and update older rules that pointed at the source.",
       ["Point every redirect straight at the final URL.", "Break loops by removing one of the two rules."],
       effort="low"),
    _f("robots_blocked", "config", "URLs blocked by robots.txt",
       "robots.txt disallows paths that pages still link to: either the rule is too broad or the links should not "
       "be there.",
       "Review robots.txt changes like code changes: test new rules against important URLs first.",
       ["Confirm each blocked path is meant to be hidden; narrow the Disallow rule if not.",
        "Stop linking to paths that are meant to stay blocked."],
       {"wordpress": ("Yoast SEO > Tools > File editor or Rank Math > General Settings > Edit robots.txt.",),
        "shopify": ("Edit the robots.txt.liquid template (Online Store > Themes > Edit code > Add template).",),
        "magento": ("Content > Design > Configuration > [store view] > Search Engine Robots.",),
        "nextjs": ("app/robots.ts (or public/robots.txt).",)},
       effort="low"),
    _f("broken_external", "links", "broken outbound links",
       "Other websites moved or deleted the pages you link to. This happens to every site over time, mostly in "
       "older blog posts.",
       "Run a broken-link check every quarter and prefer linking to stable pages (home pages, official docs).",
       ["Replace each dead link with a working source or remove the link.",
        "For links repeated in the footer/header, fix the template once."], effort="medium"),
    _f("url_format", "cms_setting", "URL format",
       "Addresses were typed with capitals, spaces, underscores or special characters, or the permalink settings "
       "produce long URLs.",
       "Use lowercase, hyphenated, short slugs; set this as a rule in the publishing checklist.",
       ["Only change URLs that matter: new URL + 301 redirect from the old one + update internal links."],
       {"wordpress": ("Settings > Permalinks ('Post name'); edit a slug in the page's URL/Permalink box.",),
        "shopify": ("Edit the URL handle under 'Search engine listing' and keep 'Create a URL redirect' ticked.",),
        "magento": ("Product/Category > Search Engine Optimization > URL Key ('Create Permanent Redirect' on).",)},
       effort="medium"),
    _f("url_params", "config", "URL parameters",
       "Links carry tracking or filter parameters (utm_, sort, search), creating many addresses for one page.",
       "Never put tracking parameters on internal links; use canonical tags on filter/sort URLs.",
       ["Remove tracking parameters from internal links.", "Give parameter URLs a canonical to the clean URL."],
       effort="low"),
    _f("titles_missing", "content", "missing / broken page titles",
       "The page was published without an SEO title, or the theme prints the <title> in the wrong place.",
       "Make the SEO title a required field in the publishing checklist.",
       ["Write a unique title (about 50-60 characters) for each page."],
       {"wordpress": ("Edit the page > Yoast/Rank Math box > SEO title; defaults in Yoast SEO > Settings > Content "
                      "types or Rank Math > Titles & Meta.",),
        "shopify": ("Page/product editor > Search engine listing > Edit > Page title; home page in Online Store > "
                    "Preferences.",),
        "magento": ("Catalog > Products (or Content > Pages) > Search Engine Optimization > Meta Title.",),
        "nextjs": ("export const metadata / generateMetadata({ title }) in the route's page.tsx.",)},
       effort="medium"),
    _f("titles_length", "content", "title length",
       "Titles were written without a length guide, or the site-wide title template adds a long brand suffix.",
       "Show a live character counter in the editor (Yoast/Rank Math do) and keep the brand suffix short.",
       ["Rewrite titles to about 50-60 characters with the main keyword first."],
       {"wordpress": ("Yoast SEO > Settings > Content types (title template) or Rank Math > Titles & Meta; per page "
                      "in the SEO box.",),
        "shopify": ("Search engine listing > Page title on each product/page/collection.",),
        "magento": ("Meta Title per product/page; default pattern in Stores > Configuration > Catalog > Catalog > "
                    "Product Fields Auto-Generation.",),
        "nextjs": ("metadata.title (template: '%s | Brand' in the root layout sets the suffix).",)},
       effort="medium"),
    _f("titles_duplicate", "content", "duplicate titles",
       "Pages share a title because they were cloned from each other, or the CMS falls back to a default title.",
       "When cloning a page, change its title, H1 and description before publishing.",
       ["Give each page a unique title that says what makes it different."], effort="medium"),
    _f("meta_missing", "content", "missing meta descriptions",
       "Pages were published without a meta description and no default template fills it.",
       "Add the meta description to the pre-publish checklist; set a sensible default template for posts/products.",
       ["Write a unique 120-155 character description for each important page."],
       {"wordpress": ("Edit the page > Yoast/Rank Math box > Meta description; defaults in Yoast SEO > Settings > "
                      "Content types.",),
        "shopify": ("Search engine listing > Meta description on each product/page/collection.",),
        "magento": ("Search Engine Optimization > Meta Description; defaults via Product Fields Auto-Generation.",),
        "nextjs": ("metadata.description in the route's page.tsx / generateMetadata.",)},
       effort="medium"),
    _f("meta_length", "content", "meta description length",
       "Descriptions were written without a length guide or are auto-generated from the first paragraph.",
       "Use the SEO plugin's length bar while writing; aim for 120-155 characters.",
       ["Rewrite descriptions to 120-155 characters with a clear reason to click."], effort="medium"),
    _f("meta_duplicate", "content", "duplicate meta descriptions",
       "Pages were cloned or the CMS uses one default description for many pages.",
       "Change the description whenever a page is cloned.",
       ["Write a unique description per page (or let the default template include the page's own name)."],
       effort="medium"),
    _f("h1_missing", "template", "H1 headings",
       "The page template does not print the page title as an <h1>, the heading was styled with a different tag, "
       "or the theme also uses <h1> for the logo.",
       "Check one page of every template after theme or page-builder changes: exactly one <h1> per page.",
       ["Make the template output the page title once as <h1>; use h2/h3 or plain text for logos and widgets."],
       {"wordpress": ("Elementor: set the heading widget's HTML tag to H1; theme: single.php/page.php should print "
                      "the_title() in <h1>.",),
        "shopify": ("sections/main-product.liquid / main-page.liquid: the title should use <h1>.",),
        "magento": ("The page title block (Magento_Theme::html/title.phtml) prints the H1; check the theme override.",),
        "nextjs": ("Render one <h1> in each page component.",)},
       effort="medium"),
    _f("headings_quality", "content", "heading structure",
       "Headings are used for styling (big text) instead of structure, or a repeated widget (footer, sidebar, cards) "
       "uses heading tags.",
       "Pick heading tags by meaning, not by size; style with CSS classes.",
       ["Keep one H1, then H2 for sections and H3 inside them.",
        "Change repeated widget titles (footer, sidebar, cards) to plain text or a lower level once in the template."],
       effort="medium"),
    _f("content_thin", "content", "thin content",
       "Pages were published with very little text (placeholders, listing pages, contact-like pages).",
       "Set a minimum content brief for new pages; noindex utility pages (thank-you, login).",
       ["Add useful text (what it is, who it is for, details, FAQs) or merge the page into a stronger one.",
        "Set utility pages to noindex."], effort="high"),
    _f("content_duplicate", "content", "duplicate content",
       "The same content is reachable at several addresses (cloned pages, parameter or print versions, test copies).",
       "Do not publish copies; use canonical tags when two URLs must show the same content.",
       ["Keep one version; 301-redirect or canonicalise the others."], effort="medium"),
    _f("content_quality", "content", "content quality",
       "Text is hard to read or still contains placeholder text from the theme demo.",
       "Remove demo content during site setup; review readability before publishing.",
       ["Replace placeholder text.", "Shorten sentences and use simpler words on key pages."], effort="medium"),
    _f("soft_404", "content", "soft 404 pages",
       "A page shows an error or 'not found' message but the server answers 200 OK, so search engines keep it.",
       "Make the CMS return a real 404 status for missing content.",
       ["Return 404/410 for gone pages, or redirect them; fill real pages that only look empty."], effort="medium"),
    _f("images_alt", "content", "image alt text",
       "Images were uploaded without alternative text, or the theme/page builder outputs images without passing "
       "the alt text through.",
       "Make alt text part of the upload routine (media library field) and the pre-publish checklist.",
       ["Add a short description of each image as alt text (decorative images: alt=\"\" on purpose)."],
       {"wordpress": ("Media > Library > image > Alternative Text (fills every new use); Elementor image widgets "
                      "may need the alt per widget.",),
        "shopify": ("Products > product > Media > image > Add alt text; theme images in the section settings.",),
        "magento": ("Catalog > Products > Images and Videos > image > Alt Text.",),
        "nextjs": ("next/image requires alt; pass it from the CMS field.",)},
       effort="medium"),
    _f("images_weight", "content", "heavy images",
       "Images were uploaded at camera/design-tool size and the site does not compress or resize them.",
       "Compress and resize on upload (an image optimisation plugin/app or the CDN), and use WebP/AVIF.",
       ["Re-export images at display size and compress them; serve WebP/AVIF."],
       {"wordpress": ("An optimiser plugin (ShortPixel, Imagify, EWWW, Smush) can bulk-compress the library.",),
        "shopify": ("Shopify's CDN resizes when the theme uses image_url with a width; re-upload huge originals "
                    "smaller.",),
        "magento": ("Resize originals before upload; enable image optimisation on the CDN (Fastly IO on Cloud).",),
        "nextjs": ("Use next/image with sizes; keep originals under ~300 KB.",)},
       effort="medium"),
    _f("images_size_attr", "template", "image width/height",
       "The theme or page builder prints <img> without width and height, so the layout jumps while images load.",
       "Use a theme/page builder version that outputs dimensions; check Core Web Vitals (CLS) after theme updates.",
       ["Output width and height on <img> in the template (or the CSS aspect-ratio)."],
       effort="medium"),
    _f("canonical_setup", "config", "canonical tags",
       "Canonical tags are missing, duplicated or point at the wrong URL, usually because of a theme/plugin setting "
       "or pages copied from another URL.",
       "Let exactly one tool (the SEO plugin) write canonicals; check them after migrations.",
       ["Each indexable page should have one canonical pointing at itself (absolute https URL)."],
       {"wordpress": ("Yoast/Rank Math write canonicals; per-page override in the SEO box > Advanced.",),
        "shopify": ("theme.liquid should contain {{ canonical_url }} once.",),
        "magento": ("Stores > Configuration > Catalog > Catalog > Search Engine Optimization > Use Canonical Link "
                    "Meta Tag for Categories/Products.",),
        "nextjs": ("metadata.alternates.canonical in the layout / page.",)},
       effort="low"),
    _f("pagination_setup", "config", "pagination markup", "The theme writes rel=next/prev links that point at "
       "missing or wrong pages.", "Test paginated archives after theme changes.",
       ["Fix the pagination links in the archive template; make sure every page in the series loads."],
       effort="low"),
    _f("noindex", "cms_setting", "noindex / nofollow",
       "Pages carry a robots noindex/nofollow from a CMS setting (archives, a 'hide from search' toggle, a "
       "site-wide 'discourage search engines' switch left on after launch).",
       "Keep a written list of which page types are indexable; check the switch after every launch or migration.",
       ["Confirm each noindexed page is meant to be hidden.", "Remove noindex from pages that should rank."],
       {"wordpress": ("Settings > Reading > 'Discourage search engines' must be off; per page: SEO box > Advanced; "
                      "archives: Yoast SEO > Settings > Categories & tags, Rank Math > Titles & Meta.",),
        "shopify": ("Theme code (theme.liquid) or an SEO app adds noindex; check templates for 'noindex'.",),
        "magento": ("Content > Design > Configuration > Search Engine Robots (Default Robots), and per CMS page.",),
        "nextjs": ("metadata.robots in the route's layout/page (index: false).",)},
       effort="low"),
    _f("directives_misc", "config", "robots directives", "Extra robots directives (nosnippet, noimageindex, ...) "
       "were set by a plugin or theme option.", "Only use special directives on purpose and document why.",
       ["Remove the directive where it was not intended."], effort="low"),
    _f("hreflang_setup", "config", "hreflang", "Language versions do not link back to each other correctly, "
       "usually a multilingual plugin setting or pages missing a translation.",
       "Let one multilingual tool generate hreflang; publish translations as complete sets.",
       ["Every language version lists all versions (itself included) and is linked back from each of them."],
       {"wordpress": ("WPML / Polylang language settings; translate or unlink the missing versions.",),
        "shopify": ("Shopify Markets / Translate & Adapt write hreflang; check Settings > Markets > domains.",)},
       effort="medium"),
    _f("amp_setup", "config", "AMP pages", "AMP versions are broken or not linked back to the normal page.",
       "If AMP is not needed any more, turn it off completely and redirect AMP URLs.",
       ["Fix the AMP template or disable AMP and 301 the AMP URLs to the normal pages."], effort="medium"),
    _f("structured_data", "template", "structured data",
       "Schema markup comes from the theme or an SEO/schema plugin; missing or broken markup usually means the "
       "template or plugin setting does not cover that page type.",
       "Validate one page per template in Google's Rich Results Test after theme/plugin updates.",
       ["Add or fix the schema type for the page template (Product, Article, Organization...)."],
       {"wordpress": ("Yoast/Rank Math > Schema settings per content type; remove duplicate schema plugins.",),
        "shopify": ("Theme sections output Product JSON-LD; check main-product.liquid or a schema app.",),
        "magento": ("Theme templates or a structured-data extension output it.",),
        "nextjs": ("Render a <script type=\"application/ld+json\"> in the page component.",)},
       effort="medium"),
    _f("internal_linking", "links", "internal linking",
       "Pages are hard to reach (many clicks deep, linked only from a few places, or only through nofollow / "
       "non-indexable pages), usually because navigation and related-content links do not cover them.",
       "Link every new page from at least one relevant hub page and the sitemap.",
       ["Add links from relevant hub pages or menus; avoid nofollow on internal links."], effort="medium"),
    _f("anchor_text", "template", "link text",
       "Links have no readable text (icon/logo links) or say only 'read more'; often one repeated element in the "
       "header, footer or card template.",
       "Give icon links an aria-label/alt and write link text that names the target.",
       ["Add descriptive text (or alt/aria-label for icon links) to the links."], effort="low"),
    _f("outlinks_volume", "template", "number of links per page",
       "A page links to very many URLs, often because the footer/menu repeats many external or internal links.",
       "Keep site-wide footer and menu link lists short.",
       ["Trim repeated link lists in the template; keep content links purposeful."], effort="low"),
    _f("localhost_links", "content", "links to localhost",
       "Links to localhost / 127.0.0.1 were copied from a development site.",
       "Search the database for localhost after every migration.",
       ["Replace localhost links with the live URL."], effort="low"),
    _f("sitemap_setup", "config", "XML sitemap",
       "The XML sitemap is generated with the wrong rules: it lists noindexed/redirected pages, misses live pages or "
       "keeps old URLs.",
       "Let the SEO plugin build the sitemap from indexable pages only; re-submit it in Search Console after changes.",
       ["Remove non-indexable URLs from the sitemap; add missing indexable pages; link orphan pages from the site."],
       {"wordpress": ("Yoast SEO > Settings > Site features > XML sitemaps, or Rank Math > Sitemap Settings "
                      "(exclude noindexed types).",),
        "shopify": ("Shopify builds /sitemap.xml itself; hidden products and noindexed pages are dropped "
                    "automatically by the theme seo settings.",),
        "magento": ("Marketing > SEO & Search > Site Map, settings in Stores > Configuration > Catalog > XML Sitemap.",),
        "nextjs": ("app/sitemap.ts: build the list from published, indexable routes only.",)},
       effort="low"),
    _f("html_validation", "template", "HTML structure",
       "The template prints elements in the wrong place (for example a <div> or <img> inside <head>), which can end "
       "the <head> early so search engines miss tags after it.",
       "Validate the page source of each template after adding tracking codes or widgets to the head.",
       ["Move non-head elements (img, div, iframe) out of <head>; keep one <head> and one <body>."], effort="low"),
    _f("mobile", "template", "mobile setup", "The template has no responsive viewport meta tag or serves a "
       "separate mobile site.", "Use one responsive template with <meta name=\"viewport\" content=\"width=device-width, "
       "initial-scale=1\">.", ["Add the viewport meta tag to the shared head template."], effort="low"),
    _f("slow_server", "server", "slow server answers",
       "Pages take long to leave the server: no full-page cache, slow hosting or database, or heavy plugins that "
       "run on every request. A small server also slows down when many requests arrive at once (the crawl's "
       "parallel requests add load).",
       "Keep a page cache in front of the CMS and watch the server response time (TTFB) after plugin or hosting "
       "changes.",
       ["Turn on full-page caching (server cache, caching plugin or a CDN that caches HTML).",
        "Find the slowest templates and remove or replace the plugins or queries that slow them down.",
        "Re-check a few pages one at a time: when they are fast, the server only slows down under load."],
       {"wordpress": ("A page-cache plugin (WP Rocket, LiteSpeed Cache, W3 Total Cache) or the host's server cache; "
                      "Query Monitor shows the slow plugins and database queries.",),
        "shopify": ("Shopify hosts the store: remove unused apps and heavy Liquid loops (large collections, many "
                    "sections) from the slow templates.",),
        "magento": ("Full Page Cache with Varnish (Stores > Configuration > Advanced > System), production mode, "
                    "and a Redis cache.",),
        "nextjs": ("Serve pages statically or with ISR (revalidate) instead of rendering on every request; cache "
                   "slow data fetches.",)},
       effort="medium", verify="Re-check a few pages: each should answer in under 1 second."),
    _f("caching", "server", "browser caching and compression",
       "Cache and compression headers come from the web server, CDN or a caching plugin. Files with a short or no "
       "cache lifetime (or sent uncompressed) mean nobody has set browser-caching rules for them.",
       "Keep caching rules in the server/CDN config and use versioned file names for CSS and JavaScript.",
       ["Give versioned CSS, JavaScript, fonts and images a long Cache-Control max-age (for example one year).",
        "Give HTML a short lifetime or no-cache with ETag / Last-Modified, so updates show at once.",
        "Turn on gzip or brotli compression for HTML, CSS and JavaScript."],
       {"wordpress": ("The browser-caching option of the caching plugin (WP Rocket, LiteSpeed Cache), or Expires / "
                      "Cache-Control rules in .htaccess or the Nginx server block.",),
        "shopify": ("Shopify's CDN sets cache headers for theme files; files of third-party apps are outside your "
                    "control.",),
        "magento": ("Cache headers for pub/static and pub/media in the Nginx/Apache config; Varnish for HTML.",),
        "nextjs": ("Files under /_next/static are cached automatically; set headers() in next.config.js for public/ "
                   "files.",)},
       effort="low", verify="Open a CSS file and an image in the browser dev tools: Cache-Control shows a long max-age."),
    _f("page_weight", "template", "page weight",
       "Heavy pages usually load many scripts, styles and large images from the theme, page builder and plugins on "
       "every page, or nest many wrapper elements.",
       "Check the weight of one page per template after adding plugins, widgets or tracking tags.",
       ["Remove scripts and styles the page does not use; defer the rest.",
        "Compress and resize images; lazy-load images below the fold.",
        "Simplify page-builder layouts that nest many wrapper elements."],
       {"wordpress": ("Unload plugin files per page with Asset CleanUp or Perfmatters; Elementor > Settings > "
                      "Performance (optimized DOM output, improved asset loading).",),
        "shopify": ("Remove leftover app code from theme.liquid and load app blocks only where they are used.",),
        "magento": ("Enable JS/CSS bundling and minification in production mode; remove unused modules' assets.",),
        "nextjs": ("Split large bundles with dynamic imports and use next/image for images.",)},
       effort="medium"),
    _f("accessibility", "template", "accessibility",
       "Accessibility gaps usually sit in shared templates and widgets (menus, icon links, forms, sliders) that "
       "miss labels, accessible names, landmarks or a heading order.",
       "Check one page per template with an accessibility checker (Lighthouse, axe) before each release.",
       ["Give every form field a <label>, icon links and buttons an aria-label, and images alt text.",
        "Wrap the main content in <main> and keep headings in order (H1, then H2, then H3).",
        "Set <html lang> and remove duplicate ids from the template.",
        "Work through the axe-core findings by impact: critical and serious ones first (they stop some people from "
        "using the page at all), then moderate, then minor."],
       effort="low", verify="Re-crawl (or run Lighthouse > Accessibility on one page per template)."),
    _f("social_tags", "cms_setting", "social sharing tags",
       "Open Graph and Twitter card tags are written by the SEO plugin or the theme; missing ones mean the social "
       "settings or a default share image are not set.",
       "Set a default share image and check the share preview of new page types.",
       ["Turn on Open Graph and Twitter card output in the SEO plugin or theme.",
        "Set a default share image, and a specific one for the key pages."],
       {"wordpress": ("Yoast SEO > Settings > Site features (Open Graph, Twitter), or Rank Math > Titles & Meta > "
                      "Social Meta; set the default image there.",),
        "shopify": ("Online Store > Preferences > Social sharing image; the theme prints the og: tags in "
                    "theme.liquid.",)},
       effort="low"),
    _f("code_hygiene", "template", "links and markup crawlers cannot use",
       "The template or content has links and markup browsers tolerate but crawlers cannot follow: javascript: "
       "links, empty # anchors, malformed URLs, or pages without a doctype or character set.",
       "Use real <a href> links for navigation and <button> for actions in new components.",
       ["Replace javascript: and '#' links with real URLs (or a <button> for actions).",
        "Fix malformed link URLs in the template or content.",
        "Start every page with <!DOCTYPE html> and <meta charset=\"utf-8\">."], effort="low"),
    _f("javascript_rendering", "template", "JavaScript-dependent content",
       "Titles, links or content that appear (or change) only after JavaScript runs depend on Google rendering "
       "the page; script errors and blocked files can stop that.",
       "Keep titles, canonicals, the H1, main content and navigation links in the server HTML.",
       ["Put the title, description, canonical, H1 and main links in the HTML the server sends.",
        "Fix the JavaScript errors shown in the Chrome Console Log of the affected templates.",
        "Unblock CSS and JavaScript files that robots.txt blocks."], effort="medium"),
    _f("third_party", "plugin", "third-party tags",
       "Every analytics, chat, font, video or ad tool adds requests to other servers; tags are added by plugins, the "
       "theme or a tag manager, and missing tracking means a template does not include the tag.",
       "Load third-party tags through one tag manager and review the list every quarter.",
       ["List the third-party hosts and remove the tools nobody uses.",
        "Load the rest through one tag manager and defer the non-essential tags.",
        "Add the analytics tag to the templates that miss it."], effort="low"),
    _f("bot_access", "config", "search and AI crawler access",
       "robots.txt rules, a firewall or CDN bot protection, or the server answer search engines and AI crawlers "
       "differently from browsers.",
       "Review robots.txt and the bot-protection rules whenever crawler access is changed on purpose.",
       ["Check the robots.txt groups for Googlebot and for the AI crawlers you want to allow (or block).",
        "Allow-list verified search and AI crawlers in the firewall / CDN bot protection.",
        "Serve crawlers the same HTML as browsers."], effort="low"),
    _f("copy_editing", "content", "spelling and grammar",
       "Typos and grammar slips come from copy that went live without a second read: rushed updates, text pasted "
       "from other tools, or product and brand words the editor's dictionary does not know.",
       "Run a spelling and grammar check in the editor before publishing, keep a shared list of brand and product "
       "words, and have a second person read new landing pages.",
       ["Open each flagged page and correct the misspelt words and grammar slips in the CMS.",
        "Add real brand, product and technical words to the site dictionary so they stop being flagged.",
        "Fix shared text (menus, footers, reusable blocks) once in its template or block."],
       effort="low", verify="Re-crawl and check the Spelling / Grammar counts drop."),
    _f("llms_txt", "config", "llms.txt file",
       "llms.txt is a Markdown file at the site root that tells AI tools what the site is and which pages to read. "
       "Problems come from a file that was never published (the address shows the site's error page), was written "
       "by hand without the expected outline, or lists pages that moved or were removed later.",
       "Keep llms.txt in the same release checklist as robots.txt and the XML sitemap: update its links when pages "
       "move, and check it after each site migration.",
       ["Publish /llms.txt as a text file (text/plain or text/markdown) that starts with '# Site name'.",
        "Add a one-line '> summary', then '## Section' lists of '- [Page](URL): note' links to the key pages.",
        "Point every link at the final, indexable URL (no redirects, errors or noindex pages)."],
       {"wordpress": ("Upload llms.txt to the site root over SFTP / the host's file manager, or create it with an "
                      "SEO plugin that writes llms.txt (Yoast and Rank Math have options).",),
        "shopify": ("Shopify cannot serve arbitrary files at the root: publish the text as a page or use an app "
                    "that serves /llms.txt through an app proxy.",),
        "magento": ("Place llms.txt in the pub/ folder (the web root) next to robots.txt.",),
        "nextjs": ("Put llms.txt in public/ (served at /llms.txt), or return it from app/llms.txt/route.ts.",)},
       effort="low", verify="Open /llms.txt in a browser: it shows plain text, and every link opens a 200 page."),
    _f("indexing", "cms_setting", "pages missing from Google",
       "Google reports pages it has not indexed, has indexed with problems, or shows no search data for: often "
       "pages that are thin or duplicate, blocked or noindex by mistake, new, or reachable only through the sitemap.",
       "Check Search Console's Page indexing report after each release and keep important pages linked from the "
       "menus and listed in the XML sitemap.",
       ["Open the URL in Search Console > URL Inspection to see Google's own reason.",
        "Make sure the page answers 200, is indexable, self-canonical and linked from other pages.",
        "Improve thin or duplicate pages, then request indexing for the important ones."], effort="medium",
       verify="Re-fetch Search Console data after Google has crawled the pages again (it can take days)."),
    _f("backlinks", "content", "backlink metrics",
       "Link metrics (from Moz, Majestic or Ahrefs) show how other websites link to the pages; weak or missing "
       "numbers usually mean pages that earn few links, or pages whose links point at an old address.",
       "Review the pages with the most backlinks after every migration so none of them breaks or loses its redirect.",
       ["Make sure pages with many backlinks answer 200, or 301-redirect to the closest live page.",
        "Link internally to the pages that hold outside links, so their value reaches the rest of the site.",
        "Plan outreach or content for important pages that earn no links."], effort="medium"),
    _f("crawl_activity", "server", "search engine crawl activity",
       "Server logs show what search engines and AI crawlers actually requested: errors they hit, pages they never "
       "visit, and bots that only claim to be Googlebot.",
       "Check the log report after releases and migrations, and keep important pages linked and in the sitemap so "
       "crawlers keep visiting them.",
       ["Fix the URLs crawlers get 4xx / 5xx answers on (or redirect them to the right page).",
        "Link to and list in the sitemap the important pages Googlebot never requested.",
        "Block or rate-limit bots whose user agent says Googlebot but whose address does not verify."],
       effort="medium"),
    _f("other", "content", "other issues", "This issue has no specific explanation yet.",
       "Review the issue description and add the fix to the team checklist.",
       ["See the issue's own description and fix in the Issues tab."], effort="medium"),
]}

TAB_FAMILY = {
    "security": "https", "response_codes": "broken_internal", "url": "url_format", "page_titles": "titles_length",
    "meta_description": "meta_length", "h1": "headings_quality", "h2": "headings_quality", "content": "content_quality",
    "images": "images_alt", "canonicals": "canonical_setup", "pagination": "pagination_setup",
    "directives": "directives_misc", "hreflang": "hreflang_setup", "amp": "amp_setup",
    "structured_data": "structured_data", "links": "internal_linking", "sitemaps": "sitemap_setup",
    "validation": "html_validation", "mobile": "mobile",
    "performance": "page_weight", "pagespeed": "page_weight", "accessibility": "accessibility",
    "social": "social_tags", "hygiene": "code_hygiene", "javascript": "javascript_rendering",
    "third_parties": "third_party", "ai_crawlers": "bot_access", "robots_txt": "bot_access",
    "parameters": "url_params",
    # Build 4 tabs: AI prompts review the wording of pages (content quality); backlink metrics and server logs
    # have families of their own
    "ai": "content_quality", "link_metrics": "backlinks", "log_files": "crawl_activity",
    "search_console": "indexing", "analytics": "content_quality",
}
# Generated filters that all share one family: (tab, filter id prefix) -> family
FILTER_PREFIX_FAMILY: Tuple[Tuple[str, str, str], ...] = (
    ("accessibility", "axe_", "accessibility"),      # one filter per axe-core rule (A3); steps ordered by impact
)
FILTER_FAMILY = {
    ("security", "missing_hsts"): "server_headers", ("security", "missing_csp"): "server_headers",
    ("security", "missing_xcto"): "server_headers", ("security", "missing_xfo"): "server_headers",
    ("security", "missing_referrer_policy"): "server_headers", ("security", "bad_content_type"): "content_type",
    ("security", "unsafe_cross_origin"): "cross_origin",
    ("response_codes", "internal_no_response"): "server_errors", ("response_codes", "internal_server_error"): "server_errors",
    ("response_codes", "internal_client_error"): "broken_internal",
    ("response_codes", "internal_redirection"): "redirects", ("response_codes", "internal_meta_refresh"): "redirects",
    ("response_codes", "internal_http_refresh"): "redirects",
    ("response_codes", "internal_redirect_chain"): "redirect_chains", ("response_codes", "internal_redirect_loop"): "redirect_chains",
    ("response_codes", "internal_blocked_robots"): "robots_blocked",
    ("response_codes", "external_no_response"): "broken_external", ("response_codes", "external_client_error"): "broken_external",
    ("response_codes", "external_server_error"): "broken_external",
    ("url", "parameters"): "url_params", ("url", "ga_tracking"): "url_params", ("url", "internal_search"): "url_params",
    ("page_titles", "missing"): "titles_missing", ("page_titles", "multiple"): "titles_missing",
    ("page_titles", "outside_head"): "html_validation", ("page_titles", "duplicate"): "titles_duplicate",
    ("page_titles", "same_as_h1"): "titles_duplicate",
    ("meta_description", "missing"): "meta_missing", ("meta_description", "multiple"): "meta_missing",
    ("meta_description", "outside_head"): "html_validation", ("meta_description", "duplicate"): "meta_duplicate",
    ("h1", "missing"): "h1_missing", ("h1", "multiple"): "h1_missing", ("h1", "alt_text_in_h1"): "h1_missing",
    ("content", "low_content_pages"): "content_thin", ("content", "exact_duplicates"): "content_duplicate",
    ("content", "near_duplicates"): "content_duplicate", ("content", "soft_404_pages"): "soft_404",
    ("images", "over_100_kb"): "images_weight", ("images", "missing_size_attributes"): "images_size_attr",
    ("directives", "noindex"): "noindex", ("directives", "nofollow"): "noindex", ("directives", "none"): "noindex",
    ("directives", "outside_head"): "html_validation", ("canonicals", "outside_head"): "html_validation",
    ("hreflang", "outside_head"): "html_validation",
    ("links", "no_anchor_text"): "anchor_text", ("links", "non_descriptive_anchor"): "anchor_text",
    ("links", "high_external_outlinks"): "outlinks_volume", ("links", "high_internal_outlinks"): "outlinks_volume",
    ("links", "outlinks_to_localhost"): "localhost_links",
    ("performance", "slow_1s"): "slow_server", ("performance", "slow_3s"): "slow_server",
    ("performance", "slowest_10pct"): "slow_server", ("performance", "uncacheable"): "caching",
    ("performance", "short_cache"): "caching", ("pagespeed", "document_request_latency"): "slow_server",
    ("pagespeed", "efficient_cache_lifetimes"): "caching",
    ("accessibility", "images_no_alt"): "images_alt",
    ("hygiene", "no_cache_control"): "caching", ("hygiene", "no_compression"): "caching",
    ("hygiene", "large_dom"): "page_weight", ("hygiene", "http_internal_links"): "https",
    ("hygiene", "cross_host_canonical"): "canonical_setup", ("hygiene", "soft_404_server"): "soft_404",
    ("hygiene", "staging_links"): "localhost_links",
    # Build 4 (PLAN4_build.md 10.6); the All Errors views are plain filters, mapped for completeness
    ("response_codes", "all_error"): "broken_internal", ("response_codes", "internal_all_error"): "broken_internal",
    ("response_codes", "external_all_error"): "broken_external",
    ("response_codes", "external_meta_refresh"): "broken_external",
    ("response_codes", "external_http_refresh"): "broken_external",
    ("validation", "high_carbon_rating"): "page_weight",
    ("content", "spelling_errors"): "copy_editing", ("content", "grammar_errors"): "copy_editing",
    ("links", "no_content_inlinks"): "internal_linking",
    ("ai_crawlers", "llms_txt_missing"): "llms_txt", ("ai_crawlers", "llms_txt_invalid"): "llms_txt",
    ("ai_crawlers", "llms_txt_broken_links"): "llms_txt", ("ai_crawlers", "llms_txt_html"): "llms_txt",
    ("log_files", "bot_4xx"): "crawl_activity", ("log_files", "bot_5xx"): "crawl_activity",
    ("log_files", "not_hit_by_googlebot"): "crawl_activity", ("log_files", "unverified_bot_hits"): "crawl_activity",
    ("log_files", "googlebot_hits"): "crawl_activity", ("log_files", "ai_bot_hits"): "crawl_activity",
    ("link_metrics", "with_metrics"): "backlinks", ("link_metrics", "no_metrics"): "backlinks",
    ("link_metrics", "not_fetched"): "backlinks",
    # Search Console and GA4 filters (Build 3) with the family that fixes them (they fell back to "other" before)
    ("search_console", "orphan_urls"): "internal_linking", ("search_console", "non_indexable_with_data"): "noindex",
    ("search_console", "canonical_not_selected"): "canonical_setup",
    ("search_console", "not_mobile_friendly"): "mobile",
    ("search_console", "rich_result_invalid"): "structured_data",
    ("search_console", "no_search_analytics_data"): "indexing", ("search_console", "url_not_on_google"): "indexing",
    ("search_console", "indexable_not_indexed"): "indexing", ("search_console", "on_google_has_issues"): "indexing",
    ("analytics", "orphan_urls"): "internal_linking", ("analytics", "non_indexable_with_ga_data"): "noindex",
    ("analytics", "no_ga_data"): "third_party", ("analytics", "bounce_rate_above_70"): "content_quality",
}


def family_source(tab: str, filter_id: str) -> str:
    """Where a filter's family comes from: "filter" (FILTER_FAMILY), "prefix" (FILTER_PREFIX_FAMILY), "tab"
    (the tab's family) or "other" (no mapping at all)."""
    if (tab, filter_id) in FILTER_FAMILY:
        return "filter"
    if any(tab == t and filter_id.startswith(prefix) for t, prefix, _ in FILTER_PREFIX_FAMILY):
        return "prefix"
    return "tab" if tab in TAB_FAMILY else "other"


def family_of(tab: str, filter_id: str) -> str:
    fam = FILTER_FAMILY.get((tab, filter_id))
    if fam:
        return fam
    for t, prefix, family in FILTER_PREFIX_FAMILY:
        if tab == t and filter_id.startswith(prefix):
            return family
    return TAB_FAMILY.get(tab) or "other"


def region_steps(platform_key: str, region: str, plugins: Tuple[str, ...] = ()) -> List[str]:
    """Where to edit a repeated element (header, footer, menu...) on the detected platform."""
    out: List[str] = []
    builder = next((p for p in plugins if "header" in p.lower() and "footer" in p.lower()), "")
    if builder and region in ("Header", "Footer"):
        out.append(f"The {region.lower()} is built with {builder}: edit the {region.lower()} template there.")
    where = REGION_WHERE.get(platform_key, {}).get(region)
    if where:
        out.append(f"Edit it in {where}.")
    elif platform_key in TEMPLATE_WHERE:
        out.append(f"Edit it in {TEMPLATE_WHERE[platform_key]}.")
    return out


def platform_steps(family: Family, platform_key: str, extra: Tuple[str, ...] = ()) -> List[str]:
    """Steps for the detected platform; when it is unknown, one line per main platform (labelled)."""
    out = list(extra)
    if platform_key in PLATFORMS:
        out += list(family.steps.get(platform_key, ()))
        return out
    for key in PLATFORMS:
        for step in family.steps.get(key, ())[:1]:
            out.append(f"{PLATFORM_LABELS[key]}: {step}")
    return out
