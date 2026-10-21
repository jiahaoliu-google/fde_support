import type { TicketStatus, TicketSummary } from '../types';

export interface HeaderBarProps {
  customerId: string;
  authFailed: boolean;
  currentSessionId: string | null;
  currentTicketStatus: TicketStatus;
  cachedPreviousTickets: TicketSummary[];
  isResolved: boolean;
  backendStatus: 'checking' | 'online' | 'offline' | 'error';
  onSelectSession: (ticketId: string) => void;
  onResolveTicket: () => void;
  onStartNewTicket: () => void;
  onLogoutSwitchUser: () => void;
}

export interface SessionOptionItem {
  value: string;
  label: string;
  disabled: boolean;
}

export function renderSessionSelect(
  currentSessionId: string | null,
  currentTicketStatus: TicketStatus,
  cachedPreviousTickets: TicketSummary[],
): SessionOptionItem[] {
  const currentInList = Boolean(
    currentSessionId && cachedPreviousTickets.some((t) => t.id === currentSessionId),
  );
  const options: SessionOptionItem[] = [];

  if (!currentInList) {
    const shortCurrent = currentSessionId
      ? `${String(currentSessionId).substring(0, 8)}...`
      : 'pending';
    options.push({
      value: currentSessionId || '',
      label: `🎫 Ticket #${shortCurrent} [${currentTicketStatus}]`,
      disabled: false,
    });
  }

  cachedPreviousTickets.forEach((t) => {
    const isCurrent = Boolean(currentSessionId && t.id === currentSessionId);
    const shortId = String(t.id || '').substring(0, 8);
    const previewText = t.preview ? ` — ${t.preview}` : '';
    const statusLabel = isCurrent ? currentTicketStatus || t.current_status : t.current_status;
    const isExpired = Boolean(t.session_expired || statusLabel === 'EXPIRED');

    options.push({
      value: t.id,
      disabled: isExpired,
      label: isExpired
        ? `🎫 Ticket #${shortId} [EXPIRED] (${t.current_status})${previewText}`
        : `🎫 Ticket #${shortId} [${statusLabel}]${previewText}`,
    });
  });

  return options;
}

export function HeaderBar({
  customerId,
  authFailed,
  currentSessionId,
  currentTicketStatus,
  cachedPreviousTickets,
  isResolved,
  backendStatus,
  onSelectSession,
  onResolveTicket,
  onStartNewTicket,
  onLogoutSwitchUser,
}: HeaderBarProps) {
  const sessionOptions = renderSessionSelect(
    currentSessionId,
    currentTicketStatus,
    cachedPreviousTickets,
  );

  const userBadgeText = authFailed
    ? '👤 Not signed in'
    : customerId
      ? `👤 User: ${customerId}`
      : '👤 User: Authenticating...';

  const statusText =
    backendStatus === 'checking'
      ? 'Checking Backend...'
      : backendStatus === 'online'
        ? 'Backend: Online'
        : backendStatus === 'offline'
          ? 'Backend: Offline'
          : 'Backend Offline';

  return (
    <div className="header-bar">
      <div className="title-group">
        <h1>
          <span className="google-g-dot" aria-hidden="true">
            <span style={{ background: '#4285f4' }} />
            <span style={{ background: '#ea4335' }} />
            <span style={{ background: '#fbbc05' }} />
            <span style={{ background: '#34a853' }} />
          </span>
          🛡️ FDE Support Portal
        </h1>
        <p>Smart Ticket Router &amp; Grounded Knowledge Assistant</p>
      </div>

      <div className="header-controls">
        <span className="user-badge" id="userBadge">
          {userBadgeText}
        </span>

        <select
          className="session-select"
          id="sessionSelect"
          value={currentSessionId || ''}
          onChange={(e) => onSelectSession(e.target.value)}
          aria-label="Current ticket and previous sessions"
          title="Current ticket session — click to switch to a previous session"
        >
          {sessionOptions.map((opt, idx) => (
            <option key={`${opt.value || 'pending'}-${idx}`} value={opt.value} disabled={opt.disabled}>
              {opt.label}
            </option>
          ))}
        </select>

        <button
          type="button"
          className="btn-resolve"
          onClick={onResolveTicket}
          id="resolveBtn"
          disabled={isResolved}
        >
          Resolve Ticket
        </button>

        <button
          type="button"
          className="btn-new-ticket"
          onClick={onStartNewTicket}
          id="newTicketBtn"
        >
          + Start New Ticket
        </button>

        <button
          type="button"
          className="btn-logout"
          onClick={onLogoutSwitchUser}
          id="logoutBtn"
          title="Clear IAP session and switch Google user"
        >
          🚪 Sign Out / Switch User
        </button>
      </div>

      <div className="service-status">
        <span
          className={`badge-dot${backendStatus === 'online' ? ' online' : ''}`}
          id="statusDot"
        />
        <span id="statusText">{statusText}</span>
      </div>
    </div>
  );
}
