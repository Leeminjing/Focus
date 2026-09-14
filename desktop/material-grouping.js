/**
 * 本文件对外提供材料库分组和折叠状态的纯函数 API。
 *
 * 输入为材料、自定义组、run/type 自动模式、折叠 key 与当前选择；输出为自定义优先且每份材料
 * 恰好出现一次的稳定组树和仍有效的折叠 key。具体工作流为先消费 membership 材料，再按首次
 * Run 或统一 material_kind 投影其余材料；自定义组按持久位置排序，自动组按稳定 key 排序，
 * 折叠仅改变呈现标志而不修改运行选择。
 *
 * 示例：project(materials, groups, "run", ["custom:g1"], selection)。
 */
(function (global) {
  "use strict";

  function project(materials, customGroups, mode = "run", collapsedKeys = [], selection = null) {
    const byId = new Map((materials || []).map(item => [String(item.material_id), item]));
    const consumed = new Set();
    const selected = new Set((selection?.bindings || []).map(item => String(item.materialId)));
    const groups = [...(customGroups || [])]
      .sort((a, b) => Number(a.position || 0) - Number(b.position || 0))
      .map(group => {
        const members = [...(group.memberships || [])]
          .sort((a, b) => Number(a.position || 0) - Number(b.position || 0))
          .map(item => byId.get(String(item.material_id)))
          .filter(Boolean);
        members.forEach(item => consumed.add(String(item.material_id)));
        return makeGroup(`custom:${group.group_id}`, group.name, "custom", members, selected);
      });
    const automatic = new Map();
    for (const material of materials || []) {
      if (consumed.has(String(material.material_id))) continue;
      const descriptor = mode === "type" ? typeDescriptor(material) : runDescriptor(material);
      if (!automatic.has(descriptor.key)) automatic.set(descriptor.key, makeGroup(descriptor.key, descriptor.label, "automatic", [], selected));
      automatic.get(descriptor.key).materials.push(material);
    }
    groups.push(...[...automatic.values()].sort((a, b) => a.key.localeCompare(b.key)));
    for (const group of groups) group.selectedCount = group.materials.filter(item => selected.has(String(item.material_id))).length;
    const valid = new Set(groups.map(group => group.key));
    const collapsed = [...new Set(collapsedKeys || [])].filter(key => valid.has(key));
    const folded = new Set(collapsed);
    groups.forEach(group => { group.collapsed = folded.has(group.key); });
    return { groups, collapsedKeys: collapsed };
  }

  function makeGroup(key, label, source, materials, selected) {
    return { key, label: String(label || "未命名分组"), source, materials, selectedCount: 0, collapsed: false };
  }

  function runDescriptor(material) {
    const runId = String(material.first_run_id || "");
    return runId
      ? { key: `run:${runId}`, label: `Run ${runId.slice(0, 8)}` }
      : { key: "run:unassigned", label: "未随运行发送" };
  }

  function typeDescriptor(material) {
    const kind = String(material.material_kind || "other");
    const labels = { image: "图片", text: "文本与代码", pdf: "PDF", document: "文档", spreadsheet: "表格", presentation: "演示文稿", archive: "压缩包", other: "其它文件" };
    return { key: `type:${kind}`, label: labels[kind] || labels.other };
  }

  global.materialGrouping = { project };
})(window);
