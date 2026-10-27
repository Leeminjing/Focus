const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { verify } = require("../../scripts/prepare-browser.cjs");
test("bundled independent browser is pinned and corrupt distribution is rejected", () => {
  const manifest = verify();
  assert.equal(manifest.version, "0.6.1");
  assert.equal(manifest.revision, "e0222b2d1be404a3f496c31e4c9ed25b78a88f7c");
  assert.equal(manifest.protocol_version, 1);
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "focus-browser-artifact-"));
  try {
    fs.writeFileSync(path.join(temporary, "manifest.json"), JSON.stringify(manifest));
    fs.writeFileSync(path.join(temporary, "distinct-browser.exe"), "corrupt");
    assert.throws(() => verify(temporary), /does not match/);
    fs.copyFileSync(path.join(__dirname, "../../resources/distinct-browser/distinct-browser.exe"), path.join(temporary, "distinct-browser.exe"));
    for (const changed of [
      { version: "0.5.0" },
      { version: "0.6.0" },
      { revision: "146a4bb85d4aafeefbac388855941cd82f1c9036" },
      { revision: "fffc2c458c56c0dc173feed45bd0ff1e63fcc0a9" },
      { protocol_version: 2 },
      { sha256: "0".repeat(64) },
    ]) {
      fs.writeFileSync(path.join(temporary, "manifest.json"), JSON.stringify({ ...manifest, ...changed }));
      assert.throws(() => verify(temporary), /does not match/);
    }
  } finally { fs.rmSync(temporary, { recursive: true }); }
});
