"""
Earnings Pulse algorithm: fuse X, articles and YouTube into one 0-100 score per phase
(pre / during / post), plus momentum, expectation gap and a Grok-written actionable brief.
Writes one row per phase to public.pulse_snapshots.

    python -m grok_pulse.pulse --ticker AAPL --date 2025-01-30
    python -m grok_pulse.pulse --ticker AAPL --date 2025-01-30 --no-brief   # skip Grok calls
"""
import argparse
import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone

from grok_pulse.common import fetch_all, grok_json, grok_model, log10p, norm_phase, num, sb

PHASES = ("pre", "during", "post")
SOURCE_WEIGHTS = {"x": 0.40, "articles": 0.35, "youtube": 0.25}
BUCKET_MIN = {"pre": 60, "during": 5, "post": 60}


# ---------- load everything the Grok bots collected for one event ----------
def load_event(ticker: str, date: str) -> dict:
    x_events = [e for e in fetch_all("x_earnings_events", ticker=ticker)
                if str(e.get("call_time_utc", "")).startswith(date) or str(e.get("report_time_utc", "")).startswith(date)]
    x_event = x_events[0] if x_events else None
    authors = {a["id"]: a for a in fetch_all("x_authors")} if x_event else {}
    posts = fetch_all("x_posts", event_id=x_event["id"]) if x_event else []
    for p in posts:
        p["author"] = authors.get(p.get("author_id"), {})

    articles = [a for a in fetch_all("articles", ticker=ticker) if str(a.get("earnings_date")) == date]
    try:
        videos = [v for v in fetch_all("youtube_videos", ticker=ticker) if str(v.get("earnings_date")) == date]
        insights = {i["video_id"]: i for i in fetch_all("youtube_video_insights") if i.get("video_id")}
    except Exception:
        videos, insights = [], {}
    for v in videos:
        v["insight"] = insights.get(v["video_id"], {})

    ee = [e for e in fetch_all("earnings_events", ticker=ticker) if str(e.get("earnings_date")) == date]
    return {"ticker": ticker, "date": date, "event_key": f"{ticker}-{date}", "x_event": x_event,
            "posts": posts, "articles": articles, "videos": videos, "fundamentals": ee[0] if ee else None}


# ---------- per-source weighted sentiment ----------
def x_weight(p) -> float:
    a = p.get("author") or {}
    engagement = (p.get("like_count") or 0) + 2 * (p.get("repost_count") or 0) + (p.get("reply_count") or 0)
    w = (1 + log10p(a.get("followers"))) * (0.5 + log10p(1 + engagement))
    return w * (1.25 if a.get("verified") else 1.0)


def weighted(items):
    """items: [(sentiment, weight)] -> (weighted mean, n)"""
    items = [(s, w) for s, w in items if s is not None and w > 0]
    if not items:
        return None, 0
    tw = sum(w for _, w in items)
    return sum(s * w for s, w in items) / tw, len(items)


def source_scores(ev, phase):
    xs = [(p.get("sentiment_score"), x_weight(p)) for p in ev["posts"]
          if norm_phase(p.get("time_window")) == phase and p.get("sentiment_score") is not None]
    arts = [(a.get("sentiment_score"), 1.0 + (0.5 if a.get("analyst_actions") else 0.0))
            for a in ev["articles"] if norm_phase(a.get("phase")) == phase]
    yts = [((v["insight"] or {}).get("sentiment_score"), 1 + log10p(v.get("view_count")))
           for v in ev["videos"] if norm_phase(v.get("phase") or (v["insight"] or {}).get("phase")) == phase]
    out = {}
    for name, items in (("x", xs), ("articles", arts), ("youtube", yts)):
        s, n = weighted(items)
        out[name] = {"sentiment": None if s is None else round(s, 4), "n": n, "weight": SOURCE_WEIGHTS[name]}
    return out


def pulse_score(scores) -> float | None:
    live = [(v["sentiment"], v["weight"]) for v in scores.values() if v["sentiment"] is not None]
    if not live:
        return None
    s = sum(x * w for x, w in live) / sum(w for _, w in live)  # renormalize over sources with data
    return round(50 + 50 * s, 1)


# ---------- momentum: X volume + sentiment per bucket, with spike flags ----------
def momentum(ev, phase, upto_minute=None):
    size = BUCKET_MIN[phase]
    buckets = defaultdict(list)
    for p in ev["posts"]:
        m = p.get("minutes_from_call")
        if norm_phase(p.get("time_window")) != phase or m is None:
            continue
        if upto_minute is not None and float(m) > upto_minute:
            continue
        buckets[int(float(m) // size * size)].append(p)
    rows = []
    for b in sorted(buckets):
        ps = buckets[b]
        s, _ = weighted([(p.get("sentiment_score"), x_weight(p)) for p in ps])
        eng = sum((p.get("like_count") or 0) + (p.get("repost_count") or 0) for p in ps)
        rows.append({"minutes_from_call": b, "posts": len(ps), "engagement": eng,
                     "sentiment": None if s is None else round(s, 3)})
    counts = [r["posts"] for r in rows]
    if len(counts) > 2:
        mu, sd = statistics.mean(counts), statistics.pstdev(counts) or 1
        for r in rows:
            r["spike"] = (r["posts"] - mu) / sd > 2
    return rows


# ---------- expectation vs reality ----------
def expectation_gap(ev, pre_score, post_score):
    f = ev["fundamentals"] or {}
    rep, con = f.get("reported_metrics") or {}, f.get("consensus_estimates") or {}
    metrics = []

    def add(name, actual, expected, unit):
        if actual is not None and expected is not None and expected:
            metrics.append({"metric": name, "actual": actual, "consensus": expected, "unit": unit,
                            "surprise_pct": round((actual - expected) / abs(expected) * 100, 2)})

    rev = num(rep.get("revenue_musd"))
    add("Revenue", None if rev is None else round(rev / 1000, 2), num(con.get("revenue_busd")), "$B")
    add("EPS", num(rep.get("eps_diluted")), num(con.get("eps")), "$")
    add("Gross margin", num(rep.get("gross_margin_pct")), num(con.get("gross_margin_pct")), "%")
    seg = rep.get("segments_musd") or {}
    for key, label in (("iphone", "iPhone"), ("services", "Services"), ("mac", "Mac"), ("ipad", "iPad")):
        v = num(seg.get(key))
        add(label, None if v is None else round(v / 1000, 2), num(con.get(f"{key}_busd")), "$B")

    beats = sum(1 for m in metrics if m["surprise_pct"] > 0)
    shift = None if pre_score is None or post_score is None else round(post_score - pre_score, 1)
    beat = metrics and beats >= len(metrics) / 2
    if shift is None:
        verdict = "awaiting post-call data"
    elif beat:
        verdict = "beat & cheered" if shift > 3 else "beat but sold the news" if shift < -3 else "beat, priced in"
    else:
        verdict = "miss but relief rally" if shift > 3 else "miss & punished" if shift < -3 else "miss, shrugged off"
    return {"pre_score": pre_score, "post_score": post_score, "sentiment_shift": shift,
            "beats": beats, "metrics_compared": len(metrics), "metrics": metrics,
            "guidance": f.get("guidance"), "verdict": verdict}


# ---------- Grok brief ----------
BRIEF_SYSTEM = (
    "You are an earnings-call strategist briefing a retail investor. Use ONLY the evidence given "
    "(X posts, news articles, YouTube analyst videos, reported numbers). Be specific and cite numbers. "
    'Return JSON only: {"headline": str, "bull_points": [str, str, str], "bear_points": [str, str, str], '
    '"watch_items": [str, str, str], "signal": "bullish|neutral|bearish", "confidence": 0-1, '
    '"action": str (one sentence: what an investor should watch or do next; not personalized advice)}'
)
PHASE_ASK = {
    "pre": "BEFORE the call: what is the market expecting and worried about going in?",
    "during": "DURING the call: how is sentiment reacting in real time, and what moments moved it?",
    "post": "AFTER the call: how did the market digest results vs expectations, and what matters next?",
}


def evidence(ev, phase, k=15):
    posts = sorted((p for p in ev["posts"] if norm_phase(p.get("time_window")) == phase),
                   key=x_weight, reverse=True)[:k]
    arts = [a for a in ev["articles"] if norm_phase(a.get("phase")) == phase][:10]
    vids = [v for v in ev["videos"] if norm_phase(v.get("phase")) == phase][:6]
    return {
        "x_posts": [{"user": (p.get("author") or {}).get("username"), "followers": (p.get("author") or {}).get("followers"),
                     "text": (p.get("text") or "")[:280], "likes": p.get("like_count"),
                     "sentiment": p.get("sentiment_score")} for p in posts],
        "articles": [{"source": a.get("source"), "title": a.get("title"), "summary": (a.get("summary") or "")[:400],
                      "sentiment": a.get("sentiment_score"), "analyst_actions": a.get("analyst_actions")} for a in arts],
        "youtube": [{"title": v.get("title"), "channel": v.get("channel_name"), "views": v.get("view_count"),
                     "summary": ((v.get("insight") or {}).get("summary") or "")[:400],
                     "price_targets": (v.get("insight") or {}).get("price_targets")} for v in vids],
    }


def brief(ev, phase, scores, gap):
    payload = {"company": ev["ticker"], "earnings_date": ev["date"], "question": PHASE_ASK[phase],
               "source_sentiment": scores, "evidence": evidence(ev, phase)}
    if phase != "pre":
        payload["expectation_gap"] = gap
    return grok_json(BRIEF_SYSTEM, json.dumps(payload, ensure_ascii=False, default=str), temperature=0.3)


# ---------- main ----------
def compute(ticker, date, with_brief=True, phases=PHASES, upto_minute=None, quiet=False):
    ev = load_event(ticker, date)
    if not quiet:
        print(f"📦 {ev['event_key']}: {len(ev['posts'])} X posts, {len(ev['articles'])} articles, "
              f"{len(ev['videos'])} videos, fundamentals={'yes' if ev['fundamentals'] else 'no'}")
    scores = {ph: source_scores(ev, ph) for ph in PHASES}
    pulse = {ph: pulse_score(scores[ph]) for ph in PHASES}
    gap = expectation_gap(ev, pulse["pre"], pulse["post"])
    rows = []
    for ph in phases:
        row = {"ticker": ticker, "event_key": ev["event_key"], "phase": ph, "pulse_score": pulse[ph],
               "source_scores": scores[ph], "momentum": momentum(ev, ph, upto_minute if ph == "during" else None),
               "gap": gap if ph == "post" else {"sentiment_shift": gap["sentiment_shift"]},
               "generated_at": datetime.now(timezone.utc).isoformat()}
        if with_brief:
            try:
                row["brief"] = brief(ev, ph, scores[ph], gap)
                row["model"] = grok_model()
            except Exception as e:
                print(f"⚠️  brief for {ph} failed: {e}")
        sb().table("pulse_snapshots").upsert(row, on_conflict="event_key,phase").execute()
        rows.append(row)
        if not quiet:
            srcs = ", ".join(f"{k}={v['sentiment']}({v['n']})" for k, v in scores[ph].items())
            print(f"   {ph:>6}: pulse {pulse[ph]}  [{srcs}]"
                  + (f"  → {row.get('brief', {}).get('signal')}: {row.get('brief', {}).get('headline')}" if with_brief else ""))
    if not quiet:
        print(f"   gap: {gap['verdict']} (shift {gap['sentiment_shift']}, beats {gap['beats']}/{gap['metrics_compared']})")
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="AAPL")
    ap.add_argument("--date", default="2025-01-30")
    ap.add_argument("--no-brief", action="store_true")
    ap.add_argument("--phase", choices=PHASES, nargs="*", default=list(PHASES))
    a = ap.parse_args()
    compute(a.ticker, a.date, with_brief=not a.no_brief, phases=a.phase)
