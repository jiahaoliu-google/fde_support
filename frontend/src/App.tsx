import { useCallback, useEffect, useRef, useState } from 'react';
import {
  createTicketRequest,
  fetchStatusRequest,
  fetchTicketDetailRequest,
  fetchTicketsRequest,
  fetchUserMe,
  postChatRequest,
  postResolveTicketRequest,
} from './api/client';
import { ChatPanel, unwrapAnswerText } from './components/ChatPanel';
import { HeaderBar } from './components/HeaderBar';
import { Sidebar } from './components/Sidebar';
import type {
  BadgeVariant,
  ChatFeedItem,
  HistoricalMessage,
  TicketStatus,
  TicketSummary,
} from './types';

function createInitialWelcomeItem(): ChatFeedItem {
  return {
    id: 'welcome-initial',
    kind: 'welcome',
    badgeText: '🟢 SYSTEM READY',
    routeText: 'Support Session Active',
  };
}

export function renderHistoricalMessage(
  msg: HistoricalMessage,
  ticket: TicketSummary,
  idx: number,
): ChatFeedItem {
  const itemId = `hist-${ticket.id}-${msg.id ?? idx}`;
  if (msg.sender === 'CUSTOMER') {
    return {
      id: itemId,
      kind: 'user',
      text: msg.message_text || '',
    };
  }

  let badgeVariant: BadgeVariant = 'neutral';
  let badgeText = '🟢 NEUTRAL / FACTUAL';
  if (msg.sender === 'SYSTEM' && (msg.message_text || '').includes('RESOLVED')) {
    badgeVariant = 'resolved';
    badgeText = '🔴 TICKET RESOLVED';
  } else if (ticket.sentiment === 'URGENT_FRUSTRATED') {
    badgeVariant = 'urgent';
    badgeText = '🚨 URGENT / FRUSTRATED';
  }

  const routedLabel =
    msg.sender === 'SYSTEM' ? 'System / Human Queue' : 'Automated RAG Assistant';
  const routeText = `➔ Status: ${ticket.current_status || 'IN_PROGRESS'} | Routed To: ${routedLabel}`;

  return {
    id: itemId,
    kind: 'agent',
    badgeVariant,
    badgeText,
    routeText,
    answerText: msg.message_text || '',
  };
}

export function App() {
  const [customerId, setCustomerId] = useState<string>('');
  const [authFailed, setAuthFailed] = useState<boolean>(false);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [currentTicketStatus, setCurrentTicketStatus] = useState<TicketStatus>('NEW');
  const [cachedPreviousTickets, setCachedPreviousTickets] = useState<TicketSummary[]>([]);
  const [isResolved, setIsResolved] = useState<boolean>(false);
  const [backendStatus, setBackendStatus] = useState<
    'checking' | 'online' | 'offline' | 'error'
  >('checking');
  const [feedItems, setFeedItems] = useState<ChatFeedItem[]>([createInitialWelcomeItem()]);
  const [inputValue, setInputValue] = useState<string>('');
  const [citations, setCitations] = useState<string[] | null>(null);

  const sessionIdRef = useRef<string | null>(null);
  sessionIdRef.current = currentSessionId;

  const updateTicketBadge = useCallback((status: TicketStatus = 'NEW') => {
    setCurrentTicketStatus(status);
  }, []);

  const disableChat = useCallback(() => {
    setIsResolved(true);
  }, []);

  const resetCitationsSidebar = useCallback(() => {
    setCitations(null);
  }, []);

  const createNewTicketInDB = useCallback(async () => {
    try {
      const data = await createTicketRequest();
      if (data.ticket_id) {
        sessionIdRef.current = data.ticket_id;
        setCurrentSessionId(data.ticket_id);
      }
      updateTicketBadge(data.ticket_status || 'NEW');
    } catch {
      console.error('Failed to create ticket in DB');
    }
  }, [updateTicketBadge]);

  const loadPreviousSessions = useCallback(async () => {
    try {
      const data = await fetchTicketsRequest();
      setCachedPreviousTickets(Array.isArray(data.tickets) ? data.tickets : []);
    } catch {
      console.error('Failed to load previous sessions');
    }
  }, []);

  const loadTicketSession = useCallback(
    async (ticketId: string) => {
      try {
        const data = await fetchTicketDetailRequest(ticketId);
        const ticket = data.ticket;
        const messages = Array.isArray(data.messages) ? data.messages : [];

        if (!ticket) {
          throw new Error('Ticket not found');
        }

        resetCitationsSidebar();

        if (ticket.session_expired) {
          sessionIdRef.current = ticket.id;
          setCurrentSessionId(ticket.id);
          updateTicketBadge('EXPIRED');
          disableChat();

          const expiredNotice: ChatFeedItem = {
            id: `notice-expired-${ticket.id}`,
            kind: 'notice',
            badgeText: '⚠️ SESSION EXPIRED',
            badgeVariant: 'resolved',
            routeText: 'Session Expired',
            messageText: `Session #${String(ticket.id).substring(0, 12)}... has expired and cannot be resumed. Click "+ Start New Ticket" to begin a new inquiry.`,
          };
          const histItems = messages.map((m, idx) => renderHistoricalMessage(m, ticket, idx));
          setFeedItems([expiredNotice, ...histItems]);
          return;
        }

        sessionIdRef.current = ticket.id;
        setCurrentSessionId(ticket.id);
        updateTicketBadge(ticket.current_status || 'IN_PROGRESS');

        const restoredNotice: ChatFeedItem = {
          id: `notice-restored-${ticket.id}`,
          kind: 'notice',
          badgeText: '🟢 SESSION RESTORED',
          badgeVariant: 'neutral',
          routeText: `Restored Ticket #${String(ticket.id).substring(0, 8)} (${ticket.current_status})`,
          messageText: 'Previous chat history and agent session context have been restored.',
        };
        const histItems = messages.map((m, idx) => renderHistoricalMessage(m, ticket, idx));
        setFeedItems([restoredNotice, ...histItems]);

        if (ticket.current_status === 'RESOLVED') {
          disableChat();
        } else {
          setIsResolved(false);
        }
      } catch {
        console.error('Failed to load ticket session');
        await createNewTicketInDB();
      }
    },
    [createNewTicketInDB, disableChat, resetCitationsSidebar, updateTicketBadge],
  );

  const selectPreviousSession = useCallback(
    async (ticketId: string) => {
      if (!ticketId || ticketId === sessionIdRef.current) {
        return;
      }
      const url = new URL(window.location.href);
      url.searchParams.set('ticket_id', ticketId);
      window.history.pushState({}, '', url);
      await loadTicketSession(ticketId);
      await loadPreviousSessions();
    },
    [loadPreviousSessions, loadTicketSession],
  );

  const checkStatus = useCallback(async () => {
    try {
      const data = await fetchStatusRequest();
      if (data.backend_service && data.backend_service.status === 'online') {
        setBackendStatus('online');
      } else {
        setBackendStatus('offline');
      }
    } catch {
      setBackendStatus('error');
    }
  }, []);

  useEffect(() => {
    let active = true;

    async function fetchUserInfo() {
      try {
        const data = await fetchUserMe();
        if (!active) return;
        if (data.user_email) {
          setCustomerId(data.user_email);
          setAuthFailed(false);
          const urlTicketId = new URLSearchParams(window.location.search).get('ticket_id');
          if (urlTicketId) {
            await loadTicketSession(urlTicketId);
          } else {
            await createNewTicketInDB();
          }
          await loadPreviousSessions();
        }
      } catch {
        if (!active) return;
        console.error('Failed to fetch user info');
        setAuthFailed(true);
      }
    }

    fetchUserInfo();
    updateTicketBadge('NEW');
    checkStatus();
    const intervalId = window.setInterval(checkStatus, 5000);

    return () => {
      active = false;
      window.clearInterval(intervalId);
    };
  }, [checkStatus, createNewTicketInDB, loadPreviousSessions, loadTicketSession, updateTicketBadge]);

  const sendQuery = async () => {
    if (isResolved) return;
    const text = inputValue.trim();
    if (!text) return;

    const userMsgId = `user-${Date.now()}`;
    const interimId = `interim-${Date.now()}`;

    setInputValue('');
    setFeedItems((prev) => [
      ...prev,
      { id: userMsgId, kind: 'user', text },
      { id: interimId, kind: 'interim' },
    ]);

    try {
      const { status, ok, data } = await postChatRequest(text, sessionIdRef.current);

      if (
        status === 400 &&
        data.detail &&
        (data.detail.includes('RESOLVED') || data.detail.includes('expired'))
      ) {
        const isExp = data.detail.includes('expired');
        const noticeItem: ChatFeedItem = {
          id: `notice-${Date.now()}`,
          kind: 'notice',
          badgeText: isExp ? '⚠️ SESSION EXPIRED' : '🔴 TICKET RESOLVED',
          badgeVariant: 'resolved',
          routeText: isExp ? 'Session Expired' : 'Ticket Closed',
          messageText: data.detail,
        };
        setFeedItems((prev) => [
          ...prev.filter((item) => item.id !== interimId),
          noticeItem,
        ]);
        updateTicketBadge(isExp ? 'EXPIRED' : 'RESOLVED');
        disableChat();
        await loadPreviousSessions();
        return;
      }

      if (!ok) {
        throw new Error(data.detail || data.error || `HTTP ${status}`);
      }

      if (data.ticket_id) {
        sessionIdRef.current = data.ticket_id;
        setCurrentSessionId(data.ticket_id);
      }
      if (data.ticket_status) {
        updateTicketBadge(data.ticket_status);
      }

      const isUrgent = data.sentiment === 'URGENT_FRUSTRATED';
      const badgeVariant: BadgeVariant = isUrgent ? 'urgent' : 'neutral';
      const badgeText = isUrgent ? '🚨 URGENT / FRUSTRATED' : '🟢 NEUTRAL / FACTUAL';

      const entities = data.extracted_entities || {};
      const orderId = entities.order_id || data.order_id;
      const productName = entities.product_name || data.product_name;

      let entityStr = '';
      if (orderId) entityStr += `Order #${orderId} `;
      if (productName) entityStr += `[Product: ${productName}] `;

      const answerText = unwrapAnswerText(data.answer);

      const agentItem: ChatFeedItem = {
        id: `agent-${Date.now()}`,
        kind: 'agent',
        badgeVariant,
        badgeText,
        routeText: `➔ Status: ${data.ticket_status || 'NEW'} | Routed To: ${data.routed_to || ''}`,
        entitySummary: entityStr || undefined,
        answerText,
      };

      setFeedItems((prev) => [
        ...prev.filter((item) => item.id !== interimId),
        agentItem,
      ]);

      if (data.citations && data.citations.length > 0) {
        setCitations(data.citations);
      } else {
        setCitations([]);
      }

      await loadPreviousSessions();
    } catch (err) {
      const errDetail = err instanceof Error && err.message ? ` (${err.message})` : '';
      const errorItem: ChatFeedItem = {
        id: `err-${Date.now()}`,
        kind: 'error',
        errorText: `Error communicating with Smart Router Backend${errDetail}.`,
      };
      setFeedItems((prev) => [
        ...prev.filter((item) => item.id !== interimId),
        errorItem,
      ]);
    }
  };

  const resolveTicket = async () => {
    if (isResolved || !sessionIdRef.current) return;
    try {
      await postResolveTicketRequest(sessionIdRef.current);
      disableChat();
      updateTicketBadge('RESOLVED');
      const resolvedItem: ChatFeedItem = {
        id: `resolved-${Date.now()}`,
        kind: 'notice',
        badgeText: '🔴 TICKET RESOLVED',
        badgeVariant: 'resolved',
        messageText:
          'This ticket has been marked as RESOLVED. Chat is closed. Click "+ Start New Ticket" to submit a new inquiry.',
        italic: true,
      };
      setFeedItems((prev) => [...prev, resolvedItem]);
      await loadPreviousSessions();
    } catch {
      console.error('Failed to resolve ticket');
    }
  };

  const startNewTicket = async () => {
    sessionIdRef.current = null;
    setCurrentSessionId(null);
    setIsResolved(false);

    const url = new URL(window.location.href);
    if (url.searchParams.has('ticket_id')) {
      url.searchParams.delete('ticket_id');
      window.history.pushState({}, '', url);
    }

    setFeedItems([
      {
        id: `welcome-${Date.now()}`,
        kind: 'welcome',
        badgeText: '🟢 NEW TICKET READY',
        routeText: 'Clean Session Active',
        customerId,
      },
    ]);
    resetCitationsSidebar();
    updateTicketBadge('NEW');
    await createNewTicketInDB();
    await loadPreviousSessions();
  };

  const logoutSwitchUser = () => {
    setCustomerId('');
    setCurrentSessionId(null);
    setCachedPreviousTickets([]);
    setFeedItems([]);
    window.location.href = '/_gcp_iap/clear_login_cookie';
  };

  return (
    <>
      <HeaderBar
        customerId={customerId}
        authFailed={authFailed}
        currentSessionId={currentSessionId}
        currentTicketStatus={currentTicketStatus}
        cachedPreviousTickets={cachedPreviousTickets}
        isResolved={isResolved}
        backendStatus={backendStatus}
        onSelectSession={selectPreviousSession}
        onResolveTicket={resolveTicket}
        onStartNewTicket={startNewTicket}
        onLogoutSwitchUser={logoutSwitchUser}
      />

      <div className="app-container">
        <ChatPanel
          items={feedItems}
          inputValue={inputValue}
          isResolved={isResolved}
          onInputChange={setInputValue}
          onSendQuery={sendQuery}
        />
        <Sidebar citations={citations} />
      </div>
    </>
  );
}
