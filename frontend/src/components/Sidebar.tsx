export interface SidebarProps {
  citations: string[] | null;
}

function getCitationIcon(citation: string): string {
  if (
    citation.startsWith('Document #') ||
    citation.startsWith('Doc:') ||
    citation.startsWith('Datastore Doc')
  ) {
    return '📄';
  }
  if (citation.startsWith('Datastore Search:') || citation.startsWith('Web Search:')) {
    return '🔍';
  }
  return '📌';
}

export function Sidebar({ citations }: SidebarProps) {
  return (
    <div className="sidebar">
      <div>
        <h2>🔀 Ticket Lifecycle</h2>
        <div className="trace-card">
          <div>
            <b>1. Customer Session Active</b>
          </div>
          <div>
            <b>2. Sentiment Evaluation</b>
          </div>
          <div>
            <b>3. Status Tracking</b>:
          </div>
          <div style={{ paddingLeft: '10px', color: 'var(--google-green)' }}>
            • Neutral ➔ NEW / IN_PROGRESS
          </div>
          <div style={{ paddingLeft: '10px', color: 'var(--google-red)' }}>
            • Angry ➔ ESCALATED
          </div>
          <div style={{ paddingLeft: '10px', color: 'var(--text-secondary)' }}>
            • Resolved ➔ RESOLVED
          </div>
        </div>
      </div>

      <div>
        <h2>📌 Active Citation Sources</h2>
        <div id="citationsList">
          {citations === null ? (
            <div
              style={{
                color: 'var(--text-secondary)',
                fontSize: '0.8rem',
                fontStyle: 'italic',
              }}
            >
              Retrieved reference citations will appear here for factual queries.
            </div>
          ) : citations.length === 0 ? (
            <div style={{ color: 'var(--text-secondary)', fontSize: '0.8rem' }}>
              No citations.
            </div>
          ) : (
            citations.map((c, idx) => (
              <div key={`${c}-${idx}`} className="citation-item">
                <span style={{ marginRight: '6px' }}>{getCitationIcon(c)}</span>
                <span>{c}</span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
