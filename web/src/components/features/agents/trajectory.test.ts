import { describe, expect, it } from 'vitest';

import type { AgentJobEventApi } from '@/lib/api/agents';
import { filterTrajectory, mergeEvents, preview, toSessionLog, toTrajectory } from './trajectory';

function event(over: Partial<AgentJobEventApi> & { id: number }): AgentJobEventApi {
  return {
    attempt_id: 1,
    seq: over.id,
    event_type: 'message',
    payload: {},
    created_at: '2026-08-19T10:00:00.000Z',
    ...over,
  } as AgentJobEventApi;
}

describe('toTrajectory', () => {
  it('keeps the rows the chat view folds away', () => {
    // The chat adapter merges a tool_result into its call and drops the
    // result row. The trajectory is the stored stream, so both survive —
    // that difference is the entire reason this view exists.
    const rows = toTrajectory([
      event({ id: 1, event_type: 'tool_use', payload: { id: 't1', name: 'Bash' } }),
      event({ id: 2, event_type: 'tool_result', payload: { tool_use_id: 't1', content: 'ok' } }),
    ]);

    expect(rows.map((row) => row.eventType)).toEqual(['tool_use', 'tool_result']);
  });

  it('keeps repeated thinking rows instead of collapsing them', () => {
    const rows = toTrajectory([
      event({ id: 1, event_type: 'thinking', payload: { text: 'a' } }),
      event({ id: 2, event_type: 'thinking', payload: { text: 'b' } }),
    ]);

    expect(rows).toHaveLength(2);
  });

  it('renders a raw row rather than dropping what nobody parsed', () => {
    // A Tier-2 runtime's whole output arrives this way. The chat view has no
    // row for it; here it is the point.
    const [row] = toTrajectory([
      event({ id: 1, event_type: 'raw', payload: { line: '{"weird":true}' } }),
    ]);

    expect(row.eventType).toBe('raw');
    expect(row.detail).toContain('weird');
  });

  it('times a call against its own attempt, not another retry of it', () => {
    const rows = toTrajectory([
      event({
        id: 1,
        attempt_id: 1,
        event_type: 'tool_use',
        payload: { id: 'dup', name: 'Bash' },
        created_at: '2026-08-19T10:00:00.000Z',
      }),
      event({
        id: 2,
        attempt_id: 2,
        event_type: 'tool_use',
        payload: { id: 'dup', name: 'Bash' },
        created_at: '2026-08-19T10:00:10.000Z',
      }),
      event({
        id: 3,
        attempt_id: 2,
        event_type: 'tool_result',
        payload: { tool_use_id: 'dup' },
        created_at: '2026-08-19T10:00:12.000Z',
      }),
    ]);

    expect(rows[0].durationMs).toBeUndefined();
    expect(rows[1].durationMs).toBe(2000);
  });

  it('leaves a call that never returned without a duration', () => {
    const [row] = toTrajectory([
      event({ id: 1, event_type: 'tool_use', payload: { id: 't1', name: 'Read' } }),
    ]);

    expect(row.durationMs).toBeUndefined();
  });

  it('sorts events into lanes so a long turn has a shape', () => {
    const rows = toTrajectory([
      event({ id: 1, event_type: 'message' }),
      event({ id: 2, event_type: 'tool_use', payload: { id: 't', name: 'Bash' } }),
      event({ id: 3, event_type: 'lifecycle', payload: { phase: 'succeeded' } }),
    ]);

    expect(rows.map((row) => row.lane)).toEqual(['model', 'tools', 'other']);
    expect(rows.map((row) => row.actor)).toEqual(['assistant', 'assistant', 'system']);
  });

  it('marks a failed call from either the result flag or a non-zero exit', () => {
    const rows = toTrajectory([
      event({ id: 1, event_type: 'tool_result', payload: { is_error: true } }),
      event({ id: 2, event_type: 'tool_use', payload: { id: 'x', exit_code: 2 } }),
      event({ id: 3, event_type: 'tool_use', payload: { id: 'y', exit_code: 0 } }),
    ]);

    expect(rows.map((row) => row.isError)).toEqual([true, true, false]);
  });
});

describe('preview', () => {
  it('collapses whitespace so a row stays one line', () => {
    expect(preview('a\n\n  b')).toBe('a b');
  });

  it('truncates rather than letting one row push the others off', () => {
    expect(preview('x'.repeat(300))).toHaveLength(160);
  });

  it('renders a structured payload rather than [object Object]', () => {
    expect(preview({ a: 1 })).toBe('{"a":1}');
  });
});

describe('filterTrajectory', () => {
  const rows = toTrajectory([
    event({
      id: 1,
      event_type: 'tool_use',
      payload: { id: 't1', name: 'Bash', detail: 'cargo build' },
    }),
    event({ id: 2, event_type: 'message', payload: { text: 'done' } }),
  ]);

  it('matches what the reader can actually see on the row', () => {
    expect(filterTrajectory(rows, 'cargo').map((row) => row.id)).toEqual([1]);
    expect(filterTrajectory(rows, 'BASH').map((row) => row.id)).toEqual([1]);
    expect(filterTrajectory(rows, 'message').map((row) => row.id)).toEqual([2]);
  });

  it('returns everything for an empty query', () => {
    expect(filterTrajectory(rows, '   ')).toHaveLength(2);
  });
});

describe('toSessionLog', () => {
  it('exports the stored rows verbatim, one JSON object per line', () => {
    const events = [event({ id: 1 }), event({ id: 2, event_type: 'raw' })];

    const lines = toSessionLog(events).split('\n');

    expect(lines).toHaveLength(2);
    expect(JSON.parse(lines[1]).event_type).toBe('raw');
  });
});

describe('mergeEvents', () => {
  it('keeps the history row for an id the live stream repeats', () => {
    // The history read carries the turn label; the live stream cannot know
    // it. Both describe the same append-only log, so identity settles it.
    const history = [{ ...event({ id: 1 }), job_id: 'ajob_1', turn_no: 1 }] as never[];
    const live = [event({ id: 1 })];

    const merged = mergeEvents(history, live);

    expect(merged).toHaveLength(1);
    expect((merged[0] as { turn_no?: number }).turn_no).toBe(1);
  });

  it('restores the order the platform appended them in', () => {
    const history = [{ ...event({ id: 5 }), job_id: 'a', turn_no: 1 }] as never[];
    const live = [event({ id: 2 }), event({ id: 9 })];

    expect(mergeEvents(history, live).map((row) => row.id)).toEqual([2, 5, 9]);
  });

  it('shows a still-streaming turn the history snapshot missed', () => {
    // The fetch is a snapshot taken when the view opened; the running turn
    // keeps producing rows after it.
    const history = [{ ...event({ id: 1 }), job_id: 'a', turn_no: 1 }] as never[];
    const live = [event({ id: 1 }), event({ id: 2 })];

    expect(mergeEvents(history, live).map((row) => row.id)).toEqual([1, 2]);
  });
});

describe('turn labelling', () => {
  it('carries the turn each row belongs to, so history is attributable', () => {
    const rows = toTrajectory([
      {
        ...event({ id: 1, event_type: 'tool_use', payload: { id: 't', name: 'Bash' } }),
        job_id: 'a',
        turn_no: 1,
      },
      { ...event({ id: 2 }), job_id: 'b', turn_no: 2 },
    ] as never[]);

    expect(rows.map((row) => row.turnNo)).toEqual([1, 2]);
  });

  it('defaults to turn 1 for a per-job read, which carries no label', () => {
    expect(toTrajectory([event({ id: 1 })])[0].turnNo).toBe(1);
  });
});
