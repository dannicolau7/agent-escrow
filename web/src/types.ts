export interface EscrowAction {
  id: string
  rpc_id: string
  tool_name: string
  category: string
  risk_level: 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW'
  reason: string | null
  raw_arguments: Record<string, unknown>
  status: string
  rejection_reason: string | null
  submitted_by: string | null
  resolved_by: string | null
  created_at: string
  resolved_at: string | null
  execution_status?: 'SUCCEEDED' | 'FAILED' | null
  execution_result?: string | null
  executed_at?: string | null
}
