(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.FocusSkillPicker = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  function queryFromInput(value) {
    const match = String(value).match(/^\/([^\n]*)$/);
    return match ? match[1].trim() : null;
  }

  function filterSkills(skills, query, selected = []) {
    const needle = String(query || "").toLocaleLowerCase();
    const excluded = new Set(selected);
    return skills.filter(skill => {
      if (excluded.has(skill.name)) return false;
      return !needle || `${skill.name}\n${skill.description}`.toLocaleLowerCase().includes(needle);
    });
  }

  function addSelection(selected, name) {
    return selected.includes(name) ? [...selected] : [...selected, name];
  }

  function removeSelection(selected, name) {
    return selected.filter(item => item !== name);
  }

  function moveActive(current, direction, length) {
    if (!length) return -1;
    const start = current < 0 ? (direction > 0 ? -1 : 0) : current;
    return (start + direction + length) % length;
  }

  function keyAction(key) {
    if (key === "ArrowDown") return "next";
    if (key === "ArrowUp") return "previous";
    if (key === "Enter" || key === "Tab") return "select";
    if (key === "Escape") return "close";
    return null;
  }

  return { queryFromInput, filterSkills, addSelection, removeSelection, moveActive, keyAction };
});
