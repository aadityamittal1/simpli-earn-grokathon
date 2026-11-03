"""Shared helpers for Grok Earnings Pulse: env, Supabase, Grok (xAI) client, phase handling."""
import json
import math
import os
import re
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

XAI_BASE_URL = "https://api.x.ai/v1"
# Preference order when XAI_MODEL is not set; the first one this key can see wins.
MODEL_PREFERENCE = ("grok-4-fast-non-reasoning", "grok-4-fast", "grok-4", "grok-3-mini", "grok-3")

_sb = None
_grok = None
_model = None


def sb():
    global _sb
    if _sb is None:
        from supabase import create_client
        _sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    return _sb


def grok():
    global _grok
    if _grok is None:
        key = os.getenv("XAI_API_KEY", "").strip()
        if not key:
            raise SystemExit("XAI_API_KEY is empty: add it to grok_pulse/.env (console.x.ai)")
        from openai import OpenAI
        _grok = OpenAI(api_key=key, base_url=XAI_BASE_URL, timeout=120)
    return _grok


def grok_model() -> str:
    """XAI_MODEL if set, else the best available model from GET /v1/models."""
    global _model
    if _model:
        return _model
    _model = os.getenv("XAI_MODEL", "").strip()
    if not _model:
        ids = sorted(m.id for m in grok().models.list().data)
        _model = next((p for p in MODEL_PREFERENCE if p in ids), None) or next(
            (i for i in ids if i.startswith("grok")), ids[0])
        print(f"🤖 Grok model: {_model}  (set XAI_MODEL to override; available: {', '.join(ids)})")
    return _model


def parse_json(raw: str):
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def grok_json(system: str, user: str, temperature: float = 0.2) -> dict:
    """One Grok chat call that must return a JSON object."""
    kwargs = dict(model=grok_model(), temperature=temperature,
                  messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
    try:
        resp = grok().chat.completions.create(response_format={"type": "json_object"}, **kwargs)
    except Exception as e:  # some models reject response_format; the prompt still asks for JSON
        if "response_format" not in str(e):
            raise
        resp = grok().chat.completions.create(**kwargs)
    return parse_json(resp.choices[0].message.content)


def fetch_all(table: str, columns: str = "*", **eq) -> list[dict]:
    """Select every row (PostgREST pages at 1000)."""
    rows, start = [], 0
    while True:
        q = sb().table(table).select(columns)
        for k, v in eq.items():
            q = q.eq(k, v)
        page = q.range(start, start + 999).execute().data
        rows += page
        if len(page) < 1000:
            return rows
        start += 1000


def norm_phase(value) -> str | None:
    """Bots label phases differently (pre / pre_earnings / before ...); map to pre|during|post."""
    v = str(value or "").lower()
    if v.startswith(("pre", "before")):
        return "pre"
    if v.startswith(("during", "live", "intra")):
        return "during"
    if v.startswith(("post", "after")):
        return "post"
    return None


def num(value):
    """'46.9%' -> 46.9, 124300 -> 124300.0, junk -> None."""
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"-?\d+(\.\d+)?", str(value or "").replace(",", ""))
    return float(m.group(0)) if m else None


def log10p(x) -> float:
    return math.log10(1 + max(0.0, float(x or 0)))
