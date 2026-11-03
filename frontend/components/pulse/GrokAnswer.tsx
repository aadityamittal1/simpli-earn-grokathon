"use client";

import { useState } from "react";
import type { Citation, GrokPayload, GrokSection, PulsePhase, SourceKey } from "@/lib/pulse-chat";

const SOURCE_STYLE: Record<SourceKey, { icon: string; color: string }> = {
  transcript: { icon: "📞", color: "#6fd98a" },
  x: { icon: "𝕏", color: "#e8e8e8" },
  youtube: { icon: "▶", color: "#ff5a5a" },
  news: { icon: "📰", color: "#7fb3ff" },
};

const PHASE: Record<PulsePhase, { label: string; color: string }> = {
  pre: { label: "Before call", color: "#e0c56b" },
  during: { label: "During call", color: "#c79bff" },
  post: { label: "After call", color: "#6fd98a" },
};

const SIGNAL_COLOR: Record<string, string> = { bullish: "#6fd98a", bearish: "#e06b6b", neutral: "#e0c56b" };
const sentimentColor = (s?: string | null) => SIGNAL_COLOR[String(s ?? "").toLowerCase()] ?? "#9aa0a6";

function PhaseBadge({ phase }: { phase?: PulsePhase | null }) {
  if (!phase) return null;
  const p = PHASE[phase];
  return (
    <span className="rounded-full px-1.5 py-px text-[10px] font-semibold uppercase tracking-wide" style={{ background: `${p.color}22`, color: p.color }}>
      {p.label}
    </span>
  );
}

function CitationChip({ c }: { c: Citation }) {
  const inner = (
    <>
      <div className="mb-0.5 flex flex-wrap items-center gap-1.5 text-[11px] text-[var(--muted-foreground)]">
        <span className="font-semibold text-[var(--foreground)]">{c.label}</span>
        {c.minutes != null && <span>{c.minutes > 0 ? "+" : ""}{c.minutes}m</span>}
        <PhaseBadge phase={c.phase} />
        {c.sentiment && <span className="ml-auto text-[10px] font-semibold uppercase" style={{ color: sentimentColor(c.sentiment) }}>{c.sentiment}</span>}
      </div>
      <div className="flex gap-2">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        {c.thumbnail && <img src={c.thumbnail} alt="" className="h-10 w-16 shrink-0 rounded object-cover" />}
        <p className="line-clamp-3 text-xs leading-snug text-[var(--foreground)]/90">{c.text}</p>
      </div>
    </>
  );
  const cls = "block rounded-lg border border-[var(--border)] bg-black/20 px-2.5 py-2 transition hover:border-[var(--brand)]/60";
  return c.url ? (
    <a href={c.url} target="_blank" rel="noreferrer" className={`${cls} no-underline`}>{inner}</a>
  ) : (
    <div className={cls}>{inner}</div>
  );
}

function SourceCard({ s }: { s: GrokSection }) {
  const style = SOURCE_STYLE[s.source];
  const noData = s.empty || (!s.citations.length && /no relevant coverage/i.test(s.summary));
  const [open, setOpen] = useState(!noData);
  return (
    <div className="rounded-xl border border-[var(--border)] bg-[var(--muted)]/60" style={{ borderLeft: `3px solid ${style.color}` }}>
      <button onClick={() => setOpen((o) => !o)} className="flex w-full items-center gap-2 px-3 py-2 text-left">
        <span className="w-4 text-center text-sm" style={{ color: style.color }}>{style.icon}</span>
        <span className="text-xs font-semibold uppercase tracking-wide">{s.title}</span>
        <span className="ml-auto text-[11px] text-[var(--muted-foreground)]">
          {noData ? (s.empty ? "Grok bot collecting…" : "no match") : `${s.citations.length} cited`} {open ? "▾" : "▸"}
        </span>
      </button>
      {open && (
        <div className="space-y-2 px-3 pb-3">
          <p className="text-sm leading-snug">{s.summary || "No relevant coverage in this source yet."}</p>
          {s.citations.length > 0 && <div className="grid gap-1.5">{s.citations.map((c) => <CitationChip key={c.id} c={c} />)}</div>}
        </div>
      )}
    </div>
  );
}

/** Sectioned, source-cited SimpliChat answer: one card per source, combined synthesis at the bottom. */
export default function GrokAnswer({ grok }: { grok: GrokPayload }) {
  const cited = grok.sections.filter((s) => s.citations.length).length;
  const signal = String(grok.signal ?? "").toLowerCase();
  const p = grok.pulse ?? {};
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        <span className="rounded-full bg-[var(--brand)]/15 px-2 py-0.5 font-semibold text-[var(--brand)]">✨ Grok-enhanced SimpliChat</span>
        <span className="text-[var(--muted-foreground)]">{cited} of {grok.sections.length} sources cited</span>
      </div>
      {grok.sections.map((s) => <SourceCard key={s.source} s={s} />)}
      <div className="rounded-xl border border-[var(--brand)]/30 bg-[var(--accent)] p-3">
        <div className="mb-1 flex flex-wrap items-center gap-2">
          <span className="text-xs font-semibold uppercase tracking-wide text-[var(--brand)]">Grok synthesis: all sources combined</span>
          {signal && (
            <span className="rounded-full px-2 py-0.5 text-[10px] font-bold uppercase" style={{ background: `${sentimentColor(signal)}22`, color: sentimentColor(signal) }}>
              {signal}
            </span>
          )}
        </div>
        <p className="text-sm leading-snug">{grok.synthesis}</p>
        {(p.pre != null || p.post != null) && (
          <p className="mt-2 text-[11px] text-[var(--muted-foreground)]">
            Grok Pulse: before {p.pre ?? "—"} → during {p.during ?? "—"} → after {p.post ?? "—"} (0-100, 50 = neutral)
            {grok.model ? ` · ${grok.model}` : ""}
          </p>
        )}
      </div>
    </div>
  );
}
