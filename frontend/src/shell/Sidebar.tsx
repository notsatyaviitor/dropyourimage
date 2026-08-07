/**
 * Product sidebar, ported from the design prototype.
 *
 * Every item except "AI Copilot" is **inert and visibly disabled**. They are kept because they show
 * where this POC sits inside the real platform, which is useful in a stakeholder demo — but a nav
 * item that looks clickable and does nothing reads as a broken build, and one that looks functional
 * implies a backend that does not exist. So they carry `disabled`, a "not in this POC" tooltip, and
 * a muted style. See frontend/CLAUDE.md: guard-rail states must be real UI, never silent.
 */

const NOT_IN_POC = 'Part of the full platform — not built in this POC'

function DashboardIcon() {
  return (
    <svg className="nav-icon" viewBox="0 0 18 18" fill="currentColor" aria-hidden="true">
      <rect x="1" y="1" width="7" height="7" rx="1.5" opacity=".85" />
      <rect x="10" y="1" width="7" height="7" rx="1.5" opacity=".85" />
      <rect x="1" y="10" width="7" height="7" rx="1.5" opacity=".85" />
      <rect x="10" y="10" width="7" height="7" rx="1.5" opacity=".85" />
    </svg>
  )
}

function OrdersIcon() {
  return (
    <svg className="nav-icon" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M9 2L1.5 5.5V12L9 16L16.5 12V5.5Z" />
      <polyline points="1.5,5.5 9,9 16.5,5.5" />
      <line x1="9" y1="9" x2="9" y2="16" />
    </svg>
  )
}

function SpecsIcon() {
  return (
    <svg className="nav-icon" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
      <line x1="2" y1="5" x2="16" y2="5" />
      <circle cx="6" cy="5" r="2.5" fill="currentColor" stroke="none" />
      <line x1="2" y1="13" x2="16" y2="13" />
      <circle cx="12" cy="13" r="2.5" fill="currentColor" stroke="none" />
    </svg>
  )
}

function InvoicesIcon() {
  return (
    <svg className="nav-icon" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 2H11L15 6V16H4V2Z" />
      <polyline points="11,2 11,6 15,6" />
      <line x1="6.5" y1="9.5" x2="12.5" y2="9.5" />
      <line x1="6.5" y1="12.5" x2="10" y2="12.5" />
    </svg>
  )
}

function AccountIcon() {
  return (
    <svg className="nav-icon" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" aria-hidden="true">
      <circle cx="9" cy="6" r="3.5" />
      <path d="M1.5 17C1.5 13.2 4.9 10.5 9 10.5C13.1 10.5 16.5 13.2 16.5 17" />
    </svg>
  )
}

function CopilotIcon() {
  return (
    <svg className="nav-icon" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M9 1.5L10.9 7H16.5L11.8 10.3L13.6 15.8L9 12.5L4.4 15.8L6.2 10.3L1.5 7H7.1Z" />
    </svg>
  )
}

function HelpIcon() {
  return (
    <svg className="nav-icon" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" aria-hidden="true">
      <circle cx="9" cy="9" r="7.5" />
      <path d="M7 7C7 5.2 10.5 5 10.5 7.5C10.5 9.2 9 9.5 9 11.5" />
      <circle cx="9" cy="13.8" r=".7" fill="currentColor" stroke="none" />
    </svg>
  )
}

const INERT_ITEMS = [
  { label: 'Dashboard', Icon: DashboardIcon },
  { label: 'Orders', Icon: OrdersIcon },
  { label: 'Specifications', Icon: SpecsIcon },
  { label: 'Invoices', Icon: InvoicesIcon },
  { label: 'Account', Icon: AccountIcon },
]

export function Sidebar({ onCreateOrder }: { onCreateOrder: () => void }) {
  return (
    <aside className="sidebar">
      <div className="sidebar-brand">
        <svg className="brand-drop" viewBox="0 0 26 30" fill="none" aria-hidden="true">
          <path
            d="M13 0C13 0 1.5 11.5 1.5 18.5C1.5 24.8 6.7 30 13 30C19.3 30 24.5 24.8 24.5 18.5C24.5 11.5 13 0 13 0Z"
            fill="white"
          />
        </svg>
        <span className="brand-name">DROPYOURIMAGE</span>
      </div>

      <div className="sidebar-create">
        <button type="button" className="btn-create-order" onClick={onCreateOrder}>
          create order
        </button>
      </div>

      <nav className="sidebar-nav">
        {INERT_ITEMS.map(({ label, Icon }) => (
          <button key={label} type="button" className="nav-item nav-item-inert" disabled title={NOT_IN_POC}>
            <Icon />
            {label}
            <span className="nav-poc-tag">n/a</span>
          </button>
        ))}

        <button type="button" className="nav-item ai-item active">
          <CopilotIcon />
          AI Copilot
          <span className="ai-new-badge">New</span>
        </button>

        <button type="button" className="nav-item nav-item-inert" disabled title={NOT_IN_POC}>
          <HelpIcon />
          Help / Contact
          <span className="nav-poc-tag">n/a</span>
        </button>
      </nav>

      <p className="sidebar-poc-note">
        Only <strong>AI Copilot</strong> is built in this POC. The other sections show where it fits
        in the platform.
      </p>
    </aside>
  )
}
