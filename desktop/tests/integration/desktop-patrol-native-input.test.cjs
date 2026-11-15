/* Real Windows activation regression for the production Patrol host.
 * A direct renderer click bypasses WM_MOUSEACTIVATE, so probe the native HWND
 * without activating it or moving the desktop mouse. The isolated blank page
 * deliberately checks only the Windows input gate, not renderer/IPC behavior.
 * Run: node --test tests/integration/desktop-patrol-native-input.test.cjs.
 */
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

if (!process.versions.electron) {
  const { test } = require("node:test");
  const { spawn } = require("node:child_process");
  test("restored Patrol preserves an inactive window's first mouse down", { skip: process.platform !== "win32", timeout: 20000 }, async () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), "focus-patrol-native-input-"));
    const env = { ...process.env, FOCUS_PATROL_NATIVE_INPUT_USER_DATA: directory,
      FOCUS_PATROL_NATIVE_KOFFI: require.resolve("koffi") };
    delete env.ELECTRON_RUN_AS_NODE;
    const child = spawn(require("electron"), [__filename], { env, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "", stderr = "";
    child.stdout.on("data", value => { stdout += value; });
    child.stderr.on("data", value => { stderr += value; });
    const timer = setTimeout(() => child.kill(), 15000);
    try {
      const code = await new Promise((resolve, reject) => { child.once("exit", resolve); child.once("error", reject); });
      assert.equal(code, 0, `${stdout}\n${stderr}`);
      assert.match(stdout, /PATROL_NATIVE_INPUT_PASS/);
      console.log(stdout.trim());
    } finally {
      clearTimeout(timer);
      assert.ok(path.resolve(directory).startsWith(path.resolve(os.tmpdir()) + path.sep));
      fs.rmSync(directory, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
    }
  });
} else {
  const electron = require("electron");
  const { app, BrowserWindow } = electron;
  const koffi = require(process.env.FOCUS_PATROL_NATIVE_KOFFI);
  const { DesktopPatrolHost } = require("../../src/main/desktop-patrol-host.cjs");
  const directory = process.env.FOCUS_PATROL_NATIVE_INPUT_USER_DATA;
  app.setPath("userData", directory);
  app.on("window-all-closed", () => {});
  const user32 = koffi.load("user32.dll");
  const send = user32.func("intptr_t __stdcall SendMessageTimeoutW(uintptr_t hwnd, uint32_t message, uintptr_t wParam, intptr_t lParam, uint32_t flags, uint32_t timeout, void *result)");
  let host;
  app.whenReady().then(async () => {
    host = new DesktopPatrolHost({
      electron: { ...electron, BrowserWindow: function (options) {
        const win = new BrowserWindow(options);
        // Keep the production native options and lifecycle, with no backend or
        // user content. No renderer input is injected into this native probe.
        const load = win.loadURL.bind(win);
        win.loadURL = () => load("about:blank");
        return win;
      } },
      apiBase: "http://127.0.0.1:1", desktopDir: path.resolve(__dirname, "../.."),
      icon: path.join(__dirname, "../../src/renderer/assets/focus-icon.ico"),
      limits: { general_file_bytes: 1024, run_material_count: 1 },
      getWorkbench: () => null, openWorkbench: async () => {}, quit: () => {},
    });
    await host.start();
    const robot = host.robot;
    const handle = robot.getNativeWindowHandle();
    const hwnd = Number(handle.length === 8 ? handle.readBigUInt64LE() : handle.readUInt32LE());
    for (let cycle = 0; cycle < 3; cycle++) {
      host.panel.hide(); robot.hide(); host.restore();
      assert.equal(robot.isVisible(), true);
      assert.equal(robot.isFocused(), false, "showInactive must not take focus while restoring the robot");
      const result = Buffer.alloc(8);
      // HTCLIENT (low word) + WM_LBUTTONDOWN (high word), exactly the native
      // activation query Windows sends before delivering the first press.
      assert.notEqual(send(hwnd, 0x0021, hwnd, 0x02010001, 2, 1000, result), 0, "WM_MOUSEACTIVATE must complete");
      const activation = Number(handle.length === 8 ? result.readBigInt64LE() : result.readInt32LE());
      console.log(`PATROL_NATIVE_ACTIVATION cycle=${cycle} result=${activation}`);
      assert.ok(activation === 1 || activation === 3, `robot must preserve the first mouse down, received activation ${activation} (2/4 eat input)`);
      assert.equal(robot.isFocused(), false, "probing native activation must not pre-focus the robot");
    }
    console.log("PATROL_NATIVE_INPUT_PASS");
  }).catch(error => { console.error(error.stack); process.exitCode = 1; }).finally(() => {
    host?.dispose();
    app.exit(process.exitCode || 0);
  });
}
