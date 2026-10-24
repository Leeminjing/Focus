/* Managed install/update verifies the bundled native artifact. End users need
 * WebView2, never the SDK, compiler, or the browser repository's Python demo. */
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
function verify(directory = path.join(__dirname, "../resources/distinct-browser")) {
  const manifest = JSON.parse(fs.readFileSync(path.join(directory, "manifest.json"), "utf8"));
  const hash = crypto.createHash("sha256").update(fs.readFileSync(path.join(directory, "distinct-browser.exe"))).digest("hex");
  if (manifest.version !== "0.3.2" || manifest.revision !== "8837b4f40a8aac875ebb800fd119a8c9a906c9fa" || manifest.protocol_version !== 1 || manifest.sha256 !== hash) {
    throw new Error("Bundled distinct-browser.exe does not match its pinned build manifest.");
  }
  return manifest;
}
if (require.main === module && process.platform === "win32") { verify(); console.log("Distinct Browser artifact verified."); }
module.exports = { verify };
