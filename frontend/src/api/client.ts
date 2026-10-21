import type {
  ChatResponse,
  CreateTicketResponse,
  ListTicketsResponse,
  ServiceStatusResponse,
  TicketDetailResponse,
  UserMeResponse,
} from '../types';

// Same-origin relative API calls (no hardcoded backend host in the client).
export const BACKEND_URL = '';

export async function fetchUserMe(): Promise<UserMeResponse> {
  const res = await fetch(`${BACKEND_URL}/api/user/me`);
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  return (await res.json()) as UserMeResponse;
}

export async function createTicketRequest(): Promise<CreateTicketResponse> {
  const res = await fetch(`${BACKEND_URL}/api/tickets/create`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  return (await res.json()) as CreateTicketResponse;
}

export async function fetchTicketsRequest(): Promise<ListTicketsResponse> {
  const res = await fetch(`${BACKEND_URL}/api/tickets`);
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  return (await res.json()) as ListTicketsResponse;
}

export async function fetchTicketDetailRequest(ticketId: string): Promise<TicketDetailResponse> {
  const res = await fetch(`${BACKEND_URL}/api/tickets/${encodeURIComponent(ticketId)}`);
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  return (await res.json()) as TicketDetailResponse;
}

export async function fetchStatusRequest(): Promise<ServiceStatusResponse> {
  const res = await fetch(`${BACKEND_URL}/api/status`);
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
  return (await res.json()) as ServiceStatusResponse;
}

export async function postChatRequest(
  message: string,
  sessionId: string | null,
): Promise<{ status: number; ok: boolean; data: ChatResponse }> {
  const chatBody: { message: string; session_id?: string } = { message };
  if (sessionId) {
    chatBody.session_id = sessionId;
  }
  const res = await fetch(`${BACKEND_URL}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(chatBody),
  });
  const data = (await res.json()) as ChatResponse;
  return { status: res.status, ok: res.ok, data };
}

export async function postResolveTicketRequest(ticketId: string): Promise<void> {
  const res = await fetch(`${BACKEND_URL}/api/tickets/${encodeURIComponent(ticketId)}/resolve`, {
    method: 'POST',
  });
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }
}
