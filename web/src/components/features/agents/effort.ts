import type { AgentConfigApi } from '@/lib/api/agents';

// Which reasoning efforts a (runtime, model) pair can honour.
//
// The list is the model's API vocabulary intersected with what the runtime can
// say, and that is the whole of it: there is no platform-invented "auto" entry
// any more. An Auto option could only mean "we send nothing and let the
// defaults decide", and for claude-code that was measurably a lie — the CLI
// sends `high` whether or not we pass a flag, so Auto and high were the same
// request wearing two names.
//
// The intersection is computed here rather than by either composer, so the two
// cannot disagree about what is on offer — and the backend validates the same
// rule again at create time, because a UI is not a permission.

/**
 * Levels both halves accept, weakest first. Empty means no control belongs on
 * screen: either this runtime cannot express one, this model takes none, or
 * the backend predates the feature and reports neither.
 *
 * Order comes from the declarations, which are contractually weakest-to-
 * strongest on both sides (`RuntimeCapabilities.reasoning_efforts` and
 * models.yaml `reasoning_efforts`, both pinned by tests). That is what lets
 * {@link highestEffort} be "the last one" instead of a ranking table this
 * layer would have to invent and keep in step with two vocabularies.
 */
export function offeredEfforts(
  config: AgentConfigApi | null,
  runtime: string,
  model: string,
): string[] {
  const runtimeLevels = config?.runtime_reasoning_efforts?.[runtime] ?? [];
  const modelLevels = config?.model_reasoning_efforts?.[model] ?? [];
  return modelLevels.filter((level) => runtimeLevels.includes(level));
}

/** The deepest level on offer, or `''` when the pair offers no control. */
export function highestEffort(offered: string[]): string {
  return offered.length > 0 ? offered[offered.length - 1] : '';
}
