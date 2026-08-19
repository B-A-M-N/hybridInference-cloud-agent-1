// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
// No user-event dependency in this repo; fireEvent is the house click.
import { afterEach, describe, expect, it } from 'vitest';

import { TurnFooter, formatBytes, formatCpu, formatDuration, toolTimings } from './TurnFooter';
import type { AgentEvent, AgentJob, TurnResources } from './types';

afterEach(cleanup);

const RESOURCES: TurnResources = {
  cpuUsec: 18_200_000,
  memPeakBytes: 1_288_490_188,
  memAvgBytes: 600_000_000,
  ioReadBytes: 1024,
  ioWriteBytes: 2048,
  netRxBytes: 35_651_584,
  netTxBytes: 2_097_152,
  sampleCount: 42,
  coverage: 1,
};

function job(overrides: Partial<AgentJob> = {}): AgentJob {
  return {
    id: 'ajob_1',
    title: 'a turn',
    state: 'done',
    repo: 'o/r',
    baseSha: 'abc1234',
    branch: 'agent/t',
    runtime: 'claude-code',
    model: 'glm-4.6',
    spentUsd: 0,
    timeoutLabel: '',
    networkSetup: '',
    networkAgent: '',
    sandbox: '',
    attempts: [],
    events: [],
    eventCount: 0,
    diffFiles: [],
    diffLines: [],
    rawLines: [],
    gates: [],
    usage: { tokensIn: '0', tokensOut: '0', cachePct: 0, turns: 0 },
    egressDenials: 0,
    ...overrides,
  };
}

describe('formatters', () => {
  it('reads durations the way a person would say them', () => {
    expect(formatDuration(420)).toBe('420ms');
    expect(formatDuration(4_200)).toBe('4.2s');
    expect(formatDuration(42_000)).toBe('42s');
    expect(formatDuration(125_000)).toBe('2m 5s');
  });

  it('refuses to render a number it was not given', () => {
    expect(formatDuration(null)).toBeNull();
    expect(formatDuration(undefined)).toBeNull();
    expect(formatBytes(null)).toBeNull();
    expect(formatCpu(null)).toBeNull();
  });

  it('scales bytes and cpu time to a readable unit', () => {
    expect(formatBytes(1024)).toBe('1KB');
    expect(formatBytes(1_288_490_188)).toBe('1.2GB');
    expect(formatCpu(18_200_000)).toBe('18.2s');
  });
});

describe('toolTimings', () => {
  const events: AgentEvent[] = [
    {
      kind: 'tool_use',
      tool: 'Bash',
      detail: 'cargo build',
      id: 't1',
      at: '2026-08-19T10:00:00.000Z',
    },
    {
      kind: 'tool_result',
      text: 'ok',
      toolUseId: 't1',
      isError: false,
      at: '2026-08-19T10:00:15.000Z',
    },
    { kind: 'tool_use', tool: 'Read', detail: 'src/main.rs', id: 't2', at: '2026-08-19T10:00:16.000Z' },
  ];

  it('pairs each call with its result to derive a window', () => {
    const [first] = toolTimings(events);
    expect(first.tool).toBe('Bash');
    expect(first.ms).toBe(15_000);
  });

  it('keeps a call that never returned, rather than dropping it', () => {
    const timings = toolTimings(events);
    expect(timings).toHaveLength(2);
    expect(timings[1].ms).toBeNull();
  });

  it('marks a failed call from either the result or the tool row', () => {
    const failed: AgentEvent[] = [
      { kind: 'tool_use', tool: 'Bash', detail: 'exit 1', id: 't9', at: '2026-08-19T10:00:00.000Z' },
      {
        kind: 'tool_result',
        text: 'boom',
        toolUseId: 't9',
        isError: true,
        at: '2026-08-19T10:00:01.000Z',
      },
    ];
    expect(toolTimings(failed)[0].isError).toBe(true);
  });
});

describe('TurnFooter', () => {
  it('shows the continuity rung and what the turn consumed', () => {
    render(<TurnFooter job={job({ continuityMode: 'hot', durationMs: 42_000, resources: RESOURCES })} />);
    expect(screen.getByText('hot')).toBeInTheDocument();
    expect(screen.getByText('42s')).toBeInTheDocument();
    expect(screen.getByText('18.2s')).toBeInTheDocument();
    expect(screen.getByText('1.2GB')).toBeInTheDocument();
  });

  it('explains a replay turn, which is why the badge exists', () => {
    render(<TurnFooter job={job({ continuityMode: 'replay' })} />);
    expect(screen.getByText('replay').getAttribute('title')).toMatch(/working memory/i);
  });

  it('calls a partly sampled turn a floor, not a total', () => {
    render(<TurnFooter job={job({ resources: { ...RESOURCES, coverage: 0.4 } })} />);
    const partial = screen.getByText('partial');
    expect(partial.getAttribute('title')).toMatch(/40% of this turn was sampled/);
  });

  it('says nothing was measured rather than showing zeros', () => {
    const unmeasured: TurnResources = {
      cpuUsec: null,
      memPeakBytes: null,
      memAvgBytes: null,
      ioReadBytes: null,
      ioWriteBytes: null,
      netRxBytes: null,
      netTxBytes: null,
      sampleCount: 0,
      coverage: 0,
    };
    render(<TurnFooter job={job({ resources: unmeasured })} />);
    expect(screen.getByText('resource data unavailable')).toBeInTheDocument();
    expect(screen.queryByText(/^0/)).toBeNull();
  });

  it('renders nothing at all when there is nothing honest to say', () => {
    const { container } = render(<TurnFooter job={job()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('opens the per-tool table and labels its timing as approximate', () => {
    const withTools = job({
      events: [
        {
          kind: 'tool_use',
          tool: 'Bash',
          detail: 'cargo build',
          id: 't1',
          at: '2026-08-19T10:00:00.000Z',
        },
        {
          kind: 'tool_result',
          text: 'ok',
          toolUseId: 't1',
          isError: false,
          at: '2026-08-19T10:00:15.000Z',
        },
      ],
    });
    render(<TurnFooter job={withTools} />);
    fireEvent.click(screen.getByRole('button', { name: '1 tool call' }));
    expect(screen.getByText('cargo build')).toBeInTheDocument();
    expect(screen.getByText('15s')).toBeInTheDocument();
    expect(screen.getByText(/include the model’s own thinking time/)).toBeInTheDocument();
  });
});
