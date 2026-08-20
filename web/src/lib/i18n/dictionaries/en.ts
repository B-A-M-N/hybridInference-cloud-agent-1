/**
 * The source dictionary.
 *
 * English is the key set: every other language is typed as a total map over
 * these keys, so adding a string here fails the type-check until it has been
 * translated everywhere. That is deliberate — a language that silently falls
 * back for half its screens is worse than one we never shipped, because the
 * reader cannot tell the difference between "untranslated" and "broken".
 *
 * Placeholders are `{name}` and are substituted by `translate()`.
 */
export const en = {
  // ── Shell chrome ────────────────────────────────────────────────────────
  'nav.aria': 'Product navigation',
  'nav.status': 'Status',
  'nav.docsAssistant': 'Docs Assistant',
  'nav.agents': 'Agents',
  'nav.hosts': 'Hosts',
  'nav.dashboard': 'Dashboard',
  'nav.logOut': 'Log out',
  'nav.expandTaskList': 'Expand task list',
  'nav.collapseTaskList': 'Collapse task list',

  'shell.internalRequired.title': 'Internal access required',
  'shell.internalRequired.body': 'The agent sandbox is dogfood-only for now (issue #1041).',
  'shell.internalRequired.back': 'Back to Dashboard',

  // ── Sidebar ─────────────────────────────────────────────────────────────
  'sidebar.newTask': 'New task',
  'sidebar.archived': 'Archived',
  'sidebar.integrations': 'Integrations',
  'sidebar.noJobs': 'No jobs yet.',
  'sidebar.resize': 'Resize task list',
  'sidebar.showMore': 'Show {count} more',
  'sidebar.newTaskIn': 'New task in {project}',
  'sidebar.runningOrQueued': '{count} running or queued',
  'sidebar.pin': 'Pin {title}',
  'sidebar.unpin': 'Unpin {title}',
  'sidebar.pinTask': 'Pin task',
  'sidebar.unpinTask': 'Unpin task',
  'sidebar.archive': 'Archive {title}',
  'sidebar.archiveTask': 'Archive task',
  'sidebar.archiveFailed': 'Could not archive the task.',
  'sidebar.pinFailed': 'Could not pin the task.',
  'sidebar.unpinFailed': 'Could not unpin the task.',
  'sidebar.openSettings': 'Open settings',

  // ── Settings ────────────────────────────────────────────────────────────
  'settings.title': 'Settings',
  'settings.close': 'Close settings',
  'settings.done': 'Done',
  'settings.signedInAs': 'Signed in as',

  'settings.appearance.title': 'Appearance',
  'settings.appearance.theme': 'Theme',
  'settings.appearance.themeHint': 'System follows your operating system setting.',
  'settings.appearance.light': 'Light',
  'settings.appearance.dark': 'Dark',
  'settings.appearance.system': 'System',

  'settings.language.title': 'Language',
  'settings.language.label': 'Display language',
  'settings.language.hint':
    'Changes the interface only. Agents keep answering in whatever language you write to them in.',

  // ── Shared ──────────────────────────────────────────────────────────────
  'common.loading': 'Loading…',
} as const;

export type TranslationKey = keyof typeof en;
