/**
 * 本文件对外提供 Focus Windows 原生沙箱的失败关闭探针。
 * 输入为一次性 NTFS 工作区、私有临时区与注入的原生启动阶段故障；输出为目标零启动、明确 API 错误和极端中断观察。
 * 具体工作流为建立真实授权令牌，逐项破坏标准句柄、Job、创建、纳管、状态确认及恢复步骤，再强制控制端在纳管前退出并清理挂起目标。
 * 示例：node --test desktop/windows-sandbox/native.test.mjs。
 */
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";
import { resolveBoundary } from "./boundary.mjs";
import { prepareGrant, releaseGrant } from "./grants.mjs";
import { startRestricted } from "./process.mjs";
import { createCallToken } from "./token.mjs";
import { win32 } from "./win32.mjs";

test("native preparation and process protection fail before target code", async () => {
  const parent = resolve(tmpdir());
  const root = join(parent, `focus-native-test-${randomUUID()}`);
  const work = join(root, "work"), temps = join(root, "temps"), own = join(temps, "own");
  const marker = join(work, "must-not-run.txt");
  mkdirSync(root);
  for (const directory of [work, temps, own]) mkdirSync(directory);
  let boundary = null, token = null;
  try {
    boundary = resolveBoundary(work, temps, own);
    assert.throws(() => prepareGrant(join(root, "missing"), boundary.writeSid), /GetNamedSecurityInfoW/);
    prepareGrant(boundary.temp, boundary.tempWriteSid);
    prepareGrant(boundary.workspace, boundary.writeSid);
    assert.throws(() => createCallToken("workspace-write", "invalid-sid", boundary.tempWriteSid), /ConvertStringSidToSidW/);
    token = createCallToken("workspace-write", boundary.writeSid, boundary.tempWriteSid);
    const command = `echo ran > "${marker}"`;
    const invoke = (controls) => startRestricted(token, "cmd.exe", ["/c", command], work, controls);
    assert.throws(() => invoke({ stdio: () => { throw new Error("injected handle failure"); } }), /handle failure/);
    assert.throws(() => invoke({ job: () => { throw new Error("injected Job failure"); } }), /Job failure/);
    assert.throws(() => invoke({ assignProcess: () => 0 }), /AssignProcessToJobObject/);
    assert.throws(() => invoke({ beforeResume: () => { throw new Error("injected status failure"); } }), /status failure/);
    assert.throws(() => invoke({ resumeThread: () => 0xffffffff }), /ResumeThread/);
    assert.throws(() => startRestricted(token, join(work, "missing.exe"), [], work), /CreateProcessAsUserW/);
    await new Promise((done) => setTimeout(done, 300));
    assert.equal(existsSync(marker), false);
  } finally {
    if (token !== null) win32().close(token);
    if (boundary !== null) releaseGrant(boundary.temp, boundary.tempWriteSid);
    assert.equal(resolve(root).startsWith(`${parent}\\`), true);
    assert.match(root, /focus-native-test-[\da-f-]+$/);
    rmSync(root, { recursive: true, force: true });
  }
});

test("abrupt controller exit before Job assignment leaves target unexecuted", async () => {
  const parent = resolve(tmpdir());
  const root = join(parent, `focus-native-test-${randomUUID()}`);
  const work = join(root, "work"), temps = join(root, "temps"), own = join(temps, "own");
  const marker = join(work, "must-not-run.txt"), pidFile = join(root, "suspended.pid");
  mkdirSync(root);
  for (const directory of [work, temps, own]) mkdirSync(directory);
  let boundary = null, suspendedPid = null, suspendedTid = null;
  try {
    boundary = resolveBoundary(work, temps, own);
    prepareGrant(boundary.temp, boundary.tempWriteSid);
    prepareGrant(boundary.workspace, boundary.writeSid);
    const modulePath = (name) => new URL(`./${name}.mjs`, import.meta.url).href;
    const code = `
      import { writeFileSync } from 'node:fs';
      import { createCallToken } from ${JSON.stringify(modulePath("token"))};
      import { startRestricted } from ${JSON.stringify(modulePath("process"))};
      import { win32 } from ${JSON.stringify(modulePath("win32"))};
      const token = createCallToken('workspace-write', ${JSON.stringify(boundary.writeSid)}, ${JSON.stringify(boundary.tempWriteSid)});
      startRestricted(token, process.execPath,
        ['-e', ${JSON.stringify(`require('fs').writeFileSync(${JSON.stringify(marker)},'ran')`)}],
        ${JSON.stringify(work)}, { assignProcess: (_job, child, thread) => {
          writeFileSync(${JSON.stringify(pidFile)}, JSON.stringify({
            pid: win32().api.getProcessId(child), tid: win32().api.getThreadId(thread)
          }));
          process.exit(86);
        }});
    `;
    const controller = spawnSync(process.execPath, ["--input-type=module", "-e", code],
      { encoding: "utf8", timeout: 5000 });
    assert.equal(controller.status, 86, controller.stderr);
    const suspended = JSON.parse(readFileSync(pidFile, "utf8"));
    suspendedPid = suspended.pid;
    suspendedTid = suspended.tid;
    assert.ok(Number.isInteger(suspendedPid) && suspendedPid > 0);
    await new Promise((done) => setTimeout(done, 250));
    assert.equal(existsSync(marker), false);
  } finally {
    if (suspendedPid !== null) {
      const native = win32();
      const processHandle = native.api.openProcess(0x100001, 0, suspendedPid);
      if (!native.isNull(processHandle)) {
        try {
          native.check(native.api.terminateProcess(processHandle, 1), "TerminateProcess", `PID ${suspendedPid}`);
          let completed = native.api.waitForSingleObject(processHandle, 200);
          if (completed === 258 && suspendedTid !== null) {
            const thread = native.api.openThread(2, 0, suspendedTid);
            if (!native.isNull(thread)) {
              try { native.api.resumeThread(thread); }
              finally { native.close(thread); }
            }
            completed = native.api.waitForSingleObject(processHandle, 5000);
          }
          assert.equal(completed, 0, `Suspended target ${suspendedPid} did not terminate`);
        } finally { native.close(processHandle); }
      }
    }
    if (boundary !== null) releaseGrant(boundary.temp, boundary.tempWriteSid);
    assert.equal(resolve(root).startsWith(`${parent}\\`), true);
    assert.match(root, /focus-native-test-[\da-f-]+$/);
    rmSync(root, { recursive: true, force: true });
  }
});
