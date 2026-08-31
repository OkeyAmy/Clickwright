import { useState } from 'react';
import { api, artifactUrl } from '../api';
import type { Approval } from '../types';

/**
 * Where a run waits for a person. Three things end up here: a call held before
 * it starts, an agent stopped with its finger over an irreversible button, and
 * an agent asking for something only a human has — a code sent to a phone, a
 * choice between options, a detail the task never supplied.
 */
export function Approvals({
  approvals,
  error,
  onChange,
}: {
  approvals: Approval[] | null;
  error: string | null;
  onChange: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});

  if (error) return <div className="panel"><div className="empty"><p>{error}</p></div></div>;
  if (!approvals) return <div className="panel"><div className="empty"><p className="dim">Loading…</p></div></div>;

  if (approvals.length === 0) {
    return (
      <div className="panel">
        <div className="empty">
          <p>Nothing is waiting on a human.</p>
          <p className="dim">
            A run stops here when it reaches something it should not decide alone — an action it
            cannot undo, or a value only you have.
          </p>
        </div>
      </div>
    );
  }

  async function decide(id: string, decision: 'approve' | 'deny') {
    setBusy(id);
    try {
      await api.decideApproval(id, decision);
    } finally {
      setBusy(null);
      onChange();
    }
  }

  async function answer(id: string) {
    const value = (answers[id] ?? '').trim();
    if (!value) return;
    setBusy(id);
    try {
      await api.answerApproval(id, value);
      setAnswers((current) => ({ ...current, [id]: '' }));
    } finally {
      setBusy(null);
      onChange();
    }
  }

  return (
    <div className="panel">
      <div className="panel-head">
        <span className="panel-label">Waiting on you</span>
        <span className="mono dim">{approvals.length} pending</span>
      </div>
{approvals.map((approval) => {
              const asking = approval.kind === 'input_needed';
              return (
                <div className={`approval approval-card`} key={approval.id}>
                  <div className="row">
                    <span className={asking ? 'tag tag-warm' : 'tag tag-danger'}>
                      {asking ? 'needs an answer' : approval.kind === 'in_run' ? 'paused mid-run' : 'held'}
                    </span>
                    <span className="mono faint">{approval.connector_id}</span>
                    <span className="mono faint">run {approval.run_id}</span>
                  </div>

                  <p className="approval-reason" style={{ margin: 0 }}>{approval.reason}</p>

                  {approval.screenshot && approval.run_id && (
                    <img
                      src={artifactUrl(approval.run_id, approval.screenshot)}
                      alt="What the agent is looking at"
                      style={{ maxWidth: '100%', borderRadius: 6, border: '1px solid var(--line-soft)' }}
                    />
                  )}

                  {approval.target && (
                    <p className="dim" style={{ margin: 0, fontSize: '.78rem' }}>
                      on <span className="mono">{approval.target}</span>
                    </p>
                  )}

                  {!asking && Object.keys(approval.payload ?? {}).length > 0 && (
                    <pre className="payload">{JSON.stringify(approval.payload, null, 2)}</pre>
                  )}

                  {asking ? (
                    <div className="row">
                      <input
                        aria-label={approval.reason}
                        value={answers[approval.id] ?? ''}
                        onChange={(e) => setAnswers({ ...answers, [approval.id]: e.target.value })}
                        onKeyDown={(e) => e.key === 'Enter' && answer(approval.id)}
                        placeholder="Type the answer and press Enter"
                        style={{ flex: 1, minWidth: 200 }}
                      />
                      <button
                        className="btn btn-primary btn-sm"
                        disabled={busy === approval.id || !(answers[approval.id] ?? '').trim()}
                        onClick={() => answer(approval.id)}
                      >
                        Send to the agent
                      </button>
                    </div>
                  ) : (
                    <div className="row">
                      <button
                        className="btn btn-primary btn-sm"
                        disabled={busy === approval.id}
                        onClick={() => decide(approval.id, 'approve')}
                      >
                        {approval.kind === 'in_run' ? 'Let it continue' : 'Approve and run'}
                      </button>
                      <button
                        className="btn btn-danger btn-sm"
                        disabled={busy === approval.id}
                        onClick={() => decide(approval.id, 'deny')}
                      >
                        Deny
                      </button>
                    </div>
                  )}
                </div>
              );
            })}
    </div>
  );
}
