const test = require("node:test");
const assert = require("node:assert/strict");

const keywordCommand = require("./keyword-command.js");

const escapeHtml = value => value
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;");

test("only the canonical keyword compression command is parsed", () => {
  assert.equal(keywordCommand.parse("@关键字快捷压缩 胡萝卜"), "胡萝卜");
  assert.equal(keywordCommand.parse("@关键字快捷压缩"), null);
  assert.equal(keywordCommand.parse("@压缩 胡萝卜"), null);
});

test("the command token turns blue only after trailing whitespace", () => {
  assert.equal(
    keywordCommand.highlightHtml("@关键字快捷压缩", escapeHtml),
    "@关键字快捷压缩",
  );
  assert.equal(
    keywordCommand.highlightHtml("@关键字快捷压缩 ", escapeHtml),
    '<span class="cmd-at">@关键字快捷压缩</span> ',
  );
  assert.equal(
    keywordCommand.highlightHtml("@关键字快捷压缩 胡萝卜", escapeHtml),
    '<span class="cmd-at">@关键字快捷压缩</span> 胡萝卜',
  );
});

test("the removed command remains ordinary black text", () => {
  assert.equal(
    keywordCommand.highlightHtml("@压缩 胡萝卜", escapeHtml),
    "@压缩 胡萝卜",
  );
});
