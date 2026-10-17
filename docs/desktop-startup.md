# 桌面启动准备与耗时

安装及 `focus update` 安装依赖后，通过 `desktop/startup-runtime.cjs --prepare <python> <home>`
检查 Python 桌面依赖、Git 和 Docker Compose，并保存已经确认的 Python 绝对路径。
源码开发直接启动 Electron 时，首次启动自动完成同样的准备。

日常启动读取缓存，异步检查真实数据库的连接与 Alembic heads。数据库已可用且版本一致时，
不执行 Compose 或 `alembic upgrade`；本地数据服务不可连接时才执行有界的 Compose up/wait，
有待执行迁移时才 upgrade，升级后再次确认版本。设置 `FOCUS_DATABASE_URL` 的外部数据库
不可连接时直接提示检查该服务，不启动本地 Compose。认证错误、版本查询错误会阻止启动。

缓存放在 Focus home 下的 `runtime/startup-<checkout hash>.json`，绑定仓库位置、平台、
显式 Python 选择、相关环境与依赖清单内容，以及解释器文件身份。更新依赖、替换解释器或
缓存损坏会触发重新准备。数据库状态不缓存，因此空库、恢复备份或切换数据库都重新核对。
手动修改 Python 包后遇到依赖错误，可运行 `focus update` 重新准备。

每次启动的阶段耗时写入 Focus home 下的 `startup-timing.jsonl`（默认 `~/.focus/`），
并以 `[focus-startup]` 前缀输出到终端。超过约 1 MiB 时轮换到 `.previous`。
每行有 `startup`、`at`、`stage`、`status`、`duration_ms` 和 `elapsed_ms`，不写连接串或密钥。

| 阶段 | 含义 |
| --- | --- |
| splash | 启动窗口加载 |
| runtime | 获取或准备 Python 运行时 |
| environment-prepare | 完整预检，仅首次、更新或缓存失效时出现；包含在 runtime 内 |
| database-check | 实际数据库连接、当前版本与脚本 heads 核对 |
| data-services / database-recheck | 按需启动本地服务及复检 |
| migrations / migration-verify | 按需迁移及版本复检 |
| gateway-health | 等待 Gateway 导入、初始化及健康响应 |
| interface | 材料限制读取、Patrol 或工作台窗口加载 |
| ready | 界面已就绪；elapsed_ms 表示本次启动累计耗时 |

不要累加嵌套阶段的 duration_ms。失败阶段记录 `status=failed`，退出取消会终止正在执行的
启动子进程与健康轮询。一般预检和版本检查限时 15 秒，Compose 与迁移限时 180 秒，
Compose 内部等待健康上限 90 秒；Gateway 健康检查仍为 30 秒。

2026-10-17 在 Windows / Python 3.12 / 已运行的 PostgreSQL 上做了一次分阶段采样：
完整预检 2311 ms，运行时缓存复用 4 ms。最初的数据库版本探测为 10398 ms；发现三份
迁移脚本在读取 revision 图时导入整个后端，将这些导入移到 DDL 函数后，同类探测为
1294 ms。采样只做只读数据库查询，不对用户库执行迁移；这些数字不代表完整启动提速比例。

另外用真实 Electron + Gateway + 独立空白 PostgreSQL 测试库连续启动两次，均正常到达
ready 并退出（已提前迁移测试库，关闭自动 Loop 调度，使用占位模型密钥，不调用 Provider）：

| 阶段 | 首次准备 | 复用缓存 |
| --- | ---: | ---: |
| splash | 248 ms | 181 ms |
| runtime | 1449 ms | 16 ms |
| database-check | 838 ms | 1208 ms |
| gateway-health | 5754 ms | 5778 ms |
| interface | 987 ms | 556 ms |
| ready 累计 | 9324 ms | 7839 ms |

这是当前实现两条路径的单次采样，包含文件系统/模块缓存波动，不是修改前后整机对照。
当前主要剩余耗时在 Gateway 初始化；未测量停机容器启动或首次镜像下载耗时。

前端仍在 bootstrap 返回前渲染 Patrol，并按入口延迟读取 MCP、插件与任务树。
本轮没有将静态界面改为 Gateway 启动前的离线草稿宿主，也未改变模型配置自检的失败策略。
可通过 gateway-health 和 interface 的实测结果决定是否需要后续拆分。

验证命令：

```powershell
node --test desktop/startup-runtime.test.cjs desktop/startup-ui.test.cjs desktop/startup-lifecycle.test.cjs desktop/managed-distribution.test.cjs desktop/update-lock.test.cjs desktop/bootstrap-loading.test.cjs desktop/composer-draft.test.cjs desktop/desktop-patrol-host.test.cjs desktop/window-material.test.cjs
$env:PYTHONPATH = "$PWD/backend/packages/harness;$PWD"
python -m pytest backend/tests/test_desktop_startup_database.py -q
```

生命周期测试使用真实 Electron 窗口与受控子进程，覆盖失败窗口、30 秒健康超时、用户取消、
Patrol、按需工作台和第二实例；数据库测试使用独立 PostgreSQL 测试库，验证真实迁移后
head 检查、空库/多头、连接拒绝及读取 revision 图时不加载桌面后端。

额外运行 `desktop/f20-architecture-guard.test.cjs` 时，现有 HTML 脚本版本号与其硬编码
断言不一致而失败。使用 `git show HEAD:<file>` 的原始测试与源码也复现相同断言失败，
确认早于本次修改；本轮未调整该前端断言。
