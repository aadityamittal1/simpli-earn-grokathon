"""
Grok scores every row the bots stored without a sentiment score. Safe to re-run while bots write.

    python -m grok_pulse.score            # x_posts + articles + youtube_video_insights
    python -m grok_pulse.score --limit 200
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor

from grok_pulse.common import fetch_all, grok_json, grok_model, sb

SYSTEM = (
    "You are a sell-side equity analyst scoring investor sentiment about a stock around its earnings call. "
    "For each item return a score from -1 (very bearish) to 1 (very bullish), 0 = neutral or off-topic, "
    "and a label: bullish | neutral | bearish. Judge the stance toward the company/stock, not general mood. "
    'Return JSON only: {"results": [{"id": "<id>", "score": <float>, "label": "<label>"}]}'
)

# table -> (id column, text builder)
TARGETS = {
    "x_posts": ("id", lambda r: r.get("text") or ""),
    "articles": ("id", lambda r: f"{r.get('title') or ''}. {r.get('summary') or r.get('content_excerpt') or ''}"),
    "youtube_video_insights": ("id", lambda r: r.get("summary") or ""),
}


def score_batch(rows, id_col, text_of):
    items = [{"id": str(r[id_col]), "text": text_of(r)[:600]} for r in rows]
    out = grok_json(SYSTEM, json.dumps(items, ensure_ascii=False))
    by_id = {str(x.get("id")): x for x in out.get("results", [])}
    return [(r[id_col], by_id.get(str(r[id_col]))) for r in rows]


def write(table, id_col, rid, res):
    score = max(-1.0, min(1.0, float(res.get("score", 0))))
    label = res.get("label") if res.get("label") in ("bullish", "neutral", "bearish") else (
        "bullish" if score > 0.15 else "bearish" if score < -0.15 else "neutral")
    col = "sentiment_label" if table == "x_posts" else "sentiment"
    for attempt in range(3):  # the shared HTTP/2 connection occasionally drops; retry
        try:
            sb().table(table).update({"sentiment_score": round(score, 4), col: label}).eq(id_col, rid).execute()
            return
        except Exception:
            if attempt == 2:
                raise


def run(table, limit=None, batch=50, workers=4):
    id_col, text_of = TARGETS[table]
    try:
        rows = [r for r in fetch_all(table) if r.get("sentiment_score") is None]
    except Exception as e:
        print(f"⏭️  {table}: {e}")
        return 0
    rows = rows[:limit] if limit else rows
    if not rows:
        print(f"✅ {table}: nothing to score")
        return 0
    print(f"🧮 {table}: scoring {len(rows)} rows with {grok_model()} ({batch}/call)")
    batches = [rows[i:i + batch] for i in range(0, len(rows), batch)]
    done = 0
    with ThreadPoolExecutor(workers) as pool:
        for results in pool.map(lambda b: score_batch(b, id_col, text_of), batches):
            pairs = [(rid, res) for rid, res in results if res]
            for p in pairs:  # Supabase writes stay on this thread (client is not thread-safe)
                write(table, id_col, *p)
            done += len(pairs)
            print(f"   {done}/{len(rows)}")
    return done


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", choices=list(TARGETS), nargs="*", default=list(TARGETS))
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()
    for t in args.table:
        run(t, args.limit)
