'use client';

// The per-turn account: how this turn was continued, how long it took, and
// what it consumed on a machine its owner cannot see.
//
// Two rules run through every line of this file, both from the observability
// design's §11, and both are the reason it is worth showing numbers at all:
//
//   1. A number we did not measure is never rendered as a number. The
//      sampler reports `coverage`, and anything short of a full window is
//      labelled a floor rather than quietly rounded into a total.
//   2. Timings derived from message arrival are labelled approximate. A tool
//      row's window starts when the model asked for the call, which includes
//      the model's own thinking before the tool ever ran, until per-call
//      boundaries land.

import { useMemo, useState } from 'react';

import type { AgentEvent, AgentJob, TurnResources } from './types';

/** One tool call's derived window, paired from the event stream. */
export interface ToolTiming {
  tool: string;
  detail: string;
  ms: number | null;
  isError: boolean;
}

const CONTINUITY_LABEL: Record<string, { text: string; tone: string; title: string }> = {
  hot: {
    text: 'hot',
    tone: 'bg-emerald-50 text-emerald-700 ring-emerald-600/20',
    title:
      'Continued in the live process from the previous turn — the agent kept its full working memory, and the workspace was never rebuilt.',
  },
  warm_native: {
    text: 'warm',
    tone: 'bg-emerald-50 text-emerald-700 ring-emerald-600/20',
    title:
      'Resumed the agent’s own session in the workspace this thread already had — memory and files continuous, the agent process restarted for this turn.',
  },
  cold_native: {
    text: 'cold',
    tone: 'bg-amber-50 text-amber-700 ring-amber-600/20',
    title:
      'Rebuilt in a fresh sandbox from the saved session and patch — the agent’s memory was restored, but anything it had installed by hand was not.',
  },
  replay: {
    text: 'replay',
    tone: 'bg-rose-50 text-rose-700 ring-rose-600/20',
    title:
      'Rebuilt from the written transcript: the agent’s working memory from earlier turns was not available, so it may not recall why it did something. This happens when the session could not be saved or the thread moved hosts.',
  },
};

/**
 * Round to at most one decimal, without printing a pointless ".0".
 *
 * The decimal is kept below 100 because that is where it still carries
 * meaning — "18.2s" of CPU is a different statement from "18s" — and dropped
 * above it, where a tenth of a gigabyte is noise in a footer.
 */
function trim(value: number): string {
  return value >= 100 || Number.isInteger(value) ? String(Math.round(value)) : value.toFixed(1);
}

export function formatDuration(ms: number | null | undefined): string | null {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms < 0) return null;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${trim(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds - minutes * 60);
  return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
}

export function formatBytes(bytes: number | null | undefined): string | null {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return null;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${trim(value)}${units[unit]}`;
}

/** CPU time, reported in the same unit a person would say it in. */
export function formatCpu(usec: number | null | undefined): string | null {
  if (typeof usec !== 'number' || !Number.isFinite(usec) || usec < 0) return null;
  return formatDuration(usec / 1000);
}

/**
 * Pair `tool_use` rows with their `tool_result` to time each call.
 *
 * Message-level by construction: the window opens when the model asked for
 * the call, not when the sandbox began running it, so it carries whatever
 * thinking preceded execution. Unpaired calls (still running, or a stream
 * that ended early) keep their row with a null duration rather than being
 * dropped — a call that never returned is exactly what a reader is looking
 * for.
 */
export function toolTimings(events: AgentEvent[]): ToolTiming[] {
  const results = new Map<string, { at: string | null; isError: boolean }>();
  for (const event of events) {
    if (event.kind === 'tool_result' && event.toolUseId) {
      results.set(event.toolUseId, { at: event.at ?? null, isError: event.isError });
    }
  }
  const timings: ToolTiming[] = [];
  for (const event of events) {
    if (event.kind !== 'tool_use') continue;
    const result = event.id ? results.get(event.id) : undefined;
    const start = event.at ? Date.parse(event.at) : NaN;
    const end = result?.at ? Date.parse(result.at) : NaN;
    const ms =
      Number.isFinite(start) && Number.isFinite(end) && end >= start ? end - start : null;
    timings.push({
      tool: event.tool,
      detail: event.detail,
      ms,
      isError: result?.isError === true || event.outputIsError === true,
    });
  }
  return timings;
}

/** The measured stats, in display order, skipping anything unmeasured. */
function resourceStats(resources: TurnResources): { label: string; value: string }[] {
  const net =
    formatBytes(resources.netRxBytes) || formatBytes(resources.netTxBytes)
      ? `↓${formatBytes(resources.netRxBytes) ?? '—'} ↑${formatBytes(resources.netTxBytes) ?? '—'}`
      : null;
  const io =
    formatBytes(resources.ioReadBytes) || formatBytes(resources.ioWriteBytes)
      ? `r${formatBytes(resources.ioReadBytes) ?? '—'} w${formatBytes(resources.ioWriteBytes) ?? '—'}`
      : null;
  return [
    { label: 'CPU', value: formatCpu(resources.cpuUsec) },
    { label: 'peak mem', value: formatBytes(resources.memPeakBytes) },
    { label: 'net', value: net },
    { label: 'disk', value: io },
  ].filter((stat): stat is { label: string; value: string } => Boolean(stat.value));
}

function ToolTimingTable({ timings }: { timings: ToolTiming[] }) {
  return (
    <table className="w-full text-[12px]">
      <thead>
        <tr className="text-left text-gray-500">
          <th className="py-1 font-medium">Tool</th>
          <th className="py-1 font-medium">Detail</th>
          <th className="py-1 text-right font-medium">Duration</th>
        </tr>
      </thead>
      <tbody>
        {timings.map((timing, index) => (
          <tr key={`${timing.tool}-${index}`} className="border-t border-gray-100">
            <td className="py-1 pr-3 align-top text-gray-800">
              {timing.tool}
              {timing.isError ? <span className="ml-1 text-rose-600">failed</span> : null}
            </td>
            <td className="max-w-0 truncate py-1 pr-3 align-top font-mono text-gray-500">
              {timing.detail}
            </td>
            <td className="py-1 text-right align-top tabular-nums text-gray-700">
              {formatDuration(timing.ms) ?? 'unfinished'}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function TurnFooter({ job }: { job: AgentJob }) {
  const [open, setOpen] = useState(false);
  const timings = useMemo(() => toolTimings(job.events), [job.events]);
  const badge = job.continuityMode ? CONTINUITY_LABEL[job.continuityMode] : undefined;
  const duration = formatDuration(job.durationMs);
  const resources = job.resources;
  const stats = resources ? resourceStats(resources) : [];
  // A sampler that reported at all but covered only part of the window makes
  // every number below a floor. Saying so is the difference between a partial
  // measurement and a wrong one.
  const partial = Boolean(resources && resources.coverage < 0.99 && stats.length > 0);
  const unmeasured = Boolean(resources && stats.length === 0);

  // `unmeasured` counts as something to say: a sampler that ran and could
  // read nothing is a different fact from a turn nobody tried to measure, and
  // only the first of the two has anything to tell the reader.
  if (!badge && !duration && stats.length === 0 && timings.length === 0 && !unmeasured) {
    return null;
  }

  return (
    <section className="mt-3 rounded-lg border border-gray-200 bg-gray-50/60 px-3 py-2 text-[12px]">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-gray-600">
        {badge ? (
          <span
            title={badge.title}
            className={`inline-flex cursor-help items-center rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${badge.tone}`}
          >
            {badge.text}
          </span>
        ) : null}
        {duration ? <span className="tabular-nums">{duration}</span> : null}
        {stats.map((stat) => (
          <span key={stat.label} className="tabular-nums">
            <span className="text-gray-500">{stat.label} </span>
            {stat.value}
          </span>
        ))}
        {partial ? (
          <span
            className="cursor-help text-amber-700"
            title={`Only ${Math.round((resources?.coverage ?? 0) * 100)}% of this turn was sampled, so these are lower bounds rather than totals.`}
          >
            partial
          </span>
        ) : null}
        {unmeasured ? (
          <span
            className="cursor-help text-gray-400"
            title="This turn ran on a host whose resource counters could not be read, so nothing was measured. This is not a measurement of zero."
          >
            resource data unavailable
          </span>
        ) : null}
        {timings.length > 0 ? (
          <button
            type="button"
            onClick={() => setOpen((value) => !value)}
            className="ml-auto text-gray-500 underline-offset-2 hover:text-gray-800 hover:underline"
          >
            {open ? 'Hide' : `${timings.length} tool ${timings.length === 1 ? 'call' : 'calls'}`}
          </button>
        ) : null}
      </div>
      {open && timings.length > 0 ? (
        <div className="mt-2 border-t border-gray-200 pt-2">
          <ToolTimingTable timings={timings} />
          <p className="mt-2 text-[11px] text-gray-500">
            Durations are measured from when the model requested each call to when its result
            arrived, so they include the model’s own thinking time. Per-call execution timing
            arrives with the next step of this work.
          </p>
        </div>
      ) : null}
    </section>
  );
}
