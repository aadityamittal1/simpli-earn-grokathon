"use client";

import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import MomentumChart, { type MomentumPoint } from "@/components/pulse/MomentumChart";

type Phase = "pre" | "during" | "post";
type Row = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
type Brief = { headline?: string; bull_points?: string[]; bear_points?: string[]; watch_items?: string[]; signal?: string; confidence?: number; action?: string };
type Snapshot = { phase: Phase; pulse_score: number | null; source_scores: Record<string, { sentiment: number | null; n: number }>; momentum: MomentumPoint[]; gap: Row; brief?: Brief; model?: string; generated_at?: string };
type PulseData = {
  ticker: string; date: string; event: Row | null; fundamentals: Row | null;
  snapshots: Partial<Record<Phase, Snapshot>>;
  x: { windows: Row[]; buckets: Row[]; topPosts: Row[] };
  articles: Row[]; videos: Row[];
};

const PHASES: { key: Phase; label: string; blurb: string; unit: number }[] = [
  { key: "pre", label: "Before", blurb: "What the market expected going in, from X chatter, news and YouTube previews.", unit: 60 },
  { key: "during", label: "During", blurb: "Minute by minute X reaction while management spoke.", unit: 5 },
  { key: "post", label: "After", blurb: "How investors digested the results against expectations.", unit: 60 },
];

const tone = (s: number | null | undefined) =>
  s == null ? "text-[var(--muted-foreground)]" : s > 0.1 ? "text-[#6fd98a]" : s < -0.1 ? "text-[#e06b6b]" : "text-[#e0c56b]";
const pulseTone = (p: number | null | undefined) =>
  p == null ? "#9aa0a6" : p >= 58 ? "#6fd98a" : p <= 42 ? "#e06b6b" : "#e0c56b";
const money = (v: unknown, unit: string) => (unit.startsWith("$") ? `$${v}${unit.slice(1)}` : `${v}${unit}`);
const fmt = (n: unknown) => (typeof n === "number" ? n.toLocaleString() : "—");

/** Aggregate 5-minute view buckets into `unit`-minute points (fallback when no snapshot exists). */
function bucketsToPoints(buckets: Row[], phase: Phase, unit: number): MomentumPoint[] {
  const acc = new Map<number, { posts: number; sum: number; n: number }>();
  for (const b of buckets) {
    if (b.phase !== phase) continue;
    const k = Math.floor(Number(b.bucket_minutes_from_call) / unit) * unit;
    const a = acc.get(k) ?? { posts: 0, sum: 0, n: 0 };
    a.posts += Number(b.posts) || 0;
    if (b.avg_sentiment != null) { a.sum += Number(b.avg_sentiment) * (Number(b.posts) || 1); a.n += Number(b.posts) || 1; }
    acc.set(k, a);
  }
  return [...acc.entries()].sort((a, b) => a[0] - b[0]).map(([m, a]) => ({ minutes_from_call: m, posts: a.posts, sentiment: a.n ? a.sum / a.n : null }));
}

function Card({ title, children, right }: { title: string; children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <section className="rounded-2xl border border-[var(--border)] bg-[var(--card)] p-5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold uppercase tracking-wider text-[var(--muted-foreground)]">{title}</h3>
        {right}
      </div>
      {children}
    </section>
  );
}

function Empty({ what }: { what: string }) {
  return (
    <p className="flex items-center gap-2 text-sm text-[var(--muted-foreground)]">
      <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-[var(--brand)]" /> Grok bot still collecting {what}…
    </p>
  );
}

function PulseGauge({ snap }: { snap?: Snapshot }) {
  const p = snap?.pulse_score ?? null;
  const sig = snap?.brief?.signal;
  return (
    <Card title="Grok Pulse score">
      <div className="flex items-end gap-4">
        <span className="text-6xl font-bold leading-none" style={{ color: pulseTone(p) }}>{p ?? "—"}</span>
        <span className="pb-1 text-sm text-[var(--muted-foreground)]">/ 100 · 50 = neutral</span>
        {sig && (
          <span className="ml-auto rounded-full px-3 py-1 text-xs font-semibold uppercase" style={{ background: `${pulseTone(p)}22`, color: pulseTone(p) }}>
            {sig}{snap?.brief?.confidence != null ? ` · ${Math.round(snap.brief.confidence * 100)}%` : ""}
          </span>
        )}
      </div>
      <div className="mt-4 h-2 overflow-hidden rounded-full bg-[var(--muted)]">
        <div className="h-full rounded-full transition-all duration-700" style={{ width: `${p ?? 0}%`, background: pulseTone(p) }} />
      </div>
      <div className="mt-4 grid grid-cols-3 gap-2 text-center">
        {(["x", "articles", "youtube"] as const).map((k) => {
          const s = snap?.source_scores?.[k];
          return (
            <div key={k} className="rounded-xl bg-[var(--muted)] px-2 py-2">
              <div className="text-[11px] uppercase tracking-wide text-[var(--muted-foreground)]">{k === "x" ? "X" : k === "youtube" ? "YouTube" : "News"}</div>
              <div className={`text-lg font-semibold ${tone(s?.sentiment)}`}>{s?.sentiment == null ? "—" : (s.sentiment > 0 ? "+" : "") + s.sentiment.toFixed(2)}</div>
              <div className="text-[11px] text-[var(--muted-foreground)]">{s?.n ?? 0} items</div>
            </div>
          );
        })}
      </div>
      <p className="mt-3 text-[11px] text-[var(--muted-foreground)]">
        Weighted: X 40% (by followers × engagement) · News 35% (+analyst actions) · YouTube 25% (by views)
      </p>
    </Card>
  );
}

function List({ title, items, color }: { title: string; items?: string[]; color: string }) {
  if (!items?.length) return null;
  return (
    <div>
      <div className="mb-1 text-xs font-semibold uppercase" style={{ color }}>{title}</div>
      <ul className="space-y-1 text-sm leading-snug">{items.map((t, i) => <li key={i} className="flex gap-2"><span style={{ color }}>•</span><span>{t}</span></li>)}</ul>
    </div>
  );
}

function BriefCard({ snap }: { snap?: Snapshot }) {
  const b = snap?.brief;
  if (!b) return <Card title="Grok brief"><Empty what="evidence for the brief" /></Card>;
  return (
    <Card title="Grok brief" right={<span className="text-[11px] text-[var(--muted-foreground)]">{snap?.model}</span>}>
      <p className="mb-4 text-lg font-semibold leading-snug">{b.headline}</p>
      <div className="grid gap-4 md:grid-cols-3">
        <List title="Bull case" items={b.bull_points} color="#6fd98a" />
        <List title="Bear case" items={b.bear_points} color="#e06b6b" />
        <List title="Watch" items={b.watch_items} color="#e0c56b" />
      </div>
      {b.action && <p className="mt-4 rounded-xl bg-[var(--accent)] px-4 py-3 text-sm"><span className="font-semibold text-[var(--brand)]">Actionable: </span>{b.action}</p>}
    </Card>
  );
}

function Posts({ posts }: { posts: Row[] }) {
  if (!posts.length) return <Empty what="X posts" />;
  return (
    <ul className="max-h-[420px] space-y-2 overflow-y-auto pr-1">
      {posts.slice(0, 12).map((p) => (
        <li key={p.id} className="rounded-xl bg-[var(--muted)] p-3 text-sm">
          <div className="mb-1 flex items-center gap-2 text-xs text-[var(--muted-foreground)]">
            <a href={p.url} target="_blank" rel="noreferrer" className="font-semibold text-[var(--foreground)] hover:underline">@{p.username}</a>
            <span>{fmt(p.followers)} followers</span>
            <span>· {Number(p.minutes_from_call) > 0 ? "+" : ""}{Math.round(Number(p.minutes_from_call))}m</span>
            {p.sentiment_label && <span className={`ml-auto font-semibold uppercase ${tone(p.sentiment_score)}`}>{p.sentiment_label}</span>}
          </div>
          <p className="leading-snug">{p.text}</p>
          <div className="mt-1 text-xs text-[var(--muted-foreground)]">♥ {fmt(p.like_count)} · ↻ {fmt(p.repost_count)} · 👁 {fmt(p.impression_count)}</div>
        </li>
      ))}
    </ul>
  );
}

function Articles({ items }: { items: Row[] }) {
  if (!items.length) return <Empty what="articles" />;
  return (
    <ul className="max-h-[420px] space-y-2 overflow-y-auto pr-1">
      {items.map((a) => (
        <li key={a.id} className="rounded-xl bg-[var(--muted)] p-3 text-sm">
          <div className="mb-1 flex items-center gap-2 text-xs text-[var(--muted-foreground)]">
            <span className="font-semibold text-[var(--foreground)]">{a.source}</span>
            {a.published_at && <span>{new Date(a.published_at).toLocaleDateString()}</span>}
            <span className={`ml-auto font-semibold uppercase ${tone(a.sentiment_score)}`}>{a.sentiment}</span>
          </div>
          <a href={a.url} target="_blank" rel="noreferrer" className="font-medium leading-snug hover:underline">{a.title}</a>
          {Array.isArray(a.analyst_actions) && a.analyst_actions.length > 0 && (
            <div className="mt-1 flex flex-wrap gap-1">
              {a.analyst_actions.slice(0, 3).map((x: Row, i: number) => (
                <span key={i} className="rounded-full bg-[var(--accent)] px-2 py-0.5 text-[11px] text-[var(--brand)]">
                  {[x.firm, x.action, x.price_target ?? x.new_price_target].filter(Boolean).join(" · ") || JSON.stringify(x).slice(0, 60)}
                </span>
              ))}
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

function Videos({ items }: { items: Row[] }) {
  if (!items.length) return <Empty what="YouTube videos" />;
  return (
    <ul className="grid gap-2 sm:grid-cols-2">
      {items.slice(0, 6).map((v) => (
        <li key={v.video_id} className="overflow-hidden rounded-xl bg-[var(--muted)] text-sm">
          <a href={v.url || `https://www.youtube.com/watch?v=${v.video_id}`} target="_blank" rel="noreferrer">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={`https://i.ytimg.com/vi/${v.video_id}/mqdefault.jpg`} alt="" className="aspect-video w-full object-cover" />
          </a>
          <div className="p-3">
            <p className="line-clamp-2 font-medium leading-snug">{v.title}</p>
            <div className="mt-1 flex items-center gap-2 text-xs text-[var(--muted-foreground)]">
              <span>{v.channel_name}</span><span>· {fmt(v.view_count)} views</span>
              {v.insight?.sentiment && <span className={`ml-auto font-semibold uppercase ${tone(v.insight.sentiment_score)}`}>{v.insight.sentiment}</span>}
            </div>
          </div>
        </li>
      ))}
    </ul>
  );
}

function GapCard({ snap, pre }: { snap?: Snapshot; pre?: Snapshot }) {
  const g = snap?.gap;
  if (!g?.metrics?.length) return <Card title="Expectations vs reality"><Empty what="reported numbers" /></Card>;
  return (
    <Card title="Expectations vs reality" right={<span className="rounded-full bg-[var(--accent)] px-3 py-1 text-xs font-semibold text-[var(--brand)]">{g.verdict}</span>}>
      <div className="mb-3 flex items-center gap-3 text-sm">
        <span>Pulse before <b style={{ color: pulseTone(pre?.pulse_score) }}>{pre?.pulse_score ?? "—"}</b></span>
        <span className="text-[var(--muted-foreground)]">→</span>
        <span>after <b style={{ color: pulseTone(snap?.pulse_score) }}>{snap?.pulse_score ?? "—"}</b></span>
        {g.sentiment_shift != null && <span className={tone(g.sentiment_shift / 50)}>({g.sentiment_shift > 0 ? "+" : ""}{g.sentiment_shift} pts)</span>}
        <span className="ml-auto text-[var(--muted-foreground)]">{g.beats}/{g.metrics_compared} beats</span>
      </div>
      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase text-[var(--muted-foreground)]">
          <tr><th className="py-1">Metric</th><th>Consensus</th><th>Actual</th><th className="text-right">Surprise</th></tr>
        </thead>
        <tbody>
          {g.metrics.map((m: Row) => (
            <tr key={m.metric} className="border-t border-[var(--border)]">
              <td className="py-1.5">{m.metric}</td>
              <td>{money(m.consensus, m.unit)}</td>
              <td>{money(m.actual, m.unit)}</td>
              <td className={`text-right font-semibold ${m.surprise_pct >= 0 ? "text-[#6fd98a]" : "text-[#e06b6b]"}`}>{m.surprise_pct > 0 ? "+" : ""}{m.surprise_pct}%</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

function PulseContent() {
  const params = useSearchParams();
  const ticker = (params.get("ticker") || "AAPL").toUpperCase();
  const date = params.get("date") || "2025-01-30";
  const [phase, setPhase] = useState<Phase>((params.get("phase") as Phase) || "pre");
  const [data, setData] = useState<PulseData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [replayAt, setReplayAt] = useState<number | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      fetch(`/api/pulse?ticker=${ticker}&date=${date}`)
        .then((r) => r.json())
        .then((d) => { if (alive) { if (d.error) setError(d.error); else setData(d); } })
        .catch((e) => alive && setError(String(e)));
    load();
    const poll = setInterval(load, 30000); // bots keep writing; refresh every 30s
    return () => { alive = false; clearInterval(poll); };
  }, [ticker, date]);

  useEffect(() => () => { if (timer.current) clearInterval(timer.current); }, []);

  const meta = PHASES.find((p) => p.key === phase)!;
  const snap = data?.snapshots[phase];
  const points = useMemo(() => {
    if (!data) return [];
    const fromSnap = snap?.momentum?.length ? snap.momentum : null;
    return fromSnap ?? bucketsToPoints(data.x.buckets, phase, meta.unit);
  }, [data, snap, phase, meta.unit]);
  const shown = replayAt == null ? points : points.slice(0, replayAt);

  const startReplay = () => {
    if (timer.current) clearInterval(timer.current);
    setReplayAt(1);
    timer.current = setInterval(() => {
      setReplayAt((i) => {
        if (i == null || i >= points.length) { if (timer.current) clearInterval(timer.current); return null; }
        return i + 1;
      });
    }, 350);
  };

  const posts = (data?.x.topPosts ?? []).filter((p) => p.phase === phase);
  const articles = (data?.articles ?? []).filter((a) => a.phase === phase);
  const videos = (data?.videos ?? []).filter((v) => v.phase === phase);
  const win = data?.x.windows.find((w) => String(w.time_window).startsWith(phase));
  const liveSoFar = replayAt != null && shown.length ? shown[shown.length - 1] : null;

  return (
    <main className="mx-auto max-w-7xl px-4 pb-16 pt-6 text-[var(--foreground)] md:px-8">
      <div className="mb-6 flex flex-wrap items-center gap-3">
        <Link href="/dashboard?id=1" className="btn-outline no-underline">← Back to call dashboard</Link>
        <div className="ml-auto flex flex-wrap gap-2 text-xs text-[var(--muted-foreground)]">
          {[["X bot", data?.x.windows.reduce((s, w) => s + Number(w.posts || 0), 0)], ["News bot", data?.articles.length], ["YouTube bot", data?.videos.length]].map(([n, c]) => (
            <span key={String(n)} className="flex items-center gap-1.5 rounded-full border border-[var(--border)] px-3 py-1">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-[var(--brand)]" />{n}: {c ?? "…"}
            </span>
          ))}
        </div>
      </div>

      <h1 className="text-3xl font-bold md:text-4xl">Grok Earnings Pulse</h1>
      <p className="mt-1 text-[var(--muted-foreground)]">
        {data?.event?.company ?? ticker} · {data?.fundamentals?.fiscal_period ?? data?.event?.fiscal_quarter ?? ""} · call {data?.event?.call_time_utc ? new Date(data.event.call_time_utc).toLocaleString() : date}
      </p>

      <div className="mt-6 inline-flex rounded-full border border-[var(--border)] bg-[var(--card)] p-1">
        {PHASES.map((p) => (
          <button key={p.key} onClick={() => { setPhase(p.key); setReplayAt(null); }}
            className={`rounded-full px-5 py-2 text-sm font-medium transition ${phase === p.key ? "bg-[var(--brand)] text-[var(--primary-foreground)]" : "text-[var(--muted-foreground)] hover:text-[var(--foreground)]"}`}>
            {p.label}
          </button>
        ))}
      </div>
      <p className="mt-3 text-sm text-[var(--muted-foreground)]">{meta.blurb}</p>

      {error && <p className="mt-6 rounded-xl bg-[#e06b6b22] p-4 text-sm text-[#e06b6b]">{error}</p>}
      {!data && !error && <p className="mt-6 text-sm text-[var(--muted-foreground)]">Loading Grok bot data…</p>}

      {data && (
        <div className="mt-6 grid gap-5 lg:grid-cols-3">
          <div className="space-y-5 lg:col-span-2">
            <Card
              title={phase === "during" ? "Live X momentum (5-min)" : "X momentum (hourly)"}
              right={
                <div className="flex items-center gap-3 text-xs text-[var(--muted-foreground)]">
                  {win && <span>{fmt(Number(win.posts))} posts · {fmt(Number(win.authors))} authors · {fmt(Number(win.impressions))} impressions</span>}
                  <button onClick={startReplay} className="rounded-full border border-[var(--border)] px-3 py-1 font-medium text-[var(--foreground)] hover:border-[var(--brand)]">
                    {replayAt == null ? "▶ Replay" : "⟲ Restart"}
                  </button>
                </div>
              }>
              {points.length ? <MomentumChart points={shown} unit={meta.unit} /> : <Empty what="X posts" />}
              {liveSoFar && (
                <p className="mt-2 text-xs text-[var(--muted-foreground)]">
                  Replaying {liveSoFar.minutes_from_call > 0 ? "+" : ""}{liveSoFar.minutes_from_call}m from call · {liveSoFar.posts} posts in bucket · sentiment {liveSoFar.sentiment?.toFixed(2) ?? "—"}
                  {liveSoFar.spike ? " · ⚡ spike" : ""}
                </p>
              )}
            </Card>
            <BriefCard snap={snap} />
            {phase === "post" && <GapCard snap={snap} pre={data.snapshots.pre} />}
            <Card title="YouTube">{<Videos items={videos} />}</Card>
          </div>
          <div className="space-y-5">
            <PulseGauge snap={snap} />
            <Card title="Top X posts">{<Posts posts={posts} />}</Card>
            {phase !== "during" && <Card title="News & analyst notes">{<Articles items={articles} />}</Card>}
          </div>
        </div>
      )}
    </main>
  );
}

export default function PulsePage() {
  return (
    <Suspense fallback={null}>
      <PulseContent />
    </Suspense>
  );
}
