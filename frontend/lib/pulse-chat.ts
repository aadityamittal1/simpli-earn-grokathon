// Shapes for the Grok-enhanced SimpliChat (/api/pulse/chat). Kept separate from chat-history.ts so
// the classic RAG chat types stay as they were.

export type PulsePhase = "pre" | "during" | "post";
export type SourceKey = "transcript" | "x" | "youtube" | "news";

export type Citation = {
  id: string;
  label: string; // e.g. "@zerohedge · 3.4M followers" or "CNBC · Jan 30"
  text: string; // quote / title / transcript excerpt
  url?: string;
  phase?: PulsePhase | null;
  minutes?: number | null; // minutes from the call (X posts)
  sentiment?: string | null;
  thumbnail?: string;
};

export type GrokSection = {
  source: SourceKey;
  title: string;
  summary: string;
  citations: Citation[];
  empty?: boolean; // no data for this source yet
};

export type GrokPayload = {
  sections: GrokSection[];
  synthesis: string;
  signal?: "bullish" | "neutral" | "bearish" | string;
  pulse?: Partial<Record<PulsePhase, number | null>>;
  model?: string;
};
