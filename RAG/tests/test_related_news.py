"""Related-news endpoint: RSS parsing, Google News link decoding, Grok, and the project LLM.

No test touches the network. http_request is blocked unless a test replaces it.
"""

import json
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import grok_client
import related_news
from related_news import Article, HttpResult

RSS_ITEM = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item>
  <title>Tesla Q4 recap - Yahoo Finance</title>
  <link>https://news.google.com/rss/articles/CBMiTEST?oc=5</link>
  <pubDate>Wed, 29 Jan 2025 15:00:00 GMT</pubDate>
  <description>Tesla's automotive margin fell while energy storage hit a record, according to this recap of the call.</description>
  <source url="https://finance.yahoo.com">Yahoo Finance</source>
</item>
<item>
  <title>Unrelated market wrap</title>
  <link>https://example.com/old</link>
  <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
  <description>A story from a year earlier that the date window should drop.</description>
  <source url="https://example.com">Example</source>
</item>
</channel></rss>"""

EMPTY_RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Google News</title></channel></rss>"""

SIGNATURE_HTML = '<div data-n-a-sg="SIG" data-n-a-ts="1700000000"></div>'

BATCH_BODY = (
    ")]}'\n\n"
    '[["wrb.fr","Fbv4je","[\\"garturlres\\",\\"https://example.com/tesla-q4\\",1]",null,null,null,"0"]]'
)

ARTICLE_HTML = """<html><head>
<meta property="og:description" content="Services and iPhone led the quarter.">
</head><body>
<script>var secret = "ignore me";</script>
<p>Subscribe to continue reading this newspaper.</p>
<p>Tesla reported record deliveries for the fourth quarter and said energy storage reached an all-time high.</p>
<p>Automotive margins declined as average selling prices fell, which several notes called the main blemish.</p>
</body></html>"""

PAYWALL_HTML = "<html><body><p>Subscribe</p></body></html>"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    related_news.clear_cache()
    related_news._fallback_rows = None
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("GROK_MODEL", raising=False)
    monkeypatch.delenv("NEWS_MAX_ARTICLES", raising=False)
    monkeypatch.delenv("NEWS_WINDOW_DAYS", raising=False)
    monkeypatch.delenv("NEWS_CACHE_TTL_SECONDS", raising=False)

    def blocked(*args, **kwargs):
        raise AssertionError(f"unexpected network call: {args[:2]}")

    monkeypatch.setattr(related_news, "http_request", blocked)
    yield
    related_news.clear_cache()


def _article(**kwargs):
    base = dict(
        title="Tesla Q4 recap",
        source="Yahoo Finance",
        published_at="2025-01-29",
        published=related_news.parse_call_date("2025-01-29"),
        url="https://example.com/tesla-q4",
        snippet="",
        text="",
    )
    base.update(kwargs)
    return Article(**base)


def test_preloaded_calls_resolve_without_network():
    tesla = related_news.resolve_identity(dashboard_id="5")
    apple = related_news.resolve_identity(dashboard_id="1")
    quarter = related_news.resolve_identity(dashboard_id="aapl_2024Q4")
    assert (tesla.ticker, tesla.company, tesla.call_date.isoformat(), tesla.quarter_label) == (
        "TSLA", "Tesla", "2025-01-29", "Q4 2024",
    )
    assert (apple.ticker, apple.call_date.isoformat()) == ("AAPL", "2025-01-30")
    assert quarter.call_date.isoformat() == "2024-10-31"
    assert "Shell" in related_news.build_search_query(
        related_news.resolve_identity(dashboard_id="4"), include_quarter=True, window_days_value=4,
    )


def test_explicit_ticker_and_date_override_the_dashboard_id():
    identity = related_news.resolve_identity(dashboard_id="5", ticker="$aapl", date="1/30/25", company="Apple")
    assert identity.ticker == "AAPL"
    assert identity.call_date.isoformat() == "2025-01-30"
    assert identity.quarter_label == "Q1 2025"
    query = related_news.build_search_query(identity, include_quarter=True, window_days_value=4)
    assert "after:2025-01-26" in query
    assert "before:2025-02-04" in query
    assert '"Q1 2025"' in query


def test_missing_identity_is_rejected():
    with pytest.raises(related_news.NewsLookupError):
        related_news.resolve_identity()
    with pytest.raises(related_news.NewsLookupError):
        related_news.resolve_identity(dashboard_id="not-a-call")


def test_video_url_uses_dashboard_metadata(monkeypatch):
    fake = types.ModuleType("api_chatbot")
    fake.video_info = lambda url: {"ticker": "tsla", "date": "1/29/25", "title": "Tesla"}
    monkeypatch.setitem(sys.modules, "api_chatbot", fake)
    identity = related_news.resolve_identity(video_url="https://www.youtube.com/watch?v=abc")
    assert identity.ticker == "TSLA"
    assert identity.company == "Tesla"
    assert identity.call_date.isoformat() == "2025-01-29"


def test_parse_rss_cleans_title_and_keeps_a_real_snippet():
    articles = related_news.parse_rss(RSS_ITEM)
    assert articles[0].title == "Tesla Q4 recap"
    assert articles[0].source == "Yahoo Finance"
    assert articles[0].published_at == "2025-01-29"
    assert "energy storage" in articles[0].snippet
    assert articles[0].url.startswith("https://news.google.com/")


def test_select_articles_prefers_the_call_window_and_caps_the_list():
    near = _article(title="Near", published=related_news.parse_call_date("2025-01-30"), published_at="2025-01-30")
    far = _article(title="Far", published=related_news.parse_call_date("2024-01-01"), published_at="2024-01-01", url="https://example.com/far")
    duplicate = _article(title="Near!", published=related_news.parse_call_date("2025-01-30"), published_at="2025-01-30")
    chosen = related_news.select_articles(
        [far, near, duplicate], related_news.parse_call_date("2025-01-29"), window=4, limit=8,
    )
    assert [item.title for item in chosen] == ["Near"]
    many = [
        _article(title=f"Story {index}", url=f"https://example.com/{index}", published_at="2025-01-29")
        for index in range(12)
    ]
    assert len(related_news.select_articles(many, related_news.parse_call_date("2025-01-29"), 4, 8)) == 8


def test_extract_paragraphs_skips_scripts_and_short_boilerplate():
    text = related_news.extract_paragraphs(ARTICLE_HTML)
    assert "ignore me" not in text
    assert "Subscribe" not in text
    assert "record deliveries" in text
    assert "energy storage" in related_news.meta_description(ARTICLE_HTML) or "Services" in related_news.meta_description(ARTICLE_HTML)
    thin = _article(text="", meta_description="", snippet="Tesla's automotive margin fell while energy storage hit a record, according to this recap.")
    assert "energy storage" in related_news.best_fallback_summary(thin)
    assert related_news.public_http_url("http://127.0.0.1/secret") is False
    assert related_news.public_http_url("https://example.com/story") is True


def test_decode_google_news_link(monkeypatch):
    def fake_http(method, url, **kwargs):
        if "batchexecute" in url:
            assert method == "POST"
            return HttpResult(200, BATCH_BODY, url)
        assert "rss/articles/CBMiTEST" in url
        return HttpResult(200, SIGNATURE_HTML, url)

    monkeypatch.setattr(related_news, "http_request", fake_http)
    link = "https://news.google.com/rss/articles/CBMiTEST?oc=5"
    assert related_news.decode_google_links([link]) == {link: "https://example.com/tesla-q4"}
    assert related_news.parse_batchexecute(BATCH_BODY)[0][1] == "https://example.com/tesla-q4"


def test_queries_from_separate_searches_are_deduped(monkeypatch):
    queries = []

    def fake_http(method, url, **kwargs):
        queries.append(url)
        if "drop-me" in url:
            return HttpResult(200, EMPTY_RSS, url)
        return HttpResult(200, RSS_ITEM, url)

    identity = related_news.resolve_identity(dashboard_id="5")
    plan = related_news.SearchPlan(
        ["drop-me after:2025-01-25 before:2025-02-03", "tesla earnings after:2025-01-25 before:2025-02-03"],
        ["drop-me", "tesla earnings"],
        4,
        True,
    )
    monkeypatch.setattr(related_news, "http_request", fake_http)
    monkeypatch.setattr(related_news, "decode_google_links", lambda urls: {})
    monkeypatch.setattr(related_news, "read_article", lambda url: (url, "", ""))
    articles = related_news.fetch_coverage(identity, plan)
    assert len(queries) == 2
    assert len(articles) == 1
    assert articles[0].title == "Tesla Q4 recap"


def test_planning_failure_uses_company_quarter_earnings(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-key")

    def boom(system, user, timeout=90):
        raise grok_client.GrokError("planner down")

    monkeypatch.setattr(grok_client, "chat_json", boom)
    plan = related_news.plan_searches(related_news.resolve_identity(dashboard_id="5"))
    assert plan.planned_by_grok is False
    assert plan.display_queries == ["Tesla Q4 2024 earnings"]
    assert "after:2025-01-25" in plan.queries[0]


def _pipeline_http(calls):
    def fake_http(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if "rss/search" in url:
            return HttpResult(200, RSS_ITEM, url)
        if "rss/articles/" in url:
            return HttpResult(200, SIGNATURE_HTML, url)
        if "batchexecute" in url:
            return HttpResult(200, BATCH_BODY, url)
        if "example.com/tesla-q4" in url:
            return HttpResult(200, ARTICLE_HTML, url)
        raise AssertionError(url)

    return fake_http


GROK_ANALYSIS = {
    "articles": [{"index": 0, "summary": "Grok says deliveries hit a record and margins slipped."}],
    "tone": "mixed",
    "takeaway": "Coverage treated the quarter as a strong print with a margin caveat.",
    "for_investors": "Management led with record deliveries. Coverage adds that automotive margins fell, which the headline praise can hide.",
    "themes": ["deliveries", "margins"],
}
GROK_PLAN = {
    "queries": ["Tesla Q4 guidance", "Tesla energy storage deployments", "analyst reaction to Tesla earnings"],
    "window_days": 3,
}


def test_endpoint_uses_grok_and_then_the_cache(monkeypatch):
    calls = []
    prompts = []
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    monkeypatch.setattr(related_news, "http_request", _pipeline_http(calls))

    def fake_chat(system, user, timeout=90):
        prompts.append((system, user))
        if "Google News searches" in system:
            return GROK_PLAN
        return GROK_ANALYSIS

    monkeypatch.setattr(grok_client, "chat_json", fake_chat)
    app = FastAPI()
    app.include_router(related_news.router)
    client = TestClient(app)

    first = client.get("/news", params={"id": "5"})
    second = client.get("/news", params={"id": "5"})

    assert first.status_code == 200
    body = first.json()
    assert body["ticker"] == "TSLA"
    assert body["company"] == "Tesla"
    assert body["call_date"] == "2025-01-29"
    assert body["cached"] is False
    assert body["analysis"] == "grok"
    assert body["takeaway"]["tone"] == "mixed"
    assert body["takeaway"]["themes"] == ["deliveries", "margins"]
    assert "margins fell" in body["takeaway"]["for_investors"]
    assert body["fallback"] is None
    assert body["notice"] is None
    assert len(body["articles"]) == 1
    article = body["articles"][0]
    assert article["url"] == "https://example.com/tesla-q4"
    assert article["source"] == "Yahoo Finance"
    assert "Grok says" in article["summary"]
    assert "record deliveries" in article["summary"] or "margins" in article["summary"]
    assert len(prompts) == 2
    assert "Google News searches" in prompts[0][0]
    assert "Key topics" in prompts[0][1] and "Tesla" in prompts[0][1]
    assert "for_investors" in prompts[1][0]
    assert "What management said on the call" in prompts[1][1]
    assert body["pipeline"]["planned_by_grok"] is True
    assert body["pipeline"]["analyzed_by_grok"] is True
    assert body["pipeline"]["queries"] == GROK_PLAN["queries"]
    assert body["pipeline"]["label"] == "Grok planned 3 searches, scraped 1 article, Grok analyzed"
    assert second.status_code == 200 and second.json()["cached"] is True
    assert len(prompts) == 2


def test_grok_failure_keeps_article_text_and_does_not_call_another_model(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    monkeypatch.setattr(related_news, "http_request", _pipeline_http([]))

    def boom(system, user, timeout=90):
        raise grok_client.GrokError("down")

    monkeypatch.setattr(grok_client, "chat_json", boom)
    payload = related_news.get_related_news(dashboard_id="5")
    assert payload["analysis"] == "extractive"
    assert payload["notice"] == related_news.GROK_FAILED_NOTICE
    assert "record deliveries" in payload["articles"][0]["summary"].lower()
    assert "llm_provider" not in open(related_news.__file__, encoding="utf-8").read()


def test_missing_key_returns_saved_coverage_and_does_not_call_grok(monkeypatch):
    monkeypatch.setattr(related_news, "_fallback_rows", {
        "5": [{
            "title": "Tesla saved recap",
            "source": "Reuters",
            "published_at": "2025-01-29",
            "url": "https://example.com/saved-tesla",
            "text": "Tesla reported record deliveries. Automotive margins declined as prices fell and energy storage hit a high.",
        }],
    })

    def forbid(*args, **kwargs):
        raise AssertionError("Grok must not be called when XAI_API_KEY is missing")

    monkeypatch.setattr(grok_client, "chat_json", forbid)
    payload = related_news.get_related_news(dashboard_id="5")
    assert payload["analysis"] == "unconfigured"
    assert payload["fallback"] == "saved"
    assert "XAI_API_KEY is not set" in payload["notice"]
    assert "record deliveries" in payload["articles"][0]["summary"].lower()
    assert payload["articles"][0]["title"] == "Tesla saved recap"


def test_paywall_falls_back_to_the_rss_snippet(monkeypatch):
    html = PAYWALL_HTML

    def fake_read(url):
        return url, related_news.extract_paragraphs(html), ""

    monkeypatch.setattr(related_news, "decode_google_links", lambda urls: {urls[0]: "https://example.com/paywall"})
    monkeypatch.setattr(related_news, "read_article", fake_read)
    monkeypatch.setenv("XAI_API_KEY", "test-key")
    monkeypatch.setattr(related_news, "fetch_feed", lambda query: related_news.parse_rss(RSS_ITEM)[:1])
    monkeypatch.setattr(grok_client, "chat_json", lambda *args, **kwargs: (_ for _ in ()).throw(grok_client.GrokError("no")))
    payload = related_news.get_related_news(dashboard_id="5")
    assert "energy storage" in payload["articles"][0]["summary"]


def test_endpoint_rejects_a_missing_call_and_a_feed_failure(monkeypatch):
    app = FastAPI()
    app.include_router(related_news.router)
    client = TestClient(app)
    missing = client.get("/news")
    assert missing.status_code == 400
    assert "dashboard id" in missing.json()["detail"]

    def down(*args, **kwargs):
        raise related_news.NewsFetchError("Google News did not return a feed (HTTP 503).")

    monkeypatch.setenv("XAI_API_KEY", "test-key")
    monkeypatch.setattr(related_news, "http_request", down)
    failed = client.get("/news", params={"ticker": "ZZZZ", "date": "2020-01-01"})
    assert failed.status_code == 502
    assert "Google News" in failed.json()["detail"]


def test_demo_call_survives_a_feed_failure(monkeypatch):
    monkeypatch.setattr(related_news, "_fallback_rows", {
        "5": [{
            "title": "Tesla saved recap",
            "source": "Reuters",
            "published_at": "2025-01-29",
            "url": "https://example.com/saved-tesla",
            "text": "Tesla reported record deliveries. Automotive margins declined as prices fell and energy storage hit a high.",
        }],
    })

    def down(*args, **kwargs):
        raise related_news.NewsFetchError("Google News did not return a feed (HTTP 503).")

    monkeypatch.setenv("XAI_API_KEY", "test-key")
    monkeypatch.setattr(related_news, "http_request", down)
    monkeypatch.setattr(
        grok_client, "chat_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(grok_client.GrokError("no")),
    )
    payload = related_news.get_related_news(dashboard_id="5")
    assert payload["fallback"] == "saved"
    assert payload["articles"][0]["title"] == "Tesla saved recap"
    assert "record deliveries" in payload["articles"][0]["summary"].lower()
    assert "Management's recap" in payload["takeaway"]["for_investors"]


def test_prompt_compares_coverage_with_what_management_said():
    assert "retail investor" in related_news.SYSTEM_PROMPT
    prompt = related_news.analysis_prompt(related_news.resolve_identity(dashboard_id="5"), [_article()])
    assert "What management said on the call" in prompt
    assert "Model Y" in prompt or "deliveries" in prompt.lower()


def test_saved_demo_file_covers_apple_and_tesla():
    from conftest import RAG_DIR
    saved = json.loads((RAG_DIR / "related_news_fallback.json").read_text(encoding="utf-8"))
    for dashboard_id in ("1", "5"):
        rows = saved[dashboard_id]
        assert len(rows) >= 3
        for row in rows:
            assert row["title"] and row["url"].startswith("http")
            assert row.get("text") or row.get("snippet")


def test_article_and_window_limits_are_clamped(monkeypatch):
    monkeypatch.setenv("NEWS_MAX_ARTICLES", "40")
    monkeypatch.setenv("NEWS_WINDOW_DAYS", "0")
    monkeypatch.setenv("NEWS_CACHE_TTL_SECONDS", "nope")
    assert related_news.max_articles() == 10
    assert related_news.window_days() == 1
    assert related_news.cache_ttl_seconds() == 21600
    monkeypatch.setenv("GROK_MODEL", "grok-4.20-non-reasoning")
    assert grok_client.model_name() == "grok-4.20-non-reasoning"
    assert grok_client.is_configured() is False
