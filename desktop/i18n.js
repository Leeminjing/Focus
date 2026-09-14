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
      "nav.collapse": "收起导航",
      "nav.expand": "展开导航",
      "zoom.announce": "缩放 {percent}%",
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
      "settings.models": "模型",
      "settings.models_help": "模型条目、默认选择与密钥都在这里配置；保存后立即生效，无需重启。",
      "settings.data": "数据",
      "settings.language": "界面语言",
      "settings.language_help": "切换后立即应用，并保存在本机。",
      "settings.chinese": "中文",
      "settings.english": "English",
      "settings.archived": "已归档的会话",
      "settings.archived_help": "归档可恢复；删除需确认",
      "settings.close": "关闭",
      "settings.archived_count": "{count} 个已归档会话",
      "settings.model_add": "新增模型",
      "settings.model_edit": "编辑",
      "settings.model_remove": "删除",
      "settings.model_save": "保存并立即生效",
      "settings.model_cancel": "取消",
      "settings.model_reset": "恢复发行默认",
      "settings.model_test": "测试连接",
      "settings.model_testing": "正在测试…",
      "settings.model_test_ok": "连接成功",
      "settings.model_test_fail": "连接失败：{reason}",
      "settings.model_saved": "已保存并立即生效",
      "settings.model_dirty": "有未保存的改动，保存后立即生效",
      "settings.model_empty": "当前没有可用模型条目，请新增一个。",
      "settings.model_default_error": "默认模型无法解析：{message}",
      "settings.model_removed_note": "已删除：{names}",
      "settings.model_referenced": "这些条目仍被任务或运行引用：{names}。确认删除后，相关任务需要重新选择模型。仍要删除吗？",
      "settings.model_key_set": "已设置",
      "settings.model_key_unset": "未设置",
      "settings.model_key_literal": "已内联字面量（不建议）",
      "settings.model_key_placeholder": "留空表示不修改",
      "settings.model_source_shipped": "发行自带",
      "settings.model_source_user_modified": "已由你修改",
      "settings.model_source_user_added": "你新增的",
      "settings.model_field_name": "条目名（查表键）",
      "settings.model_field_display_name": "显示名",
      "settings.model_field_use": "适配器",
      "settings.model_field_model": "模型标识",
      "settings.model_field_base_url": "端点",
      "settings.model_field_api_key": "密钥来源变量",
      "settings.model_field_key_value": "密钥值",
      "settings.model_field_context_window": "上下文窗口",
      "settings.model_field_curation_method": "策展输出方式",
      "settings.model_curation_method_none": "不用于策展",
      "settings.model_field_curation_tokens": "策展输出上限",
      "settings.model_field_default": "默认模型",
      "settings.model_field_curation_default": "策展默认模型",
      "settings.model_field_supports_image": "支持图像输入",
      "settings.model_unknown": "未知",
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
      "status.ready": "就绪",
      "access.panel_title": "本机资源访问请求",
      "access.badge": "等待批准",
      "access.background_title": "后台执行主体请求本机资源",
      "access.subject_main": "主 Agent",
      "access.field_tool": "工具",
      "access.field_target": "目标或命令",
      "access.operation_read": "读取",
      "access.operation_write": "写入",
      "access.operation_command": "执行",
      "access.field_cwd": "执行位置",
      "access.field_agent_role": "发起角色",
      "access.field_access_mode": "当前访问模式",
      "access.mode_workspace": "工作区保护",
      "access.mode_full": "本机完全权限",
      "access.mode_switch_hint": "切换本机资源访问模式",
      "access.mode_pinned_curator": "策展小兵固定为工作区保护",
      "access.mode_changed_workspace": "已切回工作区保护：越界操作将逐次请求批准",
      "access.mode_changed_full": "已启用本机完全权限：后续运行不再逐次询问",
      "access.approve_once": "仅允许这一次",
      "access.reject": "拒绝",
      "access.switch_full": "切换为完全权限",
      "access.approved": "已允许这一次：{tool}",
      "access.denied": "已拒绝：{tool} 未执行，模型将收到可读的失败结果",
      "access.switched_full": "已允许本次调用；后续运行按完全权限执行",
      "access.risk_title": "启用完全权限前确认",
      "access.risk_os_permissions": "它不绕过操作系统自身的权限：这些调用仍受当前用户与文件系统的权限限制。",
      "access.risk_other_reviews": "它也不关闭其他人工审核机制：承诺确认、上下文投影决断与压缩范围确认仍然需要你决定。",
      "access.risk_confirm": "确认切换",
      "access.risk_cancel": "取消"
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
      "nav.collapse": "Collapse Navigation",
      "nav.expand": "Expand Navigation",
      "zoom.announce": "Zoom {percent}%",
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
      "settings.models": "Models",
      "settings.models_help": "Configure model entries, the default choice and credentials here; saving applies immediately, no restart needed.",
      "settings.data": "Data",
      "settings.language": "Interface Language",
      "settings.language_help": "Changes apply immediately and are saved on this device.",
      "settings.chinese": "中文",
      "settings.english": "English",
      "settings.archived": "Archived Sessions",
      "settings.archived_help": "Archived sessions can be restored; deletion requires confirmation.",
      "settings.close": "Close",
      "settings.archived_count": "{count} archived sessions",
      "settings.model_add": "Add model",
      "settings.model_edit": "Edit",
      "settings.model_remove": "Delete",
      "settings.model_save": "Save and apply",
      "settings.model_cancel": "Cancel",
      "settings.model_reset": "Restore shipped defaults",
      "settings.model_test": "Test connection",
      "settings.model_testing": "Testing…",
      "settings.model_test_ok": "Connection succeeded",
      "settings.model_test_fail": "Connection failed: {reason}",
      "settings.model_saved": "Saved and applied",
      "settings.model_dirty": "Unsaved changes — saving applies them immediately",
      "settings.model_empty": "No usable model entries yet — add one.",
      "settings.model_default_error": "The default model cannot be resolved: {message}",
      "settings.model_removed_note": "Removed: {names}",
      "settings.model_referenced": "These entries are still referenced by tasks or runs: {names}. After deleting them, those tasks must pick a model again. Delete anyway?",
      "settings.model_key_set": "Set",
      "settings.model_key_unset": "Not set",
      "settings.model_key_literal": "Inline literal (not recommended)",
      "settings.model_key_placeholder": "Leave empty to keep unchanged",
      "settings.model_source_shipped": "Shipped",
      "settings.model_source_user_modified": "Modified by you",
      "settings.model_source_user_added": "Added by you",
      "settings.model_field_name": "Entry name (lookup key)",
      "settings.model_field_display_name": "Display name",
      "settings.model_field_use": "Adapter",
      "settings.model_field_model": "Model id",
      "settings.model_field_base_url": "Endpoint",
      "settings.model_field_api_key": "Credential variable",
      "settings.model_field_key_value": "Credential value",
      "settings.model_field_context_window": "Context window",
      "settings.model_field_curation_method": "Curation output method",
      "settings.model_curation_method_none": "Not used for curation",
      "settings.model_field_curation_tokens": "Curation max tokens",
      "settings.model_field_default": "Default model",
      "settings.model_field_curation_default": "Curation default model",
      "settings.model_field_supports_image": "Supports image input",
      "settings.model_unknown": "Unknown",
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
      "status.ready": "Ready",
      "access.panel_title": "Local Resource Access Request",
      "access.badge": "Awaiting approval",
      "access.background_title": "A background agent requests local resources",
      "access.subject_main": "Main agent",
      "access.field_tool": "Tool",
      "access.field_target": "Target or command",
      "access.operation_read": "Read",
      "access.operation_write": "Write",
      "access.operation_command": "Run",
      "access.field_cwd": "Runs in",
      "access.field_agent_role": "Requested by",
      "access.field_access_mode": "Current access mode",
      "access.mode_workspace": "Workspace protection",
      "access.mode_full": "Full local access",
      "access.mode_switch_hint": "Change local resource access mode",
      "access.mode_pinned_curator": "Curator agents are pinned to workspace protection",
      "access.mode_changed_workspace": "Back to workspace protection: out-of-root calls ask again",
      "access.mode_changed_full": "Full local access enabled: later runs stop asking",
      "access.approve_once": "Allow once",
      "access.reject": "Deny",
      "access.switch_full": "Switch to full access",
      "access.approved": "Allowed once: {tool}",
      "access.denied": "Denied: {tool} was not executed and the model receives a readable failure",
      "access.switched_full": "This call was allowed; later runs use full access",
      "access.risk_title": "Confirm before enabling full access",
      "access.risk_os_permissions": "It does not bypass your operating system's own permissions: these calls remain limited by your user account and the file system.",
      "access.risk_other_reviews": "It does not disable the other human review steps: commitment confirmation, context projection decisions and compression scope confirmation still need your decision.",
      "access.risk_confirm": "Switch",
      "access.risk_cancel": "Cancel"
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
