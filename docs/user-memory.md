# Avatars and personal memory

The homepage displays an automatically generated illustrated portrait for
your account. **Setting → Memory** manages your saved preferences. Its face, hair, and skin stay recognizable across
devices. Its backdrop, clothing colors, and illustrated work motifs evolve with
your recent work areas, such as APIs, UI, testing, or data storage. The most-used
work areas lead the theme; the same recent-work profile produces the same image.
It is an assigned avatar and does not attempt to infer your appearance. No image
service, inference quota, or uploaded photo is required.

Themes follow the work areas shown on the page, detected with English keywords.
Requests without a recognized work area keep the default theme.

Memory builds automatically from up to your 50 most recent requests. The page
shows recent work areas, projects, commonly used agent settings, saved explicit
preferences, compact task references, and suggestions for future requests.
Suggestions address concrete patterns such as requests without verification,
success checks, or failure reproduction steps. These keyword-based suggestions
are guidance, rather than an assessment of your abilities.

Preferences beginning with phrases such as “I prefer” or “Remember that” can
guide future tasks in their original project. A current task takes precedence
over remembered context. Task titles remain visible as source-linked history;
they are not added to future task prompts. Quoted text, fenced code, and
recognized credential-like text are excluded from saved preferences.
You can examine each entry's original chat, project, and date, then edit its
text or remove it. Edited preferences can guide future tasks without an
“I prefer” prefix. Repository preferences guide their source project, while
user preferences apply across projects. Editing a task reference does not turn
it into a preference.

Saved memory is limited to 12 entries, 240 characters per entry, and 2,400
characters total. At most three entries apply across repositories. Each entry
shows its scope, and the editor lets you choose repository or user scope.
Repository scope is the default for project conventions and technical choices.

## Controls

- **Consolidate** uses the model you choose from your account's available models,
  under your account's permissions and quota, to merge duplicates, shorten
  preferences, and decide their scope. It favors repository memory and reserves
  user memory for general communication and collaboration preferences. It drops
  temporary instructions and task references. Saving is revision-checked so a
  slow model cannot overwrite
  newer edits or a clear. An empty model result leaves saved memory unchanged.
  Consolidation is disabled while an editor is open or no model is available.
- **Refresh** rebuilds memory and the avatar's usage theme from currently eligible
  requests.
- Turning off **Automatically build memory** pauses automatic updates and
  stops adding preferences to newly claimed tasks. Saved memory and the current
  avatar theme remain visible, and individual entries can still be edited or
  removed.
- **Edit** opens the saved text for correction. **Save** persists the correction
  across automatic updates, while **Cancel** leaves it unchanged. Text must be
  nonempty, at most 240 characters, and contain no recognized credentials.
  Preference edits must apply to future tasks rather than a temporary request.
- **Remove** asks for confirmation and excludes that entry from subsequent
  refreshes. The original chat remains available.
- **Clear memory** removes the recent-work profile, saved memory, and
  suggestions, including individual corrections and removals. Confirming this
  action excludes all earlier requests from future memory creation. The avatar
  returns to its default theme while keeping the same face. New requests can
  build new memory and update the avatar theme while
  memory is enabled. Your original chats remain available.

Deleting a source chat removes its derived memory during the next refresh while
memory is enabled, including its contribution to the avatar's usage theme.
Memory is a bounded view of recent activity, so a preference can also leave the
view when its source is older than the 50-request window. Disable and clear take
effect for subsequent task claims; they cannot remove context already delivered
to a running agent or retained in an existing native session.

Corrections apply to an eligible entry from its original source; they do not
pin it beyond the recent-request window. If another tab or a refresh changes
memory before you save, the page preserves your draft and asks you to refresh,
review the latest memory, and retry.
If the entry has disappeared, your draft remains available to copy or cancel;
it cannot be saved to a missing entry.

## Deployment and storage

Apply Alembic migrations through `0052_memory_overrides` before starting the
updated control plane. The `agent_user_memories` table stores one derived record
per account, including its enabled setting and a reset fence that prevents
forgotten history from reappearing. Writes and refreshes serialize per account.
The control plane refreshes bounded batches every minute, and refreshes the owner's memory when
claiming a task or opening the Setting page. Each owner API response derives the
avatar from that saved work profile, so usage changes appear on the next page
load or refresh. The MCP-only listener serves no memory management routes.

The `agent_user_memory_edits` table stores owner-scoped corrections and removal
markers separately from automatic snapshots. Consolidated and scope-edited
entries use source-backed overlays in `agent_user_memories.overrides`. Overlays
expire with eligible evidence and never pin deleted chats. Clear removes both.

The owner API exposes `GET`, `PATCH`, and `DELETE` at
`/v1/agent/user-memory`. The authenticated session selects the account; callers
cannot supply a different account identifier. Reads and mutations require
current Agent admission and return `Cache-Control: no-store`. `PATCH` accepts
an `enabled` boolean or `{revision, memories: [{id, content, scope}]}`; `DELETE`
clears derived context and advances the reset fence. Each response includes the account's generated avatar as a safe SVG data
URL. Automatic refresh makes no additional model calls.

`PATCH /v1/agent/user-memory/entries/{memory_id}` accepts `content` and a
nonnegative integer `expected_revision`. `DELETE` at the same path accepts
`expected_revision`. Both return the updated profile, reject stale revisions
with `409`, and return `404` when the owner has no such visible entry. Entry
kind and source information cannot be changed. Responses mark corrected
entries with `user_edited`.

`POST /v1/agent/user-memory/consolidate` accepts `{revision, model}` and returns the
updated profile. The optional `model` must be available to the authenticated
account; unavailable choices return `400`. Omitting it preserves automatic model
selection for existing clients. Model output is checked for owned source
references, duplicate references, cross-repository merges, credentials,
temporary instructions, and all memory limits before saving. Only nonempty
results replace saved memory; empty results return the latest profile without
recording removals. Stale output
returns `409` when a replacement is attempted.

## Future improvements

Useful extensions include choosing avatar styles, adding new preferences,
pinning confirmed preferences beyond the recent-request window,
and evaluating suggestions against task outcomes. A model-assisted extraction
option could support more languages and subtler preferences, with explicit
source references and the account's existing model permissions and quota.
