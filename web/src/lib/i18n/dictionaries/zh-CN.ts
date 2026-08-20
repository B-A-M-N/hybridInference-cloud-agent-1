import type { TranslationKey } from './en';

/**
 * Simplified Chinese.
 *
 * Typed as a *total* map over `TranslationKey`, so this file cannot fall
 * behind: a new English string breaks the build here until it is translated.
 *
 * Product nouns that this team says in English — "Agent", the section name
 * "Agents" — stay in English rather than being rendered 智能体. Translating the
 * word people actually use in standup makes the UI harder to talk about, not
 * easier to read.
 */
export const zhCN: Record<TranslationKey, string> = {
  // ── Shell chrome ────────────────────────────────────────────────────────
  'nav.aria': '产品导航',
  'nav.status': '服务状态',
  'nav.docsAssistant': '文档助手',
  'nav.agents': 'Agents',
  'nav.hosts': '主机',
  'nav.dashboard': '控制台',
  'nav.logOut': '退出登录',
  'nav.expandTaskList': '展开任务列表',
  'nav.collapseTaskList': '收起任务列表',

  'shell.internalRequired.title': '需要内部权限',
  'shell.internalRequired.body': 'Agent 沙箱目前仅限内部试用(issue #1041)。',
  'shell.internalRequired.back': '返回控制台',

  // ── Sidebar ─────────────────────────────────────────────────────────────
  'sidebar.newTask': '新建任务',
  'sidebar.archived': '已归档',
  'sidebar.integrations': '集成',
  'sidebar.noJobs': '还没有任务。',
  'sidebar.resize': '调整任务列表宽度',
  'sidebar.showMore': '显示另外 {count} 条',
  'sidebar.newTaskIn': '在 {project} 中新建任务',
  'sidebar.runningOrQueued': '{count} 个运行中或排队中',
  'sidebar.pin': '置顶 {title}',
  'sidebar.unpin': '取消置顶 {title}',
  'sidebar.pinTask': '置顶任务',
  'sidebar.unpinTask': '取消置顶',
  'sidebar.archive': '归档 {title}',
  'sidebar.archiveTask': '归档任务',
  'sidebar.archiveFailed': '归档失败。',
  'sidebar.pinFailed': '置顶失败。',
  'sidebar.unpinFailed': '取消置顶失败。',
  'sidebar.openSettings': '打开设置',

  // ── Settings ────────────────────────────────────────────────────────────
  'settings.title': '设置',
  'settings.close': '关闭设置',
  'settings.done': '完成',
  'settings.signedInAs': '当前登录',

  'settings.appearance.title': '外观',
  'settings.appearance.theme': '主题',
  'settings.appearance.themeHint': '「跟随系统」会使用你操作系统的深浅色设置。',
  'settings.appearance.light': '浅色',
  'settings.appearance.dark': '深色',
  'settings.appearance.system': '跟随系统',

  'settings.language.title': '语言',
  'settings.language.label': '界面语言',
  'settings.language.hint': '只影响界面显示。你用什么语言跟 Agent 说话,它就用什么语言回答。',

  // ── Shared ──────────────────────────────────────────────────────────────
  'common.loading': '加载中…',
};
