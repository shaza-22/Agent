"""Query intent and record classification for metadata-aware ranking.

What: cheap, deterministic classifiers - what is this query asking for, and
      what kind of page/section is this record?
Inputs: query strings; record URL / title / heading.
Outputs: labels used as small ranking boosts, never as hard filters (except
         where explicitly requested).
Why: a purely lexical/semantic ranker has no idea that a news article about a
     card launch is a worse answer to "what are the eligibility requirements"
     than a page literally headed "Eligibility". That knowledge is in the URL
     and the heading, and it is free to use.

No LLM, no API. Rules only - this is Phase 2.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# --- page type, from URL and title -----------------------------------------
# News/press/about pages dominate lexical search for product queries because
# they are short and mention every product by name.
_NEWS_PAT = re.compile(
    r"(news|press|media|article|blog|event|celebrat|announce|awards?|csr|"
    r"sustainab|initiative|campaign|day\b|anniversar|sponsor)", re.I)
_ABOUT_PAT = re.compile(
    r"(about|who-?we-?are|history|board|governance|management|investor|"
    r"career|jobs|vacanc|contact|branch|atm|locations?|sitemap|privacy|terms)", re.I)
_PRODUCT_PAT = re.compile(
    r"(product|cards?|accounts?|loans?|deposits?|certificates?|savings?|"
    r"finance|financing|mortgage|insurance|transfers?|services?|tariff|fees?|"
    r"rates?|personal|retail|individual)", re.I)

# --- customer segment -------------------------------------------------------
_CORPORATE_PAT = re.compile(
    r"(corporate|business|sme|enterprise|company|companies|commercial|"
    r"institution|wholesale|trade-?finance|payroll)", re.I)
_CONSUMER_PAT = re.compile(
    r"(personal|individual|retail|consumer|private|youth|student)", re.I)

PAGE_TYPES = ("product", "news", "about", "other")
SEGMENTS = ("consumer", "corporate", "unknown")


def classify_page(url: str, title: str = "", breadcrumbs: str = "") -> str:
    """product | news | about | other. URL is weighted over title."""
    blob = f"{url} {title} {breadcrumbs}"
    if _NEWS_PAT.search(url) or _NEWS_PAT.search(title):
        return "news"
    if _ABOUT_PAT.search(url):
        return "about"
    if _PRODUCT_PAT.search(url):
        return "product"
    if _ABOUT_PAT.search(blob):
        return "about"
    if _PRODUCT_PAT.search(blob):
        return "product"
    return "other"


def classify_segment(url: str, title: str = "", text: str = "") -> str:
    """consumer | corporate | unknown, decided from the URL PATH only.

    An earlier version also scanned page body text and returned `corporate`
    when corporate-ish words appeared twice. On the real corpus that labelled
    637 of 1264 units corporate against only 88 consumer - a retail card page
    saying "business hours" or "our company" was enough to flip it. Because the
    ranker penalises corporate results on personal questions, that demoted half
    the corpus and let unclassified generic pages float to the top.

    The URL path is the only signal the site actually controls per-page
    (/corporate/..., /personal/...), so it is the only one trusted here.
    Everything else stays `unknown`, which carries no penalty - a wrong label
    is worse than no label.
    """
    path = urlparse(url).path if "://" in url else url
    if _CORPORATE_PAT.search(path):
        return "corporate"
    if _CONSUMER_PAT.search(path):
        return "consumer"
    # A title is weaker than a path but still page-specific; require it to be
    # unambiguous (exactly one side present).
    corp_t = bool(_CORPORATE_PAT.search(title or ""))
    cons_t = bool(_CONSUMER_PAT.search(title or ""))
    if corp_t and not cons_t:
        return "corporate"
    if cons_t and not corp_t:
        return "consumer"
    return "unknown"


# --- query intent -----------------------------------------------------------
# Each intent lists query cues, and the heading words that should be rewarded.
INTENTS: dict[str, dict] = {
    "eligibility": {
        "query": [r"eligib", r"qualif", r"who can (apply|get)", r"requirements? (for|to)",
                  r"criteria", r"minimum (income|salary|age)", r"am i eligible",
                  r"شروط", r"الاستحقاق", r"من يستطيع", r"الحد الادنى"],
        "headings": ["eligibility", "eligible", "criteria", "requirements",
                     "who can apply", "conditions", "شروط", "الاستحقاق", "المؤهلات"],
    },
    "documents": {
        "query": [r"documents?", r"paperwork", r"what do i need", r"required to open",
                  r"needed to open", r"how (do i|to) open", r"مستندات", r"اوراق",
                  r"المطلوبة", r"فتح حساب"],
        "headings": ["documents", "required documents", "requirements", "how to apply",
                     "how to open", "steps", "المستندات", "المستندات المطلوبة", "الاوراق"],
    },
    "fees": {
        "query": [r"\bfees?\b", r"charges?", r"commission", r"annual fee", r"cost",
                  r"tariff", r"how much", r"price", r"رسوم", r"عمولة", r"تكلفة",
                  r"التعريفة", r"كم تكلفة"],
        "headings": ["fee", "fees", "charges", "commission", "tariff", "pricing",
                     "cost", "رسوم", "الرسوم", "العمولات", "التعريفة"],
    },
    "rates": {
        "query": [r"interest rate", r"\brates?\b", r"return", r"yield", r"profit rate",
                  r"عائد", r"فائدة", r"سعر الفائدة"],
        "headings": ["rate", "rates", "interest", "return", "yield", "عائد", "فائدة"],
    },
    "compare": {
        "query": [r"compare", r"comparison", r"difference between", r"which (card|account)",
                  r"best (card|account)", r"options", r"types of", r"قارن", r"مقارنة",
                  r"الفرق بين", r"انواع"],
        "headings": [],   # comparison wants breadth, not a particular heading
    },
    "benefits": {
        "query": [r"benefits?", r"rewards?", r"advantages?", r"features?", r"perks?",
                  r"مزايا", r"فوائد", r"مكافآت"],
        "headings": ["benefits", "features", "advantages", "rewards", "مزايا", "الفوائد"],
    },
}

# Topic cues, used to bias the segment and product type of results.
_TOPIC_PATS = {
    "cards": re.compile(r"(cards?|visa|mastercard|credit card|debit card|prepaid|"
                        r"بطاق|بطاقات|ائتمان)", re.I),
    "accounts": re.compile(r"(accounts?|current account|savings|deposit|"
                           r"حساب|حسابات|ودائع|توفير)", re.I),
    "loans": re.compile(r"(loans?|financ|mortgage|credit facilit|قرض|قروض|تمويل)", re.I),
}


def extract_intents(query: str) -> list[str]:
    """All intents the query matches, strongest-signal first (order of INTENTS)."""
    q = query.lower()
    found = []
    for name, spec in INTENTS.items():
        if any(re.search(p, q, re.I) for p in spec["query"]):
            found.append(name)
    return found


def extract_topics(query: str) -> list[str]:
    return [name for name, pat in _TOPIC_PATS.items() if pat.search(query)]


def query_segment(query: str) -> str:
    """What segment is the *user* asking about? Defaults to consumer.

    A person asking "what are the eligibility requirements for a credit card"
    means a personal card. Corporate pages answering that question is the
    failure mode this exists to correct.
    """
    if _CORPORATE_PAT.search(query):
        return "corporate"
    if _CONSUMER_PAT.search(query):
        return "consumer"
    return "consumer_default"


def heading_matches_intent(heading: str, intents: list[str]) -> bool:
    """True if this section's heading is what the query asked for."""
    h = (heading or "").lower()
    if not h:
        return False
    for intent in intents:
        for word in INTENTS.get(intent, {}).get("headings", []):
            if word in h:
                return True
    return False


def looks_like_product_page(url: str, title: str) -> bool:
    return classify_page(url, title) == "product"
