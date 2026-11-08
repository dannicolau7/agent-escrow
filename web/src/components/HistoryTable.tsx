import type { EscrowAction } from '@/types'

const RISK_BADGE: Record<string, string> = {
  CRITICAL: 'border-red-700 bg-red-950 text-red-300',
  HIGH:     'border-amber-600 bg-amber-950 text-amber-300',
  MEDIUM:   'border-blue-700 bg-blue-950 text-blue-300',
  LOW:      'border-blue-800 bg-blue-950 text-blue-400',
}

function relTime(iso: string | null): string {
  if (!iso) return '–'
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 5)    return 'just now'
  if (s < 60)   return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return new Date(iso).toLocaleTimeString()
}

export default function HistoryTable({ items }: { items: EscrowAction[] }) {
  if (items.length === 0) {
    return (
      <div className="rounded-lg border border-gray-800 p-6 text-center text-gray-600 text-sm">
        No history yet.
      </div>
    )
  }

  return (
    <div className="rounded-lg border border-gray-800 overflow-hidden">
      <table className="w-full text-sm">
        <thead className="bg-gray-900">
          <tr className="text-gray-500 text-left text-xs uppercase tracking-wide">
            <th className="px-4 py-3 font-medium">Time</th>
            <th className="px-4 py-3 font-medium">Tool</th>
            <th className="px-4 py-3 font-medium">Risk</th>
            <th className="px-4 py-3 font-medium">Decision</th>
            <th className="px-4 py-3 font-medium">Note</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => {
            const approved = item.status === 'APPROVED'
            const riskCls  = RISK_BADGE[item.risk_level] ?? 'border-gray-700 bg-gray-900 text-gray-300'
            return (
              <tr key={item.id} className="border-t border-gray-800 hover:bg-gray-900/40 transition-colors">
                <td className="px-4 py-3 text-gray-500 text-xs whitespace-nowrap">
                  {relTime(item.resolved_at)}
                </td>
                <td className="px-4 py-3 text-white">{item.tool_name}</td>
                <td className="px-4 py-3">
                  <span className={`inline-block border text-xs font-bold px-2 py-0.5 rounded ${riskCls}`}>
                    {item.risk_level}
                  </span>
                </td>
                <td className={`px-4 py-3 font-semibold ${approved ? 'text-green-400' : 'text-red-400'}`}>
                  {item.status}
                </td>
                <td className="px-4 py-3 text-gray-500 text-xs max-w-xs truncate">
                  {item.rejection_reason || '–'}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
