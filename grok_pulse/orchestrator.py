"""
Grok Pulse orchestrator: the same three Grok-bot workflows, generalized to ANY earnings call.

    # 1) Show the full Grok-bot schedule for an upcoming call
    python -m grok_pulse.orchestrator plan --ticker NVDA --call-time 2026-11-19T21:30:00Z

    # 2) Run one live tick now: Grok searches X + the web for the ticker, stores items, rescores, recomputes pulse
    python -m grok_pulse.orchestrator tick --ticker NVDA --call-time 2026-11-19T21:30:00Z

    # 3) Replay the preloaded Apple Q1 FY25 call minute by minute as if it were live
    python -m grok_pulse.orchestrator replay --ticker AAPL --date 2025-01-30 --step 5 --delay 0.5
"""
import argparse
import json
import time
from datetime import datetime, timedelta, timezone

from grok_pulse.common import grok, grok_model, norm_phase, sb

# Phase windows relative to the call (minutes) and how often each Grok bot runs inside them.
SCHEDULE = [
    # phase,   start,     end,      every, bots
    ("pre",    -7 * 1440, -30,      60,    ["x_search", "news_search", "youtube_search"]),
    ("during", -30,       120,      1,     ["x_search"]),
    ("post",   120,       2 * 1440, 60,    ["x_search", "news_search", "youtube_search"]),
]
BOT_TABLES = {"x_search": "x_posts", "news_search": "articles", "youtube_search": "youtube_videos"}


def ticks(call_time: datetime):
    for phase, start, end, every, bots in SCHEDULE:
        m = start
        while m <= end:
            yield phase, m, call_time + timedelta(minutes=m), bots
            m += every


def cmd_plan(a):
    call = datetime.fromisoformat(a.call_time.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    print(f"📅 Grok Pulse schedule for {a.ticker} · call {call:%Y-%m-%d %H:%M} UTC\n")
    for phase, start, end, every, bots in SCHEDULE:
        n = (end - start) // every + 1
        print(f"  {phase.upper():<7} {call + timedelta(minutes=start):%b %d %H:%M} → {call + timedelta(minutes=end):%b %d %H:%M} UTC"
              f"  every {every} min  ({n} runs)")
        for b in bots:
            print(f"           • Grok {b:<15} → supabase.{BOT_TABLES[b]}")
    print("           • after each run: grok_pulse.score (Grok sentiment) → grok_pulse.pulse (snapshot + brief)\n")
    upcoming = [t for t in ticks(call) if t[2] >= now][:5]
    print("  Next runs:" if upcoming else "  Call already finished — use `replay` to demo it.")
    for phase, m, at, bots in upcoming:
        print(f"    {at:%Y-%m-%d %H:%M} UTC  T{m:+} min  [{phase}]  {', '.join(bots)}")
    total = sum((e - s) // ev + 1 for _, s, e, ev, _ in SCHEDULE)
    print(f"\n  Total: {total} Grok runs per earnings call. Same workflow as the Apple demo — just change --ticker/--call-time.")


COLLECT_PROMPT = (
    "Search X and the web for the most relevant investor discussion about ${T} (ticker) and its earnings call at {CALL} UTC, "
    "published in the last {HOURS} hours. Return JSON only: "
    '{{"x_posts": [{{"url": str, "username": str, "text": str, "created_at": iso8601, "likes": int}}], '
    '"articles": [{{"url": str, "title": str, "source": str, "published_at": iso8601, "summary": str}}]}} '
    "with up to 10 of each. Only include real items you found."
)


def grok_collect(ticker: str, call: datetime, hours: int) -> dict:
    """One Grok call with live X + web search tools (xAI Agent Tools via the Responses API)."""
    prompt = COLLECT_PROMPT.replace("${T}", ticker).format(CALL=f"{call:%Y-%m-%d %H:%M}", HOURS=hours)
    resp = grok().responses.create(model=grok_model(), input=prompt,
                                   tools=[{"type": "x_search"}, {"type": "web_search"}])
    from grok_pulse.common import parse_json
    return parse_json(resp.output_text)


def cmd_tick(a):
    call = datetime.fromisoformat(a.call_time.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    minutes = (now - call).total_seconds() / 60
    phase = "pre" if minutes < -30 else "during" if minutes <= 120 else "post"
    print(f"⏱️  {a.ticker}: T{minutes:+.0f} min → phase {phase}; asking Grok (live X + web search)…")
    try:
        found = grok_collect(a.ticker, call, hours=a.hours)
    except Exception as e:
        print(f"⚠️  Grok live search failed ({type(e).__name__}: {str(e)[:200]}). "
              "Check that your xAI key has Agent Tools / search access.")
        return
    xs, arts = found.get("x_posts", []), found.get("articles", [])
    print(f"   Grok found {len(xs)} X posts and {len(arts)} articles")
    for p in xs[:5]:
        print(f"     @{p.get('username')}: {str(p.get('text'))[:100]}")
    for r in arts[:5]:
        print(f"     📰 {r.get('source')}: {r.get('title')}")
    if a.store:
        date = call.date().isoformat()
        rows = [{"ticker": a.ticker, "event": f"{a.ticker} earnings", "earnings_date": date,
                 "phase": f"{phase}_earnings", "title": r.get("title"), "url": r.get("url"),
                 "source": r.get("source"), "published_at": r.get("published_at"), "summary": r.get("summary")}
                for r in arts if r.get("url")]
        if rows:
            sb().table("articles").insert(rows).execute()
            print(f"   💾 stored {len(rows)} articles → run `python -m grok_pulse.score` then `python -m grok_pulse.pulse --ticker {a.ticker} --date {date}`")
    else:
        print("   (dry run: add --store to write to Supabase)")


def cmd_replay(a):
    """Recompute the DURING snapshot as the preloaded call unfolds, printing a live ticker."""
    from grok_pulse import pulse as P
    ev = P.load_event(a.ticker, a.date)
    during = [p for p in ev["posts"] if norm_phase(p.get("time_window")) == "during" and p.get("minutes_from_call") is not None]
    if not during:
        print("No during-call X posts for this event.")
        return
    lo, hi = int(min(float(p["minutes_from_call"]) for p in during)), int(max(float(p["minutes_from_call"]) for p in during))
    print(f"▶ Replaying {ev['event_key']} from T{lo:+} to T{hi:+} min ({len(during)} posts)\n")
    for t in range(lo, hi + a.step, a.step):
        seen = [p for p in during if float(p["minutes_from_call"]) <= t]
        s, n = P.weighted([(p.get("sentiment_score"), P.x_weight(p)) for p in seen])
        score = None if s is None else round(50 + 50 * s, 1)
        recent = [p for p in seen if float(p["minutes_from_call"]) > t - a.step]
        bar = "█" * min(40, len(recent))
        top = max(recent, key=P.x_weight, default=None)
        print(f"  T{t:+4} min  pulse {score if score is not None else '—':>5}  {bar:<40} {len(recent):>3} new"
              + (f"  | @{top['author'].get('username')}: {str(top.get('text'))[:60]}" if top else ""))
        time.sleep(a.delay)
    P.compute(a.ticker, a.date, with_brief=False, phases=("during",), quiet=True)
    print("\n✅ during-call snapshot recomputed in pulse_snapshots")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan"); p.add_argument("--ticker", required=True); p.add_argument("--call-time", required=True)
    t = sub.add_parser("tick"); t.add_argument("--ticker", required=True); t.add_argument("--call-time", required=True)
    t.add_argument("--hours", type=int, default=24); t.add_argument("--store", action="store_true")
    r = sub.add_parser("replay"); r.add_argument("--ticker", default="AAPL"); r.add_argument("--date", default="2025-01-30")
    r.add_argument("--step", type=int, default=5); r.add_argument("--delay", type=float, default=0.4)
    a = ap.parse_args()
    {"plan": cmd_plan, "tick": cmd_tick, "replay": cmd_replay}[a.cmd](a)
