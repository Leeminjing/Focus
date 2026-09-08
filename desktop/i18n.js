/* Focus desktop interface localization. Language choice is browser-local. */
(function exposeFocusI18n(global) {
  "use strict";

  const STORAGE_KEY = "focus-interface-language-v1";
  const DEFAULT_LOCALE = "zh-CN";
  const SUPPORTED_LOCALES = new Set(["zh-CN", "en-US"]);
  const messages = Object.freeze({
    "zh-CN": Object.freeze({
      "nav.tasks": "任务",
      "nav.map": "全图",
      "nav.contexts": "上下文",
      "nav.agents": "代理",
      "nav.plugins": "插件",
      "nav.assembly": "无工作区模式",
      "nav.memory": "记忆库",
      "nav.settings": "设置",
      "header.no_task": "尚未选择任务",
      "header.workbench": "本地 Agent 工作台",
      "header.new_task": "新增任务",
      "inspector.contexts": "上下文",
      "inspector.materials": "材料",
      "inspector.agents": "代理",
      "inspector.run": "运行",
      "dialog.new_task": "新增工作区任务",
      "dialog.workspace_folder": "工作区文件夹",
      "dialog.choose": "选择",
      "dialog.task_title": "任务标题",
      "dialog.default_title": "新任务",
      "dialog.cancel": "取消",
      "dialog.create": "创建",
      "settings.title": "设置",
      "settings.subtitle": "Focus 桌面配置",
      "settings.general": "常规",
      "settings.data": "数据",
      "settings.language": "界面语言",
      "settings.language_help": "切换后立即应用，并保存在本机。",
      "settings.chinese": "中文",
      "settings.english": "English",
      "settings.archived": "已归档的会话",
      "settings.archived_help": "归档可恢复；删除需确认",
      "settings.close": "关闭",
      "settings.archived_count": "{count} 个已归档会话",
      "empty.title": "建立第一个工作区任务",
      "empty.body": "一个全图单元由本地工作区文件夹和线程共同组成。",
      "empty.choose_folder": "选择文件夹",
      "common.local_workspace": "本地工作区",
      "common.restore": "恢复",
      "common.delete": "删除",
      "common.no_archived": "暂无已归档会话",
      "common.close": "关闭",
      "focus.current_task": "当前任务",
      "focus.run_details": "运行详情",
      "focus.input_label": "任务输入",
      "focus.input_placeholder": "描述下一步，或输入 / 选择技能…",
      "focus.add_file": "添加文件",
      "focus.send": "发送",
      "focus.interrupt": "中断",
      "focus.context_root": "根 Context",
      "focus.context_derived": "派生 Context",
      "focus.context_add": "新增 Context",
      "focus.cache": "缓存",
      "focus.enter_hint": "Enter 发送 · Shift+Enter 换行",
      "status.pending": "排队中",
      "status.running": "运行中",
      "status.success": "已完成",
      "status.interrupted": "已中断",
      "status.error": "运行失败",
      "status.cancelled": "已取消",
      "status.ready": "就绪"
    }),
    "en-US": Object.freeze({
      "nav.tasks": "Task",
      "nav.map": "Map",
      "nav.contexts": "Contexts",
      "nav.agents": "Agents",
      "nav.plugins": "Plugins",
      "nav.assembly": "No Workspace",
      "nav.memory": "Memory",
      "nav.settings": "Settings",
      "header.no_task": "No task selected",
      "header.workbench": "Local Agent Workbench",
      "header.new_task": "New Task",
      "inspector.contexts": "Contexts",
      "inspector.materials": "Materials",
      "inspector.agents": "Agents",
      "inspector.run": "Run",
      "dialog.new_task": "New Workspace Task",
      "dialog.workspace_folder": "Workspace Folder",
      "dialog.choose": "Choose",
      "dialog.task_title": "Task Title",
      "dialog.default_title": "New Task",
      "dialog.cancel": "Cancel",
      "dialog.create": "Create",
      "settings.title": "Settings",
      "settings.subtitle": "Focus desktop preferences",
      "settings.general": "General",
      "settings.data": "Data",
      "settings.language": "Interface Language",
      "settings.language_help": "Changes apply immediately and are saved on this device.",
      "settings.chinese": "中文",
      "settings.english": "English",
      "settings.archived": "Archived Sessions",
      "settings.archived_help": "Archived sessions can be restored; deletion requires confirmation.",
      "settings.close": "Close",
      "settings.archived_count": "{count} archived sessions",
      "empty.title": "Create your first workspace task",
      "empty.body": "A map unit combines a local workspace folder with a thread.",
      "empty.choose_folder": "Choose Folder",
      "common.local_workspace": "Local Workspace",
      "common.restore": "Restore",
      "common.delete": "Delete",
      "common.no_archived": "No archived sessions",
      "common.close": "Close",
      "focus.current_task": "Current Task",
      "focus.run_details": "Run Details",
      "focus.input_label": "Task input",
      "focus.input_placeholder": "Describe the next step, or type / to choose a skill…",
      "focus.add_file": "Add File",
      "focus.send": "Send",
      "focus.interrupt": "Interrupt",
      "focus.context_root": "Root Context",
      "focus.context_derived": "Derived Context",
      "focus.context_add": "New Context",
      "focus.cache": "Cache",
      "focus.enter_hint": "Enter to send · Shift+Enter for a new line",
      "status.pending": "Queued",
      "status.running": "Running",
      "status.success": "Completed",
      "status.interrupted": "Interrupted",
      "status.error": "Failed",
      "status.cancelled": "Cancelled",
      "status.ready": "Ready"
    })
  });

  function storedLocale() {
    try {
      const value = global.localStorage?.getItem(STORAGE_KEY);
      return SUPPORTED_LOCALES.has(value) ? value : DEFAULT_LOCALE;
    } catch {
      return DEFAULT_LOCALE;
    }
  }

  let activeLocale = storedLocale();

  function syncDocumentLanguage() {
    if (!global.document?.documentElement) return;
    global.document.documentElement.lang = activeLocale;
    global.document.documentElement.dataset.locale = activeLocale;
  }

  function locale() {
    return activeLocale;
  }

  function t(key, variables = {}) {
    const template = messages[activeLocale]?.[key] ?? messages[DEFAULT_LOCALE]?.[key] ?? key;
    return template.replace(/\{(\w+)\}/g, (_match, name) => String(variables[name] ?? `{${name}}`));
  }

  function apply(root = global.document) {
    if (!root?.querySelectorAll) return;
    root.querySelectorAll("[data-i18n]").forEach(node => {
      node.textContent = t(node.dataset.i18n);
    });
    root.querySelectorAll("[data-i18n-aria-label]").forEach(node => {
      node.setAttribute("aria-label", t(node.dataset.i18nAriaLabel));
    });
    root.querySelectorAll("[data-i18n-placeholder]").forEach(node => {
      node.setAttribute("placeholder", t(node.dataset.i18nPlaceholder));
    });
  }

  function setLocale(value) {
    if (!SUPPORTED_LOCALES.has(value)) return false;
    activeLocale = value;
    try { global.localStorage?.setItem(STORAGE_KEY, value); } catch { /* read-only storage */ }
    syncDocumentLanguage();
    apply();
    global.document?.dispatchEvent?.(new global.CustomEvent("focus:languagechange", {
      detail: { locale: value },
    }));
    return true;
  }

  syncDocumentLanguage();
  global.FocusI18n = Object.freeze({ apply, locale, setLocale, t });
})(window);
