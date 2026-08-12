import { useEffect, useMemo, useRef, useState } from 'react'
import { MultiPaneChart, DRAWING_TOOLS, createDrawingOptions } from './MultiPaneChart.jsx'

async function fetchJson(url) {
  const res = await fetch(url, { cache: 'no-store' })
  if (res.status === 404) return null
  if (!res.ok) throw new Error(`${url} -> HTTP ${res.status}`)
  return res.json()
}

// lightweight-charts' time scale wants a UNIX timestamp (seconds) or a plain
// "yyyy-mm-dd" business-day string — it rejects the full ISO-8601 with a
// time component that the backend returns (e.g. "2026-07-16T14:45:00Z").
function toChartTime(isoString) {
  return Math.floor(new Date(isoString).getTime() / 1000)
}

function withChartTime(rows) {
  return rows?.map((row) => ({ ...row, time: toChartTime(row.time) })) ?? null
}

export const DEFAULT_STRATEGY_ID = 'default' // matches backend's DEFAULT_STRATEGY_ID (#179)

// Standard EMA over candle closes. Returns points only from the `period`-th
// candle onward (no seeded/partial values before enough closes exist).
function computeEma(ohlcv, period) {
  if (!ohlcv?.length || ohlcv.length < period) return []
  const k = 2 / (period + 1)
  const out = []
  let ema = ohlcv.slice(0, period).reduce((sum, c) => sum + c.close, 0) / period
  out.push({ time: ohlcv[period - 1].time, value: ema })
  for (let i = period; i < ohlcv.length; i++) {
    ema = ohlcv[i].close * k + ema * (1 - k)
    out.push({ time: ohlcv[i].time, value: ema })
  }
  return out
}

// Timeframe length in seconds, for judging whether the last candle is stale
// relative to how often this timeframe is supposed to produce a new one.
const TIMEFRAME_SECONDS = {
  '1m': 60, '5m': 300, '15m': 900, '1h': 3600, '4h': 14400, '1d': 86400,
}

function formatAge(seconds) {
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}min`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`
  return `${Math.floor(seconds / 86400)}d`
}

// Point-sample data_kinds (open_interest/taker_volume/long_short_ratio) share
// the same independent 1d/5m/1h track model (#164-166) — none of them match
// OHLC's 15m/1h/4h choices, so all three auto-pick the closer-density track
// from the OHLC timeframe the same way OI already does (#170: fixed the
// axis-scrambling bug this avoids — see MultiPaneChart's fitContent comment).
// funding has no track at all (fixed "1h" bucket, #163) so it's absent here.
const POINT_SAMPLE_TIMEFRAME_BY_OHLCV_TIMEFRAME = {
  '1m': '5m', '5m': '5m', '15m': '5m',
  '1h': '1d', '4h': '1d', '1d': '1d',
}

// EMA overlays drawn in pane 0 alongside the candlesticks — toggled via the
// same checkbox pattern as PANEL_DEFS, default on (unlike PANEL_DEFS panels).
const EMA_DEFS = [
  { key: 'ema100', period: 100, label: 'EMA 100', color: '#f0c14b' },
  { key: 'ema200', period: 200, label: 'EMA 200', color: '#5aa9e6' },
]

// #246: BB/EMA(21/50/200)/EMA-projekcja/VWAP overlaye z backendu #242
// (/api/bollinger, /api/ema, /api/ema_projection, /api/vwap) — pane 0, same
// `priceLines` mechanism MultiPaneChart already uses for EMA_DEFS/OKX
// position lines above (no new rendering infra needed). All OFF by default
// (task #246 AC), unlike EMA_DEFS which defaults on.
//
// `_EMA_PROJECTION_SOURCES` mirrors backend/main.py's own map — the
// projection endpoint only accepts target_timeframe 5m/15m, so its checkbox
// is disabled (not just empty) outside those two.
const EMA_PROJECTION_SOURCES = { '5m': ['15m', '1h'], '15m': ['1h', '4h'] }

const OVERLAY_EMA_PERIODS = [21, 50, 200]
const OVERLAY_EMA_COLORS = { 21: '#5ee6c4', 50: '#e0a262', 200: '#c896e0' }

// Strips warm-up/no-data points (`value`/`middle` null) before handing a
// series to lightweight-charts — LineSeries.setData() throws on a point
// whose value field is null (only `undefined`-shaped whitespace points are
// accepted), and every one of these endpoints returns explicit nulls during
// their warm-up window (BB period=20, EMA period, projection before the
// first HTF candle closes, VWAP before an anchor).
function dropNullValues(rows, field = 'value') {
  return rows?.filter((r) => r[field] != null) ?? []
}

// One entry per optional panel: `dataKind` matches /api/available's keys and
// the backfill data_kind; `endpoint` is the dashboard API path; `needsTimeframe`
// controls whether the auto-picked point-sample timeframe is sent as a query
// param (funding has none). `toPanelData` adapts each endpoint's row shape to
// what MultiPaneChart's LineSeries panels expect ({time, value}); taker_volume
// returns two panels (sell/buy) from one fetch since it's two series.
// Toolbar buttons — key must match DRAWING_TOOLS in MultiPaneChart.jsx and
// the SerializedDrawing.type the library itself uses ("disjoint-channel" is
// the library's two-independent-lines channel, used here as "megafon"/
// broadening formation since the library has no dedicated type for it).
const DRAWING_TOOL_DEFS = [
  { type: 'trend-line', label: 'Linia trendu' },
  { type: 'parallel-channel', label: 'Kanał równoległy' },
  { type: 'fib-channel', label: 'Kanał Fibonacciego' },
  { type: 'fib-retracement', label: 'Zniesienie Fibo' },
  { type: 'disjoint-channel', label: 'Megafon' },
  { type: 'brush', label: '✏️ Pisak (swobodne rysowanie)' }, // freehand — user request 2026-08-07, own drag gesture in MultiPaneChart.jsx (#193)
  { type: 'horizontal-line', label: 'Linia pozioma + opis' }, // user request 2026-08-08 — single click, prompts for label text in MultiPaneChart.jsx (#204)
]

const PANEL_DEFS = [
  {
    dataKind: 'open_interest', endpoint: 'open_interest', needsTimeframe: true,
    toPanelData: (rows, tf) => [{ key: 'oi', label: `Open Interest (${tf})`, data: rows, color: '#7c88f5' }],
  },
  {
    dataKind: 'funding', endpoint: 'funding', needsTimeframe: false,
    toPanelData: (rows) => [{ key: 'funding', label: 'Funding Rate', data: rows, color: '#e0a262' }],
  },
  {
    dataKind: 'taker_volume', endpoint: 'taker_volume', needsTimeframe: true,
    toPanelData: (rows, tf) => [
      { key: 'taker_sell', label: `Taker Sell Volume (${tf})`, data: rows?.map((r) => ({ time: r.time, value: r.sell })), color: '#f0716a' },
      { key: 'taker_buy', label: `Taker Buy Volume (${tf})`, data: rows?.map((r) => ({ time: r.time, value: r.buy })), color: '#4ecf8e' },
    ],
  },
  {
    dataKind: 'long_short_ratio', endpoint: 'long_short_ratio', needsTimeframe: true,
    toPanelData: (rows, tf) => [{ key: 'lsr', label: `Long/Short Ratio (${tf})`, data: rows, color: '#c896e0' }],
  },
  {
    // #183: pct_change(symbol) - pct_change(BTC), computed server-side (own
    // endpoint, not toPanelData, since it needs a second series — BTC's own
    // OHLCV — that no other panel fetches). Independent-agent review
    // rejected a candlestick BTC overlay on a shared axis as visually
    // confusing; this single line showing the spread was the recommended
    // alternative. Uses the OHLCV timeframe directly (unlike OI/taker/LSR's
    // point-sample track), since it's built from the same ohlcv the main
    // candlesticks already use.
    dataKind: 'relative_strength', endpoint: 'relative_strength', needsTimeframe: true, usesOhlcvTimeframe: true,
    toPanelData: (rows) => [{ key: 'rel_strength', label: 'Siła względem BTC (%)', data: rows, color: '#5ee6c4' }],
  },
  {
    // #181: simplified pv.pine "Risk Indicator" port — 0-100 weighted mean
    // of 6 oscillators (see backend/risk_indicator.py) plus bull/bear
    // divergence markers against price. Response shape differs from every
    // other panel ({series, divergences} instead of a flat rows array), so
    // this is the one entry needing a custom `parseResponse`. Built from the
    // main OHLCV, same as relative_strength above.
    dataKind: 'risk_indicator', endpoint: 'risk_indicator', needsTimeframe: true, usesOhlcvTimeframe: true,
    parseResponse: (raw) => ({ series: withChartTime(raw?.series), divergences: withChartTime(raw?.divergences) }),
    toPanelData: (parsed) => [{
      key: 'risk_indicator',
      label: 'Risk Indicator',
      data: parsed?.series,
      color: '#e0a262',
      markers: parsed?.divergences?.map((d) => ({
        time: d.time,
        position: d.type === 'bull' ? 'belowBar' : 'aboveBar',
        shape: d.type === 'bull' ? 'arrowUp' : 'arrowDown',
        color: d.type === 'bull' ? '#4ecf8e' : '#f0716a',
        // No `text` label (#187 fix): risk_indicator's pane is only ~44px
        // tall — a text label's fixed-px font height (from the chart's
        // global layout.fontSize, not scaled per-pane) routinely exceeds
        // that, visually bleeding into the panes above/below it. The arrow
        // shape + color already encode direction without it.
      })) ?? [],
    }],
  },
  {
    // Standalone RSI(14) — user request 2026-08-07, distinct from
    // risk_indicator's RSI (one of 6 inputs into that panel's weighted
    // mean, clamped to a neutral 50 during warmup rather than empty here).
    dataKind: 'rsi', endpoint: 'rsi', needsTimeframe: true, usesOhlcvTimeframe: true,
    toPanelData: (rows) => [{ key: 'rsi', label: 'RSI (14)', data: rows, color: '#c896e0' }],
  },
  {
    // MACD (12/26/9) — three lines share one pane (macd/signal/histogram);
    // histogram rendered as a LineSeries (panels only support LineSeries,
    // not true histogram bars) rather than a fourth API call/panel. `pane`
    // (user report 2026-08-08: was rendering as 3 separate panes despite
    // this comment — MultiPaneChart previously gave every panels[] entry
    // its own pane unconditionally) groups these three onto one shared pane.
    dataKind: 'macd', endpoint: 'macd', needsTimeframe: true, usesOhlcvTimeframe: true,
    parseResponse: (raw) => ({ macd: withChartTime(raw?.macd), signal: withChartTime(raw?.signal), histogram: withChartTime(raw?.histogram) }),
    toPanelData: (parsed) => [
      { key: 'macd_line', pane: 'macd', label: 'MACD', data: parsed?.macd, color: '#5ee6c4' },
      { key: 'macd_signal', pane: 'macd', label: 'Signal', data: parsed?.signal, color: '#e0a262' },
      { key: 'macd_hist', pane: 'macd', label: 'Histogram', data: parsed?.histogram, color: '#7c88f5' },
    ],
  },
  {
    // Classic Stochastic Oscillator (%K/%D from price highs/lows) — NOT
    // Stochastic RSI (that one is inside risk_indicator's weighted mean).
    // Same shared-pane fix as MACD above — %K/%D belong on one pane.
    dataKind: 'stochastic', endpoint: 'stochastic', needsTimeframe: true, usesOhlcvTimeframe: true,
    parseResponse: (raw) => ({ k: withChartTime(raw?.k), d: withChartTime(raw?.d) }),
    toPanelData: (parsed) => [
      { key: 'stoch_k', pane: 'stochastic', label: '%K', data: parsed?.k, color: '#5ee6c4' },
      { key: 'stoch_d', pane: 'stochastic', label: '%D', data: parsed?.d, color: '#e0a262' },
    ],
  },
  {
    // #242: Cumulative Volume Delta (session-reset, UTC calendar-day
    // boundary) from OKX taker_volume only — NOT whole-market delta, see
    // backend/technical_overlays.py's cvd_session docstring. Own pane
    // (single line: the cumulative `value`, not the per-bar `delta`) — the
    // per-bar delta/taker_buy/taker_sell fields the API also returns are not
    // plotted here, this MVP panel only shows the running cumulative line
    // (label makes that explicit so it isn't mistaken for a raw taker_volume
    // panel). Anchored-mode CVD (task #242 point 3) is API-ready
    // (/api/cvd?mode=anchored&anchor_time=...) but has no UI control yet —
    // this checkbox always requests session mode; anchoring is a follow-up
    // if the PM wants it exposed in this window's UI.
    dataKind: 'cvd', endpoint: 'cvd', needsTimeframe: true, usesOhlcvTimeframe: true,
    parseResponse: (raw) => withChartTime(raw?.series),
    toPanelData: (rows) => [{ key: 'cvd', label: 'CVD (OKX, session UTC)', data: rows, color: '#5ee6c4' }],
  },
]

/** One chart + its own symbol/timeframe/strategy/panel selection — the unit
 * that #184's window manager (App.jsx) mounts multiple independent copies
 * of. `prefs`/`onPrefsChange` carry this window's own persisted
 * symbol/timeframe/enabled-panels (App.jsx owns the localStorage write, one
 * entry per window, so each window remembers its own selection across
 * reloads independently of the others). */
export function ChartWindow({ windowId, prefs, onPrefsChange, onOpenPopup }) {
  const [available, setAvailable] = useState(null)
  const [symbol, setSymbol] = useState('')
  const [timeframe, setTimeframe] = useState('')
  const [strategies, setStrategies] = useState([]) // [{id, name}] for the current symbol (#179)
  const [strategyId, setStrategyId] = useState(DEFAULT_STRATEGY_ID)
  const [ohlcv, setOhlcv] = useState(null)
  const [enabled, setEnabled] = useState(() => prefs.enabled ?? {}) // dataKind -> bool
  const [emaEnabled, setEmaEnabled] = useState(() => prefs.emaEnabled ?? { ema100: true, ema200: true }) // key -> bool
  // #246: BB/EMA21/50/200/EMA-projekcja/VWAP — key -> bool, all off by
  // default (task AC), separate from EMA_DEFS' emaEnabled above since these
  // hit different endpoints and have their own metadata (bandwidth_percentile,
  // source_candle_close_time, session_timezone) shown near the checkboxes.
  const [overlayEnabled, setOverlayEnabled] = useState(() => prefs.overlayEnabled ?? {})
  const [overlayData, setOverlayData] = useState({}) // key -> raw endpoint response (not persisted — refetched on mount like panelData)
  const [emaProjectionSource, setEmaProjectionSource] = useState('') // chosen source_timeframe for the projection checkbox — reset whenever `timeframe` changes to stay one of EMA_PROJECTION_SOURCES[timeframe]
  const [okxPositionEnabled, setOkxPositionEnabled] = useState(() => prefs.okxPositionEnabled ?? false) // #190: opt-in — a REAL-account fetch, off by default unlike EMA
  const [okxPosition, setOkxPosition] = useState(null) // #190: {side, entry, stop_loss, take_profit} | null — last /api/okx_position response for `symbol`
  const [panelData, setPanelData] = useState({}) // dataKind -> rows
  const [error, setError] = useState(null)
  const [now, setNow] = useState(() => Date.now())
  const [panelsMenuOpen, setPanelsMenuOpen] = useState(false) // #178: always starts collapsed, not persisted
  const [drawToolsMenuOpen, setDrawToolsMenuOpen] = useState(false)
  const [activeTool, setActiveTool] = useState(null)
  const [selectedDrawing, setSelectedDrawing] = useState(null)
  const [drawingEditPopup, setDrawingEditPopup] = useState(null) // {x, y} in chart-container pixels, or null — opened by MultiPaneChart's onDrawingDblClick, closed on outside click/Escape/deletion (user request: color+extend controls moved here from the always-visible toolbar)
  const [analyzing, setAnalyzing] = useState(false) // #189: only gates the buttons now — the reply itself goes to a popup (onOpenPopup), owned by the window manager, not this component
  const [chatMessage, setChatMessage] = useState('') // #200: single free-text chat input, replaces the three separate prompt()-based buttons
  const drawingManagerRef = useRef(null)
  const panelsMenuRef = useRef(null)
  const drawToolsMenuRef = useRef(null)
  const drawingEditPopupRef = useRef(null)
  const chartContainerRef = useRef(null) // wraps MultiPaneChart — the drawingEditPopup's x/y (chart-container pixels, from onDrawingDblClick) are positioned absolutely against this
  const savedDrawingsRef = useRef(null) // last /api/drawings response for the current symbol+strategy (#182: re-imported, snapped to the new bar grid, on every timeframe switch — without re-fetching)
  const isImportingRef = useRef(false) // true while importSavedDrawings() is running — suppresses the PUTs importDrawings()'s per-drawing "added" events would otherwise trigger (bug found 2026-08-07, #186 testing)
  const pointSampleTimeframe = POINT_SAMPLE_TIMEFRAME_BY_OHLCV_TIMEFRAME[timeframe] || '1d'

  // Close the panels dropdown on outside click (#178) — standard dropdown UX.
  useEffect(() => {
    if (!panelsMenuOpen) return
    const handleClickOutside = (e) => {
      if (panelsMenuRef.current && !panelsMenuRef.current.contains(e.target)) setPanelsMenuOpen(false)
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [panelsMenuOpen])

  // Same pattern for the drawing-tools popup.
  useEffect(() => {
    if (!drawToolsMenuOpen) return
    const handleClickOutside = (e) => {
      if (drawToolsMenuRef.current && !drawToolsMenuRef.current.contains(e.target)) setDrawToolsMenuOpen(false)
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [drawToolsMenuOpen])

  // Same pattern for the drawing-edit popup (opened by double-clicking a
  // drawing, see onDrawingDblClick below) — click anywhere outside it closes
  // it, including a click back on the chart itself (chartContainerRef wraps
  // the whole MultiPaneChart, so a click that starts a NEW selection/dblclick
  // there also closes this stale popup, not just clicks fully off-chart).
  useEffect(() => {
    if (!drawingEditPopup) return
    const handleClickOutside = (e) => {
      if (drawingEditPopupRef.current && !drawingEditPopupRef.current.contains(e.target)) setDrawingEditPopup(null)
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [drawingEditPopup])

  // Drives the "last candle age" freshness indicator below the chart —
  // ticks on its own so the label keeps counting up even without new data.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 10_000)
    return () => clearInterval(id)
  }, [])

  // Persist this window's last-viewed symbol/timeframe/enabled-panels (user
  // request, extended by #184 to be per-window). Skipped while symbol/
  // timeframe are still empty (initial mount, before /api/available
  // resolves) so a fresh window doesn't overwrite `prefs` with blanks before
  // the initial-selection effect below runs.
  useEffect(() => {
    if (!symbol || !timeframe) return
    onPrefsChange({ symbol, timeframe, enabled, emaEnabled, okxPositionEnabled, overlayEnabled })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, timeframe, enabled, emaEnabled, okxPositionEnabled, overlayEnabled])

  // #246: EMA projection's source_timeframe must always be one of
  // EMA_PROJECTION_SOURCES[timeframe] — reset to the first allowed option
  // whenever `timeframe` changes (covers both a fresh mount and switching
  // e.g. 5m -> 1h, where the old source_timeframe becomes invalid/undefined).
  useEffect(() => {
    const allowed = EMA_PROJECTION_SOURCES[timeframe] ?? []
    if (!allowed.includes(emaProjectionSource)) setEmaProjectionSource(allowed[0] ?? '')
  }, [timeframe]) // eslint-disable-line react-hooks/exhaustive-deps

  // #190: real-account open position for the current symbol — fetched only
  // while the checkbox is on (opt-in, unlike EMA which defaults on: this
  // hits the REAL account, not demo/lake data). Polled every 15s like the
  // other panels while enabled; cleared immediately when disabled or the
  // symbol changes, so a stale position from the PREVIOUS symbol never
  // lingers on screen.
  useEffect(() => {
    if (!symbol || !okxPositionEnabled) {
      setOkxPosition(null)
      return
    }
    let cancelled = false
    const load = () => {
      fetchJson(`/api/okx_position?symbol=${symbol}`)
        .then((row) => { if (!cancelled) setOkxPosition(row) })
        .catch((err) => setError(String(err)))
    }
    load()
    const id = setInterval(load, 15_000)
    return () => { cancelled = true; clearInterval(id) }
  }, [symbol, okxPositionEnabled])

  // Available symbols/timeframes come from what's actually backfilled
  // (#170 AC) — never a hardcoded list. Prefers this window's last-viewed
  // symbol/timeframe (user request) when it's still in the backfilled list;
  // falls back to the first symbol + 5m the same way as before otherwise
  // (e.g. first-ever visit, or the remembered symbol got dropped from the
  // lake).
  useEffect(() => {
    fetchJson('/api/available')
      .then((data) => {
        setAvailable(data)
        const symbols = Object.keys(data?.ohlcv || {})
        if (!symbols.length) return
        const chosenSymbol = symbols.includes(prefs.symbol) ? prefs.symbol : symbols[0]
        setSymbol(chosenSymbol)
        const tfs = data.ohlcv[chosenSymbol]
        const chosenTimeframe = tfs.includes(prefs.timeframe) ? prefs.timeframe : (tfs.includes('5m') ? '5m' : tfs[0])
        setTimeframe(chosenTimeframe)
      })
      .catch((err) => setError(String(err)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Full closed-candle history — fetched once per symbol/timeframe change,
  // NOT on the 15s tick (that would re-run the backend's incremental-backfill
  // subprocess call on every closed-candle series for no benefit, since
  // closed history doesn't change more than once per candle close anyway).
  //
  // Bug found 2026-08-08 (user report: fib-retracement drawn on 5m invisible
  // after switching to 15m): the re-snap-on-timeframe-switch effect below
  // USED to be its own `useEffect(..., [timeframe])`, firing the instant
  // `timeframe` changes — but at that exact moment `ohlcv` in its closure is
  // still the OLD timeframe's candles (this fetch is async, hasn't resolved
  // yet), so importSavedDrawings() snapped every anchor onto the WRONG bar
  // grid. Moved inline here, right after the NEW timeframe's `rows` actually
  // arrive, so snapping always targets the candles it's about to be
  // rendered against.
  useEffect(() => {
    if (!symbol || !timeframe) return
    let cancelled = false
    fetchJson(`/api/ohlcv?symbol=${symbol}&timeframe=${timeframe}`)
      .then((rows) => {
        if (cancelled) return
        const withTime = withChartTime(rows)
        setOhlcv(withTime)
        // #182's re-snap, see comment above — only meaningful on a
        // TIMEFRAME switch (symbol switches already get a fresh import from
        // the symbol/strategy effect below); harmless no-op otherwise since
        // importSavedDrawings() itself no-ops on an empty/absent cache.
        if (withTime?.length && savedDrawingsRef.current) importSavedDrawings(savedDrawingsRef.current, withTime)
      })
      .catch((err) => setError(String(err)))
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, timeframe])

  // The current, still-forming candle — polled every 15s and stitched onto
  // the tail of `ohlcv` client-side (see toChartTime/live_candle backend
  // docstring: it's never written to the lake, so it has to be merged here
  // instead of coming back from /api/ohlcv itself).
  useEffect(() => {
    if (!symbol || !timeframe) return
    let cancelled = false
    const load = () => {
      fetchJson(`/api/live_candle?symbol=${symbol}&timeframe=${timeframe}`)
        .then((row) => {
          // A stale response from the previous symbol/timeframe (in flight
          // when the user switched) must not be stitched onto the new
          // ohlcv — it would append an out-of-order/wrong-timeframe candle
          // and crash lightweight-charts' setData() ("data must be asc
          // ordered by time"), since the effect below already replaced
          // `ohlcv` with the new timeframe's history by the time this
          // resolves.
          if (cancelled || !row) return
          const point = { ...row, time: toChartTime(row.time) }
          setOhlcv((prev) => {
            if (!prev?.length) return prev
            const last = prev[prev.length - 1]
            if (point.time < last.time) return prev // defensive: never append out of order
            const withoutLive = last.time === point.time ? prev.slice(0, -1) : prev
            return [...withoutLive, point]
          })
        })
        .catch((err) => setError(String(err)))
    }
    load()
    const id = setInterval(load, 15_000)
    return () => { cancelled = true; clearInterval(id) }
  }, [symbol, timeframe])

  // Fetch each *enabled* panel's data kind when the symbol or its relevant
  // timeframe changes — disabled panels don't fetch (#172 AC: toggling a
  // checkbox off stops its requests, not just hides the pane). Also
  // auto-refreshes every 15s while enabled, same as OHLCV above.
  useEffect(() => {
    if (!symbol) return
    const load = () => {
      for (const def of PANEL_DEFS) {
        if (!enabled[def.dataKind]) continue
        let url = `/api/${def.endpoint}?symbol=${symbol}`
        // relative_strength (#183) is built from the same OHLCV the main
        // candlesticks use, unlike OI/taker/LSR's independent point-sample
        // track (#164-166) — needs the OHLCV timeframe itself, not
        // pointSampleTimeframe.
        if (def.usesOhlcvTimeframe) url += `&timeframe=${timeframe}`
        else if (def.needsTimeframe) url += `&timeframe=${pointSampleTimeframe}`
        fetchJson(url)
          .then((raw) => setPanelData((prev) => ({ ...prev, [def.dataKind]: (def.parseResponse ?? withChartTime)(raw) })))
          .catch((err) => setError(String(err)))
      }
    }
    load()
    const id = setInterval(load, 15_000)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, timeframe, pointSampleTimeframe, enabled])

  // #246: BB/EMA21/50/200/EMA-projekcja/VWAP fetches — same enabled-only +
  // 15s poll pattern as the panel-data effect above. All keyed off the main
  // OHLCV timeframe (not pointSampleTimeframe — these overlays sit in pane 0
  // next to the candlesticks, so they must share the candlesticks' own bar
  // grid). Cleared to {} on symbol/timeframe change before the new fetch
  // resolves so a stale PREVIOUS symbol's line never lingers mid-transition
  // (mirrors the live_candle effect's same concern above).
  useEffect(() => {
    setOverlayData({})
    if (!symbol || !timeframe) return
    let cancelled = false
    const load = () => {
      if (overlayEnabled.bb) {
        fetchJson(`/api/bollinger?symbol=${symbol}&timeframe=${timeframe}`)
          .then((raw) => { if (!cancelled) setOverlayData((prev) => ({ ...prev, bb: withChartTime(raw?.series) })) })
          .catch((err) => setError(String(err)))
      }
      for (const period of OVERLAY_EMA_PERIODS) {
        const key = `ema${period}`
        if (!overlayEnabled[key]) continue
        fetchJson(`/api/ema?symbol=${symbol}&timeframe=${timeframe}&period=${period}`)
          .then((raw) => { if (!cancelled) setOverlayData((prev) => ({ ...prev, [key]: withChartTime(raw?.series) })) })
          .catch((err) => setError(String(err)))
      }
      // Guards against `emaProjectionSource` momentarily lagging a `timeframe`
      // change (its own reset effect runs in a separate effect, so on the
      // render right after switching timeframe this one can still see the
      // PREVIOUS timeframe's source_timeframe) — re-validated here against
      // EMA_PROJECTION_SOURCES directly rather than trusting the state value,
      // or a stale e.g. "15m" source would be sent alongside a new "1h"
      // target_timeframe and get rejected with HTTP 422.
      const allowedSources = EMA_PROJECTION_SOURCES[timeframe] ?? []
      if (overlayEnabled.emaProjection && allowedSources.includes(emaProjectionSource)) {
        fetchJson(`/api/ema_projection?symbol=${symbol}&target_timeframe=${timeframe}&source_timeframe=${emaProjectionSource}&period=50`)
          .then((raw) => {
            if (cancelled) return
            // target_time -> time so this matches every other series' shape
            // (withChartTime/dropNullValues both key off `time`).
            const series = raw?.series?.map((row) => ({ ...row, time: row.target_time }))
            setOverlayData((prev) => ({ ...prev, emaProjection: withChartTime(series) }))
          })
          .catch((err) => setError(String(err)))
      }
      if (overlayEnabled.vwap) {
        fetchJson(`/api/vwap?symbol=${symbol}&timeframe=${timeframe}&mode=session`)
          .then((raw) => { if (!cancelled) setOverlayData((prev) => ({ ...prev, vwap: withChartTime(raw?.series), vwapMeta: raw })) })
          .catch((err) => setError(String(err)))
      }
    }
    load()
    const id = setInterval(load, 15_000)
    return () => { cancelled = true; clearInterval(id) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, timeframe, overlayEnabled, emaProjectionSource])

  // Strategies list for the current symbol (#179) — reloaded on symbol
  // change, and resets the active strategy to the default so switching to a
  // symbol that never had a non-default strategy doesn't leave a stale
  // strategyId from the previous symbol selected.
  useEffect(() => {
    if (!symbol) return
    fetchJson(`/api/strategies?symbol=${symbol}`)
      .then((list) => {
        setStrategies(list ?? [])
        if (!(list ?? []).some((s) => s.id === strategyId)) setStrategyId(DEFAULT_STRATEGY_ID)
      })
      .catch((err) => setError(String(err)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol])

  // Snap each anchor's time to the nearest candle actually present on the
  // CURRENT timeframe (#182) — lightweight-charts' timeToCoordinate() returns
  // null, and a primitive draws nothing, for a time that doesn't land
  // exactly on this series' bar grid. A drawing's anchors are only
  // guaranteed to align with the timeframe it was DRAWN on (#175: they're
  // absolute time/price, meant to stay visible across every timeframe of the
  // symbol), so switching to a timeframe with a different bar spacing
  // routinely produces an anchor that falls between two bars of the new
  // grid. Binary search since `candles` is time-ascending.
  const snapToNearestCandle = (time, candles) => {
    let lo = 0
    let hi = candles.length - 1
    while (lo < hi) {
      const mid = (lo + hi) >> 1
      if (candles[mid].time < time) lo = mid + 1
      else hi = mid
    }
    if (lo > 0 && Math.abs(candles[lo - 1].time - time) <= Math.abs(candles[lo].time - time)) lo -= 1
    return candles[lo].time
  }

  const importSavedDrawings = (saved, candles) => {
    if (!saved?.length) return
    // Bug found 2026-08-07 (#186 testing): DrawingManager.importDrawings()
    // calls addDrawing() per drawing, and EACH ONE fires its own
    // "drawing:added" event — MultiPaneChart's onDrawingsChange forwards
    // every one of those straight into handleDrawingsChange below, so
    // importing N drawings fired N nearly-simultaneous PUTs of the SAME
    // strategy back at the backend (racing on the backend's own tmp-file
    // write, fixed separately there). None of that PUT traffic is
    // meaningful — importing already-saved drawings is not a user edit —
    // so it's suppressed here at the source rather than only patched on the
    // backend side.
    isImportingRef.current = true
    drawingManagerRef.current?.clearAll()
    drawingManagerRef.current?.importDrawings(saved, (type, data) => {
      const ToolClass = DRAWING_TOOLS[type]
      if (!ToolClass) return null // unknown/future tool type — skip rather than crash
      // #193: freehand (brush) is skipped from snapping — it can carry
      // dozens/hundreds of points, and snapping each one to the nearest
      // candle of a DIFFERENT timeframe's grid would collapse a smooth
      // stroke into a jagged staircase (few unique candle timestamps vs.
      // many original points). It's a loose annotation, not a price/time
      // level meant to line up with candles, so rendering it at its
      // original absolute coordinates (unsnapped) is the right call here —
      // unlike every other tool, where 2-3 anchors make the snap
      // imperceptible and #182's fix (rendering on a foreign timeframe at
      // all) still matters.
      const anchors = type === 'brush' ? data.anchors : data.anchors.map((a) => ({ ...a, time: snapToNearestCandle(a.time, candles) }))
      return new ToolClass(data.id, anchors, data.style, createDrawingOptions(type, data.options))
    })
    isImportingRef.current = false
    // Library bug workaround (see MultiPaneChart's requestRepaint comment):
    // imported drawings don't paint until something else repaints the chart.
    drawingManagerRef.current?.requestRepaint?.()
  }

  // Drawings persistence (user request, #175, extended by #179 with a
  // strategy_id dimension): on SYMBOL or STRATEGY change, fetch+cache this
  // symbol+strategy's saved set and import it. Also waits for `ohlcv` —
  // importDrawings() before the chart has a real price/time range can
  // misplace anchors on some tools.
  //
  // React.StrictMode (main.jsx) double-invokes effects in dev — this one's
  // async fetch can resolve after MultiPaneChart's own effect has torn down
  // the DrawingManager it started with and mounted a fresh one. The `cancelled`
  // flag plus re-reading drawingManagerRef.current *inside* the .then() (not
  // capturing `manager` from the outer scope) makes sure an import always
  // lands on whichever DrawingManager instance is actually live when the
  // fetch resolves, not a stale/detached one from the first of the two mounts.
  useEffect(() => {
    if (!symbol || !strategyId || !ohlcv?.length) return
    let cancelled = false
    savedDrawingsRef.current = null // stale symbol+strategy's cache must not be re-imported by the OHLCV effect's re-snap while this fetch is in flight
    drawingManagerRef.current?.clearAll()
    fetchJson(`/api/drawings?symbol=${symbol}&strategy_id=${strategyId}`)
      .then((saved) => {
        if (cancelled) return
        savedDrawingsRef.current = saved
        importSavedDrawings(saved, ohlcv)
      })
      .catch((err) => setError(String(err)))
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol, strategyId, !!ohlcv?.length])

  // Fires on every drawing add/update/remove (MultiPaneChart's DrawingManager
  // event subscriptions) — always the manager's full current export, so a
  // plain PUT (replace, not merge) on the backend is correct. Replace is
  // scoped to one symbol+strategy file (#179), so it never touches another
  // strategy's or another user's drawings.
  const handleDrawingsChange = (serialized) => {
    if (!symbol || isImportingRef.current) return
    fetch(`/api/drawings?symbol=${symbol}&strategy_id=${strategyId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(serialized),
    }).catch((err) => setError(String(err)))
  }

  const deleteSelectedDrawing = () => {
    if (!selectedDrawing) return
    drawingManagerRef.current?.removeDrawing(selectedDrawing.id)
  }

  const recolorSelectedDrawing = (color) => {
    if (!selectedDrawing) return
    // updateStyle() (unlike addDrawing/importDrawings) does call
    // requestUpdate() internally, so no repaint workaround needed here.
    selectedDrawing.updateStyle({ lineColor: color })
    setSelectedDrawing(selectedDrawing) // same object, new color — force this panel's swatch to re-render
    handleDrawingsChange(drawingManagerRef.current?.exportDrawings() ?? [])
  }

  // User request: no way to extend a trend line to the right (or left) edge
  // of the chart — the library supports it natively (base DrawingOptions'
  // extendLeft/extendRight, read directly by TrendLine.computeGeometry, same
  // mechanism as fib-retracement's extendLines) but nothing in the UI ever
  // set it. updateOptions() (unlike updateStyle, still) calls requestUpdate()
  // internally, so no repaint workaround needed here either.
  const toggleSelectedDrawingExtend = (side) => {
    if (!selectedDrawing) return
    const key = side === 'left' ? 'extendLeft' : 'extendRight'
    selectedDrawing.updateOptions({ [key]: !selectedDrawing.options?.[key] })
    setSelectedDrawing(selectedDrawing) // same object, new options — force this panel's buttons to re-render
    handleDrawingsChange(drawingManagerRef.current?.exportDrawings() ?? [])
  }

  // Strategy CRUD (#179) — id is server-generated, so create/rename never
  // touch the drawings filename; delete falls back to the default strategy
  // if the currently-active one was removed.
  const createStrategy = () => {
    const name = window.prompt('Nazwa nowej strategii:')
    if (!name) return
    fetch(`/api/strategies?symbol=${symbol}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    })
      .then((res) => res.json())
      .then((entry) => {
        setStrategies((prev) => [...prev, entry])
        setStrategyId(entry.id)
      })
      .catch((err) => setError(String(err)))
  }

  const renameActiveStrategy = () => {
    const current = strategies.find((s) => s.id === strategyId)
    const name = window.prompt('Nowa nazwa strategii:', current?.name ?? '')
    if (!name || name === current?.name) return
    fetch(`/api/strategies/${strategyId}?symbol=${symbol}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    })
      .then(() => setStrategies((prev) => prev.map((s) => (s.id === strategyId ? { ...s, name } : s))))
      .catch((err) => setError(String(err)))
  }

  const deleteActiveStrategy = () => {
    if (strategyId === DEFAULT_STRATEGY_ID) return // default is never deletable
    if (!window.confirm('Usunąć tę strategię wraz z jej rysunkami?')) return
    fetch(`/api/strategies/${strategyId}?symbol=${symbol}`, { method: 'DELETE' })
      .then(() => {
        setStrategies((prev) => prev.filter((s) => s.id !== strategyId))
        setStrategyId(DEFAULT_STRATEGY_ID)
      })
      .catch((err) => setError(String(err)))
  }

  // #200: single chat replacing the old three buttons (Analiza AI / Zaproponuj
  // strategię / Sprawdź) — user types anything, agent replies in the context
  // of the currently ACTIVE strategy (its plan + drawings, #179/#186), via
  // one backend endpoint that itself decides (via a JSON block or its
  // absence, #200 user decision) whether the message turns into a new
  // strategy proposal (+ possible auto-order, #198) or stays a plain reply.
  // #199: threaded — the backend resumes this strategy's own `claude -p`
  // session across messages, so the agent doesn't lose context turn to turn.
  const sendChat = () => {
    const message = chatMessage.trim()
    if (!symbol || !strategyId || !message || analyzing) return
    const activeStrategyName = strategies.find((s) => s.id === strategyId)?.name ?? strategyId
    setAnalyzing(true)
    setChatMessage('')
    fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        symbol, strategy_id: strategyId, window_id: windowId, message,
        panels: panels.map((p) => ({ label: p.label, data: p.data })),
      }),
    })
      .then(async (res) => {
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? `HTTP ${res.status}`)
        return res.json()
      })
      .then((body) => {
        if (body.saved) {
          setStrategies((prev) => [...prev, body.strategy])
          setStrategyId(body.strategy.id)
          // #198: execution is null (symbol not on the OKX limit-order
          // allowlist, or no numeric entry/SL/TP proposed), {skipped: "..."}
          // (user already has a position on this symbol), or the real OKX
          // result — surfaced in the same popup so a placed/rejected order
          // is never silent.
          const exec = body.execution
          const execLine = exec == null ? ''
            : exec.skipped ? `\n\n⏭️ Zlecenie pominięte: ${exec.skipped}`
            : exec.ok ? `\n\n✅ Zlecenie LIMIT złożone na OKX Demo: ${exec.side} ${exec.qty_filled === 0 ? exec.qty_requested : exec.qty_filled} kontraktów @ ${exec.limit_price} (order_id ${exec.order_id}, stan: ${exec.order_state})`
            : `\n\n❌ Zlecenie odrzucone: ${exec.error ?? 'nieznany błąd'}`
          onOpenPopup({ title: `Agent #${windowId} — ${symbol} / ${activeStrategyName}`, text: `${body.reply}\n\n(Nowa strategia "${body.strategy.name}" utworzona.)${execLine}` })
        } else {
          onOpenPopup({ title: `Agent #${windowId} — ${symbol} / ${activeStrategyName}`, text: body.reply })
        }
      })
      .catch((err) => onOpenPopup({ title: `Agent #${windowId} — ${symbol} / ${activeStrategyName}`, text: String(err), isError: true }))
      .finally(() => setAnalyzing(false))
  }

  const symbols = Object.keys(available?.ohlcv || {})
  const timeframes = available?.ohlcv?.[symbol] || []

  // Memoized on its real inputs (not recomputed on every render) so identity
  // stays stable across the freshness-indicator's 10s re-render tick — an
  // unstable `panels` array reference was re-triggering MultiPaneChart's
  // fitContent() on that same 10s cadence, resetting the user's zoom/pan.
  const panels = useMemo(
    () =>
      PANEL_DEFS
        .filter((def) => enabled[def.dataKind])
        .flatMap((def) => def.toPanelData(panelData[def.dataKind], pointSampleTimeframe)),
    [enabled, panelData, pointSampleTimeframe],
  )

  // #203: history is scoped to the WINDOW (was: active strategy, #199; was:
  // whole symbol, #196) — matches the per-window session_id threading, one
  // conversation per chart window regardless of which strategy/symbol is
  // selected inside it at any given moment. Opens as its own list-popup
  // (App.jsx); clicking an entry opens ANOTHER popup with its full text, so
  // the list stays open alongside it.
  const openHistory = () => {
    if (!windowId) return
    // #201: toggleKey — repeat clicks close the already-open history popup
    // for this window instead of spawning another one (bug user found:
    // every click opened a new window). Still fetches on the closing click
    // too (App.jsx's toggle check runs after this resolves) — a wasted
    // request on close, traded for not needing a second code path here.
    const toggleKey = `history-window-${windowId}`
    fetchJson(`/api/analysis_history?window_id=${windowId}`)
      .then((entries) => onOpenPopup({ title: `Historia — Okno #${windowId}`, entries: [...(entries ?? [])].reverse(), toggleKey }))
      .catch((err) => onOpenPopup({ title: `Historia — Okno #${windowId}`, text: String(err), isError: true, toggleKey }))
  }

  const priceLines = useMemo(() => {
    if (!ohlcv?.length) return []
    const emaLines = EMA_DEFS
      .filter((def) => emaEnabled[def.key])
      .map((def) => ({ key: def.key, label: def.label, color: def.color, data: computeEma(ohlcv, def.period) }))

    // #246: Bollinger Bands — three lines (upper/middle/lower) sharing one
    // pane-0 series group; middle drawn in a neutral color, bands in a
    // shared BB color at reduced-emphasis width. lightweight-charts v5's
    // LineSeries has no built-in "fill between two lines" option (verified:
    // only band/area series types fill against a baseline, not against
    // another series) — two lines is the pragmatic MVP the task brief
    // explicitly allows as a fallback.
    const bbRows = overlayEnabled.bb ? overlayData.bb : null
    const bbLines = bbRows?.length
      ? [
          { key: 'bb_upper', label: 'BB górne (20,2)', color: '#7c88f5', data: dropNullValues(bbRows, 'upper').map((r) => ({ time: r.time, value: r.upper })) },
          { key: 'bb_middle', label: 'BB SMA20', color: '#9ba0b0', data: dropNullValues(bbRows, 'middle').map((r) => ({ time: r.time, value: r.middle })) },
          { key: 'bb_lower', label: 'BB dolne (20,2)', color: '#7c88f5', data: dropNullValues(bbRows, 'lower').map((r) => ({ time: r.time, value: r.lower })) },
        ]
      : []

    // #246: EMA 21/50/200 from /api/ema — distinct from EMA_DEFS' own
    // client-side computeEma(100/200) above; this is the task's own
    // period set, computed server-side.
    const overlayEmaLines = OVERLAY_EMA_PERIODS
      .filter((period) => overlayEnabled[`ema${period}`])
      .map((period) => ({
        key: `ema${period}`,
        label: `EMA ${period}`,
        color: OVERLAY_EMA_COLORS[period],
        data: dropNullValues(overlayData[`ema${period}`]),
      }))

    // #246: multi-timeframe EMA projection — forward-filled HTF value onto
    // the current (LTF) timeline, task brief's "stepped" line. The backend
    // already forward-fills (repeats the same value across every target bar
    // until the next HTF close), so the series data itself is already
    // step-shaped — no lineType/step option needed on the series, the
    // point-to-point line between successive equal-then-jumping values
    // renders visually as steps.
    const projectionRows = overlayEnabled.emaProjection ? overlayData.emaProjection : null
    const projectionLines = projectionRows?.length
      ? [{
          key: 'ema_projection',
          label: `${emaProjectionSource ? emaProjectionSource.toUpperCase() : ''} EMA50 →`,
          color: '#f0716a',
          data: dropNullValues(projectionRows),
        }]
      : []

    // #246: session VWAP.
    const vwapRows = overlayEnabled.vwap ? overlayData.vwap : null
    const vwapLines = vwapRows?.length
      ? [{ key: 'vwap', label: `VWAP (${overlayData.vwapMeta?.session_timezone ?? 'UTC'})`, color: '#4ecf8e', data: dropNullValues(vwapRows) }]
      : []

    const overlayLines = [...bbLines, ...overlayEmaLines, ...projectionLines, ...vwapLines]

    if (!okxPositionEnabled || !okxPosition) return [...emaLines, ...overlayLines]
    // #190: flat two-point series (first/last candle, same value) — the
    // cheapest way to draw a horizontal price line with the existing
    // LineSeries-based priceLines mechanism, same trick used for #186's
    // agent-proposed S/R levels.
    const span = [ohlcv[0].time, ohlcv[ohlcv.length - 1].time]
    const flatLine = (value) => span.map((time) => ({ time, value }))
    const positionLines = [
      okxPosition.entry != null && { key: 'okx_entry', label: `OKX entry (${okxPosition.side})`, color: '#e0c14b', data: flatLine(okxPosition.entry) },
      okxPosition.stop_loss != null && { key: 'okx_sl', label: 'OKX SL', color: '#f0716a', data: flatLine(okxPosition.stop_loss) },
      okxPosition.take_profit != null && { key: 'okx_tp', label: 'OKX TP', color: '#4ecf8e', data: flatLine(okxPosition.take_profit) },
    ].filter(Boolean)
    return [...emaLines, ...overlayLines, ...positionLines]
  }, [ohlcv, emaEnabled, okxPositionEnabled, okxPosition, overlayEnabled, overlayData, emaProjectionSource])

  const lastCandle = ohlcv?.length ? ohlcv[ohlcv.length - 1] : null
  const ageSeconds = lastCandle ? Math.max(0, Math.floor(now / 1000) - lastCandle.time) : null
  // Stale once the last candle is more than 2 timeframe-lengths old — one
  // length alone is expected lag while the current bar is still forming.
  const staleThreshold = 2 * (TIMEFRAME_SECONDS[timeframe] || 3600)
  const isStale = ageSeconds !== null && ageSeconds > staleThreshold

  return (
    <div style={{ padding: 12, fontFamily: 'system-ui, sans-serif', color: '#e4e6ec', background: '#14161c', height: '100%', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
      {error && <div style={{ color: '#f0716a', marginBottom: 8, fontSize: 12 }}>{error}</div>}

      <div style={{ display: 'flex', gap: 8, marginBottom: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        <label>
          Symbol:{' '}
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>
            {symbols.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </label>
        <label>
          Strategia:{' '}
          <select value={strategyId} onChange={(e) => setStrategyId(e.target.value)}>
            {strategies.map((s) => (
              <option key={s.id} value={s.id}>{s.name}</option>
            ))}
          </select>
        </label>
        <button onClick={createStrategy} title="Nowa strategia" style={{ padding: '4px 8px', fontSize: 12, cursor: 'pointer', borderRadius: 4, border: '1px solid #3a3f4d', background: '#1e212b', color: '#e4e6ec' }}>+</button>
        <button onClick={renameActiveStrategy} title="Zmień nazwę strategii" style={{ padding: '4px 8px', fontSize: 12, cursor: 'pointer', borderRadius: 4, border: '1px solid #3a3f4d', background: '#1e212b', color: '#e4e6ec' }}>✎</button>
        {strategyId !== DEFAULT_STRATEGY_ID && (
          <button onClick={deleteActiveStrategy} title="Usuń strategię" style={{ padding: '4px 8px', fontSize: 12, cursor: 'pointer', borderRadius: 4, border: '1px solid #3a3f4d', background: '#1e212b', color: '#f0716a' }}>🗑</button>
        )}
        <label>
          Timeframe:{' '}
          <select value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
            {timeframes.map((tf) => (
              <option key={tf} value={tf}>{tf}</option>
            ))}
          </select>
        </label>

        <div ref={panelsMenuRef} style={{ position: 'relative' }}>
          <button
            onClick={() => setPanelsMenuOpen((prev) => !prev)}
            title="Panele danych i wskaźniki"
            style={{
              padding: '6px 10px', fontSize: 14, cursor: 'pointer', borderRadius: 4,
              border: '1px solid #3a3f4d', background: panelsMenuOpen ? '#3a5f8f' : '#1e212b', color: '#e4e6ec',
            }}
          >
            📊
          </button>
          {panelsMenuOpen && (
            <div
              style={{
                position: 'absolute', top: '100%', left: 0, marginTop: 4, zIndex: 10,
                display: 'flex', gap: 24, padding: 12, borderRadius: 6,
                border: '1px solid #3a3f4d', background: '#1e212b', boxShadow: '0 4px 12px rgba(0,0,0,0.4)',
              }}
            >
              <div>
                <div style={{ fontSize: 11, color: '#9ba0b0', marginBottom: 6, textTransform: 'uppercase' }}>Dane</div>
                {PANEL_DEFS.map((def) => (
                  <label key={def.dataKind} style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 4 }}>
                    <input
                      type="checkbox"
                      checked={!!enabled[def.dataKind]}
                      onChange={(e) => setEnabled((prev) => ({ ...prev, [def.dataKind]: e.target.checked }))}
                    />
                    {def.dataKind}
                  </label>
                ))}
              </div>
              <div>
                <div style={{ fontSize: 11, color: '#9ba0b0', marginBottom: 6, textTransform: 'uppercase' }}>Wskaźniki</div>
                {EMA_DEFS.map((def) => (
                  <label key={def.key} style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 4 }}>
                    <input
                      type="checkbox"
                      checked={!!emaEnabled[def.key]}
                      onChange={(e) => setEmaEnabled((prev) => ({ ...prev, [def.key]: e.target.checked }))}
                    />
                    {def.label}
                  </label>
                ))}
                <label style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 4 }} title="Entry/SL/TP realnej otwartej pozycji z konta OKX (jeśli jest jakaś dla wybranej pary)">
                  <input
                    type="checkbox"
                    checked={okxPositionEnabled}
                    onChange={(e) => setOkxPositionEnabled(e.target.checked)}
                  />
                  pozycja OKX
                </label>
              </div>
              <div>
                {/* #246: BB/EMA21-50-200/EMA-projekcja/VWAP — /api/bollinger,
                    /api/ema, /api/ema_projection, /api/vwap (#242 backend).
                    All off by default (task AC), separate group from
                    EMA_DEFS (which defaults on) since these carry their own
                    per-series metadata line below each checkbox. */}
                <div style={{ fontSize: 11, color: '#9ba0b0', marginBottom: 6, textTransform: 'uppercase' }}>Overlaye (BB/EMA/VWAP)</div>
                <label style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 4 }} title="Bollinger Bands (20, 2σ) — górne/środkowe(SMA20)/dolne pasmo">
                  <input type="checkbox" checked={!!overlayEnabled.bb} onChange={(e) => setOverlayEnabled((prev) => ({ ...prev, bb: e.target.checked }))} />
                  Bollinger Bands
                </label>
                {OVERLAY_EMA_PERIODS.map((period) => (
                  <label key={period} style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 4 }}>
                    <input
                      type="checkbox"
                      checked={!!overlayEnabled[`ema${period}`]}
                      onChange={(e) => setOverlayEnabled((prev) => ({ ...prev, [`ema${period}`]: e.target.checked }))}
                    />
                    EMA {period}
                  </label>
                ))}
                <label
                  style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 4, opacity: emaProjectionSource ? 1 : 0.5 }}
                  title={emaProjectionSource ? `Projekcja EMA50 z ${emaProjectionSource} na ${timeframe} (forward-fill, bez look-ahead)` : `Projekcja EMA niedostępna dla timeframe ${timeframe} (tylko 5m/15m)`}
                >
                  <input
                    type="checkbox"
                    checked={!!overlayEnabled.emaProjection}
                    disabled={!emaProjectionSource}
                    onChange={(e) => setOverlayEnabled((prev) => ({ ...prev, emaProjection: e.target.checked }))}
                  />
                  EMA-projekcja ({emaProjectionSource ? emaProjectionSource.toUpperCase() : '—'} EMA50)
                </label>
                <label style={{ display: 'flex', alignItems: 'center', gap: 4, marginBottom: 4 }} title="VWAP sesyjny, reset o północy UTC">
                  <input type="checkbox" checked={!!overlayEnabled.vwap} onChange={(e) => setOverlayEnabled((prev) => ({ ...prev, vwap: e.target.checked }))} />
                  VWAP (sesja)
                </label>
                {overlayEnabled.emaProjection && overlayData.emaProjection?.length > 0 && (
                  <div style={{ fontSize: 10, color: '#6b7080', marginTop: 4 }}>
                    ostatnia aktualizacja: {overlayData.emaProjection[overlayData.emaProjection.length - 1]?.last_updated_at
                      ? new Date(overlayData.emaProjection[overlayData.emaProjection.length - 1].last_updated_at).toLocaleString('pl-PL')
                      : '—'}
                  </div>
                )}
                {overlayEnabled.vwap && overlayData.vwapMeta && (
                  <div style={{ fontSize: 10, color: '#6b7080', marginTop: 4 }}>
                    strefa resetu: {overlayData.vwapMeta.session_timezone ?? 'UTC'}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>

        <div ref={drawToolsMenuRef} style={{ position: 'relative' }}>
          <button
            onClick={() => setDrawToolsMenuOpen((prev) => !prev)}
            title="Narzędzia rysowania"
            style={{
              padding: '6px 10px', fontSize: 14, cursor: 'pointer', borderRadius: 4,
              border: '1px solid #3a3f4d', background: (drawToolsMenuOpen || activeTool) ? '#3a5f8f' : '#1e212b', color: '#e4e6ec',
            }}
          >
            ✏️
          </button>
          {drawToolsMenuOpen && (
            <div
              style={{
                position: 'absolute', top: '100%', left: 0, marginTop: 4, zIndex: 10,
                display: 'flex', flexDirection: 'column', gap: 4, padding: 12, borderRadius: 6, minWidth: 160,
                border: '1px solid #3a3f4d', background: '#1e212b', boxShadow: '0 4px 12px rgba(0,0,0,0.4)',
              }}
            >
              {DRAWING_TOOL_DEFS.map((def) => (
                <button
                  key={def.type}
                  onClick={() => { setActiveTool((prev) => (prev === def.type ? null : def.type)); setDrawToolsMenuOpen(false) }}
                  style={{
                    padding: '4px 10px', fontSize: 12, cursor: 'pointer', borderRadius: 4, textAlign: 'left',
                    border: '1px solid #3a3f4d',
                    background: activeTool === def.type ? '#3a5f8f' : '#262a35',
                    color: '#e4e6ec',
                  }}
                >
                  {def.label}
                </button>
              ))}
              <button
                onClick={() => {
                  drawingManagerRef.current?.clearAll()
                  handleDrawingsChange([])
                  setDrawToolsMenuOpen(false)
                }}
                style={{ padding: '4px 10px', fontSize: 12, cursor: 'pointer', borderRadius: 4, textAlign: 'left', border: '1px solid #3a3f4d', background: '#262a35', color: '#e4e6ec' }}
              >
                Wyczyść wszystko
              </button>
            </div>
          )}
        </div>

        <input
          type="text"
          value={chatMessage}
          onChange={(e) => setChatMessage(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat() } }}
          disabled={analyzing || !symbol}
          placeholder={analyzing ? '⏳ agent pracuje…' : 'Napisz do agenta o tej strategii…'}
          title="Wiadomość do agenta w kontekście aktywnej strategii — Enter wysyła"
          style={{
            marginLeft: 0, flex: '1 1 200px', minWidth: 140, padding: '6px 10px', fontSize: 14, borderRadius: 4,
            border: '1px solid #3a3f4d', background: '#1e212b', color: '#e4e6ec', opacity: analyzing ? 0.6 : 1,
          }}
        />
        <button
          onClick={sendChat}
          disabled={analyzing || !symbol || !chatMessage.trim()}
          title="Wyślij (Enter)"
          style={{
            padding: '6px 10px', fontSize: 14, cursor: analyzing ? 'default' : 'pointer', borderRadius: 4,
            border: '1px solid #3a3f4d', background: '#1e212b', color: '#e4e6ec', opacity: analyzing ? 0.6 : 1,
          }}
        >
          {analyzing ? '⏳' : '💬 Wyślij'}
        </button>

        <button
          onClick={openHistory}
          disabled={!symbol}
          title="Historia rozmowy z agentem dla tej strategii"
          style={{ padding: '6px 10px', fontSize: 14, cursor: 'pointer', borderRadius: 4, border: '1px solid #3a3f4d', background: '#1e212b', color: '#e4e6ec' }}
        >
          🕐 Historia
        </button>
      </div>

      <div style={{ marginBottom: 8, fontSize: 13, color: '#9ba0b0', display: 'flex', alignItems: 'center', gap: 8 }}>
        <span>{symbol} ({timeframe})</span>
        {lastCandle && (
          <span title="Wiek ostatniej świecy względem teraz — czerwone oznacza, że dane wymagają odświeżenia (backfill --incremental)" style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <span
              style={{
                width: 8, height: 8, borderRadius: '50%', display: 'inline-block',
                background: isStale ? '#f0716a' : '#4ecf8e',
              }}
            />
            ostatnia świeca: {formatAge(ageSeconds)} temu
          </span>
        )}
      </div>
      <div ref={chartContainerRef} style={{ flex: 1, minHeight: 0, position: 'relative' }}>
        <MultiPaneChart
          ohlcv={ohlcv} panels={panels} priceLines={priceLines} resetViewKey={`${symbol}-${timeframe}`}
          activeTool={activeTool}
          drawingManagerRef={drawingManagerRef}
          onDrawingsChange={(serialized) => { setActiveTool(null); handleDrawingsChange(serialized) }}
          onSelectionChange={setSelectedDrawing}
          onDrawingDblClick={(drawing, point) => setDrawingEditPopup(point)}
          heightRatio={null}
        />
        {drawingEditPopup && selectedDrawing && (
          <div
            ref={drawingEditPopupRef}
            style={{
              position: 'absolute', left: drawingEditPopup.x + 12, top: drawingEditPopup.y + 12, zIndex: 20,
              display: 'flex', alignItems: 'center', gap: 6, padding: '8px 10px', borderRadius: 6,
              border: '1px solid #3a3f4d', background: '#1e212b', boxShadow: '0 4px 12px rgba(0,0,0,0.4)',
            }}
          >
            <input
              type="color"
              value={selectedDrawing.style.lineColor}
              onChange={(e) => recolorSelectedDrawing(e.target.value)}
              title="Zmień kolor"
              style={{ width: 24, height: 24, padding: 0, border: 'none', background: 'none', cursor: 'pointer' }}
            />
            {selectedDrawing.type === 'trend-line' && (
              <>
                <button
                  onClick={() => toggleSelectedDrawingExtend('left')}
                  title="Przedłuż w lewo do krawędzi wykresu"
                  style={{
                    padding: '4px 10px', fontSize: 12, cursor: 'pointer', borderRadius: 4, border: '1px solid #3a3f4d',
                    background: selectedDrawing.options?.extendLeft ? '#3a5f8f' : '#262a35', color: '#e4e6ec',
                  }}
                >
                  ⟵ Przedłuż
                </button>
                <button
                  onClick={() => toggleSelectedDrawingExtend('right')}
                  title="Przedłuż w prawo do krawędzi wykresu"
                  style={{
                    padding: '4px 10px', fontSize: 12, cursor: 'pointer', borderRadius: 4, border: '1px solid #3a3f4d',
                    background: selectedDrawing.options?.extendRight ? '#3a5f8f' : '#262a35', color: '#e4e6ec',
                  }}
                >
                  Przedłuż ⟶
                </button>
              </>
            )}
            <button
              onClick={() => { deleteSelectedDrawing(); setDrawingEditPopup(null) }}
              title="Usuń (lub klawisz Delete/Backspace)"
              style={{ padding: '4px 10px', fontSize: 12, cursor: 'pointer', borderRadius: 4, border: '1px solid #3a3f4d', background: '#262a35', color: '#f0716a' }}
            >
              Usuń
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
