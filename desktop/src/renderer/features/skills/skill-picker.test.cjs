const test = require("node:test");
const assert = require("node:assert/strict");
const picker = require("./skill-picker.js");

const skills = [
  { name: "code-review", description: "Review a code change" },
  { name: "pdf", description: "Read portable documents" },
  { name: "research", description: "Investigate primary sources" },
];

test("slash query only opens for an otherwise empty input", () => {
  assert.equal(picker.queryFromInput("/rev"), "rev");
  assert.equal(picker.queryFromInput("/"), "");
  assert.equal(picker.queryFromInput("fix /rev"), null);
  assert.equal(picker.queryFromInput(" /rev"), null);
});

test("filter matches names and descriptions and excludes selected skills", () => {
  assert.deepEqual(picker.filterSkills(skills, "code", []).map(item => item.name), ["code-review"]);
  assert.deepEqual(picker.filterSkills(skills, "portable", []).map(item => item.name), ["pdf"]);
  assert.deepEqual(picker.filterSkills(skills, "", ["pdf"]).map(item => item.name), ["code-review", "research"]);
});

test("selection stays ordered, deduplicated, and removable", () => {
  let selected = picker.addSelection([], "pdf");
  selected = picker.addSelection(selected, "research");
  selected = picker.addSelection(selected, "pdf");
  assert.deepEqual(selected, ["pdf", "research"]);
  assert.deepEqual(picker.removeSelection(selected, "pdf"), ["research"]);
});

test("keyboard actions navigate, select, and close", () => {
  assert.equal(picker.moveActive(-1, 1, 3), 0);
  assert.equal(picker.moveActive(0, -1, 3), 2);
  assert.equal(picker.moveActive(2, 1, 3), 0);
  assert.equal(picker.keyAction("Enter"), "select");
  assert.equal(picker.keyAction("Tab"), "select");
  assert.equal(picker.keyAction("Escape"), "close");
});
