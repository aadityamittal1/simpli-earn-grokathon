"use client";

import { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { API_BASE_URL } from "@/lib/api-config";

type Tone = "positive" | "negative" | "mixed";

type Article = {
  title: string;
  source: string;
  published_at: string;
  url: string;
  summary: string;
};

type Pipeline = {
  label: string;
  queries: string[];
  planned_by_grok: boolean;
  analyzed_by_grok: boolean;
  article_count: number;
};

type NewsResponse = {
  ticker: string;
  company: string;
  call_date: string;
  takeaway: { tone: Tone; summary: string; themes: string[]; for_investors: string };
  articles: Article[];
  analysis?: string;
  notice?: string | null;
  pipeline?: Pipeline | null;
};

type Status =
  | { state: "idle" }
  | { state: "waiting" }
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "ready"; data: NewsResponse };

const TONE_LABEL: Record<Tone, string> = {
  positive: "Positive coverage",
  negative: "Negative coverage",
  mixed: "Mixed coverage",
};

const TONE_CLASS: Record<Tone, string> = {
  positive: "border-brand/40 bg-accent text-brand",
  negative: "border-destructive/40 bg-destructive/15 text-destructive",
  mixed: "border-white/15 bg-white/5 text-white/80",
};

function formatDay(iso: string) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (!match) return iso;
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
  return date.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  });
}

function parsePipeline(value: unknown): Pipeline | null {
  if (!value || typeof value !== "object") return null;
  const pipeline = value as Record<string, unknown>;
  if (typeof pipeline.label !== "string") return null;
  const queries = Array.isArray(pipeline.queries)
    ? pipeline.queries.filter((query): query is string => typeof query === "string" && query.trim().length > 0)
    : [];
  return {
    label: pipeline.label,
    queries,
    planned_by_grok: pipeline.planned_by_grok === true,
    analyzed_by_grok: pipeline.analyzed_by_grok === true,
    article_count: typeof pipeline.article_count === "number" ? pipeline.article_count : queries.length,
  };
}

function asTone(value: unknown): Tone {
  return value === "positive" || value === "negative" || value === "mixed" ? value : "mixed";
}

function parseNews(value: unknown): NewsResponse | null {
  if (!value || typeof value !== "object") return null;
  const body = value as Record<string, unknown>;
  const takeaway = body.takeaway as Record<string, unknown> | null;
  if (!takeaway || typeof takeaway !== "object" || typeof body.company !== "string" || typeof body.ticker !== "string") {
    return null;
  }
  const articles = Array.isArray(body.articles)
    ? body.articles.flatMap((item) => {
        if (!item || typeof item !== "object") return [];
        const article = item as Record<string, unknown>;
        if (typeof article.title !== "string" || typeof article.url !== "string") return [];
        return [{
          title: article.title,
          source: typeof article.source === "string" ? article.source : "",
          published_at: typeof article.published_at === "string" ? article.published_at : "",
          url: article.url,
          summary: typeof article.summary === "string" ? article.summary : "",
        }];
      })
    : [];
  const themes = Array.isArray(takeaway.themes)
    ? takeaway.themes.filter((theme): theme is string => typeof theme === "string" && theme.trim().length > 0)
    : [];
  return {
    ticker: body.ticker,
    company: body.company,
    call_date: typeof body.call_date === "string" ? body.call_date : "",
    takeaway: {
      tone: asTone(takeaway.tone),
      summary: typeof takeaway.summary === "string" ? takeaway.summary : "",
      themes,
      for_investors: typeof takeaway.for_investors === "string" ? takeaway.for_investors : "",
    },
    articles,
    analysis: typeof body.analysis === "string" ? body.analysis : undefined,
    notice: typeof body.notice === "string" ? body.notice : null,
    pipeline: parsePipeline(body.pipeline),
  };
}

export default function RelatedNews() {
  const searchParams = useSearchParams();
  const dashboardId = searchParams.get("id");
  const tickerParam = searchParams.get("ticker");
  const videoUrl = searchParams.get("video_url");
  const callDate = searchParams.get("date");
  const jobId = searchParams.get("job");
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<{ key: string; status: Status } | null>(null);

  const ticker = tickerParam && tickerParam !== "N/A" ? tickerParam : null;
  const identifiable = Boolean(dashboardId || videoUrl || (ticker && callDate));
  const requestKey = jobId || !identifiable
    ? null
    : [dashboardId ?? "", ticker ?? "", callDate ?? "", videoUrl ?? "", String(attempt)].join("|");

  useEffect(() => {
    if (!requestKey) return;

    const params = new URLSearchParams();
    if (dashboardId) params.set("id", dashboardId);
    if (ticker) params.set("ticker", ticker);
    if (callDate) params.set("date", callDate);
    if (videoUrl) params.set("video_url", videoUrl);

    const controller = new AbortController();
    let cancelled = false;
    const timer = setTimeout(() => controller.abort("timeout"), 90_000);
    const key = requestKey;

    fetch(`${API_BASE_URL}/news?${params.toString()}`, { signal: controller.signal })
      .then(async (res) => {
        const body = await res.json().catch(() => null);
        if (!res.ok) {
          const detail = body && typeof body.detail === "string" ? body.detail : `Server error (${res.status})`;
          throw new Error(detail);
        }
        const parsed = parseNews(body);
        if (!parsed) throw new Error("The news response was incomplete.");
        if (!cancelled) setResult({ key, status: { state: "ready", data: parsed } });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (controller.signal.reason === "timeout") {
          setResult({ key, status: { state: "error", message: "Related news took too long. Try again." } });
          return;
        }
        const message = err instanceof Error ? err.message : "Failed to load related news.";
        setResult({ key, status: { state: "error", message } });
      })
      .finally(() => clearTimeout(timer));

    return () => {
      cancelled = true;
      clearTimeout(timer);
      controller.abort();
    };
  }, [callDate, dashboardId, requestKey, ticker, videoUrl]);

  const status: Status = jobId
    ? { state: "waiting" }
    : !identifiable
      ? { state: "idle" }
      : result?.key === requestKey
        ? result.status
        : { state: "loading" };

  const ready = status.state === "ready" ? status.data : null;
  const tone = ready ? ready.takeaway.tone : "mixed";

  return (
    <section
      id="related-news"
      aria-label="Related news"
      aria-busy={status.state === "loading"}
      className="surface flex max-h-[36rem] min-h-48 w-full flex-col overflow-hidden rounded-2xl text-foreground"
    >
      <div className="panel-header justify-between gap-3">
        <h1 className="text-lg font-medium">Related News</h1>
        {ready && (
          <p className="text-sm text-white/50">
            {ready.company} ({ready.ticker})
            {ready.call_date ? ` · ${formatDay(ready.call_date)}` : ""}
          </p>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-auto px-6 pb-6" style={{ scrollbarColor: "#ffffff9f #ffffff0f" }}>
        {status.state === "loading" && (
          <div className="flex flex-col gap-3" aria-busy="true">
            <p className="text-center text-sm text-white/70">Searching coverage from around this call…</p>
            {[96, 80, 88, 70].map((width, index) => (
              <div key={index} className="h-16 animate-pulse rounded-2xl bg-white/10" style={{ width: `${width}%` }} />
            ))}
          </div>
        )}

        {status.state === "waiting" && (
          <p className="text-center text-sm leading-relaxed text-white/70">
            Related coverage appears when processing finishes and the company and call date are known.
          </p>
        )}

        {status.state === "idle" && (
          <p className="text-center text-sm leading-relaxed text-white/70">
            Open an earnings call to see news from around that call.
          </p>
        )}

        {status.state === "error" && (
          <div className="mx-auto max-w-lg text-center">
            <p className="text-lg font-medium text-white/90">Related news unavailable</p>
            <p className="mt-2 text-sm leading-relaxed text-white/70">{status.message}</p>
            <button type="button" className="btn-outline mt-4" onClick={() => setAttempt((value) => value + 1)}>
              Try again
            </button>
          </div>
        )}

        {ready && (
          <div className="flex flex-col gap-3">
            {ready.notice && (
              <p className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm leading-relaxed text-white/80">
                {ready.notice}
              </p>
            )}
            {ready.pipeline && (ready.pipeline.planned_by_grok || ready.pipeline.analyzed_by_grok || ready.pipeline.queries.length > 0) && (
              <div className="rounded-2xl border border-brand/30 bg-accent px-4 py-3">
                {(ready.pipeline.analyzed_by_grok || ready.pipeline.planned_by_grok) && (
                  <p className="text-xs font-medium text-brand">Analyzed by Grok</p>
                )}
                <p className={`text-sm leading-relaxed text-white/85 ${ready.pipeline.analyzed_by_grok || ready.pipeline.planned_by_grok ? "mt-1" : ""}`}>
                  {ready.pipeline.label}
                </p>
                {ready.pipeline.queries.length > 0 && (
                  <details className="mt-2">
                    <summary className="cursor-pointer text-xs font-medium text-white/60">
                      {ready.pipeline.planned_by_grok ? "Queries Grok planned" : "Search query"}
                    </summary>
                    <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-white/75">
                      {ready.pipeline.queries.map((query) => (
                        <li key={query}>{query}</li>
                      ))}
                    </ul>
                  </details>
                )}
              </div>
            )}
            <div className="rounded-2xl border border-white/8 bg-black/20 p-4">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <span className={`rounded-full border px-2.5 py-0.5 text-xs font-medium ${TONE_CLASS[tone]}`}>
                  {TONE_LABEL[tone]}
                </span>
                {ready.analysis === "grok" && (
                  <span className="rounded-full border border-brand/40 bg-accent px-2.5 py-0.5 text-xs font-medium text-brand">
                    Analyzed by Grok
                  </span>
                )}
                {ready.takeaway.themes.map((theme) => (
                  <span key={theme} className="rounded-full bg-white/5 px-2.5 py-0.5 text-xs text-white/70">
                    {theme}
                  </span>
                ))}
              </div>
              <p className="text-sm leading-relaxed text-white/85">{ready.takeaway.summary}</p>
              {ready.takeaway.for_investors && (
                <div className="mt-3 rounded-xl border border-brand/30 bg-accent px-3 py-2.5">
                  <p className="text-xs font-medium text-brand">For retail investors</p>
                  <p className="mt-1 text-sm leading-relaxed text-white/85">{ready.takeaway.for_investors}</p>
                </div>
              )}
            </div>

            {ready.articles.length === 0 ? (
              <p className="px-1 text-sm text-white/70">No articles matched this call.</p>
            ) : (
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                {ready.articles.map((article) => (
                  <article key={article.url} className="rounded-2xl border border-white/8 bg-black/20 p-4">
                    <a
                      href={article.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-base font-medium leading-snug text-white transition-colors hover:text-brand"
                    >
                      {article.title}
                    </a>
                    <p className="mt-1 text-xs text-white/50">
                      {[article.source, article.published_at ? formatDay(article.published_at) : ""].filter(Boolean).join(" · ")}
                    </p>
                    {article.summary && (
                      <p className="mt-2 text-sm leading-relaxed text-white/80">{article.summary}</p>
                    )}
                  </article>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
