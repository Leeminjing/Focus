/** 本文件对外提供 createDefaultSettings 配置工厂。
 * 输入为无；输出为独立的模型及会话默认配置。
 * 具体工作流为每次构造新的可变状态。示例：const settings = createDefaultSettings()。
 */
export function createDefaultSettings() {
  return { systemPrompt: "pet", messages: [] as string[] };
}
