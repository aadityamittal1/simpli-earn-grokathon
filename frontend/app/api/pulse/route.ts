// Grok Earnings Pulse data for one event: everything the Grok bots collected (X, articles,
// YouTube, fundamentals) plus pulse_snapshots computed by grok_pulse/pulse.py.
// Server-only: the bot tables have RLS with no policies, so this reads with the service-role key
// (same pattern as app/api/auth/delete-account/route.ts).
import { createClient } from '@supabase/supabase-js'
import { NextResponse, type NextRequest } from 'next/server'

export const dynamic = 'force-dynamic'

const phaseOf = (v: unknown) => {
  const s = String(v ?? '').toLowerCase()
  if (s.startsWith('pre') || s.startsWith('before')) return 'pre'
  if (s.startsWith('during') || s.startsWith('live')) return 'during'
  if (s.startsWith('post') || s.startsWith('after')) return 'post'
  return null
}

export async function GET(request: NextRequest) {
  const ticker = (request.nextUrl.searchParams.get('ticker') || 'AAPL').toUpperCase()
  const date = request.nextUrl.searchParams.get('date') || '2025-01-30'
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY
  if (!key) {
    return NextResponse.json({ error: 'SUPABASE_SERVICE_ROLE_KEY is not set in frontend/.env.local' }, { status: 500 })
  }
  const db = createClient(process.env.NEXT_PUBLIC_SUPABASE_URL!, key, {
    auth: { autoRefreshToken: false, persistSession: false },
  })
  const safe = async <T,>(p: PromiseLike<{ data: T[] | null; error: unknown }>): Promise<T[]> => {
    const { data, error } = await p
    if (error) console.warn('[pulse]', error)
    return data ?? []
  }

  type Row = Record<string, unknown>
  const xEvents = (await safe<Row>(db.from('x_earnings_events').select('*').eq('ticker', ticker)))
    .filter((e) => String(e.call_time_utc ?? e.report_time_utc ?? '').startsWith(date))
  const xEvent = xEvents[0] ?? null
  const eventId = xEvent ? String(xEvent.id) : '__none__'

  const [snapshots, buckets, topPosts, windows, articles, videos, insights, fundamentals] = await Promise.all([
    safe<Row>(db.from('pulse_snapshots').select('*').eq('event_key', `${ticker}-${date}`)),
    safe<Row>(db.from('x_posts_per_5min').select('*').eq('event_id', eventId).order('bucket_minutes_from_call').limit(1000)),
    safe<Row>(db.from('x_top_posts_by_window').select('*').eq('event_id', eventId).order('rank').limit(100)),
    safe<Row>(db.from('x_window_summary').select('*').eq('event_id', eventId)),
    safe<Row>(db.from('articles').select('id,phase,title,url,source,author,published_at,summary,key_points,sentiment,sentiment_score,analyst_actions,topics').eq('ticker', ticker).eq('earnings_date', date).order('published_at')),
    safe<Row>(db.from('youtube_videos').select('video_id,phase,hours_from_call,url,title,channel_name,view_count,like_count,published_at').eq('ticker', ticker).eq('earnings_date', date).order('published_at')),
    safe<Row>(db.from('youtube_video_insights').select('video_id,phase,summary,sentiment,sentiment_score,key_points,price_targets,red_flags')),
    safe<Row>(db.from('earnings_events').select('*').eq('ticker', ticker).eq('earnings_date', date)),
  ])

  // Grok-scored sentiment for the top posts (the view predates scoring, so join it in)
  const ids = topPosts.map((p) => String(p.id))
  const scored = ids.length
    ? await safe<Row>(db.from('x_posts').select('id,sentiment_score,sentiment_label').in('id', ids))
    : []
  const scoreById = new Map(scored.map((s) => [String(s.id), s]))
  const insightByVideo = new Map(insights.map((i) => [String(i.video_id), i]))

  return NextResponse.json({
    ticker,
    date,
    event: xEvent,
    fundamentals: fundamentals[0] ?? null,
    snapshots: Object.fromEntries(snapshots.map((s) => [s.phase, s])),
    x: {
      windows,
      buckets: buckets.map((b) => ({ ...b, phase: phaseOf(b.time_window) })),
      topPosts: topPosts.map((p) => ({ ...p, phase: phaseOf(p.time_window), ...(scoreById.get(String(p.id)) ?? {}) })),
    },
    articles: articles.map((a) => ({ ...a, phase: phaseOf(a.phase) })),
    videos: videos.map((v) => ({ ...v, phase: phaseOf(v.phase), insight: insightByVideo.get(String(v.video_id)) ?? null })),
  })
}
