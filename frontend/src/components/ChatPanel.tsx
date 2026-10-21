import { useEffect, useRef } from 'react';
import type { ChatFeedItem } from '../types';

export interface ChatPanelProps {
  items: ChatFeedItem[];
  inputValue: string;
  isResolved: boolean;
  onInputChange: (val: string) => void;
  onSendQuery: () => void;
}

export function unwrapAnswerText(raw: string | undefined): string {
  let answerText = raw || '';
  if (
    typeof answerText === 'string' &&
    (answerText.trim().startsWith('{') || answerText.includes('```json'))
  ) {
    let cleanStr = answerText.trim();
    if (cleanStr.startsWith('```json')) {
      cleanStr = cleanStr.split('```json')[1].split('```')[0].trim();
    } else if (cleanStr.startsWith('```')) {
      cleanStr = cleanStr.split('```')[1].trim();
    }
    try {
      const parsed = JSON.parse(cleanStr) as { answer?: unknown };
      if (parsed && typeof parsed.answer === 'string') {
        answerText = parsed.answer;
      }
    } catch {
      // Keep original answerText if not valid JSON
    }
  }
  return answerText;
}

export function ChatPanel({
  items,
  inputValue,
  isResolved,
  onInputChange,
  onSendQuery,
}: ChatPanelProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [items]);

  const handleKeyPress = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      onSendQuery();
    }
  };

  return (
    <div className="chat-section">
      <div className="messages-container" id="messagesContainer" ref={containerRef}>
        {items.map((item) => {
          if (item.kind === 'welcome') {
            return (
              <div key={item.id} className="message-bubble agent">
                <div className="agent-card">
                  <div className="routing-header">
                    <span className="badge-tag neutral">{item.badgeText}</span>
                    <span className="route-tag">{item.routeText}</span>
                  </div>
                  <div className="agent-answer" style={{ whiteSpace: 'normal' }}>
                    {item.customerId !== undefined ? (
                      <p>
                        Welcome! Fresh ticket session initialized for customer{' '}
                        <b>{item.customerId}</b>.
                      </p>
                    ) : (
                      <p>Welcome! Submit any customer support inquiry below.</p>
                    )}
                    <ul>
                      <li>
                        <b>Frustrated / Urgent Tickets</b> will be assigned directly to our Senior
                        Human Queue.
                      </li>
                      <li>
                        <b>Factual Questions</b> will be answered right away by our support
                        assistant.
                      </li>
                    </ul>
                  </div>
                </div>
              </div>
            );
          }

          if (item.kind === 'user') {
            return (
              <div key={item.id} className="message-bubble user">
                <div className="user-text">{item.text}</div>
              </div>
            );
          }

          if (item.kind === 'interim') {
            return (
              <div key={item.id} className="message-bubble agent">
                <div className="agent-card interim">
                  <div className="routing-header">
                    <span className="badge-tag processing">⚡ Processing your request...</span>
                  </div>
                  <div
                    className="agent-answer"
                    style={{
                      color: 'var(--google-yellow-text)',
                      fontStyle: 'italic',
                      whiteSpace: 'normal',
                    }}
                  >
                    Our assistant is looking into this and composing a response...
                  </div>
                </div>
              </div>
            );
          }

          if (item.kind === 'notice') {
            return (
              <div key={item.id} className="message-bubble agent">
                <div className="agent-card">
                  <div className="routing-header">
                    <span className={`badge-tag ${item.badgeVariant}`}>{item.badgeText}</span>
                    {item.routeText ? <span className="route-tag">{item.routeText}</span> : null}
                  </div>
                  <div
                    className="agent-answer"
                    style={
                      item.italic
                        ? {
                            color: 'var(--text-secondary)',
                            fontStyle: 'italic',
                            whiteSpace: 'normal',
                          }
                        : { whiteSpace: 'normal' }
                    }
                  >
                    {item.messageText}
                  </div>
                </div>
              </div>
            );
          }

          if (item.kind === 'agent') {
            return (
              <div key={item.id} className="message-bubble agent">
                <div className="agent-card">
                  <div className="routing-header">
                    <span className={`badge-tag ${item.badgeVariant}`}>{item.badgeText}</span>
                    <span className="route-tag">{item.routeText}</span>
                  </div>
                  {item.entitySummary ? (
                    <div className="metadata-box">
                      📌 Extracted Metadata: {item.entitySummary}
                    </div>
                  ) : null}
                  <div className="agent-answer">{item.answerText}</div>
                </div>
              </div>
            );
          }

          return (
            <div key={item.id} className="message-bubble agent">
              <div
                className="agent-card"
                style={{ borderColor: 'var(--google-red-border)' }}
              >
                <div className="agent-answer" style={{ color: 'var(--google-red)' }}>
                  {item.errorText}
                </div>
              </div>
            </div>
          );
        })}
      </div>

      <div className="input-area">
        <input
          type="text"
          id="userInput"
          placeholder="Ask a question or paste a customer ticket (e.g. 'Where is my order #12345?')"
          value={inputValue}
          disabled={isResolved}
          onChange={(e) => onInputChange(e.target.value)}
          onKeyDown={handleKeyPress}
        />
        <button
          type="button"
          id="sendBtn"
          disabled={isResolved}
          onClick={onSendQuery}
        >
          Submit Ticket
        </button>
      </div>
    </div>
  );
}
