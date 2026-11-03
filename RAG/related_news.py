"""Related news for the earnings call open on a dashboard.

Google News RSS (no API key) supplies stories from a window around the call.
Grok runs twice, both through ``grok_client.py``: first to plan 3-5 search
queries, then again to summarize the articles and compare them with what
management said. If planning fails, the scrape uses "<Company> <Quarter> earnings".
If ``XAI_API_KEY`` is missing, the response is saved demo coverage plus a notice.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import NamedTuple, Optional
from urllib.parse import quote, urlencode, urlparse
from xml.etree import ElementTree as ET

from fastapi import APIRouter, HTTPException, Query

import grok_client

router = APIRouter()

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
RSS_SEARCH = "https://news.google.com/rss/search"
BATCHEXECUTE_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute?rpcids=Fbv4je"
KEY_MISSING_NOTICE = (
    "XAI_API_KEY is not set, so Grok did not analyze this coverage. "
    "Showing saved articles for this call. Add XAI_API_KEY to RAG/.env to turn analysis on."
)
GROK_FAILED_NOTICE = "Grok could not analyze this coverage. Summaries are taken from the article text."
# Google's article-page signature request. The context array is what the
# public News site sends; the article id, timestamp, and signature are filled in.
_GARTURLREQ_CTX = [
    ["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
    "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0,
]
_SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "form", "button"}
_BOILERPLATE = (
    "subscribe", "sign in", "cookie", "all rights reserved", "advertisement",
    "enable javascript", "privacy policy", "accept cookies",
)
_CACHE_MAX = 64
_cache: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()
_cache_lock = threading.Lock()

# Dates are the earnings announcement (library / chart dates), not a later upload.
# Quarter labels are added to the search and dropped automatically when they
# leave too few stories.
PRELOADED_CALLS = {
    "1": ("AAPL", "Apple", "2025-01-30", "Q1 2025"),
    "2": ("CVS", "CVS Health", "2024-11-06", "Q3 2024"),
    "3": ("GOOGL", "Alphabet", "2025-02-04", "Q4 2024"),
    "4": ("SHEL", "Shell", "2025-01-30", "Q4 2024"),
    "5": ("TSLA", "Tesla", "2025-01-29", "Q4 2024"),
    "6": ("WMT", "Walmart", "2025-02-20", "Q4"),
    "aapl_2024Q4": ("AAPL", "Apple", "2024-10-31", "Q4 2024"),
    "aapl_2024Q3": ("AAPL", "Apple", "2024-08-01", "Q3 2024"),
    "aapl_2024Q2": ("AAPL", "Apple", "2024-05-02", "Q2 2024"),
    "aapl_2024Q1": ("AAPL", "Apple", "2024-02-01", "Q1 2024"),
    "aapl_2023Q4": ("AAPL", "Apple", "2023-11-02", "Q4 2023"),
}

COMPANY_NAMES = {
    "AAPL": "Apple",
    "TSLA": "Tesla",
    "GOOGL": "Alphabet",
    "GOOG": "Alphabet",
    "CVS": "CVS Health",
    "SHEL": "Shell",
    "WMT": "Walmart",
    "MSFT": "Microsoft",
    "AMZN": "Amazon",
    "META": "Meta",
    "NVDA": "NVIDIA",
}

SYSTEM_PROMPT = """You edit a short markets brief that helps a retail investor read news about one earnings call without being misled by hype.
Use only the article text and the management excerpt you are given. Do not invent figures, quotes, or reactions. Do not give a buy or sell recommendation.
For each article, write two sentences on what that article says about the results, guidance, or management commentary.
Then judge how the set of articles frames the call, and say where that framing disagrees with management or adds context the company did not lead with.

Return one JSON object and nothing else:
{
  "articles": [{"index": 0, "summary": "..."}],
  "tone": "positive",
  "takeaway": "2-3 sentences on how coverage framed the call",
  "for_investors": "2-4 sentences for a retail shareholder",
  "themes": ["concrete topic"]
}
tone is "positive" when the pieces mostly praise the print, "negative" when they mostly criticize it, and "mixed" when the framing is split or cautious.
themes are 2 to 4 concrete topics (a metric, a product, a risk), not single adjectives.
for_investors must name what management said on the call, then flag where coverage hypes a figure, downplays a risk, or adds a fact management did not emphasize. If coverage and management agree, say so and name the claim they agree on.
Every article index you were given must appear once."""

PLAN_PROMPT = """You plan Google News searches for one earnings call so a retail investor can see how the press covered it.
Use the company, ticker, quarter, call date, key topics, and management excerpt. Do not invent topics that are not suggested by that context.

Return one JSON object and nothing else:
{
  "queries": ["short search query", "..."],
  "window_days": 4
}
queries has 3 to 5 different angles, such as the reported results, guidance, a product or segment from the excerpt, a risk management discussed, and analyst or press reaction.
Each query is plain text a news search box would accept. Name the company. Do not add site: operators.
window_days is how many days either side of the call date to keep, an integer from 2 to 10."""

# Prepared remarks for historical Apple quarters that are not in static_summaries.
_TRANSCRIPT_FILES = {
    "aapl_2024Q4": "transcripts/apple_2024Q4_seeking_alpha.txt",
    "aapl_2024Q3": "transcripts/apple_2024Q3_seeking_alpha.txt",
    "aapl_2024Q2": "transcripts/apple_2024Q2_seeking_alpha.txt",
    "aapl_2024Q1": "transcripts/apple_2024Q1_seeking_alpha.txt",
    "aapl_2023Q4": "transcripts/apple_2023Q4_seeking_alpha.txt",
}
_FALLBACK_PATH = Path(__file__).resolve().parent / "related_news_fallback.json"
_fallback_rows: Optional[dict] = None


class NewsLookupError(ValueError):
    """The request did not identify an earnings call we can search for."""


class NewsFetchError(RuntimeError):
    """Google News could not be read."""


class HttpResult(NamedTuple):
    status: int
    text: str
    url: str


@dataclass
class CallIdentity:
    ticker: str
    company: str
    call_date: datetime.date
    dashboard_id: Optional[str] = None
    quarter_label: Optional[str] = None


@dataclass
class Article:
    title: str
    source: str
    published_at: str
    published: Optional[datetime.date]
    url: str
    snippet: str
    text: str = ""
    meta_description: str = ""
    original_url: str = field(default="")

    def as_public(self, summary: str) -> dict:
        return {
            "title": self.title,
            "source": self.source,
            "published_at": self.published_at,
            "url": self.url,
            "summary": summary,
        }


def _bounded_int(raw: Optional[str], default: int, lo: int, hi: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def max_articles() -> int:
    return _bounded_int(os.getenv("NEWS_MAX_ARTICLES"), 8, 1, 10)


def window_days() -> int:
    return _bounded_int(os.getenv("NEWS_WINDOW_DAYS"), 4, 1, 14)


def cache_ttl_seconds() -> int:
    return _bounded_int(os.getenv("NEWS_CACHE_TTL_SECONDS"), 21600, 60, 7 * 24 * 3600)


def http_request(method, url, *, json_body=None, data=None, headers=None, timeout=12.0, max_bytes=1_500_000) -> HttpResult:
    """GET or POST, following redirects, and stop reading after max_bytes."""
    import requests

    hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    try:
        response = requests.request(
            method, url, json=json_body, data=data, headers=hdrs,
            timeout=timeout, allow_redirects=True, stream=True,
        )
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            chunks.append(chunk)
            size += len(chunk)
            if size >= max_bytes:
                break
        text = b"".join(chunks).decode(response.encoding or "utf-8", "replace")
        status, final = response.status_code, str(response.url)
        response.close()
    except requests.RequestException as exc:
        raise NewsFetchError(f"Request to {urlparse(url).netloc or url} failed: {exc}") from exc
    return HttpResult(status, text, final)


def public_http_url(url: str) -> bool:
    """True for ordinary http(s) URLs. Blocks local and link-local targets."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or host in {"localhost", "0.0.0.0"} or host.endswith(".local"):
        return False
    if host == "::1" or host.startswith("127.") or host.startswith("10.") or host.startswith("192.168."):
        return False
    if host.startswith("169.254.") or host.startswith("172."):
        # 172.16.0.0/12 and the link-local range. A few public 172.* hosts are
        # rejected too; news publishers are not in that space.
        octets = host.split(".")
        if host.startswith("169.254."):
            return False
        if len(octets) == 4 and octets[0] == "172":
            try:
                second = int(octets[1])
            except ValueError:
                return False
            if 16 <= second <= 31:
                return False
    return True


def normalize_ticker(raw: str) -> str:
    cleaned = re.sub(r"[^A-Z0-9.\-]", "", (raw or "").strip().upper().lstrip("$"))
    return cleaned[:12]


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def parse_call_date(raw: str) -> datetime.date:
    text = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%m/%d/%y", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise NewsLookupError(f"Unrecognized call date: {raw}")


def parse_pub_date(raw: str) -> Optional[datetime.date]:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return parsedate_to_datetime(text).date()
    except (TypeError, ValueError, IndexError, OverflowError):
        pass
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _company_for(ticker: str, explicit: Optional[str], catalog_company: Optional[str]) -> str:
    name = (explicit or "").replace('"', "").strip()
    if name:
        return name[:80]
    if catalog_company:
        return catalog_company
    return COMPANY_NAMES.get(ticker, ticker)


def resolve_identity(dashboard_id=None, ticker=None, date=None, video_url=None, company=None) -> CallIdentity:
    """Prefer an explicit ticker and date, then a known dashboard id, then video metadata."""
    dashboard_id = (dashboard_id or "").strip() or None
    spec = PRELOADED_CALLS.get(dashboard_id) if dashboard_id else None
    video_url = (video_url or "").strip() or None

    if (ticker or "").strip() and (date or "").strip():
        symbol = normalize_ticker(ticker)
        if not symbol:
            raise NewsLookupError("Ticker is empty.")
        call_date = parse_call_date(date)
        quarter = None
        catalog_company = None
        for row in PRELOADED_CALLS.values():
            if row[0] == symbol and parse_call_date(row[2]) == call_date:
                quarter = row[3]
                catalog_company = row[1]
                break
        return CallIdentity(
            symbol, _company_for(symbol, company, catalog_company), call_date, dashboard_id, quarter,
        )

    if spec:
        symbol, name, raw_date, quarter = spec
        return CallIdentity(
            symbol, _company_for(symbol, company, name), parse_call_date(raw_date), dashboard_id, quarter,
        )

    if video_url:
        found_ticker, found_date = lookup_video(video_url)
        symbol = normalize_ticker(ticker or found_ticker or "")
        raw_date = (date or found_date or "").strip()
        if not symbol or not raw_date:
            raise NewsLookupError(
                "This call does not have a ticker and date yet. Add a ticker, or wait until processing finishes."
            )
        return CallIdentity(
            symbol, _company_for(symbol, company, None), parse_call_date(raw_date), dashboard_id, None,
        )

    if dashboard_id:
        raise NewsLookupError(
            f"Unknown dashboard id '{dashboard_id}'. Pass ticker and date, or a video_url."
        )
    raise NewsLookupError("Provide a dashboard id, a ticker and call date, or a video_url.")


def lookup_video(video_url: str) -> tuple[Optional[str], Optional[str]]:
    """Ticker and chart date already stored for a custom dashboard. Imported lazily to avoid a cycle."""
    try:
        import api_chatbot
    except ImportError as exc:
        raise NewsLookupError("Video lookup is unavailable.") from exc
    info = api_chatbot.video_info(video_url)
    if not isinstance(info, dict):
        return None, None
    return info.get("ticker"), info.get("date")


def build_search_query(identity: CallIdentity, *, include_quarter: bool, window_days_value: int) -> str:
    if identity.company and identity.company.upper() != identity.ticker.upper():
        who = f'("{identity.company}" OR {identity.ticker})'
    else:
        who = identity.ticker
    start = identity.call_date - timedelta(days=window_days_value)
    end = identity.call_date + timedelta(days=window_days_value + 1)
    parts = [who, '(earnings OR "earnings call" OR results)']
    if include_quarter and identity.quarter_label:
        parts.append(f'"{identity.quarter_label}"')
    parts.append(f"after:{start.isoformat()}")
    parts.append(f"before:{end.isoformat()}")
    return " ".join(parts)


def rss_url(query: str) -> str:
    return RSS_SEARCH + "?" + urlencode({"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})


def _child(item, name: str):
    for child in list(item):
        if child.tag.rsplit("}", 1)[-1] == name:
            return child
    return None


def html_to_text(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", unescape(fragment or ""))
    return re.sub(r"\s+", " ", text).strip()


def clean_title(title: str, source: str) -> str:
    text = unescape(title or "").strip()
    suffix = f" - {source}" if source else ""
    if suffix and text.endswith(suffix):
        text = text[: -len(suffix)].strip()
    return text


def parse_rss(xml_text: str) -> list[Article]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise NewsFetchError("Google News returned XML that could not be parsed.") from exc
    articles = []
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1] != "item":
            continue
        title_node = _child(item, "title")
        link_node = _child(item, "link")
        date_node = _child(item, "pubDate")
        description_node = _child(item, "description")
        source_node = _child(item, "source")
        link = (link_node.text or "").strip() if link_node is not None else ""
        if not link:
            continue
        source = (source_node.text or "").strip() if source_node is not None else ""
        if not source and source_node is not None:
            source = (source_node.attrib.get("url") or "").strip()
        title = clean_title((title_node.text or "") if title_node is not None else "", source)
        if not title:
            continue
        published = parse_pub_date((date_node.text or "") if date_node is not None else "")
        snippet = html_to_text((description_node.text or "") if description_node is not None else "")
        if normalize_title(snippet) == normalize_title(title) or normalize_title(snippet).startswith(normalize_title(title)):
            # Google's RSS description repeats the headline and the source.
            snippet = ""
        articles.append(Article(
            title=title,
            source=source,
            published_at=published.isoformat() if published else "",
            published=published,
            url=link,
            snippet=snippet,
            original_url=link,
        ))
        if len(articles) >= 25:
            break
    return articles


def fetch_feed(query: str) -> list[Article]:
    result = http_request(
        "GET", rss_url(query), timeout=15,
        headers={"Accept": "application/rss+xml, application/xml, text/xml"},
    )
    head = result.text[:1500].lower()
    if result.status != 200 or ("<rss" not in head and "<item" not in head):
        raise NewsFetchError(f"Google News did not return a feed (HTTP {result.status}).")
    return parse_rss(result.text)


def merge_articles(primary: list[Article], extra: list[Article]) -> list[Article]:
    merged, seen = [], set()
    for article in list(primary) + list(extra):
        key = normalize_title(article.title)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(article)
    return merged


def select_articles(items: list[Article], call_date: datetime.date, window: int, limit: int) -> list[Article]:
    """Prefer stories published inside the window. If none are, keep the closest ones."""
    ranked = []
    for index, item in enumerate(items):
        if item.published is None:
            distance, outside = window, 0
        else:
            distance = abs((item.published - call_date).days)
            outside = 0 if distance <= window else 1
        ranked.append((outside, distance, index, item))
    inside = [row for row in ranked if row[0] == 0]
    pool = inside or ranked
    pool.sort()
    chosen, seen = [], set()
    for _, _, _, item in pool:
        key = normalize_title(item.title)
        if not key or key in seen:
            continue
        seen.add(key)
        chosen.append(item)
        if len(chosen) >= limit:
            break
    return chosen


def google_article_id(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host != "news.google.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[-2] in ("articles", "read"):
        return parts[-1] or None
    return None


def signature_page_url(art_id: str) -> str:
    return f"https://news.google.com/rss/articles/{art_id}?hl=en-US&gl=US&ceid=US:en"


def read_signature(html: str) -> Optional[tuple[str, str]]:
    signature = re.search(r'data-n-a-sg="([^"]+)"', html or "")
    timestamp = re.search(r'data-n-a-ts="([^"]+)"', html or "")
    if not signature or not timestamp:
        return None
    return signature.group(1), timestamp.group(1)


def build_batchexecute_body(items: list[tuple[str, str, str, str]]) -> str:
    """items are (request id, article id, timestamp, signature)."""
    envelopes = []
    for req_id, art_id, timestamp, signature in items:
        stamp = int(timestamp) if str(timestamp).isdigit() else timestamp
        inner = ["garturlreq", _GARTURLREQ_CTX, art_id, stamp, signature]
        envelopes.append(["Fbv4je", json.dumps(inner, separators=(",", ":")), None, str(req_id)])
    return "f.req=" + quote(json.dumps([envelopes], separators=(",", ":")))


def parse_batchexecute(text: str) -> list[tuple[Optional[str], str]]:
    body = text or ""
    if "\n\n" in body:
        body = body.split("\n\n", 1)[1]
    body = body.lstrip()
    if body.startswith(")]}'"):
        body = body.split("\n", 1)[1] if "\n" in body else body[4:]
        body = body.lstrip()
    try:
        rows = json.loads(body)
    except json.JSONDecodeError:
        return []
    pairs = []
    for row in rows if isinstance(rows, list) else []:
        if not (isinstance(row, list) and len(row) >= 3 and row[0] == "wrb.fr"):
            continue
        payload = row[2]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                continue
        if isinstance(payload, list) and payload and payload[0] == "garturlres" and len(payload) > 1:
            publisher = payload[1]
            if not isinstance(publisher, str) or not publisher.startswith("http"):
                continue
            req_id = None
            for cell in reversed(row[3:]):
                if cell is not None:
                    req_id = str(cell)
                    break
            pairs.append((req_id, publisher))
    return pairs


def decode_google_links(urls: list[str]) -> dict[str, str]:
    """Resolve news.google.com article links to publisher URLs. Failures are omitted."""
    unique = []
    seen = set()
    for url in urls:
        art_id = google_article_id(url)
        if not art_id or url in seen:
            continue
        seen.add(url)
        unique.append((url, art_id))
    if not unique:
        return {}

    signed = []

    def _one(pair):
        url, art_id = pair
        try:
            page = http_request("GET", signature_page_url(art_id), timeout=12)
        except NewsFetchError:
            return None
        if page.status != 200:
            return None
        parsed = read_signature(page.text)
        if not parsed:
            return None
        return url, art_id, parsed[1], parsed[0]

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(_one, pair) for pair in unique]
        for future in as_completed(futures):
            try:
                row = future.result()
            except Exception:
                row = None
            if row:
                signed.append(row)
    if not signed:
        return {}

    items = [(str(index), art_id, timestamp, signature) for index, (_, art_id, timestamp, signature) in enumerate(signed)]
    try:
        result = http_request(
            "POST", BATCHEXECUTE_URL, data=build_batchexecute_body(items), timeout=20,
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        )
    except NewsFetchError:
        return {}
    if result.status != 200:
        return {}
    by_id = {req_id: publisher for req_id, publisher in parse_batchexecute(result.text) if req_id}
    decoded = {}
    for index, (url, _, _, _) in enumerate(signed):
        publisher = by_id.get(str(index))
        if publisher and public_http_url(publisher) and (urlparse(publisher).hostname or "").lower() != "news.google.com":
            decoded[url] = publisher
    return decoded


class _Paragraphs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.capture = 0
        self.buf: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self.skip += 1
            return
        if not self.skip and tag in {"p", "h1", "h2", "h3"}:
            self.capture += 1
            self.buf = []

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self.skip:
            self.skip -= 1
            return
        if tag in {"p", "h1", "h2", "h3"} and self.capture:
            text = " ".join("".join(self.buf).split())
            if text:
                self.parts.append(text)
            self.capture -= 1
            self.buf = []

    def handle_data(self, data):
        if self.skip or not self.capture:
            return
        self.buf.append(data)


def _keep_paragraph(text: str) -> bool:
    if len(text) < 60:
        return False
    lowered = text.lower()
    return not any(word in lowered for word in _BOILERPLATE)


def extract_paragraphs(html: str) -> str:
    parser = _Paragraphs()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        return ""
    kept = [part for part in parser.parts if _keep_paragraph(part)]
    if not kept:
        kept = [part for part in parser.parts if len(part) >= 60]
    return "\n".join(kept)


def meta_description(html: str) -> str:
    found = {}
    for tag in re.findall(r"<meta\b[^>]*>", html or "", flags=re.I):
        attrs = {key.lower(): unescape(value) for key, value in re.findall(r"([A-Za-z_:.-]+)\s*=\s*[\"'](.*?)[\"']", tag)}
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        content = re.sub(r"\s+", " ", attrs.get("content") or "").strip()
        if key in {"og:description", "description"} and content:
            found.setdefault(key, content)
    return found.get("og:description") or found.get("description") or ""


def should_fetch_article(url: str) -> bool:
    if not public_http_url(url):
        return False
    return (urlparse(url).hostname or "").lower() != "news.google.com"


def read_article(url: str) -> tuple[str, str, str]:
    """Return (final url, paragraph text, meta description). Empty text when the page cannot be read."""
    if not should_fetch_article(url):
        return url, "", ""
    try:
        result = http_request("GET", url, timeout=12, headers={"Accept": "text/html,application/xhtml+xml"})
    except NewsFetchError:
        return url, "", ""
    if result.status >= 400:
        return url, "", ""
    final = result.url if public_http_url(result.url) else url
    return final, extract_paragraphs(result.text), meta_description(result.text)


def extractive_summary(text: str, limit: int = 420) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if not compact:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", compact)
    chosen, total = [], 0
    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) < 30:
            continue
        chosen.append(sentence)
        total += len(sentence)
        if len(chosen) >= 2 or total >= limit:
            break
    summary = " ".join(chosen) if chosen else compact[:limit]
    if len(summary) > limit:
        summary = summary[: limit - 1].rsplit(" ", 1)[0].rstrip(".,;:") + "…"
    return summary


def best_fallback_summary(article: Article) -> str:
    extractive = extractive_summary(article.text)
    if len(extractive) >= 80:
        return extractive
    for candidate in (article.meta_description, article.snippet):
        cleaned = re.sub(r"\s+", " ", candidate or "").strip()
        if len(cleaned) >= 80 and normalize_title(cleaned) != normalize_title(article.title):
            return cleaned[:500]
    if extractive:
        return extractive
    return "The article page did not include readable text. Open it for the full story."


def enrich_articles(articles: list[Article]) -> None:
    decoded = decode_google_links([article.url for article in articles])
    for article in articles:
        publisher = decoded.get(article.url)
        if publisher:
            article.url = publisher
        if not article.source:
            host = (urlparse(article.url).hostname or "").lower()
            article.source = host.removeprefix("www.")
    targets = [article for article in articles if should_fetch_article(article.url)]
    if not targets:
        return
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(read_article, article.url): article for article in targets}
        for future in as_completed(futures):
            article = futures[future]
            try:
                final, text, description = future.result()
            except Exception:
                continue
            article.url = final or article.url
            article.text = text
            article.meta_description = description


@dataclass
class SearchPlan:
    """RSS queries to run, plus the wording shown in the dashboard."""

    queries: list[str]
    display_queries: list[str]
    window_days: int
    planned_by_grok: bool


def default_search_text(identity: CallIdentity) -> str:
    """'<Company> <Quarter> earnings' when Grok does not plan the searches."""
    parts = [identity.company or identity.ticker]
    if identity.quarter_label:
        parts.append(identity.quarter_label)
    parts.append("earnings")
    return " ".join(parts)


def apply_date_window(text: str, identity: CallIdentity, days: int) -> str:
    start = identity.call_date - timedelta(days=days)
    end = identity.call_date + timedelta(days=days + 1)
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    cleaned = re.sub(r"\s+after:\d{4}-\d{2}-\d{2}", "", cleaned)
    cleaned = re.sub(r"\s+before:\d{4}-\d{2}-\d{2}", "", cleaned).strip()
    return f"{cleaned} after:{start.isoformat()} before:{end.isoformat()}"


def default_search_plan(identity: CallIdentity) -> SearchPlan:
    days = window_days()
    text = default_search_text(identity)
    return SearchPlan([apply_date_window(text, identity, days)], [text], days, False)


def clean_planned_queries(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    queries, seen = [], set()
    for item in raw:
        text = re.sub(r"\s+", " ", str(item or "")).strip().strip('"')
        key = text.lower()
        if len(text) < 3 or len(text) > 180 or key in seen:
            continue
        seen.add(key)
        queries.append(text)
        if len(queries) == 5:
            break
    return queries


def key_topics(excerpt: str) -> list[str]:
    """Short phrases from the call summary so the planning prompt can name real topics."""
    skip = {
        "the", "and", "for", "with", "from", "that", "this", "quarter", "company",
        "earnings", "call", "year", "overall", "detailed", "summary",
    }
    found = []
    for match in re.findall(r"\b[A-Z][A-Za-z0-9+'-]{2,}(?:\s+[A-Z][A-Za-z0-9+'-]{2,}){0,2}", excerpt or ""):
        if match.lower() in skip or match in found:
            continue
        found.append(match)
        if len(found) == 8:
            break
    return found or ["results", "guidance", "analyst reaction"]


def plan_context(identity: CallIdentity) -> str:
    excerpt = management_context(identity)
    topics = ", ".join(key_topics(excerpt))
    return "\n".join([
        f"Company: {identity.company} ({identity.ticker})",
        f"Call date: {identity.call_date.isoformat()}",
        f"Quarter: {identity.quarter_label or 'unknown'}",
        f"Key topics: {topics}",
        "Management excerpt:",
        excerpt or "(no transcript excerpt is available for this call)",
    ])


def plan_searches(identity: CallIdentity) -> SearchPlan:
    """First Grok call. A failure uses the single default earnings query."""
    if not grok_client.is_configured():
        return default_search_plan(identity)
    try:
        payload = grok_client.chat_json(PLAN_PROMPT, plan_context(identity))
    except grok_client.GrokError as exc:
        print(f"related news: Grok query planning failed ({exc}); using the default earnings search")
        return default_search_plan(identity)
    queries = clean_planned_queries(payload.get("queries") if isinstance(payload, dict) else None)
    if not queries:
        return default_search_plan(identity)
    days = window_days()
    if isinstance(payload, dict):
        try:
            days = int(payload.get("window_days"))
        except (TypeError, ValueError):
            days = window_days()
    days = max(2, min(10, days))
    return SearchPlan(
        [apply_date_window(query, identity, days) for query in queries],
        queries,
        days,
        True,
    )


def fetch_coverage(identity: CallIdentity, plan: SearchPlan) -> list[Article]:
    """Run every planned query and keep one copy of each story."""
    items: list[Article] = []
    errors: list[NewsFetchError] = []
    for query in plan.queries:
        try:
            items = merge_articles(items, fetch_feed(query))
        except NewsFetchError as exc:
            errors.append(exc)
    if not items and errors:
        raise errors[-1]
    chosen = select_articles(items, identity.call_date, plan.window_days, max_articles())
    enrich_articles(chosen)
    return chosen


def pipeline_view(plan: Optional[SearchPlan], article_count: int, analyzed_by_grok: bool, notice: Optional[str] = None) -> dict:
    if plan is None:
        label = notice or "Saved coverage. Grok did not run because XAI_API_KEY is not set."
        return {
            "label": label,
            "queries": [],
            "planned_by_grok": False,
            "analyzed_by_grok": False,
            "article_count": article_count,
        }
    searches = f"{len(plan.display_queries)} search" if len(plan.display_queries) == 1 else f"{len(plan.display_queries)} searches"
    stories = f"{article_count} article" if article_count == 1 else f"{article_count} articles"
    if plan.planned_by_grok and analyzed_by_grok:
        label = f"Grok planned {searches}, scraped {stories}, Grok analyzed"
    elif plan.planned_by_grok:
        label = f"Grok planned {searches}, scraped {stories}. Grok analysis was unavailable."
    elif analyzed_by_grok:
        label = f"Used the default earnings search, scraped {stories}, Grok analyzed"
    else:
        label = f"Used the default earnings search, scraped {stories}."
    return {
        "label": label,
        "queries": list(plan.display_queries),
        "planned_by_grok": plan.planned_by_grok,
        "analyzed_by_grok": analyzed_by_grok,
        "article_count": article_count,
    }


def management_context(identity: CallIdentity) -> str:
    """What the company said, from the dashboard's own call summary or transcript. Not a news article."""
    text = ""
    if identity.dashboard_id:
        try:
            from static_summaries import STATIC_SUMMARIES
            text = STATIC_SUMMARIES.get(identity.dashboard_id) or ""
        except ImportError:
            text = ""
        if not text:
            relative = _TRANSCRIPT_FILES.get(identity.dashboard_id)
            if relative:
                path = Path(__file__).resolve().parent / relative
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")[:5000]
                except OSError:
                    text = ""
    text = re.sub(r"\s+", " ", text.replace("**", " ")).strip()
    return text[:2200]


def analysis_prompt(identity: CallIdentity, articles: list[Article]) -> str:
    blocks = [
        f"Company: {identity.company} ({identity.ticker})",
        f"Earnings call date: {identity.call_date.isoformat()}",
    ]
    excerpt = management_context(identity)
    if excerpt:
        blocks.append(
            "What management said on the call (summary of the company's remarks, not a news article):\n" + excerpt
        )
    blocks.append("Articles:")
    for index, article in enumerate(articles):
        body = article.text.strip()
        if len(body) < 80:
            body = best_fallback_summary(article)
        blocks.append(
            f"[{index}] {article.title}\nSource: {article.source or 'unknown'}\n"
            f"Date: {article.published_at or 'unknown'}\nURL: {article.url}\n{body[:1800]}"
        )
    return "\n\n".join(blocks)


def summaries_from_model(articles: list[Article], payload: dict) -> dict[int, str]:
    found = {}
    rows = payload.get("articles") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return found
    for row in rows:
        if not isinstance(row, dict):
            continue
        summary = str(row.get("summary") or "").strip()
        if not summary:
            continue
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(articles):
            found[index] = summary[:500]
    return found


def takeaway_from_model(payload: dict) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    summary = str(payload.get("takeaway") or "").strip()
    if not summary:
        return None
    tone = str(payload.get("tone") or "mixed").strip().lower()
    if tone not in {"positive", "negative", "mixed"}:
        tone = "mixed"
    themes = []
    raw_themes = payload.get("themes")
    if isinstance(raw_themes, list):
        for theme in raw_themes:
            text = str(theme).strip()
            if text:
                themes.append(text[:40])
            if len(themes) == 4:
                break
    note = str(payload.get("for_investors") or "").strip()
    return {"tone": tone, "summary": summary[:600], "themes": themes, "for_investors": note[:900]}


def extractive_takeaway(identity: CallIdentity, articles: list[Article]) -> dict:
    sources = []
    for article in articles:
        if article.source and article.source not in sources:
            sources.append(article.source)
    listed = f" ({', '.join(sources[:4])})" if sources else ""
    return {
        "tone": "mixed",
        "summary": (
            f"Found {len(articles)} article{'s' if len(articles) != 1 else ''} from around "
            f"{identity.company}'s {identity.call_date.isoformat()} earnings call{listed}. "
            "Summaries are taken from the article text. Grok did not score how the coverage is framed."
        ),
        "themes": [],
        "for_investors": (
            f"Management's recap of the {identity.call_date.isoformat()} {identity.company} call is on this dashboard. "
            "Treat these headlines as outside commentary: a story can hype or bury a figure the company stated plainly. "
            "Compare each claim with that recap before acting on one article."
        ),
    }


def empty_takeaway(identity: CallIdentity) -> dict:
    return {
        "tone": "mixed",
        "summary": (
            f"No news articles turned up for {identity.company}'s {identity.call_date.isoformat()} "
            "earnings call in this date window."
        ),
        "themes": [],
        "for_investors": (
            "No outside coverage was available to compare with what management said. "
            "Use the call summary on this dashboard rather than a headline you saw elsewhere."
        ),
    }


def analyze(identity: CallIdentity, articles: list[Article]) -> tuple[list[str], dict, str, Optional[str]]:
    """Grok only. A missing key or a failed call keeps the article text and sets a notice."""
    if not articles:
        notice = None if grok_client.is_configured() else KEY_MISSING_NOTICE
        return [], empty_takeaway(identity), "none" if grok_client.is_configured() else "unconfigured", notice
    fallbacks = [best_fallback_summary(article) for article in articles]
    if not grok_client.is_configured():
        return fallbacks, extractive_takeaway(identity, articles), "unconfigured", KEY_MISSING_NOTICE
    try:
        payload = grok_client.chat_json(SYSTEM_PROMPT, analysis_prompt(identity, articles))
    except grok_client.GrokError as exc:
        print(f"related news: Grok analysis failed ({exc})")
        return fallbacks, extractive_takeaway(identity, articles), "extractive", GROK_FAILED_NOTICE
    summaries = summaries_from_model(articles, payload)
    takeaway = takeaway_from_model(payload) or extractive_takeaway(identity, articles)
    ordered = [summaries.get(index) or fallbacks[index] for index in range(len(articles))]
    return ordered, takeaway, "grok", None


def cache_key(identity: CallIdentity) -> str:
    return "|".join([
        "v2",
        identity.ticker,
        identity.company,
        identity.call_date.isoformat(),
        identity.quarter_label or "",
        str(max_articles()),
        str(window_days()),
        grok_client.model_name() if grok_client.is_configured() else "no-grok",
    ])


def _cache_file(key: str) -> Path:
    directory = Path(tempfile.gettempdir()) / "simpliearn-related-news"
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(key.encode()).hexdigest()[:32]
    return directory / f"{digest}.json"


def _read_disk(key: str) -> Optional[dict]:
    try:
        data = json.loads(_cache_file(key).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("payload"), dict):
        return None
    return data


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
    directory = Path(tempfile.gettempdir()) / "simpliearn-related-news"
    if directory.exists():
        for path in directory.glob("*.json"):
            try:
                path.unlink()
            except OSError:
                pass


def cache_get(key: str) -> Optional[dict]:
    now = time.time()
    with _cache_lock:
        row = _cache.get(key)
        if row and row[0] >= now:
            _cache.move_to_end(key)
            hit = dict(row[1])
            hit["cached"] = True
            return hit
        if row:
            _cache.pop(key, None)
    disk = _read_disk(key)
    if not disk or float(disk.get("expires") or 0) < now:
        return None
    payload = disk["payload"]
    with _cache_lock:
        _cache[key] = (float(disk["expires"]), payload)
        _cache.move_to_end(key)
    hit = dict(payload)
    hit["cached"] = True
    return hit


def cache_get_stale(key: str) -> Optional[dict]:
    """Last saved result for this call, including after the TTL, so a failed refresh still renders."""
    disk = _read_disk(key)
    if not disk or not disk["payload"].get("articles"):
        return None
    hit = dict(disk["payload"])
    hit["cached"] = True
    hit["fallback"] = "cache"
    return hit


def cache_put(key: str, payload: dict, ttl: int) -> None:
    expires = time.time() + ttl
    with _cache_lock:
        _cache[key] = (expires, payload)
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    try:
        _cache_file(key).write_text(
            json.dumps({"expires": expires, "payload": payload}),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"related news: could not write cache ({exc})")


def load_fallback_rows() -> dict:
    global _fallback_rows
    if _fallback_rows is None:
        try:
            loaded = json.loads(_FALLBACK_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        _fallback_rows = loaded if isinstance(loaded, dict) else {}
    return _fallback_rows


def bundled_articles(identity: CallIdentity) -> list[Article]:
    """Articles saved for the demo library, used when Google News returns nothing."""
    rows = load_fallback_rows().get(identity.dashboard_id or "")
    if not isinstance(rows, list):
        rows = load_fallback_rows().get(f"{identity.ticker}|{identity.call_date.isoformat()}")
    if not isinstance(rows, list):
        return []
    articles = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        url = str(row.get("url") or "").strip()
        if not title or not url:
            continue
        published = parse_pub_date(str(row.get("published_at") or ""))
        articles.append(Article(
            title=title,
            source=str(row.get("source") or "").strip(),
            published_at=published.isoformat() if published else str(row.get("published_at") or ""),
            published=published,
            url=url,
            snippet=str(row.get("snippet") or "").strip(),
            text=str(row.get("text") or "").strip(),
            meta_description=str(row.get("meta_description") or "").strip(),
        ))
    return articles[:max_articles()]


def build_payload(identity, articles, summaries, takeaway, analysis, fallback=None, notice=None, pipeline=None) -> dict:
    return {
        "dashboard_id": identity.dashboard_id,
        "ticker": identity.ticker,
        "company": identity.company,
        "call_date": identity.call_date.isoformat(),
        "takeaway": takeaway,
        "articles": [article.as_public(summaries[index]) for index, article in enumerate(articles)],
        "analysis": analysis,
        "cached": False,
        "fallback": fallback,
        "notice": notice,
        "pipeline": pipeline or pipeline_view(None, len(articles), False, notice),
    }


def saved_without_grok(identity: CallIdentity, key: str) -> dict:
    """No XAI_API_KEY: saved demo articles (or the last cache) and a notice. Never calls a model."""
    articles = bundled_articles(identity)
    fallback = "saved" if articles else None
    if not articles:
        stale = cache_get_stale(key)
        if stale and stale.get("articles"):
            hit = dict(stale)
            hit["notice"] = KEY_MISSING_NOTICE
            hit["analysis"] = "unconfigured"
            hit["fallback"] = hit.get("fallback") or "cache"
            hit["cached"] = True
            return hit
    summaries = [best_fallback_summary(article) for article in articles]
    takeaway = extractive_takeaway(identity, articles) if articles else empty_takeaway(identity)
    payload = build_payload(
        identity, articles, summaries, takeaway, "unconfigured", fallback, KEY_MISSING_NOTICE,
        pipeline_view(None, len(articles), False, KEY_MISSING_NOTICE),
    )
    cache_put(key, payload, cache_ttl_seconds() if articles else min(cache_ttl_seconds(), 600))
    print(f"related news: {identity.ticker} {identity.call_date.isoformat()} saved coverage, Grok key not set")
    return payload


def get_related_news(dashboard_id=None, ticker=None, date=None, video_url=None, company=None) -> dict:
    identity = resolve_identity(dashboard_id, ticker, date, video_url, company)
    key = cache_key(identity)
    cached = cache_get(key)
    if cached:
        return cached
    if not grok_client.is_configured():
        return saved_without_grok(identity, key)
    plan = plan_searches(identity)
    articles: list[Article] = []
    live_error: Optional[NewsFetchError] = None
    try:
        articles = fetch_coverage(identity, plan)
    except NewsFetchError as exc:
        live_error = exc
        print(f"related news: live search failed ({exc})")
    fallback = None
    if not articles:
        stale = cache_get_stale(key)
        if stale:
            return stale
        saved = bundled_articles(identity)
        if saved:
            articles = saved
            fallback = "saved"
        elif live_error:
            raise live_error
    summaries, takeaway, analysis, notice = analyze(identity, articles)
    payload = build_payload(
        identity, articles, summaries, takeaway, analysis, fallback, notice,
        pipeline_view(plan, len(articles), analysis == "grok", notice),
    )
    ttl = cache_ttl_seconds() if articles else min(cache_ttl_seconds(), 600)
    cache_put(key, payload, ttl)
    print(
        f"related news: {identity.ticker} {identity.call_date.isoformat()} "
        f"articles={len(articles)} analysis={analysis} fallback={fallback or 'live'}"
    )
    return payload


@router.get("/news")
def news(
    dashboard_id: Optional[str] = Query(None, alias="id", max_length=200),
    ticker: Optional[str] = Query(None, max_length=12),
    date: Optional[str] = Query(None, max_length=32),
    video_url: Optional[str] = Query(None, max_length=2000),
    company: Optional[str] = Query(None, max_length=80),
):
    """Articles around this earnings call, with a short summary of each and an overall takeaway."""
    try:
        return get_related_news(dashboard_id, ticker, date, video_url, company)
    except NewsLookupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NewsFetchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
