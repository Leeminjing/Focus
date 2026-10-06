/** 本文件对外提供默认配置工厂的实际运行测试。
 * 输入为两次工厂调用；输出为成功退出或状态共享错误。
 * 具体工作流为修改首份会话并核对第二份隔离。示例：npm test。
 */
import { createDefaultSettings } from "./settings";
const first = createDefaultSettings();
const second = createDefaultSettings();
first.messages.push("hello");
if (second.messages.length !== 0 || first.systemPrompt !== second.systemPrompt) {
  throw new Error("default settings share session state");
}
