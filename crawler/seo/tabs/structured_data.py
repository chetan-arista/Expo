"""
Structured Data tab (Screaming Frog "Structured Data"): schema.org markup found on each internal
HTML page that loaded (2xx), in JSON-LD, Microdata and RDFa.

How the counts work:
  - Contains Structured Data + Missing = All. A page whose only JSON-LD block is broken still "contains"
    structured data (it is counted under Parse Errors as well).
  - JSON-LD / Microdata / RDFa URLs overlap: a page using two formats is in both.
  - Validation Errors / Warnings are a BASIC check of our own, not the schema.org validator: Errors = a
    few common types without the properties they cannot do without (REQUIRED below); Warnings = common
    types without the properties that describe them well (RECOMMENDED below). On top of that, every item is
    checked against the schema.org vocabulary (crawler/seo/schema_vocab.py, P12): unknown types and properties
    and values of the wrong kind (an object where text is expected, a date that is not a date) are Errors; a
    property used on a type that does not list it and replaced terms are Warnings. JSON-LD items are checked down
    through their nested items; Microdata and RDFa items by their type only.
  - Rich Result Validation Errors / Warnings / Rich Result Feature Detected follow Google's rich result
    features (RICH_RESULTS below: our own summary of the required and recommended properties Google Search
    Central lists for each feature, checked 2026-10). An item counts as a feature when its type is one
    Google turns into a rich result; Errors = a required property is missing, Warnings = a recommended one
    is. Features Google no longer shows (FAQ, How-to, Fact check...) are listed in the Rich Result
    Features column as "no longer shown" and never counted as detected.
  - Properties are checked on the top-level items of each block (and @graph members). Nested properties
    such as offers.price are checked in full when the extractor recorded the item's nested paths
    (item["paths"]; every JSON-LD item since Build 4), else down to their first level (offers present: pages
    stored by an older version).
  - Schema Values Not On Page / Schema Price Not On Page compare JSON-LD values (name, headline, sku,
    telephone, email, address parts, offer price) with the visible page (extract.schema_vs_page, after
    siteone-crawler's idea). A price on a page whose HTML shows no price at all is "unverified" (probably
    drawn by JavaScript) and never counted.
"""
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .. import schema_vocab
from ..registry import Column, Filter, Tab, register, seo
from .internal import ADDRESS, INDEXABILITY, INDEXABILITY_STATUS, memo
from .page_titles import content_rows

# type (lowercase) -> groups of properties; each group needs at least one of its names present
REQUIRED: Dict[str, List[Tuple[str, ...]]] = {
    "product": [("name",), ("offers", "review", "aggregateRating")],
    "article": [("headline",)],
    "blogposting": [("headline",)],
    "newsarticle": [("headline",)],
    "organization": [("name",)],
    "breadcrumblist": [("itemListElement",)],
    "faqpage": [("mainEntity",)],
    "localbusiness": [("name",), ("address",)],
}

# Validation Warnings: properties that describe a common type well (our own list, schema.org best practice)
RECOMMENDED: Dict[str, List[Tuple[str, ...]]] = {
    "website": [("name",), ("url",)],
    "webpage": [("name", "headline")],
    "organization": [("url",), ("logo",)],
    "corporation": [("name",), ("url",), ("logo",)],
    "person": [("name",)],
    "article": [("author",), ("datePublished",), ("image",)],
    "blogposting": [("author",), ("datePublished",), ("image",)],
    "newsarticle": [("author",), ("datePublished",), ("image",)],
    "product": [("image",), ("description",)],
    "offer": [("price", "priceSpecification"), ("priceCurrency", "priceSpecification")],
    "localbusiness": [("telephone",), ("url",), ("openingHoursSpecification", "openingHours")],
    "event": [("description",), ("image",), ("endDate",)],
    "videoobject": [("description",)],
    "imageobject": [("contentUrl", "url")],
    "recipe": [("description",), ("recipeIngredient",), ("recipeInstructions",)],
    "jobposting": [("validThrough",), ("employmentType",)],
    "review": [("datePublished",)],
}

FORMAT_LABEL = {"jsonld": "JSON-LD", "microdata": "Microdata", "rdfa": "RDFa"}

# ---------------------------------------------------------------------------------------------
# Google rich result features. Each rule: feature name, schema types (lowercase), required groups,
# recommended groups, and an optional applies() test on the item's property names. A group is a tuple of
# alternatives (any one is enough); "offers.price" = the price inside offers.
# ---------------------------------------------------------------------------------------------
Group = Tuple[str, ...]

LOCAL_BUSINESS_TYPES = {
    "localbusiness", "store", "restaurant", "foodestablishment", "cafeorcoffeeshop", "bakery", "barorpub",
    "fastfoodrestaurant", "dentist", "physician", "medicalclinic", "medicalbusiness", "hospital", "pharmacy",
    "optician", "autorepair", "autodealer", "automotivebusiness", "hotel", "motel", "lodgingbusiness",
    "realestateagent", "legalservice", "attorney", "notary", "accountingservice", "financialservice",
    "insuranceagency", "bankorcreditunion", "homeandconstructionbusiness", "plumber", "electrician",
    "hvacbusiness", "roofingcontractor", "generalcontractor", "housepainter", "locksmith", "movingcompany",
    "healthandbeautybusiness", "beautysalon", "hairsalon", "dayspa", "nailsalon", "exercisegym",
    "sportsactivitylocation", "professionalservice", "travelagency", "childcare", "entertainmentbusiness",
    "governmentoffice", "shoppingcenter", "clothingstore", "electronicsstore", "furniturestore",
    "hardwarestore", "grocerystore", "conveniencestore", "liquorstore", "petstore",
    "sportinggoodsstore", "bookstore", "jewelrystore", "shoestore", "toystore", "mobilephonestore",
    "computerstore", "homegoodsstore", "gardenstore", "florist", "departmentstore", "outletstore",
    "veterinarycare", "emergencyservice", "animalshelter", "employmentagency", "internetcafe", "library",
    "recyclingcenter", "selfstorage", "tattooparlor", "touristinformationcenter", "radiostation",
    "televisionstation", "drycleaningorlaundry",
}
ARTICLE_TYPES = {"article", "newsarticle", "blogposting", "techarticle", "scholarlyarticle", "report",
                 "analysisnewsarticle", "opinionnewsarticle", "reviewnewsarticle", "backgroundnewsarticle"}
ORGANIZATION_TYPES = {"organization", "corporation", "ngo", "educationalorganization", "onlinebusiness",
                      "onlinestore", "governmentorganization", "medicalorganization", "sportsorganization",
                      "newsmediaorganization", "airline", "consortium", "researchorganization"}
SOFTWARE_TYPES = {"softwareapplication", "mobileapplication", "webapplication", "videogame"}
LICENSE_PROPS = ("license", "acquireLicensePage", "creditText", "copyrightNotice", "creator")


class Rule:
    """One Google rich result feature for a set of schema types."""

    def __init__(self, feature: str, types: Set[str], required: Sequence[Group], recommended: Sequence[Group] = (),
                 applies=None):
        self.feature = feature
        self.types = types
        self.required = list(required)
        self.recommended = list(recommended)
        self.applies = applies


RICH_RESULTS: List[Rule] = [
    Rule("Article", ARTICLE_TYPES, [],
         [("author",), ("datePublished",), ("dateModified",), ("headline",), ("image",)]),
    Rule("Breadcrumb", {"breadcrumblist"}, [("itemListElement",)]),
    Rule("Product snippet", {"product", "productgroup"}, [("name",), ("review", "aggregateRating", "offers")],
         [("aggregateRating",), ("offers",), ("review",)]),
    Rule("Merchant listing", {"product"},
         [("name",), ("image",), ("offers",), ("offers.price", "offers.priceSpecification", "offers.lowPrice"),
          ("offers.priceCurrency", "offers.priceSpecification")],
         [("brand",), ("description",), ("sku",), ("gtin", "gtin8", "gtin12", "gtin13", "gtin14", "isbn", "mpn"),
          ("offers.availability",), ("offers.hasMerchantReturnPolicy",), ("offers.shippingDetails",)],
         applies=lambda props: "offers" in props),
    Rule("Event", {"event", "businessevent", "musicevent", "sportsevent", "theaterevent", "festival",
                   "educationevent", "exhibitionevent", "foodevent", "comedyevent", "danceevent", "literaryevent",
                   "saleevent", "screeningevent", "socialevent", "childrensevent", "courseinstance"},
         [("name",), ("startDate",), ("location",), ("location.address", "location.url")],
         [("description",), ("endDate",), ("eventStatus",), ("image",), ("offers",), ("organizer",),
          ("performer",)]),
    Rule("Job posting", {"jobposting"},
         [("datePosted",), ("description",), ("hiringOrganization",), ("title",),
          ("jobLocation", "jobLocationType")],
         [("baseSalary",), ("employmentType",), ("identifier",), ("validThrough",), ("directApply",)]),
    Rule("Local business", LOCAL_BUSINESS_TYPES, [("name",), ("address",)],
         [("telephone",), ("url",), ("geo",), ("openingHoursSpecification",), ("priceRange",)]),
    Rule("Organization", ORGANIZATION_TYPES, [],
         [("name",), ("url",), ("logo",), ("sameAs",)]),   # what Google uses for the logo and knowledge panel
    Rule("Review snippet", {"review", "criticreview"},
         [("author",), ("itemReviewed",), ("reviewRating",), ("reviewRating.ratingValue",)],
         [("datePublished",)]),
    Rule("Review snippet", {"aggregaterating"},
         [("itemReviewed",), ("ratingValue",), ("ratingCount", "reviewCount")], [("bestRating",), ("worstRating",)]),
    Rule("Employer aggregate rating", {"employeraggregaterating"},
         [("itemReviewed",), ("ratingValue",), ("ratingCount", "reviewCount")], [("bestRating",), ("worstRating",)]),
    Rule("Recipe", {"recipe"}, [("name",), ("image",)],
         [("aggregateRating",), ("author",), ("cookTime",), ("datePublished",), ("description",), ("keywords",),
          ("nutrition",), ("prepTime",), ("recipeCategory",), ("recipeCuisine",), ("recipeIngredient",),
          ("recipeInstructions",), ("recipeYield",), ("totalTime",), ("video",)]),
    Rule("Video", {"videoobject"}, [("name",), ("thumbnailUrl",), ("uploadDate",)],
         [("contentUrl", "embedUrl"), ("description",), ("duration",)]),
    Rule("Software app", SOFTWARE_TYPES, [("name",), ("offers",), ("offers.price",), ("aggregateRating", "review")],
         [("applicationCategory",), ("operatingSystem",)]),
    Rule("Dataset", {"dataset"}, [("name",), ("description",)],
         [("creator",), ("license",), ("url",), ("keywords",), ("identifier",)]),
    Rule("Q&A", {"qapage"}, [("mainEntity",)]),
    Rule("Discussion forum", {"discussionforumposting", "socialmediaposting"},
         [("author",), ("datePublished",), ("text", "image", "video", "url")],
         [("dateModified",), ("headline",), ("url",), ("comment",), ("interactionStatistic",)]),
    Rule("Profile page", {"profilepage"}, [("mainEntity",)], [("dateCreated",), ("dateModified",)]),
    Rule("Image metadata", {"imageobject"}, [("contentUrl",), LICENSE_PROPS],
         [("license",), ("acquireLicensePage",), ("creditText",), ("copyrightNotice",), ("creator",)],
         applies=lambda props: any(p.lower() in props for p in LICENSE_PROPS)),
    Rule("Vacation rental", {"vacationrental"},
         [("containsPlace",), ("identifier",), ("image",), ("latitude", "geo"), ("longitude", "geo"), ("name",)],
         [("address",), ("aggregateRating",), ("checkinTime",), ("checkoutTime",), ("description",), ("review",)]),
    Rule("Course list", {"course"}, [("name",), ("description",)], [("provider",)]),
    Rule("Movie carousel", {"movie"}, [("name",), ("image",)],
         [("aggregateRating",), ("dateCreated",), ("director",), ("review",)]),
    Rule("Carousel", {"itemlist"}, [("itemListElement",)]),
    Rule("Math solver", {"mathsolver"}, [("url",), ("usageInfo",), ("potentialAction",)],
         [("inLanguage",), ("assesses",)]),
    Rule("Education Q&A", {"quiz"}, [("hasPart",)], [("about",), ("educationalAlignment",)]),
]

# Types whose Google rich result is gone: shown in the features column, never counted or validated
RETIRED_FEATURES: Dict[str, str] = {
    "faqpage": "FAQ", "howto": "How-to", "claimreview": "Fact check", "specialannouncement": "Special announcement",
    "car": "Vehicle listing", "vehicle": "Vehicle listing", "occupation": "Estimated salary",
}


# ---------------------------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------------------------
def sd(r) -> dict:
    return seo(r).get("structured_data") or {}


def parse_errors(r) -> int:
    return int(sd(r).get("jsonld_parse_errors") or 0)


def all_types(r) -> List[str]:
    d = sd(r)
    return list(d.get("jsonld") or []) + list(d.get("microdata") or []) + list(d.get("rdfa") or [])


def unique_types(r) -> List[str]:
    return list(sd(r).get("types") or [])


def has_format(r, fmt: str) -> bool:
    d = sd(r)
    if d.get(fmt):
        return True
    if any(i.get("format") == fmt for i in d.get("items") or ()):
        return True
    return fmt == "jsonld" and parse_errors(r) > 0


def contains(r) -> bool:
    return any(has_format(r, f) for f in FORMAT_LABEL)


def _items(r) -> List[dict]:
    return [i for i in sd(r).get("items") or () if isinstance(i, dict)]


def _label(item: dict) -> str:
    return f"{item.get('type') or ''} ({FORMAT_LABEL.get(item.get('format'), item.get('format'))})"


def _props(item: dict) -> Set[str]:
    return {str(p).lower() for p in item.get("props") or ()}


def _paths(item: dict) -> Optional[Set[str]]:
    paths = item.get("paths")
    return {str(p).lower() for p in paths} if isinstance(paths, (list, tuple, set)) and paths else None


def _has(item: dict, name: str, props: Set[str], paths: Optional[Set[str]]) -> bool:
    """The item carries the property (a dotted name is checked in full when the nested paths are known,
    else down to its first level)."""
    name = name.lower()
    if "." not in name:
        return name in props
    if paths is not None:
        return name in paths or any(p.startswith(name + ".") for p in paths)
    return name.split(".", 1)[0] in props


def _missing(item: dict, groups: Sequence[Group]) -> List[str]:
    props, paths = _props(item), _paths(item)
    return [" / ".join(g) for g in groups if not any(_has(item, n, props, paths) for n in g)]


def _check(r, table: Dict[str, List[Tuple[str, ...]]]) -> List[str]:
    out: List[str] = []
    for item in _items(r):
        rules = table.get(str(item.get("type") or "").lower())
        if not rules:
            continue
        for gap in _missing(item, rules):
            msg = f"{_label(item)}: missing {gap}"
            if msg not in out:
                out.append(msg)
    return out


def vocab_checks(r) -> Dict[str, List[str]]:
    """schema.org vocabulary errors and warnings of every item of the page, each led by the item's label
    ("Product (JSON-LD): offers.price: ..."), computed once per row."""
    data = seo(r)
    cache = getattr(r, "_sd_vocab", None)
    if cache is not None and cache[0] is data:          # same PageData (a re-check replaces it)
        return cache[1]
    errors: List[str] = []
    warnings: List[str] = []
    for item in _items(r):
        res = schema_vocab.validate(item)
        for src, dst in ((res["errors"], errors), (res["warnings"], warnings)):
            for m in src:
                msg = f"{_label(item)}: {m}"
                if msg not in dst:
                    dst.append(msg)
    out = {"errors": errors, "warnings": warnings}
    try:
        r._sd_vocab = (data, out)
    except Exception:
        pass
    return out


def validation_errors(r) -> List[str]:
    """'Product (JSON-LD): missing offers / review / aggregateRating' for each failed requirement, then the
    schema.org vocabulary errors ('Product (JSON-LD): unknown type 'Prodcut' (did you mean 'Product'?)')."""
    return _check(r, REQUIRED) + [m for m in vocab_checks(r)["errors"]]


def validation_warnings(r) -> List[str]:
    """'Article (JSON-LD): missing image' for each recommended property a common type lacks, then the
    schema.org vocabulary warnings."""
    return _check(r, RECOMMENDED) + [m for m in vocab_checks(r)["warnings"]]


def _rules_for(item: dict) -> List[Rule]:
    t = str(item.get("type") or "").lower()
    props = _props(item)
    return [rule for rule in RICH_RESULTS
            if t in rule.types and (rule.applies is None or rule.applies(props))]


def rich_results(r, site=None) -> Dict[str, Any]:
    """{"features": [names], "retired": [names], "errors": [...], "warnings": [...]} for the page (computed once
    per Site when `site` is given)."""
    if site is not None:
        return memo(site, "sd_rich:" + getattr(r, "url", ""), lambda: rich_results(r))
    features: List[str] = []
    retired: List[str] = []
    errors: List[str] = []
    warnings: List[str] = []
    for item in _items(r):
        t = str(item.get("type") or "").lower()
        if t in RETIRED_FEATURES:
            name = RETIRED_FEATURES[t]
            if name not in retired:
                retired.append(name)
            continue
        for rule in _rules_for(item):
            if rule.feature not in features:
                features.append(rule.feature)
            for gap in _missing(item, rule.required):
                msg = f"{rule.feature} - {_label(item)}: missing {gap}"
                if msg not in errors:
                    errors.append(msg)
            for gap in _missing(item, rule.recommended):
                msg = f"{rule.feature} - {_label(item)}: missing {gap}"
                if msg not in warnings:
                    warnings.append(msg)
    return {"features": features, "retired": retired, "errors": errors, "warnings": warnings}


def _features_cell(r, site) -> List[str]:
    rr = rich_results(r, site)
    return rr["features"] + [f"{n} (no longer shown by Google)" for n in rr["retired"]]


def schema_mismatch(r, price: Optional[bool] = None) -> List[dict]:
    """JSON-LD values the visible page does not show; price=True only prices, False everything else."""
    out = []
    for m in seo(r).get("schema_visible_mismatch") or ():
        if not isinstance(m, dict):
            continue
        is_price = m.get("field") == "price"
        if price is None or is_price == price:
            out.append(m)
    return out


def _mismatch_cell(r, site) -> List[str]:
    return [f"{m.get('type')}.{m.get('field')} = {m.get('value')}" for m in schema_mismatch(r)][:20]


def _nth_type(i: int):
    return lambda r, site: unique_types(r)[i] if len(unique_types(r)) > i else ""


FILTERS = [
    Filter("contains", "Contains Structured Data", lambda r, site: contains(r),
           description="The page carries schema.org structured data in JSON-LD, Microdata or RDFa."),
    Filter("missing", "Missing", lambda r, site: not contains(r),
           issue_type="Opportunity", priority="Low",
           description="The page has no structured data. Markup helps search engines understand what the "
                       "page is about (an article, a product, a business) and can make it eligible for "
                       "richer search results.",
           fix="Add JSON-LD markup that describes the page's main content, using the schema.org types "
               "that fit (Organization on the home page, Article on posts, Product on product pages).",
           short_fix="Add JSON-LD for the page's main content (Article, Product...)"),
    Filter("validation_errors", "Validation Errors", lambda r, site: bool(validation_errors(r)),
           issue_type="Issue", priority="High",
           description="A common schema.org type lacks a property it needs (for example a Product without "
                       "offers, review or aggregateRating, or an Article without headline). This is a basic "
                       "check of a few types, not the full schema.org validator.",
           fix="Add the missing properties listed in the Validation Errors column, then confirm with "
               "Google's Rich Results Test.",
           short_fix="Add the missing required schema properties"),
    Filter("validation_warnings", "Validation Warnings", lambda r, site: bool(validation_warnings(r)),
           issue_type="Warning", priority="Low",
           description="A common schema.org type lacks a property that describes it well (for example an "
                       "Article without author, date or image, a WebSite without url, an Organization without "
                       "logo). The markup still works, but search engines get less to go on. Basic check of "
                       "our own on common types.",
           fix="Add the properties listed in the Validation Warnings column where the page has the "
               "information.",
           short_fix="Add the recommended schema properties listed"),
    Filter("rich_result_errors", "Rich Result Validation Errors", lambda r, site: bool(rich_results(r, site)["errors"]),
           issue_type="Issue", priority="High",
           description="Markup for a Google rich result (Product, Recipe, Event, Job posting, Review, Video...) "
                       "lacks a property Google requires for it, so the page cannot get that rich result. "
                       "Checked against Google's documented required properties; nested values such as the "
                       "price inside offers are checked as far as the crawl recorded them.",
           fix="Add the required properties listed in Rich Result Errors, then confirm with Google's Rich "
               "Results Test and the Search Console enhancement reports.",
           short_fix="Add Google's required properties for the rich result"),
    Filter("rich_result_warnings", "Rich Result Validation Warnings",
           lambda r, site: bool(rich_results(r, site)["warnings"]),
           issue_type="Warning", priority="Low",
           description="Markup for a Google rich result lacks properties Google recommends. The rich result "
                       "can still show, but with less detail (no rating, date, image or price).",
           fix="Add the recommended properties listed in Rich Result Warnings where the page has the "
               "information; never add values the visitor cannot see.",
           short_fix="Add Google's recommended rich result properties"),
    Filter("parse_errors", "Parse Errors", lambda r, site: parse_errors(r) > 0,
           issue_type="Issue", priority="High",
           description="A JSON-LD block is not valid JSON (a missing comma, a stray quote, an unclosed "
                       "bracket), so search engines cannot read any of it.",
           fix="Fix the JSON syntax in the <script type=\"application/ld+json\"> block; a JSON linter "
               "shows the exact spot.",
           short_fix="Fix the JSON syntax of the JSON-LD block"),
    Filter("microdata", "Microdata URLs", lambda r, site: has_format(r, "microdata"),
           description="The page has structured data in Microdata (itemscope / itemtype attributes)."),
    Filter("jsonld", "JSON-LD URLs", lambda r, site: has_format(r, "jsonld"),
           description="The page has structured data in JSON-LD (<script type=\"application/ld+json\">)."),
    Filter("rdfa", "RDFa URLs", lambda r, site: has_format(r, "rdfa"),
           description="The page has structured data in RDFa (typeof / property attributes)."),
    Filter("rich_result_detected", "Rich Result Feature Detected",
           lambda r, site: bool(rich_results(r, site)["features"]),
           description="The page has markup for at least one feature Google can show as a rich result (see "
                       "Rich Result Features). Features Google has retired (FAQ, How-to, Fact check...) do not "
                       "count."),
    Filter("schema_not_on_page", "Schema Values Not On Page", lambda r, site: bool(schema_mismatch(r, False)),
           issue_type="Warning", priority="Medium", requires=("pagedata:schema_values_checked",),
           description="JSON-LD names a value the visitor cannot see on the page (a product name, SKU, phone "
                       "number, email or address that the visible text does not show). Google requires "
                       "markup to match the visible content, and stale markup can cost rich results.",
           fix="Update the JSON-LD so it matches what the page shows (usually a template that fills it from "
               "old or different data), or show the value on the page.",
           short_fix="Make the JSON-LD values match the visible page"),
    Filter("schema_price_not_on_page", "Schema Price Not On Page", lambda r, site: bool(schema_mismatch(r, True)),
           issue_type="Issue", priority="High", requires=("pagedata:schema_values_checked",),
           description="The price in the Product markup is not a price the page shows (for example a sale price "
                       "missing from the schema, or a variant's price). Google can drop the rich result and "
                       "Merchant Center may flag the product. Pages whose prices are drawn by JavaScript are "
                       "not judged.",
           fix="Fill offers.price from the same value the page displays (after discounts), with the matching "
               "priceCurrency.",
           short_fix="Use the displayed price in offers.price"),
]

COLUMNS = [
    ADDRESS,
    Column("errors", "Errors", lambda r, site: len(validation_errors(r)) + parse_errors(r), "num"),
    Column("warnings", "Warnings", lambda r, site: len(validation_warnings(r)), "num"),
    Column("rich_result_errors", "Rich Result Errors", lambda r, site: len(rich_results(r, site)["errors"]), "num"),
    Column("rich_result_warnings", "Rich Result Warnings", lambda r, site: len(rich_results(r, site)["warnings"]), "num"),
    Column("parse_errors", "Parse Errors", lambda r, site: parse_errors(r), "num"),
    Column("validation_errors", "Validation Errors", lambda r, site: validation_errors(r), "list"),
    Column("validation_warnings", "Validation Warnings", lambda r, site: validation_warnings(r), "list"),
    Column("rich_result_features", "Rich Result Features", _features_cell, "list"),
    Column("rich_result_issues", "Rich Result Issues",
           lambda r, site: rich_results(r, site)["errors"] + rich_results(r, site)["warnings"], "list"),
    Column("schema_not_on_page", "Schema Values Not On Page", _mismatch_cell, "list"),
    Column("total_types", "Total Types", lambda r, site: len(all_types(r)), "num"),
    Column("unique_types", "Unique Types", lambda r, site: len(unique_types(r)), "num"),
    Column("type_1", "Type-1", _nth_type(0)),
    Column("type_2", "Type-2", _nth_type(1)),
    Column("type_3", "Type-3", _nth_type(2)),
    INDEXABILITY, INDEXABILITY_STATUS,
]

register(Tab(id="structured_data", name="Structured Data", order=180, columns=COLUMNS, filters=FILTERS,
             rows=content_rows))


# ---------------------------------------------------------------------------------------------
# Structured Data Details (URL details pane) and the per-property bulk export (P12)
# ---------------------------------------------------------------------------------------------
def _flat(values: Any, prefix: str = "", out: Optional[List[Tuple[str, str]]] = None) -> List[Tuple[str, str]]:
    """(dotted path, value) of every leaf of a values tree; nested items show their @type on their own path."""
    out = [] if out is None else out
    if isinstance(values, dict):
        for k, v in values.items():
            if k in ("@context", "@graph"):
                continue
            path = f"{prefix}.{k}" if prefix else k
            if k.startswith("@") and prefix and k != "@type":
                continue
            _flat(v, path, out)
    elif isinstance(values, list):
        for v in values:
            _flat(v, prefix, out)
    elif values is not None:
        out.append((prefix, str(values)))
    return out


def item_details(item: dict) -> dict:
    res = schema_vocab.validate(item)
    required = [f"missing {g}" for g in _missing(item, REQUIRED.get(str(item.get("type") or "").lower(), []))]
    recommended = [f"missing {g}" for g in _missing(item, RECOMMENDED.get(str(item.get("type") or "").lower(), []))]
    return {"format": FORMAT_LABEL.get(item.get("format"), item.get("format")), "type": item.get("type") or "",
            "values": _flat(item.get("values")) if isinstance(item.get("values"), dict)
            else [(p, "") for p in item.get("props") or ()],
            "errors": required + res["errors"], "warnings": recommended + res["warnings"]}


def details(site, url: str) -> Optional[dict]:
    r = (getattr(site, "by_url", None) or {}).get(url)
    if r is None or not seo(r).get("is_html"):
        return None
    if not contains(r):
        return {"not_checked": "No structured data was found on this page."} if parse_errors(r) == 0 else             {"items": [], "parse_errors": parse_errors(r), "rich_results": rich_results(r, site)}
    return {"items": [item_details(i) for i in _items(r)], "parse_errors": parse_errors(r),
            "rich_results": rich_results(r, site), "vocabulary": "schema.org" if schema_vocab.available() else
            "not available"}


PROPERTY_HEADERS = ["Address", "Item", "Format", "Type", "Property Path", "Value", "Errors", "Warnings"]


def property_rows(site, crawler=None):
    for r in content_rows(site):
        if not contains(r):
            continue
        for n, item in enumerate(_items(r), 1):
            d = item_details(item)
            errs, warns = "; ".join(d["errors"]), "; ".join(d["warnings"])
            for path, value in d["values"] or [("", "")]:
                yield [r.url, n, d["format"], d["type"], path, value, errs, warns]


def _property_not_checked(site) -> str:
    if not any(contains(r) for r in content_rows(site)):
        return "No page of this crawl carries structured data."
    return ""


def _register_outputs():
    try:
        from ..registry import register_details
        register_details("structured_data", details)
    except ImportError:
        pass
    try:
        from ...reports import register_bulk_export
        register_bulk_export("structured_data_properties", "Structured Data Properties (every value)",
                             headers=PROPERTY_HEADERS, rows=property_rows, not_checked=_property_not_checked,
                             group="Structured Data",
                             description="One row per property value of every structured data item: its dotted "
                                         "path (offers.price), the value, and the item's errors and warnings.")
    except Exception:
        pass


_register_outputs()
