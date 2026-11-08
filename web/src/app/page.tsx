'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import ActionCard from '@/components/ActionCard'
import HistoryTable from '@/components/HistoryTable'
import type { EscrowAction } from '@/types'

// When served by FastAPI at localhost:4000/ui, relative URLs resolve correctly.
// For standalone Next.js dev (port 3000), set NEXT_PUBLIC_API_URL=http://localhost:4000
const API = process.env.NEXT_PUBLIC_API_URL ?? ''

export default function Dashboard() {
  const [pending, setPending]           = useState<EscrowAction[]>([])
  const [history, setHistory]           = useState<EscrowAction[]>([])
  const [focusedIdx, setFocusedIdx]     = useState(0)
  const [rejectingId, setRejectingId]   = useState<string | null>(null)
  const [rejectReason, setRejectReason] = useState('')
  const [lastRefresh, setLastRefresh]   = useState('')
  const rejectInputRef                  = useRef<HTMLInputElement>(null)

  // -------------------------------------------------------------------------
  // Data fetching
  // -------------------------------------------------------------------------

  const fetchPending = useCallback(async () => {
    try {
      const r = await fetch(`${API}/api/pending`)
      if (!r.ok) return
      setPending(await r.json())
      setLastRefresh(new Date().toLocaleTimeString())
    } catch {}
  }, [])

  const fetchHistory = useCallback(async () => {
    try {
      const r = await fetch(`${API}/api/history?limit=25`)
      if (!r.ok) return
      setHistory(await r.json())
    } catch {}
  }, [])

  useEffect(() => {
    fetchPending()
    fetchHistory()
    const p = setInterval(fetchPending, 3000)
    const h = setInterval(fetchHistory, 5000)
    return () => { clearInterval(p); clearInterval(h) }
  }, [fetchPending, fetchHistory])

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

  const handleApprove = useCallback(async (id: string) => {
    try {
      await fetch(`${API}/api/resolve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action_id: id, decision: 'APPROVED' }),
      })
      setPending(prev => prev.filter(a => a.id !== id))
      fetchHistory()
    } catch {}
  }, [fetchHistory])

  const handleReject = useCallback(async (id: string, reason?: string) => {
    try {
      await fetch(`${API}/api/resolve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action_id: id, decision: 'REJECTED', rejection_reason: reason || null }),
      })
      setPending(prev => prev.filter(a => a.id !== id))
      setRejectingId(null)
      setRejectReason('')
      fetchHistory()
    } catch {}
  }, [fetchHistory])

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
  // Render
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
          <a href="/docs" target="_blank" rel="noopener noreferrer"
            className="hover:text-gray-200 transition-colors">
            API docs ↗
          </a>
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-6 py-10 space-y-12">

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
