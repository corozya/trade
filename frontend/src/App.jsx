import { useState } from 'react'
import { Rnd } from 'react-rnd'
import { ChartWindow } from './ChartWindow.jsx'

// Multiple independent chart windows (#184), each cascaded on top of the
// last and freely draggable/resizable (react-rnd) — replaces the old
// single-chart-fills-the-page layout (#170-179). Window position/size AND
// each window's own symbol/timeframe/enabled-panels selection persist across
// reloads (user request) under one localStorage key, keyed by window id so
// windows don't clobber each other's remembered state.
const LAYOUT_KEY = 'crypto-dashboard:windows'
// #203: separate, monotonically-increasing counter — never reset, never
// reused (unlike `id`, a random string minted fresh per window). This
// number IS the agent's session identity in each window (see ChartWindow's
// sendChat/openHistory): the backend threads `claude -p --session-id`
// conversation memory and chat history per `window_id`, independent of which
// strategy/symbol is selected inside the window at any given moment.
// Persisted in its own localStorage key so it survives closing windows
// (closing must not free up/reuse a number — a later re-visit to the same
// number should still resume the same agent conversation).
const WINDOW_COUNTER_KEY = 'crypto-dashboard:windowCounter'
const CASCADE_OFFSET = 32 // px shift per new window, both x and y
const DEFAULT_WINDOW_SIZE = { width: 900, height: 620 }
const MIN_WINDOW_SIZE = { width: 420, height: 320 }
const DEFAULT_POPUP_SIZE = { width: 420, height: 320 }
const MIN_POPUP_SIZE = { width: 280, height: 160 }

// #199: backend's history entry `role` values -> Polish labels for the list.
const HISTORY_ROLE_LABELS = {
  user: '🧑 Ty',
  agent: '🤖 Agent',
}

function loadLayout() {
  try {
    const parsed = JSON.parse(localStorage.getItem(LAYOUT_KEY))
    if (!Array.isArray(parsed) || !parsed.length) return null
    // #203: windows saved before windowNumber existed get one assigned now,
    // once, on load — never re-assigned on subsequent loads since the value
    // is written back into the persisted layout immediately below.
    let touched = false
    const migrated = parsed.map((w) => {
      if (w.windowNumber != null) return w
      touched = true
      return { ...w, windowNumber: nextWindowNumber() }
    })
    if (touched) saveLayout(migrated)
    return migrated
  } catch {
    return null
  }
}

function saveLayout(windows) {
  try {
    localStorage.setItem(LAYOUT_KEY, JSON.stringify(windows))
  } catch {
    // best-effort: private browsing / storage full — losing persistence
    // is not worth surfacing as a user-facing error
  }
}

function nextWindowNumber() {
  const current = parseInt(localStorage.getItem(WINDOW_COUNTER_KEY) ?? '0', 10) || 0
  const next = current + 1
  try {
    localStorage.setItem(WINDOW_COUNTER_KEY, String(next))
  } catch {
    // best-effort, same as saveLayout — private browsing / storage full
  }
  return next
}

function makeWindow(index) {
  return {
    id: `win-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    windowNumber: nextWindowNumber(), // #203: the agent's session identity — see WINDOW_COUNTER_KEY comment above
    x: 24 + (index % 8) * CASCADE_OFFSET,
    y: 56 + (index % 8) * CASCADE_OFFSET, // clears the "+ Nowe okno" button in the top-left corner
    width: DEFAULT_WINDOW_SIZE.width,
    height: DEFAULT_WINDOW_SIZE.height,
    zIndex: index + 1,
    prefs: {},
    // #202: OS-style minimize/maximize. `minimized` collapses the window to
    // just its title bar (in place, not a taskbar). `maximized` fills the
    // viewport; `restoreRect` remembers the pre-maximize x/y/width/height so
    // restore can put the window back exactly where it was.
    minimized: false,
    maximized: false,
    restoreRect: null,
  }
}

const HEADER_HEIGHT = 25 // px — collapsed height of a minimized window's title bar

export function App() {
  const [windows, setWindows] = useState(() => loadLayout() ?? [makeWindow(0)])
  const [nextZIndex, setNextZIndex] = useState(() => Math.max(1, ...(loadLayout() ?? []).map((w) => w.zIndex ?? 1)) + 1)
  // Agent reply popups (user request, 2026-08-07) — separate, cascaded,
  // draggable/closable windows, NOT tied to the chart window that triggered
  // them (unlike the earlier in-window text panel). Not persisted across
  // reloads — ad-hoc replies, same "no history" decision as #177/#186's
  // backend endpoints themselves.
  const [popups, setPopups] = useState([])
  const [popupCounter, setPopupCounter] = useState(0)

  const persist = (next) => {
    setWindows(next)
    saveLayout(next)
  }

  const addWindow = () => {
    persist([...windows, { ...makeWindow(windows.length), zIndex: nextZIndex }])
    setNextZIndex((z) => z + 1)
  }

  const closeWindow = (id) => {
    const next = windows.filter((w) => w.id !== id)
    // Never end up with zero windows — a fresh cascaded one replaces the last closed.
    persist(next.length ? next : [makeWindow(0)])
  }

  const focusWindow = (id) => {
    if (windows.find((w) => w.id === id)?.zIndex === nextZIndex - 1) return // already on top, skip a needless persist
    persist(windows.map((w) => (w.id === id ? { ...w, zIndex: nextZIndex } : w)))
    setNextZIndex((z) => z + 1)
  }

  const updateWindow = (id, patch) => {
    persist(windows.map((w) => (w.id === id ? { ...w, ...patch } : w)))
  }

  // #202: react-rnd's drag handle still fires a native `click` on mouseup
  // even after a real drag (no built-in click-suppression, unlike native
  // HTML5 drag-and-drop) — so a drag-to-move would also toggle maximize via
  // the header's onClick below. This set tracks window ids whose drag JUST
  // ended with real movement, so that one following click can be swallowed.
  const suppressHeaderClick = useState(() => new Set())[0]

  // #202: minimize collapses the window to its title bar in place — no
  // taskbar, click the header again (or the button) to restore.
  const toggleMinimize = (id) => {
    updateWindow(id, { minimized: !windows.find((w) => w.id === id)?.minimized })
    focusWindow(id)
  }

  // #202: maximize fills the viewport; restoreRect remembers the rect from
  // just before maximizing so restore puts the window back exactly.
  const toggleMaximize = (id) => {
    const w = windows.find((win) => win.id === id)
    if (!w) return
    if (w.maximized) {
      updateWindow(id, { maximized: false, minimized: false, ...(w.restoreRect ?? {}), restoreRect: null })
    } else {
      updateWindow(id, {
        maximized: true,
        minimized: false,
        restoreRect: { x: w.x, y: w.y, width: w.width, height: w.height },
      })
    }
    focusWindow(id)
  }

  // Multiple popups can be open at once (user decision) — each click opens
  // a NEW one, cascaded, independent of any others already open. `entries`
  // (#196, optional) makes this a HISTORY-LIST popup instead of a plain-text
  // one — a clickable list of past agent replies, each opening its own
  // plain-text popup when clicked (so opening an old reply doesn't replace
  // the list, both stay on screen).
  //
  // `toggleKey` (#201, optional) opts a popup OUT of the multi-instance
  // behavior above — passing the same key twice closes the existing popup
  // instead of opening a second one. Used by the Historia button, which the
  // user found kept spawning new windows on repeat clicks; agent-reply
  // popups (Analiza AI etc.) deliberately don't pass this, staying
  // multi-instance per #189's original decision.
  const openPopup = ({ title, text, isError, entries, toggleKey }) => {
    if (toggleKey) {
      const existing = popups.find((p) => p.toggleKey === toggleKey)
      if (existing) {
        closePopup(existing.id)
        return
      }
    }
    const id = `popup-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
    setPopups((prev) => [...prev, {
      id, title, text, isError, entries, toggleKey,
      x: 80 + (popupCounter % 8) * CASCADE_OFFSET,
      y: 100 + (popupCounter % 8) * CASCADE_OFFSET,
      width: DEFAULT_POPUP_SIZE.width,
      height: DEFAULT_POPUP_SIZE.height,
      zIndex: nextZIndex,
    }])
    setPopupCounter((c) => c + 1)
    setNextZIndex((z) => z + 1)
  }

  const closePopup = (id) => setPopups((prev) => prev.filter((p) => p.id !== id))

  const focusPopup = (id) => {
    if (popups.find((p) => p.id === id)?.zIndex === nextZIndex - 1) return
    setPopups((prev) => prev.map((p) => (p.id === id ? { ...p, zIndex: nextZIndex } : p)))
    setNextZIndex((z) => z + 1)
  }

  const updatePopup = (id, patch) => {
    setPopups((prev) => prev.map((p) => (p.id === id ? { ...p, ...patch } : p)))
  }

  return (
    <div style={{ position: 'relative', width: '100vw', height: '100vh', overflow: 'hidden', background: '#0b0d12' }}>
      <div style={{ position: 'absolute', top: 12, left: 12, zIndex: 100000 }}>
        <button
          onClick={addWindow}
          title="Nowe okno wykresu"
          style={{ padding: '8px 14px', fontSize: 14, cursor: 'pointer', borderRadius: 6, border: '1px solid #3a3f4d', background: '#1e212b', color: '#e4e6ec' }}
        >
          + Nowe okno
        </button>
      </div>

      {windows.map((w) => {
        // #202: maximized fills the viewport; minimized collapses to just
        // the title bar at its current x/y. Both bypass react-rnd's own
        // size/position so they can't be dragged/resized while in that state.
        const effectiveSize = w.maximized
          ? { width: '100vw', height: '100vh' }
          : { width: w.width, height: w.minimized ? HEADER_HEIGHT : w.height }
        const effectivePosition = w.maximized ? { x: 0, y: 0 } : { x: w.x, y: w.y }

        return (
        <Rnd
          key={w.id}
          size={effectiveSize}
          position={effectivePosition}
          minWidth={MIN_WINDOW_SIZE.width}
          minHeight={w.minimized ? HEADER_HEIGHT : MIN_WINDOW_SIZE.height}
          bounds="parent"
          dragHandleClassName="chart-window-drag-handle"
          disableDragging={w.maximized}
          enableResizing={!w.maximized && !w.minimized}
          style={{ zIndex: w.zIndex }}
          onDragStart={() => focusWindow(w.id)}
          onDrag={(_e, d) => {
            // #202: flag movement as soon as it happens (not in onDragStop)
            // so the flag is already set before the native click that
            // follows mouseup — see the header onClick handler below.
            if (d.x !== w.x || d.y !== w.y) suppressHeaderClick.add(w.id)
          }}
          onDragStop={(_e, d) => updateWindow(w.id, { x: d.x, y: d.y })}
          onResizeStart={() => focusWindow(w.id)}
          onResizeStop={(_e, _dir, ref, _delta, pos) => {
            updateWindow(w.id, {
              width: ref.offsetWidth, height: ref.offsetHeight, x: pos.x, y: pos.y,
            })
          }}
          onMouseDown={() => focusWindow(w.id)}
        >
          <div
            style={{
              width: '100%', height: '100%', display: 'flex', flexDirection: 'column',
              border: '1px solid #3a3f4d', borderRadius: w.maximized ? 0 : 6, overflow: 'hidden',
              boxShadow: '0 8px 24px rgba(0,0,0,0.5)',
            }}
          >
            <div
              className="chart-window-drag-handle"
              onClick={(e) => {
                // #202: header click toggles maximize, but only a real click
                // (no drag) — react-rnd's drag handle fires a native click on
                // mouseup even after a real drag, so onDragStop above flags
                // this window's id when it actually moved; swallow exactly
                // one click here in that case.
                if (suppressHeaderClick.has(w.id)) {
                  suppressHeaderClick.delete(w.id)
                  return
                }
                if (e.target === e.currentTarget || e.target.closest('.chart-window-title')) {
                  toggleMaximize(w.id)
                }
              }}
              style={{
                padding: '4px 6px 4px 10px', fontSize: 11, color: '#6b7080', cursor: w.maximized ? 'default' : 'move',
                background: '#1a1d26', borderBottom: '1px solid #3a3f4d', userSelect: 'none',
                flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8,
              }}
            >
              <span className="chart-window-title" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                ⠿ Okno #{w.windowNumber} — przeciągnij, aby przesunąć — klik: {w.maximized ? 'przywróć' : 'maksymalizuj'}
              </span>
              <div style={{ display: 'flex', gap: 4, flexShrink: 0 }}>
                <button
                  onClick={(e) => { e.stopPropagation(); toggleMinimize(w.id) }}
                  title={w.minimized ? 'Przywróć' : 'Minimalizuj'}
                  style={{ padding: '2px 8px', fontSize: 12, cursor: 'pointer', borderRadius: 4, border: '1px solid #3a3f4d', background: '#1e212b', color: '#e4e6ec' }}
                >
                  {w.minimized ? '▢' : '_'}
                </button>
                <button
                  onClick={(e) => { e.stopPropagation(); toggleMaximize(w.id) }}
                  title={w.maximized ? 'Przywróć' : 'Maksymalizuj'}
                  style={{ padding: '2px 8px', fontSize: 12, cursor: 'pointer', borderRadius: 4, border: '1px solid #3a3f4d', background: '#1e212b', color: '#e4e6ec' }}
                >
                  {w.maximized ? '❐' : '□'}
                </button>
                {windows.length > 1 && (
                  <button
                    onClick={(e) => { e.stopPropagation(); closeWindow(w.id) }}
                    title="Zamknij"
                    style={{ padding: '2px 8px', fontSize: 12, cursor: 'pointer', borderRadius: 4, border: '1px solid #3a3f4d', background: '#1e212b', color: '#f0716a' }}
                  >
                    ✕
                  </button>
                )}
              </div>
            </div>
            {!w.minimized && (
              <div style={{ flex: 1, minHeight: 0 }}>
                <ChartWindow
                  windowId={w.windowNumber}
                  prefs={w.prefs}
                  onPrefsChange={(prefs) => updateWindow(w.id, { prefs })}
                  onOpenPopup={openPopup}
                />
              </div>
            )}
          </div>
        </Rnd>
        )
      })}

      {popups.map((p) => (
        <Rnd
          key={p.id}
          size={{ width: p.width, height: p.height }}
          position={{ x: p.x, y: p.y }}
          minWidth={MIN_POPUP_SIZE.width}
          minHeight={MIN_POPUP_SIZE.height}
          bounds="parent"
          dragHandleClassName="analysis-popup-drag-handle"
          style={{ zIndex: p.zIndex }}
          onDragStart={() => focusPopup(p.id)}
          onDragStop={(_e, d) => updatePopup(p.id, { x: d.x, y: d.y })}
          onResizeStart={() => focusPopup(p.id)}
          onResizeStop={(_e, _dir, ref, _delta, pos) => {
            updatePopup(p.id, { width: ref.offsetWidth, height: ref.offsetHeight, x: pos.x, y: pos.y })
          }}
          onMouseDown={() => focusPopup(p.id)}
        >
          <div
            style={{
              width: '100%', height: '100%', display: 'flex', flexDirection: 'column',
              border: `1px solid ${p.isError ? '#f0716a' : '#3a3f4d'}`, borderRadius: 6, overflow: 'hidden',
              boxShadow: '0 8px 24px rgba(0,0,0,0.6)', background: '#14161c',
            }}
          >
            <div
              className="analysis-popup-drag-handle"
              style={{
                padding: '6px 10px', fontSize: 12, color: p.isError ? '#f0716a' : '#e4e6ec', cursor: 'move',
                background: '#1a1d26', borderBottom: '1px solid #3a3f4d', userSelect: 'none',
                flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8,
              }}
            >
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{p.title}</span>
              <button
                onClick={() => closePopup(p.id)}
                title="Zamknij"
                style={{ padding: '2px 8px', fontSize: 12, cursor: 'pointer', borderRadius: 4, border: '1px solid #3a3f4d', background: '#1e212b', color: '#f0716a', flexShrink: 0 }}
              >
                ✕
              </button>
            </div>
            {p.entries ? (
              <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '6px' }}>
                {p.entries.length === 0 && (
                  <div style={{ padding: '8px 6px', fontSize: 13, color: '#6b7080' }}>Brak wcześniejszej rozmowy dla tej strategii.</div>
                )}
                {p.entries.map((entry, i) => (
                  <button
                    key={i}
                    onClick={() => openPopup({
                      title: `${HISTORY_ROLE_LABELS[entry.role] ?? entry.role} — ${new Date(entry.timestamp).toLocaleString('pl-PL')}`,
                      text: entry.text,
                    })}
                    style={{
                      display: 'block', width: '100%', textAlign: 'left', padding: '8px 10px', marginBottom: 4,
                      fontSize: 13, cursor: 'pointer', borderRadius: 4, border: '1px solid #3a3f4d',
                      background: '#1e212b', color: '#e4e6ec',
                    }}
                  >
                    <div style={{ fontSize: 11, color: '#9ba0b0', marginBottom: 2 }}>
                      {new Date(entry.timestamp).toLocaleString('pl-PL')} · {HISTORY_ROLE_LABELS[entry.role] ?? entry.role}
                    </div>
                    <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{entry.text}</div>
                  </button>
                ))}
              </div>
            ) : (
              <div style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: '10px 12px', fontSize: 16, lineHeight: 1.6, whiteSpace: 'pre-wrap', color: p.isError ? '#f0716a' : '#c8ccd8' }}>
                {p.text}
              </div>
            )}
          </div>
        </Rnd>
      ))}
    </div>
  )
}
