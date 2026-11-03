"""
Grok writes youtube_video_insights for every collected video that has none yet: summary, sentiment,
score, key points, price targets, analyst actions, red flags and comment sentiment, using the video's
title, description, transcript (when the bot saved one) and top comments.

    python -m grok_pulse.youtube_insights            # all videos missing insights
    python -m grok_pulse.youtube_insights --limit 5
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor

from grok_pulse.common import fetch_all, grok_json, grok_model, sb

SYSTEM = (
    "You analyze a YouTube video about a company's earnings call for investors. Use ONLY the given title, "
    "description, transcript excerpt and viewer comments. Judge the creator's stance toward the stock. "
    "Return JSON only: {"
    '"summary": str (2-3 sentences), '
    '"sentiment": "bullish"|"bearish"|"neutral"|"mixed", '
    '"sentiment_score": float -1..1, '
    '"key_points": [str] (max 5), '
    '"topics": [str] (max 6, short lowercase), '
    '"mentioned_metrics": [{"metric": str, "value": str}], '
    '"price_targets": [{"firm_or_person": str, "target": str}], '
    '"analyst_actions": [{"firm": str, "action": str}], '
    '"guidance_commentary": [str], '
    '"red_flags": [str], '
    '"comment_sentiment": {"label": "bullish"|"bearish"|"neutral"|"mixed", "score": float -1..1, "n": int}'
    "}. Use [] when something is not mentioned. Never invent numbers."
)

PHASES = {"pre_call", "post_call"}  # allowed by youtube_video_insights_phase_check


def build_input(v, transcript, comments):
    text = (transcript or "").strip()
    excerpt = text[:6000] + ("\n...\n" + text[-1500:] if len(text) > 7500 else "")
    return {
        "title": v.get("title"),
        "channel": v.get("channel_name"),
        "phase": v.get("phase"),
        "hours_from_call": v.get("hours_from_call"),
        "views": v.get("view_count"),
        "description": (v.get("description") or "")[:1500],
        "transcript_excerpt": excerpt or "(no transcript available)",
        "top_comments": [{"text": (c.get("text") or "")[:300], "likes": c.get("like_count")} for c in comments],
    }


def analyze(v, transcript, comments):
    out = grok_json(SYSTEM, json.dumps(build_input(v, transcript, comments), ensure_ascii=False, default=str))
    score = max(-1.0, min(1.0, float(out.get("sentiment_score") or 0)))
    sentiment = out.get("sentiment") if out.get("sentiment") in ("bullish", "bearish", "neutral", "mixed") else (
        "bullish" if score > 0.15 else "bearish" if score < -0.15 else "neutral")
    lst = lambda k: out.get(k) if isinstance(out.get(k), list) else []  # noqa: E731
    return {
        "video_id": v["video_id"],
        "earnings_event_id": v.get("earnings_event_id"),
        "phase": v.get("phase") if v.get("phase") in PHASES else None,
        "summary": out.get("summary"),
        "sentiment": sentiment,
        "sentiment_score": round(score, 4),
        "key_points": lst("key_points"),
        "topics": [str(t) for t in lst("topics")][:6],
        "mentioned_metrics": lst("mentioned_metrics"),
        "price_targets": lst("price_targets"),
        "analyst_actions": lst("analyst_actions"),
        "guidance_commentary": lst("guidance_commentary"),
        "red_flags": lst("red_flags"),
        "comment_sentiment": out.get("comment_sentiment") if isinstance(out.get("comment_sentiment"), dict) else None,
        "analysis_source": "grok_pulse",
    }


def run(limit=None, workers=6):
    videos = fetch_all("youtube_videos")
    done = {i["video_id"] for i in fetch_all("youtube_video_insights", "video_id")}
    todo = [v for v in videos if v["video_id"] not in done][: limit or None]
    if not todo:
        print("✅ every video already has insights")
        return
    transcripts = {t["video_id"]: t.get("full_text") for t in fetch_all("youtube_transcripts", "video_id,full_text")}
    comments = {}
    for c in sorted(fetch_all("youtube_comments", "video_id,text,like_count"), key=lambda c: -(c.get("like_count") or 0)):
        comments.setdefault(c["video_id"], [])
        if len(comments[c["video_id"]]) < 15:
            comments[c["video_id"]].append(c)
    print(f"🎬 {len(todo)} videos need insights ({sum(v['video_id'] in transcripts for v in todo)} with transcripts) · {grok_model()}")

    def job(v):
        try:
            return analyze(v, transcripts.get(v["video_id"]), comments.get(v["video_id"], []))
        except Exception as e:
            print(f"   ⚠️ {v['video_id']}: {e}")
            return None

    ok = 0
    with ThreadPoolExecutor(workers) as pool:
        for row in pool.map(job, todo):  # Grok calls in parallel, Supabase writes on this thread
            if not row:
                continue
            for attempt in range(3):
                try:
                    sb().table("youtube_video_insights").upsert(row, on_conflict="video_id").execute()
                    ok += 1
                    break
                except Exception as e:
                    if attempt == 2:
                        print(f"   ⚠️ write {row['video_id']}: {e}")
            print(f"   {ok}/{len(todo)}  [{row['phase']}] {row['sentiment']:>7} {row['sentiment_score']:+.2f}  {str(row['summary'])[:70]}")
    print(f"✅ wrote {ok} insights")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    run(a.limit)
