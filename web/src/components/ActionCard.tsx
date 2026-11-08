'use client'

import type { RefObject } from 'react'
import type { EscrowAction } from '@/types'

// PRD-specified colors: CRITICAL=Red, HIGH=Amber, MEDIUM=Blue, LOW=Blue
const RISK_BADGE: Record<string, string> = {
  CRITICAL: 'border-red-700 bg-red-950 text-red-300',
  HIGH:     'border-amber-600 bg-amber-950 text-amber-300',
  MEDIUM:   'border-blue-700 bg-blue-950 text-blue-300',
  LOW:      'border-blue-800 bg-blue-950 text-blue-400',
}

const CATEGORY_COLOR: Record<string, string> = {
  DATABASE:      'text-blue-400',
  FINANCIAL:     'text-amber-400',
  COMMUNICATION: 'text-purple-400',
  GENERIC_WRITE: 'text-gray-400',
  READ:          'text-green-400',
}

function relTime(iso: string): string {
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 5)    return 'just now'
  if (s < 60)   return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return new Date(iso).toLocaleTimeString()
}

interface Props {
  action: EscrowAction
  isFocused: boolean
  isRejecting: boolean
  rejectReason: string
  rejectInputRef?: RefObject<HTMLInputElement>
  onFocus: () => void
  onApprove: () => void
  onRejectStart: () => void
  onRejectConfirm: (reason: string) => void
  onRejectCancel: () => void
  onRejectReasonChange: (val: string) => void
}

export default function ActionCard({
  action, isFocused, isRejecting, rejectReason, rejectInputRef,
  onFocus, onApprove, onRejectStart, onRejectConfirm, onRejectCancel, onRejectReasonChange,
}: Props) {
  const riskCls = RISK_BADGE[action.risk_level] ?? 'border-gray-700 bg-gray-900 text-gray-300'
  const catCls  = CATEGORY_COLOR[action.category] ?? 'text-gray-400'

  return (
    <div
      onClick={onFocus}
      className={[
        'fade-in border rounded-xl p-5 space-y-4 cursor-pointer transition-all',
        isFocused
          ? 'border-indigo-500 ring-1 ring-indigo-500/30 bg-gray-900'
          : 'border-gray-800 bg-gray-900 hover:border-gray-700',
      ].join(' ')}
    >
      {/* Title row */}
      <div className="flex items-start justify-between gap-3">
        <div className="space-y-1 min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-semibold text-white">{action.tool_name}</span>
            <span className={`inline-block border text-xs font-bold px-2 py-0.5 rounded ${riskCls}`}>
              {action.risk_level}
            </span>
            <span className={`text-xs ${catCls}`}>{action.category}</span>
          </div>
          <p className="text-xs text-gray-500">{relTime(action.created_at)}</p>
        </div>
        {isFocused && (
          <span className="shrink-0 text-xs text-indigo-400 font-medium">focused</span>
        )}
      </div>

      {/* Agent reason */}
      {action.reason && action.reason !== 'No justification provided by agent.' && (
        <p className="text-sm text-gray-300 italic border-l-2 border-gray-700 pl-3">
          &ldquo;{action.reason}&rdquo;
        </p>
      )}

      {/* Arguments accordion */}
      <details className="group text-xs">
        <summary className="cursor-pointer select-none text-gray-500 hover:text-gray-300 transition-colors list-none flex items-center gap-1">
          <svg className="w-3 h-3 transition-transform group-open:rotate-90" fill="currentColor" viewBox="0 0 20 20">
            <path fillRule="evenodd" clipRule="evenodd"
              d="M7.293 4.293a1 1 0 011.414 0l5 5a1 1 0 010 1.414l-5 5a1 1 0 01-1.414-1.414L11.586 10 7.293 5.707a1 1 0 010-1.414z" />
          </svg>
          Arguments
        </summary>
        <pre className="mt-2 bg-gray-950 border border-gray-800 rounded-lg p-3 overflow-x-auto text-gray-300 leading-relaxed">
          {JSON.stringify(action.raw_arguments, null, 2)}
        </pre>
      </details>

      {/* Approve / Reject controls */}
      <div className="flex items-center gap-3 pt-1">
        <button
          onClick={(e) => { e.stopPropagation(); onApprove() }}
          className="flex items-center gap-1.5 px-4 py-2 bg-green-800 hover:bg-green-700 active:bg-green-900 text-green-100 text-sm rounded-lg font-medium transition-colors"
        >
          Approve
          <kbd className="opacity-50 text-xs font-mono bg-green-900 px-1 rounded">J</kbd>
        </button>

        {!isRejecting ? (
          <button
            onClick={(e) => { e.stopPropagation(); onRejectStart() }}
            className="flex items-center gap-1.5 px-4 py-2 bg-red-900 hover:bg-red-800 active:bg-red-950 text-red-200 text-sm rounded-lg font-medium transition-colors"
          >
            Reject
            <kbd className="opacity-50 text-xs font-mono bg-red-950 px-1 rounded">K</kbd>
          </button>
        ) : (
          <div className="flex flex-1 items-center gap-2" onClick={(e) => e.stopPropagation()}>
            <input
              ref={rejectInputRef}
              type="text"
              value={rejectReason}
              onChange={(e) => onRejectReasonChange(e.target.value)}
              placeholder="Reason (optional)"
              className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-red-500 transition-colors"
              onKeyDown={(e) => {
                if (e.key === 'Enter')  onRejectConfirm(rejectReason)
                if (e.key === 'Escape') onRejectCancel()
              }}
            />
            <button
              onClick={() => onRejectConfirm(rejectReason)}
              className="px-3 py-2 bg-red-700 hover:bg-red-600 text-white text-sm rounded-lg font-medium transition-colors"
            >
              Confirm
            </button>
            <button
              onClick={onRejectCancel}
              className="px-3 py-2 bg-gray-700 hover:bg-gray-600 text-white text-sm rounded-lg font-medium transition-colors"
            >
              ✕
            </button>
          </div>
        )}
      </div>
    </div>
  )
}
