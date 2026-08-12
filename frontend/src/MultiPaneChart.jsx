import { useEffect, useRef } from 'react'
import { createChart, CandlestickSeries, LineSeries, createSeriesMarkers } from 'lightweight-charts'
import {
  DrawingManager, TrendLine, ParallelChannel, FibChannel, FibRetracement, DisjointChannel, Brush, HorizontalLine,
} from 'lightweight-charts-drawing'

// One factory per tool available in the DrawingToolbar (App.jsx) — both for
// creating a fresh drawing while the user clicks the chart, and for
// reconstructing a SerializedDrawing loaded from the backend on mount.
export const DRAWING_TOOLS = {
  'trend-line': TrendLine,
  'parallel-channel': ParallelChannel,
  'fib-channel': FibChannel,
  'fib-retracement': FibRetracement,
  'disjoint-channel': DisjointChannel, // "megafon" — two independent, non-parallel lines
  'brush': Brush, // freehand pencil (user request 2026-08-07) — library-native tool, different gesture below (drag, not click-per-anchor)
  'horizontal-line': HorizontalLine, // user request 2026-08-08 — one click sets the price level, library draws it across the whole pane with an optional label (labelText prompted for below)
}

// #204: horizontal-line needs a label TEXT before it can be created (the
// library's labelText option has no default worth keeping blank) — this is
// checked by type name in handleChartClick below, same pattern as
// FREEHAND_TOOL_TYPE, to prompt() for it right after the single click that
// places the line instead of adding a whole separate UI flow for one field.
const LABELED_HORIZONTAL_TOOL_TYPE = 'horizontal-line'

// #193: freehand needs its own gesture (mousedown-drag-mouseup, collecting
// a point per mousemove) — every other tool is click-once-per-anchor
// (handleChartClick below). Checked by type name, not e.g. a `.freehand`
// flag on the tool class, since DRAWING_TOOLS' values are the library's own
// classes and adding a flag to them isn't an option.
const FREEHAND_TOOL_TYPE = 'brush'

// Extension levels (#180) shown alongside the standard retracement ratios
// (0/23.6/38.2/50/61.8/78.6/100, the library's own default) on every
// fib-retracement drawing — projected symmetrically above 100% and below 0%
// from the end of the drawn range. Applied unconditionally (no per-drawing
// toggle) both when a fib-retracement is freshly drawn and when one is
// reconstructed from a saved SerializedDrawing, because FibRetracement's
// `levels` option lives in its subclass-only `_fibOptions`, not the base
// `_options` its inherited toJSON() serializes — a saved drawing's exported
// JSON never carries `levels` back, so the levels must come from this
// constant again on every import rather than round-tripping through storage.
const FIB_EXTENSION_RATIOS = [
  0.214, 0.236, 0.272, 0.382, 0.414, 0.5, 0.618, 0.764, 0.786, 1, 1.618, 2.618, 3.236,
]
const FIB_RETRACEMENT_LEVELS = [
  0, 0.236, 0.382, 0.5, 0.618, 0.786, 1, // library defaults, unchanged
  ...FIB_EXTENSION_RATIOS.map((r) => 1 + r), // up: 121.4% .. 423.6%
  ...FIB_EXTENSION_RATIOS.map((r) => -r), // down (mirrored): -21.4% .. -323.6%
].sort((a, b) => a - b)

// Merges FIB_RETRACEMENT_LEVELS into `options` for fib-retracement (passed
// through unchanged for every other tool type) so both the click-to-create
// path (MultiPaneChart, below) and the saved-drawing import path (App.jsx)
// produce identically-configured drawings.
//
// `extendLines: true` (#180) + `showPrices`/`showPercentages: false` (same
// day follow-up) — user wants the extend-to-edge behavior back after all,
// but the library draws each level's price/percentage LABEL pinned to the
// right EDGE OF THE PANE (`x: paneWidth - 5`, `textAlign: "left"` — verified
// by reading the compiled source, lightweight-charts-drawing.es.js)
// regardless of where the line itself ends; there is no library option to
// reposition it, so with extendLines true the label always clips
// off-screen. Turning the labels off entirely is the only way to keep
// extendLines without that clipping — the level's color (library's own
// TradingView-matching color map, keyed by ratio) still identifies which
// level is which, just without a price/percentage caption on the chart.
export function createDrawingOptions(type, options) {
  if (type !== 'fib-retracement') return options
  return { ...options, levels: FIB_RETRACEMENT_LEVELS, extendLines: true, showPrices: false, showPercentages: false }
}

/** One `createChart` instance with the OHLC candlesticks in pane 0 and one
 * extra line-series pane per entry in `panels` — native multi-pane sync
 * (#172 prep): panes managed by the SAME time scale, so there is no manual
 * setVisibleRange/subscribeVisibleTimeRangeChange wiring to get wrong (the
 * bug fixed in edf849bd/b7f700ff can no longer happen, because there is only
 * ever one time scale for the whole chart).
 *
 * `panels`: [{ key, label, data, color }] — one entry per visible extra
 * series. Panels are added/removed as this array changes (checkbox toggling
 * in #172 adds/removes entries here, not separate chart instances).
 *
 * `heightRatio`: fraction of `window.innerHeight` (the original single-chart
 * dashboard layout, #170). Pass `null` (#184's resizable ChartWindow) to size
 * off the container element's own height instead — lightweight-charts has no
 * CSS-percent height option either way, both modes need an explicit pixel
 * value recomputed on every resize.
 */
export function MultiPaneChart({
  ohlcv, panels = [], priceLines = [], resetViewKey, heightRatio = 0.7,
  activeTool = null, onDrawingsChange, onSelectionChange, onDrawingDblClick, drawingManagerRef,
}) {
  const ref = useRef(null)
  const chartRef = useRef(null)
  const candleSeriesRef = useRef(null)
  const panelSeriesRef = useRef(new Map()) // key -> ISeriesApi
  const panelMarkersRef = useRef(new Map()) // key -> ISeriesMarkersPluginApi, for panels with `markers` (#181 risk indicator divergences)
  const priceLineSeriesRef = useRef(new Map()) // key -> ISeriesApi, pane 0
  const lastFitKeyRef = useRef(null) // fitContent() only when resetViewKey changes, not on 15s auto-refresh ticks
  const drawingManagerInstanceRef = useRef(null)
  const activeToolRef = useRef(activeTool) // mirrors the `activeTool` prop for the click handler below, which is registered once (not re-registered per activeTool change)
  const onDrawingsChangeRef = useRef(onDrawingsChange) // same pattern — the drawing:added/updated/removed listeners are registered once at mount
  const onSelectionChangeRef = useRef(onSelectionChange) // same pattern — for drawing:selected/deselected
  const onDrawingDblClickRef = useRef(onDrawingDblClick) // same pattern — for the double-click-to-edit popup below

  useEffect(() => {
    if (!ref.current) return undefined
    const el = ref.current
    // heightRatio === null (#184): size off the container's own height (it
    // lives inside a resizable window) instead of a fraction of the browser
    // window's height (#170's single-chart layout).
    const computeHeight = () => (heightRatio == null ? el.clientHeight : Math.round(window.innerHeight * heightRatio))
    const chart = createChart(el, {
      height: computeHeight(),
      layout: { background: { color: 'transparent' }, textColor: '#c8ccd8' },
      grid: {
        vertLines: { color: 'rgba(128,128,128,0.12)' },
        horzLines: { color: 'rgba(128,128,128,0.12)' },
      },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false },
    })
    chartRef.current = chart
    candleSeriesRef.current = chart.addSeries(CandlestickSeries, {
      title: 'OHLC',
      upColor: '#4ecf8e',
      downColor: '#f0716a',
      borderVisible: false,
      wickUpColor: '#4ecf8e',
      wickDownColor: '#f0716a',
      // User request 2026-08-08: always 4 decimal places on the price axis,
      // regardless of symbol — lightweight-charts otherwise auto-picks
      // precision from the data (usually 2 for a BTC-range price), which
      // rounded away meaningful digits on cheaper symbols (WLD, DOGE).
      priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
    })

    const applySize = () => {
      chart.applyOptions({ width: el.clientWidth, height: computeHeight() })
    }
    const ro = new ResizeObserver(applySize)
    ro.observe(el)
    window.addEventListener('resize', applySize)

    // DrawingManager attaches to the candlestick series (pane 0) for
    // hit-testing/dragging EXISTING drawings and export/import — but its own
    // handleClick() is a no-op while a tool is active (verified by reading
    // the compiled source: the library ships no click-to-create-a-new-
    // drawing flow at all, only selection + anchor-dragging). Creating a new
    // drawing while `activeTool` is set is therefore done here manually via
    // chart.subscribeClick(), collecting anchors until the active tool's
    // `REQUIRED_ANCHORS` count is reached, then manager.addDrawing().
    const drawingManager = new DrawingManager()
    drawingManager.attach(chart, candleSeriesRef.current, el)
    // Library bug worked around here: DrawingManager.addDrawing()/importDrawings()
    // correctly call ISeriesApi.attachPrimitive() (verified by reading the
    // compiled source) but never trigger a repaint — a freshly-drawn line is
    // visible immediately only because the click that creates it happens to
    // coincide with other chart activity. A drawing added via importDrawings()
    // on mount (after the last setData() call already ran) never gets its
    // first paint until *something else* repaints the chart — confirmed by
    // manually calling drawing._requestUpdateFn() in devtools, which fixed it
    // instantly. requestRepaint() is this manual nudge, exposed on the
    // manager so App.jsx can call it right after importDrawings().
    drawingManager.requestRepaint = () => chart.applyOptions({})
    drawingManagerInstanceRef.current = drawingManager
    if (drawingManagerRef) drawingManagerRef.current = drawingManager

    let pendingAnchors = []
    const handleChartClick = (param) => {
      const toolType = activeToolRef.current
      // #193: freehand uses its own mousedown/mousemove/mouseup gesture
      // below, not click-per-anchor — a click firing here too (mouseup
      // that ends a drag also counts as a click) would misinterpret the
      // brush stroke's endpoint as the start of an unrelated tool.
      if (toolType === FREEHAND_TOOL_TYPE) return
      // TEMP DEBUG #203: diagnose why clicks near the right edge (still over
      // visible candles) don't register a drawing anchor despite a visible
      // crosshair there. Remove after diagnosis.
      console.log('[#203 debug] handleChartClick', { toolType, point: param.point, time: param.time, logical: param.logical })
      if (!toolType || !param.point || param.time === undefined) return
      const price = candleSeriesRef.current?.coordinateToPrice(param.point.y)
      if (price === null || price === undefined) return
      pendingAnchors = [...pendingAnchors, { time: param.time, price }]
      const ToolClass = DRAWING_TOOLS[toolType]
      if (!ToolClass || pendingAnchors.length < ToolClass.REQUIRED_ANCHORS) return
      // #204: horizontal-line prompts for its label text right after the
      // single click that places it — an empty/cancelled prompt still
      // creates the line (showLabel: false), just without a caption, rather
      // than silently discarding the click.
      let toolOptions = createDrawingOptions(toolType)
      if (toolType === LABELED_HORIZONTAL_TOOL_TYPE) {
        const labelText = window.prompt('Opis poziomu (opcjonalnie):', '')
        toolOptions = { ...toolOptions, labelText: labelText || '', showLabel: !!labelText }
      }
      const drawing = new ToolClass(`drawing-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, pendingAnchors, undefined, toolOptions)
      pendingAnchors = []
      drawingManager.addDrawing(drawing)
    }
    chart.subscribeClick(handleChartClick)

    // User request: color/extend controls move from the always-visible
    // toolbar into a popup opened by double-clicking a drawing. A single
    // click already selects it (DrawingManager's own click handler, wired
    // via drawingManager.attach() above) — by the time this dblclick handler
    // runs, drawingManager.getSelectedDrawing() reflects whichever drawing
    // (if any) is under the cursor, so no separate hit-test is needed here.
    // Only fires with no active drawing tool (drawing NEW shapes still wants
    // its own click-per-anchor gesture above, unaffected by this).
    const handleChartDblClick = (param) => {
      if (activeToolRef.current || !param.point) return
      const selected = drawingManager.getSelectedDrawing()
      if (!selected) return
      onDrawingDblClickRef.current?.(selected, { x: param.point.x, y: param.point.y })
    }
    chart.subscribeDblClick(handleChartDblClick)

    // DrawingManager wires its own mousedown/mousemove/mouseup on `el` to
    // drag a selected drawing's anchor (verified in #175's research), but
    // never disables the chart's own pan/scroll for the SAME gesture — so
    // dragging an anchor also scrolls the chart underneath it. Mirror the
    // anchor hit-test here on mousedown (same public hitTestAnchor() the
    // manager uses internally) to detect "this drag is an anchor edit, not
    // a pan" and disable handleScroll/handleScale for the gesture's duration.
    let isEditingAnchor = false
    const getPointFromMouseEvent = (e) => {
      const rect = el.getBoundingClientRect()
      return { x: e.clientX - rect.left, y: e.clientY - rect.top }
    }
    const timeAndPriceFromMouseEvent = (e) => {
      const point = getPointFromMouseEvent(e)
      const time = chart.timeScale().coordinateToTime(point.x)
      const price = candleSeriesRef.current?.coordinateToPrice(point.y)
      if (time === null || price === null || price === undefined) return null
      return { time, price }
    }

    // #193: freehand (Brush) — mousedown starts a new stroke, mousemove
    // while the button is held adds points to it, mouseup finalizes it
    // into the DrawingManager. Distinct from the click-per-anchor gesture
    // above (every other tool) and from anchor-dragging (isEditingAnchor):
    // this is its own drawing being BUILT, not an existing one being
    // edited, so pan/scroll is disabled the same way for the same reason
    // (a stroke shouldn't also pan the chart underneath it).
    let activeBrush = null
    const handleMouseDownForBrush = (e) => {
      if (activeToolRef.current !== FREEHAND_TOOL_TYPE) return
      const anchor = timeAndPriceFromMouseEvent(e)
      if (!anchor) return
      activeBrush = new Brush(`drawing-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, [anchor, anchor], undefined, createDrawingOptions(FREEHAND_TOOL_TYPE))
      chart.applyOptions({ handleScroll: false, handleScale: false })
    }
    const handleMouseMoveForBrush = (e) => {
      if (!activeBrush) return
      const anchor = timeAndPriceFromMouseEvent(e)
      if (!anchor) return
      activeBrush.addPoint(anchor)
      drawingManager.requestRepaint?.() // same library-repaint-bug workaround as imported drawings (see requestRepaint comment above) — a Brush's points added outside addDrawing()/importDrawings() don't trigger a repaint on their own either
    }
    const handleMouseUpForBrush = () => {
      if (!activeBrush) return
      drawingManager.addDrawing(activeBrush)
      activeBrush = null
      chart.applyOptions({ handleScroll: true, handleScale: true })
    }

    const handleMouseDownCapture = (e) => {
      if (drawingManager.getSelectedDrawing() && drawingManager.hitTestAnchor(getPointFromMouseEvent(e)) !== null) {
        isEditingAnchor = true
        chart.applyOptions({ handleScroll: false, handleScale: false })
        return
      }
      handleMouseDownForBrush(e)
    }
    const handleMouseMoveCapture = (e) => handleMouseMoveForBrush(e)
    const handleMouseUpCapture = () => {
      handleMouseUpForBrush()
      if (!isEditingAnchor) return
      isEditingAnchor = false
      chart.applyOptions({ handleScroll: true, handleScale: true })
    }
    // Capture phase so this runs before DrawingManager's own listener
    // (registered via drawingManager.attach() below) starts the drag.
    el.addEventListener('mousedown', handleMouseDownCapture, true)
    el.addEventListener('mousemove', handleMouseMoveCapture, true)
    window.addEventListener('mouseup', handleMouseUpCapture, true)

    const notifyChange = () => onDrawingsChangeRef.current?.(drawingManager.exportDrawings())
    const unsubscribe = drawingManager.on('drawing:added', notifyChange)
    const unsubscribeUpdate = drawingManager.on('drawing:updated', notifyChange)
    const unsubscribeRemove = drawingManager.on('drawing:removed', () => {
      notifyChange()
      onSelectionChangeRef.current?.(null)
    })
    const notifySelection = () => onSelectionChangeRef.current?.(drawingManager.getSelectedDrawing())
    const unsubscribeSelected = drawingManager.on('drawing:selected', notifySelection)
    const unsubscribeDeselected = drawingManager.on('drawing:deselected', notifySelection)

    // Delete/Backspace removes the selected drawing — DrawingManager itself
    // has no keyboard handling (verified in #175's research: it only wires
    // mouse events in attach()), so this is done here the same way
    // click-to-create had to be.
    const handleKeyDown = (e) => {
      if (e.key !== 'Delete' && e.key !== 'Backspace') return
      const selected = drawingManager.getSelectedDrawing()
      if (!selected) return
      e.preventDefault()
      drawingManager.removeDrawing(selected.id)
    }
    window.addEventListener('keydown', handleKeyDown)

    return () => {
      window.removeEventListener('keydown', handleKeyDown)
      el.removeEventListener('mousedown', handleMouseDownCapture, true)
      el.removeEventListener('mousemove', handleMouseMoveCapture, true)
      window.removeEventListener('mouseup', handleMouseUpCapture, true)
      chart.unsubscribeClick(handleChartClick)
      chart.unsubscribeDblClick(handleChartDblClick)
      unsubscribe?.()
      unsubscribeUpdate?.()
      unsubscribeRemove?.()
      unsubscribeSelected?.()
      unsubscribeDeselected?.()
      drawingManager.detach()
      drawingManagerInstanceRef.current = null
      if (drawingManagerRef) drawingManagerRef.current = null
      ro.disconnect()
      window.removeEventListener('resize', applySize)
      chart.remove()
      chartRef.current = null
      candleSeriesRef.current = null
      panelSeriesRef.current = new Map()
      panelMarkersRef.current = new Map()
      priceLineSeriesRef.current = new Map()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [heightRatio])

  useEffect(() => {
    activeToolRef.current = activeTool
    drawingManagerInstanceRef.current?.setActiveTool(activeTool)
  }, [activeTool])

  useEffect(() => {
    onDrawingsChangeRef.current = onDrawingsChange
  }, [onDrawingsChange])

  useEffect(() => {
    onSelectionChangeRef.current = onSelectionChange
  }, [onSelectionChange])

  useEffect(() => {
    onDrawingDblClickRef.current = onDrawingDblClick
  }, [onDrawingDblClick])

  useEffect(() => {
    if (!candleSeriesRef.current || !ohlcv?.length) return
    candleSeriesRef.current.setData(ohlcv)
    // Only re-fit when the symbol/timeframe actually changed — a 15s
    // auto-refresh tick reuses the same resetViewKey, so the viewer's
    // zoom/pan survives the periodic setData() call above.
    if (lastFitKeyRef.current !== resetViewKey) {
      lastFitKeyRef.current = resetViewKey
      chartRef.current.timeScale().fitContent()
    }
  }, [ohlcv, resetViewKey])

  // EMA overlays live in pane 0 alongside the candlesticks (same price axis),
  // not a separate pane like `panels` — added/removed/updated the same way.
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const existing = priceLineSeriesRef.current
    const seenKeys = new Set(priceLines.map((p) => p.key))

    for (const [key, series] of existing) {
      if (!seenKeys.has(key)) {
        chart.removeSeries(series)
        existing.delete(key)
      }
    }

    priceLines.forEach((line) => {
      let series = existing.get(line.key)
      if (!series) {
        series = chart.addSeries(LineSeries, {
          title: line.label,
          color: line.color,
          lineWidth: 1,
          crosshairMarkerVisible: false,
        }, 0)
        existing.set(line.key, series)
      }
      if (line.data?.length) {
        series.setData(line.data)
      }
    })
  }, [priceLines])

  // Add/remove/update extra panes to match `panels`. Each pane gets its own
  // fixed pixel height via setStretchFactor left at default (auto) plus an
  // explicit resize is not needed per-pane in v5 — panes share the chart's
  // total height proportionally, so a rough height target is achieved by
  // capping the number of visible panels rather than pixel-perfect sizing.
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const existing = panelSeriesRef.current
    const existingMarkers = panelMarkersRef.current
    const seenKeys = new Set(panels.map((p) => p.key))
    const keysChanged = seenKeys.size !== existing.size || [...seenKeys].some((k) => !existing.has(k))

    for (const [key, series] of existing) {
      if (!seenKeys.has(key)) {
        chart.removeSeries(series)
        existing.delete(key)
        existingMarkers.delete(key) // markers plugin is torn down along with its series — no separate detach needed
      }
    }

    // Panels normally get one pane each, but entries sharing the same
    // `panel.pane` group key (e.g. MACD's macd/signal/histogram lines, or
    // Stochastic's %K/%D) are plotted together on ONE pane instead — pane
    // index is assigned per first-seen group key, not per array index.
    const paneIndexByGroup = new Map()
    panels.forEach((panel) => {
      const group = panel.pane ?? panel.key
      if (!paneIndexByGroup.has(group)) paneIndexByGroup.set(group, paneIndexByGroup.size + 1) // pane 0 is the candlesticks
    })

    panels.forEach((panel) => {
      const paneIndex = paneIndexByGroup.get(panel.pane ?? panel.key)
      let series = existing.get(panel.key)
      if (!series) {
        series = chart.addSeries(
          LineSeries,
          { title: panel.label, color: panel.color || '#7c88f5', lineWidth: 2 },
          paneIndex,
        )
        existing.set(panel.key, series)
      } else {
        // panel.label changes when its auto-picked timeframe changes (e.g.
        // "Open Interest (1d)" -> "Open Interest (5m)" after switching OHLC
        // timeframe) — update the existing series' title instead of only
        // setting it at creation, or the label goes stale.
        series.applyOptions({ title: panel.label })
      }
      // #183 fix: an EMPTY `panel.data` (not just missing) must still clear
      // the series — e.g. relative_strength returns [] for BTC-vs-BTC (no
      // comparison possible), and without this the series kept showing the
      // PREVIOUS symbol's stale line, since setData() was skipped entirely
      // for an empty array. `panel.data === undefined` (still loading) is
      // the only case that should leave the series untouched.
      if (panel.data !== undefined) {
        series.setData(panel.data)
      }
      // #181: optional markers (risk indicator divergences) on this panel's
      // own series/pane, not the candlesticks — createSeriesMarkers() is a
      // plugin instance per series, created once and updated via
      // setMarkers() afterwards (recreating it on every render would be
      // wasteful and isn't needed).
      if (panel.markers !== undefined) {
        let markersApi = existingMarkers.get(panel.key)
        if (!markersApi) {
          markersApi = createSeriesMarkers(series, [])
          existingMarkers.set(panel.key, markersApi)
        }
        markersApi.setMarkers(panel.markers)
      }
    })

    // Re-fit only when the set of visible panels changed (checkbox
    // toggled) or the symbol/timeframe changed: a panel series can span a
    // wider or narrower date range than the candlesticks (e.g. OI's 1d
    // track goes back ~6 months while a 15m OHLC pull only goes back ~1
    // year), so a newly added panel needs a fit or the axis reads as
    // scrambled. A 15s auto-refresh tick reuses the same panel keys and
    // resetViewKey, so it must NOT re-fit or it resets the viewer's zoom/pan.
    if (keysChanged || lastFitKeyRef.current !== resetViewKey) {
      lastFitKeyRef.current = resetViewKey
      chart.timeScale().fitContent()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [panels, resetViewKey])

  const missingPanels = panels.filter((p) => !p.data?.length)
  // heightRatio === null (#184): fill the parent container instead of a
  // fixed pixel value — the ResizeObserver above keeps the chart itself in
  // sync as the container (a resizable window) changes size.
  const containerStyle = heightRatio == null ? { height: '100%' } : { height: Math.round(window.innerHeight * heightRatio) }

  return (
    <div style={{ position: 'relative', ...containerStyle }}>
      <div ref={ref} style={{ height: '100%' }} />
      {!ohlcv?.length && (
        <div className="chart-empty" style={{ position: 'absolute', top: 0, left: 0, right: 0, height: '100%' }}>
          Brak danych OHLCV dla wybranej pary/timeframe.
        </div>
      )}
      {missingPanels.map((p) => (
        <div key={p.key} style={{ fontSize: 12, color: '#6b7080', padding: '2px 0' }}>
          Brak danych {p.label} dla wybranej pary.
        </div>
      ))}
    </div>
  )
}
