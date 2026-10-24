/* 原生窗口、托盘和提醒共用桌面语言偏好；只翻译应用文案，任务正文保持原样。 */
"use strict";
const catalog = require("../renderer/shared/i18n/i18n-catalog.js");
function createNativeI18n(locale) {
  return source => locale() === "en-US" ? catalog[source] ?? source : source;
}
module.exports = { createNativeI18n };
