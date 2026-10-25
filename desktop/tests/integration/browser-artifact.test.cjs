const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { verify } = require("../../scripts/prepare-browser.cjs");
test("bundled independent browser is pinned and corrupt distribution is rejected", () => {
  const manifest = verify();
  assert.equal(manifest.version, "0.4.0");
  assert.equal(manifest.revision, "6fe11bbf964c6cbd8a7c3a82c6ca5f6fb9415696");
  assert.equal(manifest.protocol_version, 1);
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "focus-browser-artifact-"));
  try {
    fs.writeFileSync(path.join(temporary, "manifest.json"), JSON.stringify(manifest));
    fs.writeFileSync(path.join(temporary, "distinct-browser.exe"), "corrupt");
    assert.throws(() => verify(temporary), /does not match/);
    fs.copyFileSync(path.join(__dirname, "../../resources/distinct-browser/distinct-browser.exe"), path.join(temporary, "distinct-browser.exe"));
    fs.writeFileSync(path.join(temporary, "manifest.json"), JSON.stringify({ ...manifest, version: "0.3.1" }));
    assert.throws(() => verify(temporary), /does not match/);
  } finally { fs.rmSync(temporary, { recursive: true }); }
});
