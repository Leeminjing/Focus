/*
 * 原生启动准备：按依赖指纹复用 Python，异步执行带超时的命令，并按真实数据库版本决定迁移。
 * 安装/更新可调用 node startup-runtime.cjs --prepare <python> <home> 提前完成完整预检。
 * 耗时由调用方写入启动日志；此模块不记录环境变量、连接串或会话密钥。
 */
const { execFile } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs/promises");
const path = require("node:path");
const { performance } = require("node:perf_hooks");

function runCommand(command, args, { cwd, env = process.env, signal, timeout = 15000, label = "启动命令", hint = "请运行 focus update 检查依赖。" } = {}) {
  signal?.throwIfAborted();
  return new Promise((resolve, reject) => {
    execFile(command, args, { cwd, env, signal, timeout, windowsHide: true, encoding: "utf8", maxBuffer: 8 * 1024 * 1024 }, (error, stdout) => {
      if (!error) return resolve(stdout.trim());
      if (signal?.aborted) return reject(signal.reason);
      // 原始子进程错误会包含参数和环境细节，用户错误只显示阶段和退出原因。
      reject(new Error(`${label}${error.killed ? `超过 ${timeout / 1000} 秒` : `失败（${error.code || error.signal || "未知原因"}）`}；${hint}`));
    });
  });
}

async function fileIdentity(file) {
  const stat = await fs.stat(file);
  return [stat.size, stat.mtimeMs, stat.ctimeMs];
}

function createStartupRuntime({ rootDir, home, signal, managed = false, python = process.env.FOCUS_PYTHON, externalDatabase = Boolean(process.env.FOCUS_DATABASE_URL), run = runCommand, stage = async (_id, _label, _progress, work) => work() }) {
  const desktopDir = path.join(rootDir, "desktop");
  const cachePath = path.join(home, "runtime", `startup-${crypto.createHash("sha256").update(rootDir).digest("hex").slice(0, 16)}.json`);
  const command = (file, args, options) => run(file, args, { cwd: rootDir, signal, ...options });
  const pythonCommand = (runtime, args, options) => command(runtime.command, [...runtime.prefix, ...args], options);

  async function fingerprint() {
    const hash = crypto.createHash("sha256");
    // 托管安装指定绝对 Python 路径；npm start 添加的 PATH 不应使安装期缓存失效。
    const searchPath = python && path.isAbsolute(python) ? null : process.env.PATH;
    hash.update(JSON.stringify([3, rootDir, process.platform, process.arch, python, searchPath, process.env.PYTHONPATH, process.env.VIRTUAL_ENV, process.env.PYTHONHOME]));
    for (const relative of ["desktop/startup-runtime.cjs", "desktop/package.json", "desktop/package-lock.json", "backend/packages/harness/pyproject.toml", "backend/packages/harness/uv.lock"]) {
      hash.update(await fs.readFile(path.join(rootDir, relative)).catch(error => { if (error.code === "ENOENT") return "missing"; throw error; }));
    }
    return hash.digest("hex");
  }

  async function prepare(force = false) {
    signal?.throwIfAborted();
    const key = await fingerprint();
    if (force) await fs.rm(cachePath, { force: true });
    if (!force) {
      try {
        const cached = JSON.parse(await fs.readFile(cachePath, "utf8"));
        if (cached.key === key && path.isAbsolute(cached.runtime.command) && Array.isArray(cached.runtime.prefix) && cached.runtime.prefix.length === 0
          && JSON.stringify(await fileIdentity(cached.runtime.command)) === JSON.stringify(cached.identity)) return cached.runtime;
      } catch {} // 损坏、失效或旧缓存重新准备，不阻止恢复。
    }
    return stage("environment-prepare", "首次运行或更新：正在检查运行环境", 12, async () => {
      const candidates = python ? [{ command: python, prefix: [] }] : process.platform === "win32"
        ? [{ command: "py", prefix: ["-3.12"] }, { command: "py", prefix: ["-3"] }, { command: "python", prefix: [] }]
        : [{ command: "python3", prefix: [] }, { command: "python", prefix: [] }];
      if (!python && process.platform === "win32") {
        try {
          const found = await command("where.exe", ["python"], { label: "查找 Python" });
          candidates.unshift(...found.split(/\r?\n/).map(value => value.trim()).filter(Boolean).map(command => ({ command, prefix: [] })));
        } catch { signal?.throwIfAborted(); }
      }
      let runtime;
      for (const candidate of candidates) {
        try {
          const executable = await pythonCommand(candidate, ["-c", "import alembic, asyncpg, fastapi, psycopg, uvicorn, sys; print(sys.executable)"], { label: "Python 依赖检查" });
          if (!path.isAbsolute(executable)) throw new Error("Python 未返回绝对路径");
          runtime = { command: executable, prefix: [] };
          break;
        } catch (error) { signal?.throwIfAborted(); if (python) throw error; }
      }
      if (!runtime) throw new Error("未找到包含桌面依赖的 Python 运行时，请运行 focus update，或设置 FOCUS_PYTHON。");
      await command("git", ["--version"], { label: "Git 检查" });
      await command("docker", ["compose", "version"], { label: "Docker Compose 检查" });
      const record = { key, runtime, identity: await fileIdentity(runtime.command) };
      signal?.throwIfAborted();
      await fs.mkdir(path.dirname(cachePath), { recursive: true });
      const temporary = `${cachePath}.${process.pid}.tmp`;
      await fs.writeFile(temporary, JSON.stringify(record), "utf8");
      await fs.rename(temporary, cachePath);
      return runtime;
    });
  }

  async function prepareDatabase(runtime, env) {
    const check = async () => {
      const result = JSON.parse(await pythonCommand(runtime, [path.join(desktopDir, "database-state.py")], { env, timeout: 15000, label: "数据库连接与版本检查", hint: "请检查数据库地址、凭据与数据服务状态。" }));
      if (!["unavailable", "current", "pending"].includes(result.status)) throw new Error("数据库版本检查返回无效状态");
      return result.status;
    };
    let status = await stage("database-check", "正在检查数据服务与数据库版本", 28, check);
    if (status === "unavailable") {
      if (externalDatabase) throw new Error("配置的数据库无法连接，请检查 FOCUS_DATABASE_URL 对应的数据服务。");
      await stage("data-services", "正在启动本地数据服务（首次下载可能较慢）", 36, () => command("docker", ["compose", "-f", path.join(desktopDir, "compose.yaml"), "up", "-d", "--pull", "missing", "--wait", "--wait-timeout", "90"], { timeout: 180000, label: "启动本地数据服务", hint: "请确认 Docker Desktop 已启动，并检查镜像下载、数据库端口和容器日志。" }));
      status = await stage("database-recheck", "正在确认数据库已就绪", 44, check);
      if (status === "unavailable") throw new Error("本地数据库启动后仍无法连接，请检查 Docker Desktop 和数据库端口。");
    }
    if (status === "pending") {
      if (managed) throw new Error("数据库版本与已验收版本不一致，请关闭 Focus 后运行 focus update 完成备份、迁移与检查。");
      await stage("migrations", "检测到数据库更新，正在应用迁移", 48, () => pythonCommand(runtime, ["-m", "alembic", "-c", path.join(rootDir, "backend/packages/harness/focus/persistence/migrations/alembic.ini"), "upgrade", "head"], { env, timeout: 180000, label: "数据库迁移", hint: "请检查数据库连接与迁移版本，修复后重试。" }));
      if (await stage("migration-verify", "正在确认数据库更新完成", 56, check) !== "current") throw new Error("数据库迁移后版本仍未就绪");
    }
    if (managed) {
      await stage("database-schema", "正在检查工作区数据", 56, () => pythonCommand(runtime,
        ["-m", "backend.app.update_check", "--schema-only"], { env, timeout: 15000, label: "数据库结构检查", hint: "请关闭 Focus 后运行 focus update。" }));
    }
  }
  return { prepare, prepareDatabase, cachePath };
}

function createStageRecorder({ report = () => {}, update = async () => {} } = {}) {
  const started = performance.now();
  const entries = [];
  return {
    entries,
    async stage(id, label, progress, work) {
      await update(label, progress);
      const start = performance.now();
      let status = "ok";
      try { return await work(); } catch (error) { status = "failed"; throw error; }
      finally {
        const entry = { stage: id, status, duration_ms: Math.round(performance.now() - start), elapsed_ms: Math.round(performance.now() - started) };
        entries.push(entry);
        report(entry);
      }
    },
  };
}

module.exports = { runCommand, createStartupRuntime, createStageRecorder };

if (require.main === module) {
  const [mode, python, home] = process.argv.slice(2);
  if (mode !== "--prepare" || !python || !home) throw new Error("Usage: node startup-runtime.cjs --prepare <python> <home>");
  const recorder = createStageRecorder({ report: entry => console.log(`[focus-startup] ${JSON.stringify(entry)}`) });
  createStartupRuntime({ rootDir: path.resolve(__dirname, ".."), home, python, stage: recorder.stage }).prepare(true).catch(error => { console.error(error.message); process.exitCode = 1; });
}
