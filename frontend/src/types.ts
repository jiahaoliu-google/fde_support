export type TicketStatus = 'NEW' | 'IN_PROGRESS' | 'ESCALATED' | 'RESOLVED' | 'EXPIRED';
export type SentimentLabel = 'NEUTRAL_FACTUAL' | 'URGENT_FRUSTRATED' | 'UNAVAILABLE';

export interface TicketSummary {
  id: string;
  customer_id?: string;
  current_status: TicketStatus;
  sentiment?: SentimentLabel;
  created_at?: string | null;
  message_count?: number;
  preview?: string;
  session_expired?: boolean;
}

export interface HistoricalMessage {
  id?: number;
  sender: 'CUSTOMER' | 'AGENT' | 'SYSTEM';
  message_text: string;
  created_at?: string | null;
}

export interface TicketDetailResponse {
  ticket?: TicketSummary;
  messages?: HistoricalMessage[];
}

export interface ExtractedEntities {
  order_id?: string | null;
  product_name?: string | null;
  email?: string | null;
}

export interface ChatResponse {
  ticket_id?: string;
  ticket_status?: TicketStatus;
  answer?: string;
  sentiment?: SentimentLabel;
  routed_to?: string;
  extracted_entities?: ExtractedEntities;
  order_id?: string | null;
  product_name?: string | null;
  citations?: string[];
  grounded?: boolean;
  session_id?: string;
  detail?: string;
  error?: string;
}

export interface UserMeResponse {
  user_email?: string;
}

export interface CreateTicketResponse {
  status?: string;
  ticket_id?: string;
  ticket_status?: TicketStatus;
  customer_id?: string;
}

export interface ListTicketsResponse {
  tickets?: TicketSummary[];
}

export interface ServiceStatusResponse {
  backend_service?: {
    status?: string;
  };
}

export type BadgeVariant = 'neutral' | 'urgent' | 'resolved' | 'processing';

export type ChatFeedItem =
  | {
      id: string;
      kind: 'welcome';
      badgeText: string;
      routeText: string;
      customerId?: string;
    }
  | {
      id: string;
      kind: 'user';
      text: string;
    }
  | {
      id: string;
      kind: 'interim';
    }
  | {
      id: string;
      kind: 'notice';
      badgeText: string;
      badgeVariant: BadgeVariant;
      routeText?: string;
      messageText: string;
      italic?: boolean;
    }
  | {
      id: string;
      kind: 'agent';
      badgeText: string;
      badgeVariant: BadgeVariant;
      routeText: string;
      entitySummary?: string;
      answerText: string;
    }
  | {
      id: string;
      kind: 'error';
      errorText: string;
    };
