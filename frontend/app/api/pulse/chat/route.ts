// Grok-enhanced SimpliChat. Wraps the team's RAG chatbot (RAG/api_chatbot.py POST /chat, unchanged)
// and adds what the Grok bots collected (X, news, YouTube) for the same earnings call. Grok answers
// once per source using only that source's items, then writes a combined synthesis. Grok only picks
// item ids; links, handles and dates are attached here from the database so they can't be invented.
// Response keeps the /chat shape (response, suggestions, sources) and adds `grok`.
import { createClient } from '@supabase/supabase-js'
import { NextResponse, type NextRequest } from 'next/server'
import { API_BASE_URL } from '@/lib/api-config'
import type { Citation, GrokPayload, GrokSection, PulsePhase, SourceKey } from '@/lib/pulse-chat'

export const dynamic = 'force-dynamic'

type Row = Record<string, any> // eslint-disable-line @typescript-eslint/no-explicit-any

const STOP = new Set('a an and are as at be but by did do does for from had has have how i in is it its me my of on or so than that the their them then there these they this to was we were what when where which who why will with would you your about after before during call apple aapl earnings quarter q1 say said tell'.split(' '))

const phaseOf = (v: unknown): PulsePhase | null => {
  const s = String(v ?? '').toLowerCase()
  if (s.startsWith('pre') || s.startsWith('before')) return 'pre'
  if (s.startsWith('during') || s.startsWith('live')) return 'during'
  if (s.startsWith('post') || s.startsWith('after')) return 'post'
  return null
}
const compact = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}K` : String(n))
const keywords = (q: string) => [...new Set(q.toLowerCase().match(/[a-z0-9$%.]+/g) ?? [])].filter((w) => w.length > 2 && !STOP.has(w))
const overlap = (text: string, words: string[]) => words.reduce((n, w) => n + (text.toLowerCase().includes(w) ? 1 : 0), 0)

const SYSTEM = `You are SimpliChat, now upgraded with Grok. You answer an investor's question about one earnings call
using FOUR separate evidence sources, kept strictly apart:
- "transcript": what management said on the call (plus the transcript assistant's draft answer)
- "x": posts on X/Twitter (each tagged with its phase: before / during / after the call)
- "youtube": YouTube analyst videos
- "news": news articles and analyst notes
Rules:
- For EACH source, answer the question in 1-3 sentences using ONLY that source's items. Cite the item ids you used.
- Mention timing when it matters (e.g. "before the call X was worried..., after it...").
- If a source has no items or nothing relevant, set summary to "No relevant coverage in this source yet." and cite [].
- Never invent facts, handles, numbers or links.
- "synthesis": 2-4 sentences that combine the sources: where management and the market agree or disagree,
  and how sentiment moved from before to after the call. Plain language, no personalized investment advice.
Return JSON only:
{"transcript":{"summary":str,"cite":[ids]},"x":{"summary":str,"cite":[ids]},"youtube":{"summary":str,"cite":[ids]},
 "news":{"summary":str,"cite":[ids]},"synthesis":str,"signal":"bullish|neutral|bearish"}`

async function askGrok(payload: unknown) {
  const key = process.env.XAI_API_KEY
  if (!key) throw new Error('XAI_API_KEY is not set in frontend/.env.local')
  const res = await fetch('https://api.x.ai/v1/chat/completions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${key}` },
    body: JSON.stringify({
      model: process.env.XAI_MODEL || 'grok-4.20-0309-non-reasoning',
      temperature: 0.2,
      response_format: { type: 'json_object' },
      messages: [
        { role: 'system', content: SYSTEM },
        { role: 'user', content: JSON.stringify(payload) },
      ],
    }),
  })
  if (!res.ok) throw new Error(`Grok ${res.status}: ${(await res.text()).slice(0, 300)}`)
  const data = await res.json()
  const raw = String(data.choices?.[0]?.message?.content ?? '{}').replace(/^```(?:json)?|```$/gm, '')
  return { out: JSON.parse(raw.slice(raw.indexOf('{'), raw.lastIndexOf('}') + 1)), model: data.model as string }
}

async function marketEvidence(question: string, ticker: string, date: string) {
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY
  if (!key) return null
  const db = createClient(process.env.NEXT_PUBLIC_SUPABASE_URL!, key, { auth: { autoRefreshToken: false, persistSession: false } })
  const words = keywords(question)
  const ev = (await db.from('x_earnings_events').select('id,call_time_utc').eq('ticker', ticker)).data?.find((e) =>
    String(e.call_time_utc).startsWith(date))

  // X: full-text match on the question, best engagement first; fall back to the most engaged posts
  let posts: Row[] = []
  if (ev) {
    const cols = 'id,author_id,text,url,time_window,minutes_from_call,like_count,repost_count,sentiment_label,sentiment_score'
    if (words.length) {
      posts = (await db.from('x_posts').select(cols).eq('event_id', ev.id)
        .textSearch('text', words.slice(0, 8).join(' or '), { type: 'websearch', config: 'english' })
        .order('like_count', { ascending: false }).limit(10)).data ?? []
    }
    if (posts.length < 3) {
      posts = posts.concat((await db.from('x_posts').select(cols).eq('event_id', ev.id)
        .order('like_count', { ascending: false }).limit(10 - posts.length)).data ?? [])
    }
  }
  const authorIds = [...new Set(posts.map((p) => p.author_id))]
  const authors = new Map(((authorIds.length ? (await db.from('x_authors').select('id,username,followers').in('id', authorIds)).data : []) ?? []).map((a) => [a.id, a]))

  const articles = ((await db.from('articles').select('id,phase,title,url,source,published_at,summary,sentiment,analyst_actions')
    .eq('ticker', ticker).eq('earnings_date', date)).data ?? [])
    .map((a) => ({ a, s: overlap(`${a.title} ${a.summary}`, words) + (a.analyst_actions?.length ? 0.5 : 0) }))
    .sort((x, y) => y.s - x.s).slice(0, 6).map((x) => x.a)

  const videos = (await db.from('youtube_videos').select('video_id,phase,title,url,channel_name,view_count')
    .eq('ticker', ticker).eq('earnings_date', date).order('view_count', { ascending: false }).limit(20)).data ?? []
  const vidInsights = new Map(((videos.length ? (await db.from('youtube_video_insights').select('video_id,summary,sentiment')
    .in('video_id', videos.map((v) => v.video_id))).data : []) ?? []).map((i) => [i.video_id, i]))
  const topVideos = videos.map((v) => ({ v, s: overlap(`${v.title} ${vidInsights.get(v.video_id)?.summary ?? ''}`, words) }))
    .sort((x, y) => y.s - x.s).slice(0, 4).map((x) => x.v)

  const snaps = (await db.from('pulse_snapshots').select('phase,pulse_score,brief,gap').eq('event_key', `${ticker}-${date}`)).data ?? []
  return { posts, authors, articles, videos: topVideos, vidInsights, snaps }
}

export async function POST(request: NextRequest) {
  const body = await request.json()
  const ticker = String(body.ticker || 'AAPL').toUpperCase()
  const date = String(body.date || '2025-01-30')
  const question = String(body.message || '')

  // 1 + 2 in parallel: the team's RAG chatbot (unchanged) and the Grok bots' market data
  const [ragRes, market] = await Promise.all([
    fetch(`${API_BASE_URL}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: question, id: body.id ?? '1', video_url: body.video_url ?? null, history: body.history ?? [] }),
    }).then((r) => r.json()).catch(() => ({ response: '⚠️ Transcript chatbot (RAG API :8000) is unreachable.' })),
    marketEvidence(question, ticker, date).catch((e) => { console.warn('[pulse/chat] market data', e); return null }),
  ])

  // Number every item so Grok can cite by id
  const cites: Record<string, Citation> = {}
  const transcriptItems = ((ragRes.sources ?? []) as Row[]).slice(0, 6).map((s, i) => {
    const id = `t${i + 1}`
    cites[id] = { id, label: `Call transcript · chunk ${s.chunk}`, text: String(s.text).slice(0, 280) }
    return { id, text: String(s.text).slice(0, 700) }
  })
  const xItems = (market?.posts ?? []).map((p, i) => {
    const id = `x${i + 1}`
    const a: Row = market!.authors.get(p.author_id) ?? {}
    const m = Number(p.minutes_from_call)
    cites[id] = { id, label: `@${a.username ?? 'unknown'} · ${compact(Number(a.followers) || 0)} followers`, text: String(p.text).slice(0, 240),
      url: p.url, phase: phaseOf(p.time_window), minutes: Number.isFinite(m) ? Math.round(m) : null, sentiment: p.sentiment_label }
    return { id, phase: phaseOf(p.time_window), minutes_from_call: Math.round(m), user: a.username, followers: a.followers,
      likes: p.like_count, sentiment: p.sentiment_label, text: String(p.text).slice(0, 300) }
  })
  const newsItems = (market?.articles ?? []).map((a, i) => {
    const id = `n${i + 1}`
    const d = a.published_at ? new Date(a.published_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : ''
    cites[id] = { id, label: [a.source, d].filter(Boolean).join(' · '), text: a.title, url: a.url, phase: phaseOf(a.phase), sentiment: a.sentiment }
    return { id, phase: phaseOf(a.phase), source: a.source, title: a.title, summary: String(a.summary ?? '').slice(0, 400),
      sentiment: a.sentiment, analyst_actions: a.analyst_actions }
  })
  const ytItems = (market?.videos ?? []).map((v, i) => {
    const id = `y${i + 1}`
    const ins = market!.vidInsights.get(v.video_id)
    cites[id] = { id, label: `${v.channel_name ?? 'YouTube'} · ${compact(Number(v.view_count) || 0)} views`, text: v.title,
      url: v.url || `https://www.youtube.com/watch?v=${v.video_id}`, phase: phaseOf(v.phase), sentiment: ins?.sentiment,
      thumbnail: `https://i.ytimg.com/vi/${v.video_id}/mqdefault.jpg` }
    return { id, phase: phaseOf(v.phase), channel: v.channel_name, title: v.title, views: v.view_count, summary: String(ins?.summary ?? '').slice(0, 400) }
  })
  const pulse = Object.fromEntries((market?.snaps ?? []).map((s) => [s.phase, s.pulse_score]))
  const gap = market?.snaps.find((s) => s.phase === 'post')?.gap

  const base = { suggestions: ragRes.suggestions ?? [], sources: ragRes.sources ?? [] }
  try {
    const { out, model } = await askGrok({
      question,
      company: ticker,
      call_date: date,
      transcript_assistant_draft: ragRes.response,
      transcript: transcriptItems,
      x: xItems,
      youtube: ytItems,
      news: newsItems,
      pulse_score_by_phase: pulse,
      reported_vs_consensus: gap ? { verdict: gap.verdict, metrics: gap.metrics } : null,
    })
    const meta: Record<SourceKey, { title: string; n: number }> = {
      transcript: { title: 'Earnings call transcript', n: transcriptItems.length },
      x: { title: 'X / Twitter', n: xItems.length },
      youtube: { title: 'YouTube', n: ytItems.length },
      news: { title: 'News & analysts', n: newsItems.length },
    }
    const sections: GrokSection[] = (Object.keys(meta) as SourceKey[]).map((k) => ({
      source: k,
      title: meta[k].title,
      summary: String(out[k]?.summary ?? ''),
      citations: ((out[k]?.cite ?? []) as string[]).map((id) => cites[id]).filter((c): c is Citation => !!c && c.id.startsWith(k[0])),
      empty: meta[k].n === 0,
    }))
    const grok: GrokPayload = { sections, synthesis: String(out.synthesis ?? ''), signal: out.signal, pulse, model }
    return NextResponse.json({ ...base, response: grok.synthesis || ragRes.response, provider: 'grok+rag', grok })
  } catch (e) {
    console.warn('[pulse/chat] Grok failed, returning classic RAG answer', e)
    return NextResponse.json({ ...base, response: ragRes.response, provider: ragRes.provider ?? 'rag' })
  }
}
