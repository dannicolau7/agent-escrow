'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import ActionCard from '@/components/ActionCard'
import HistoryTable from '@/components/HistoryTable'
import type { EscrowAction } from '@/types'

const API = process.env.NEXT_PUBLIC_API_URL ?? ''
const KEY_STORAGE = 'escrow_api_key'

// null = the user hasn't connected yet; '' = connected without a key (dev mode)
function useApiKey() {
  const [key, setKeyState] = useState<string | null>(null)

  useEffect(() => {
    setKeyState(localStorage.getItem(KEY_STORAGE))
  }, [])

  const setKey = (k: string) => {
    localStorage.setItem(KEY_STORAGE, k)
    setKeyState(k)
  }
  const clearKey = () => {
    localStorage.removeItem(KEY_STORAGE)
    setKeyState(null)
  }
  return { key, setKey, clearKey }
}

function authHeaders(key: string | null): Record<string, string> {
  return key ? { 'X-Api-Key': key } : {}
}

export default function Dashboard() {
  const { key, setKey, clearKey }        = useApiKey()
  const [keyInput, setKeyInput]          = useState('')
  const [authError, setAuthError]        = useState(false)

  const [pending, setPending]           = useState<EscrowAction[]>([])
  const [history, setHistory]           = useState<EscrowAction[]>([])
  const [focusedIdx, setFocusedIdx]     = useState(0)
  const [rejectingId, setRejectingId]   = useState<string | null>(null)
  const [rejectReason, setRejectReason] = useState('')
  const [lastRefresh, setLastRefresh]   = useState('')
  const [actionError, setActionError]   = useState<string | null>(null)
  const [busyId, setBusyId]             = useState<string | null>(null)
  const rejectInputRef                  = useRef<HTMLInputElement>(null)

  // -------------------------------------------------------------------------
  // Data fetching
  // -------------------------------------------------------------------------

  const fetchPending = useCallback(async () => {
    if (authError) return
    try {
      const r = await fetch(`${API}/api/pending`, { headers: authHeaders(key) })
      if (r.status === 401 || r.status === 403) { setAuthError(true); return }
      if (!r.ok) return
      setPending(await r.json())
      setLastRefresh(new Date().toLocaleTimeString())
    } catch {}
  }, [key, authError])

  const fetchHistory = useCallback(async () => {
    if (authError) return
    try {
      const r = await fetch(`${API}/api/history?limit=25`, { headers: authHeaders(key) })
      if (r.status === 401 || r.status === 403) { setAuthError(true); return }
      if (!r.ok) return
      setHistory(await r.json())
    } catch {}
  }, [key, authError])

  useEffect(() => {
    if (authError || key === null) return
    fetchPending()
    fetchHistory()

    let es: EventSource | null = null
    let fallbackTimer: ReturnType<typeof setInterval> | null = null

    const connectSSE = () => {
      const url = key ? `${API}/api/events?api_key=${encodeURIComponent(key)}` : `${API}/api/events`
      es = new EventSource(url)
      es.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data) as { type: string }
          if (msg.type === 'action_added') {
            fetchPending()
            setLastRefresh(new Date().toLocaleTimeString())
          } else if (msg.type === 'action_resolved') {
            fetchPending()
            fetchHistory()
            setLastRefresh(new Date().toLocaleTimeString())
          }
        } catch {}
      }
      es.onopen = () => {
        // Back online: stop the fallback poll and catch up on anything missed.
        if (fallbackTimer) { clearInterval(fallbackTimer); fallbackTimer = null }
        fetchPending()
        fetchHistory()
      }
      es.onerror = () => {
        // EventSource reconnects by itself; poll meanwhile so nothing is missed.
        if (!fallbackTimer) {
          fallbackTimer = setInterval(() => {
            fetchPending()
            fetchHistory()
          }, 5000)
        }
      }
    }

    connectSSE()
    return () => {
      es?.close()
      if (fallbackTimer) clearInterval(fallbackTimer)
    }
  }, [key, authError, fetchPending, fetchHistory])

  // Keep focusedIdx in bounds as list shrinks
  useEffect(() => {
    if (pending.length > 0 && focusedIdx >= pending.length) {
      setFocusedIdx(pending.length - 1)
    }
  }, [pending.length, focusedIdx])

  // Focus the reject input when it appears
  useEffect(() => {
    if (rejectingId) rejectInputRef.current?.focus()
  }, [rejectingId])

  // -------------------------------------------------------------------------
  // Actions
  // -------------------------------------------------------------------------

  // Resolve an action and only remove its card once the server confirms.
  const resolve = useCallback(async (id: string, decision: 'APPROVED' | 'REJECTED', reason?: string) => {
    if (busyId) return
    setBusyId(id)
    setActionError(null)
    try {
      const r = await fetch(`${API}/api/resolve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders(key) },
        body: JSON.stringify({ action_id: id, decision, rejection_reason: reason || null }),
      })
      if (r.status === 404) {
        // Someone else resolved it, or it timed out: just drop the card.
        setActionError('That action was already resolved or has expired.')
      } else if (!r.ok) {
        const err = await r.json().catch(() => ({} as { detail?: string }))
        setActionError(
          r.status === 403
            ? 'Your key is not allowed to approve or reject (approver role required).'
            : `Could not ${decision === 'APPROVED' ? 'approve' : 'reject'}: ${err.detail ?? `HTTP ${r.status}`}`,
        )
        return
      }
      setPending(prev => prev.filter(a => a.id !== id))
      if (decision === 'REJECTED') { setRejectingId(null); setRejectReason('') }
      fetchHistory()
    } catch {
      setActionError('Network error: the decision was not sent.')
    } finally {
      setBusyId(null)
    }
  }, [key, busyId, fetchHistory])

  const handleApprove = useCallback((id: string) => resolve(id, 'APPROVED'), [resolve])
  const handleReject  = useCallback((id: string, reason?: string) => resolve(id, 'REJECTED', reason), [resolve])

  // -------------------------------------------------------------------------
  // Keyboard shortcuts: J = approve, K = reject, ↑↓ = navigate, Esc = cancel
  // -------------------------------------------------------------------------

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement).tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA') return

      if (pending.length === 0) return
      const idx    = Math.min(focusedIdx, pending.length - 1)
      const action = pending[idx]
      if (!action) return

      if (e.key === 'j' || e.key === 'J') {
        e.preventDefault()
        handleApprove(action.id)
      } else if (e.key === 'k' || e.key === 'K') {
        e.preventDefault()
        setRejectingId(action.id)
      } else if (e.key === 'ArrowDown') {
        e.preventDefault()
        setFocusedIdx(i => Math.min(i + 1, pending.length - 1))
      } else if (e.key === 'ArrowUp') {
        e.preventDefault()
        setFocusedIdx(i => Math.max(i - 1, 0))
      } else if (e.key === 'Escape') {
        setRejectingId(null)
        setRejectReason('')
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [pending, focusedIdx, handleApprove])

  // -------------------------------------------------------------------------
  // Auth gate — shown when no key is stored or when a 401/403 occurs
  // -------------------------------------------------------------------------

  if (key === null || authError) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-950">
        <div className="w-full max-w-sm space-y-5 p-8 border border-gray-800 rounded-2xl bg-gray-900">
          <div className="flex items-center gap-2">
            <svg className="w-5 h-5 text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
            </svg>
            <span className="font-semibold text-white">AgentEscrow</span>
          </div>
          {authError && (
            <p className="text-sm text-red-400">Invalid or expired API key.</p>
          )}
          <p className="text-sm text-gray-400">
            Enter your <code className="bg-gray-800 px-1 rounded text-gray-300">ESCROW_API_KEY</code> to continue.
            Leave blank if auth is disabled.
          </p>
          <input
            type="password"
            autoFocus
            value={keyInput}
            onChange={e => setKeyInput(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter') {
                setAuthError(false)
                setKey(keyInput)
              }
            }}
            placeholder="Paste API key…"
            className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-indigo-500 transition-colors"
          />
          <button
            onClick={() => { setAuthError(false); setKey(keyInput) }}
            className="w-full py-2 bg-indigo-600 hover:bg-indigo-500 text-white text-sm rounded-lg font-medium transition-colors"
          >
            Connect
          </button>
        </div>
      </div>
    )
  }

  // -------------------------------------------------------------------------
  // Main dashboard
  // -------------------------------------------------------------------------

  return (
    <div className="min-h-screen">
      {/* Header */}
      <header className="sticky top-0 z-10 border-b border-gray-800 bg-gray-950/95 backdrop-blur px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <svg className="w-5 h-5 text-indigo-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
              d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"
            />
          </svg>
          <span className="font-semibold tracking-tight">AgentEscrow</span>
          {pending.length > 0 && (
            <span className="bg-red-600 text-white text-xs font-bold px-2 py-0.5 rounded-full tabular-nums">
              {pending.length}
            </span>
          )}
        </div>

        <div className="flex items-center gap-5 text-sm text-gray-500">
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse" />
            {lastRefresh ? `updated ${lastRefresh}` : 'connecting…'}
          </span>
          {/* Keyboard hint strip */}
          <span className="hidden sm:flex items-center gap-2 text-xs text-gray-600">
            <span><kbd className="px-1.5 py-0.5 bg-gray-800 rounded border border-gray-700 font-mono">J</kbd> approve</span>
            <span><kbd className="px-1.5 py-0.5 bg-gray-800 rounded border border-gray-700 font-mono">K</kbd> reject</span>
            <span><kbd className="px-1.5 py-0.5 bg-gray-800 rounded border border-gray-700 font-mono">↑↓</kbd> navigate</span>
          </span>
          <button
            onClick={() => { clearKey(); setPending([]); setHistory([]) }}
            className="text-xs text-gray-600 hover:text-gray-400 transition-colors"
            title="Sign out"
          >
            sign out
          </button>
          <a href="/docs" target="_blank" rel="noopener noreferrer"
            className="hover:text-gray-200 transition-colors">
            API docs ↗
          </a>
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-6 py-10 space-y-12">

        {actionError && (
          <div role="alert" className="flex items-start justify-between gap-4 rounded-lg border border-red-800 bg-red-950/60 px-4 py-3 text-sm text-red-300">
            <span>{actionError}</span>
            <button onClick={() => setActionError(null)} className="text-red-400 hover:text-red-200">dismiss</button>
          </div>
        )}

        {/* Pending queue */}
        <section>
          <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-widest mb-5">
            Pending Review
          </h2>
          {pending.length === 0 ? (
            <p className="text-gray-600 text-sm">No pending actions — queue is clear.</p>
          ) : (
            <div className="space-y-4">
              {pending.map((action, idx) => (
                <ActionCard
                  key={action.id}
                  action={action}
                  isFocused={idx === focusedIdx}
                  isRejecting={rejectingId === action.id}
                  rejectReason={rejectingId === action.id ? rejectReason : ''}
                  rejectInputRef={rejectingId === action.id ? rejectInputRef : undefined}
                  onFocus={() => setFocusedIdx(idx)}
                  onApprove={() => handleApprove(action.id)}
                  onRejectStart={() => setRejectingId(action.id)}
                  onRejectConfirm={(reason) => handleReject(action.id, reason)}
                  onRejectCancel={() => { setRejectingId(null); setRejectReason('') }}
                  onRejectReasonChange={setRejectReason}
                />
              ))}
            </div>
          )}
        </section>

        {/* Ledger history */}
        <section>
          <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-widest mb-5">
            Ledger History
          </h2>
          <HistoryTable items={history} />
        </section>

      </main>
    </div>
  )
}
