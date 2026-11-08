import type { Metadata } from 'next'
import './globals.css'

export const metadata: Metadata = {
  title: 'AgentEscrow — Reviewer',
  description: 'Human-in-the-loop triage dashboard for autonomous agent tool calls',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-gray-950 text-gray-100 min-h-screen antialiased font-sans">
        {children}
      </body>
    </html>
  )
}
