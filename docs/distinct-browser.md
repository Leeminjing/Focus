# Context 的独立浏览器

Windows 上的每个 Context 在 Run 执行前默认创建并绑定一个真实原生浏览器。
Focus 调用独立的 `focus-distinct-browser` EXE；基建不认识 Context、Run 或 Patrol。
固定浏览器版本为 v0.6.1，源码 `e0222b2d1be404a3f496c31e4c9ed25b78a88f7c`，对应
[协议 v1](https://github.com/Leeminjing/focus-distinct-browser/blob/e0222b2d1be404a3f496c31e4c9ed25b78a88f7c/docs/protocol.md)。
会话恢复仍需显式开启，默认保留匿名管道行为。

## 用户操作

1. 在 Windows 11 x64 和 WebView2 Evergreen Runtime 138+ 环境中正常启动 Focus。
   Focus 随源码/安装版本携带 EXE，`npm start` 和托管更新会校验其摘要。
   浏览器能力不额外依赖基建的 Python 演示、C++ 工具、SDK 或 .NET。
   Focus 自身原有的后端运行依赖仍适用；macOS 保持既有执行路径。
2. 发起 Context 的 Run。只有该浏览器 `create` 成功后才发布 Run 的 `running` 状态；
   缺少 EXE、Runtime 或创建失败沿用现有 Run 错误/收尾通道。
3. 在任务列表、Context 图或 Loop 控制台点击 Context，展示它的原生窗口。
   **浏览器**按钮也可以再次展示。展示保留页面、登录、输入和执行进度，
   不取得租约，也不隐藏/最小化其他 Context 的浏览器。
   Windows 拒绝前台激活时会提示从任务栏查看，不把它当作浏览器丢失。
   **页面**按钮可列出全部页面并展示已有页面，保持各页输入、登录和滚动位置。
4. 直接点击、输入或滚动网页即可自动接管，也可点击原生工具栏的**接管**。已发动作仍可能收尾；新动作不会继续派发。
   最小化/隐藏原生窗口也会暂停自动操作；恢复窗口后还需要点击**交还**。
   Focus 的下一次模型决策等待交还，并要求 Agent 重新观察，不恢复旧动作队列。
5. Run 成功、失败或取消都只释放本 Run 的控制权，浏览器页面保留，后续 Run 复用。
   原生窗口关闭、故障或连接失效使绑定失效，相关活跃 Run 及时报告错误。
   默认的匿名管道会话在后端重启后不能恢复；旧窗口可能仍可人工使用。可选的会话恢复见下文。
6. 需要共享时，先结束目标 Context 的 Run，在**浏览器绑定**中选同一工作区的来源 Context。
   两者会共享同一浏览器会话及其全部页面。也可明确创建新独立浏览器来解除共享或替换失效绑定。
   更换后只有没有任何 Context 引用的旧浏览器才调用 `close`；共享者不会被误关。
   断连后的显式替换使用全新的宿主连接，不接续旧窗口或旧令牌。
7. **下载与权限**面板显示下载进度、保存路径、完成 / 中断原因和来源页面，也显示网站权限的来源与决定状态。
   面板查询不申请租约，人工控制时仍可查看；取消下载请到原生浏览器按 **Ctrl+J**。
   网站权限仅由用户在原生提示中允许一次或拒绝，Focus 不提供自动授权或第二套决定入口。
   下载或权限请求会暂停该浏览器的自动操作；结束后仍需明确交还，Agent 用新观察继续。

## Agent 调用

Windows 桌面 Agent 工厂装配以下内置工具，全部通过服务端 Context 绑定定位浏览器，
模型不能传入其他 `browser_id` 或令牌：

| 工具 | 用途 |
| --- | --- |
| `browser_observe(page_id=None)` | 申请/持有租约，观察选中页，返回图片、URL、视口、`page_id` 及 `observation_id` |
| `browser_list_pages()` | 列出本浏览器页面与当前页；不取得租约 |
| `browser_list_downloads()` | 只读查询所属浏览器的下载、权限事件状态与待处理标记；人工控制期间可查询，不取得租约 |
| `browser_select_page(page_id)` | 取得浏览器级租约并展示页面，之后必须重新观察 |
| `browser_close_page(page_id)` | 取得同一租约并关闭页面；最后一页关闭时浏览器失效 |
| `browser_navigate(url, observation_id)` | 导航；之后重新观察 |
| `browser_click(x, y, observation_id, coordinate_space="image")` | 坐标点击 |
| `browser_type(text, observation_id)` | 向网页当前焦点输入文字 |
| `browser_key(key, observation_id)` | Enter、Tab、Escape 等协议支持的命名按键 |
| `browser_scroll(x, y, delta_y, observation_id, delta_x=0, coordinate_space="image")` | 滚动 |
| `browser_release()` | 结束一段观察/动作，让共享者取得控制，保留页面 |

典型顺序为 `observe → navigate → observe → click → type → click → observe → release`。
同一段操作保持同一租约，模型思考期间自动续租。共享者的观察由 Focus 等待当前持有者释放；
观察、选择或关闭页面申请租约时可以等待；网页动作不会排队或自动重放。不同浏览器的请求独立进行。

每次新租约都需要新的观察。导航、URL/视口变化、接管、暂停、释放后，旧观察不能继续操作。
动作参数必须引用该 Run 的最近观察编号；Focus 将该观察的 `page_id` 原样传给宿主，禁止把动作重定向到后来选中的页面。用户切页、关闭观察页时旧观察失效，租约仍属于整个浏览器；子页关闭不终止 Run，最后一页关闭才使整个浏览器失效。后台页导航不清除另一页的有效观察。默认坐标为返回图片像素，Focus 按
`x * viewport.width / image.width`、`y * viewport.height / image.height` 换算为 CSS 视口像素；
也可明确提供 `css_viewport` 坐标。

观察需要声明 `supports_image_input: true` 的模型。工具沿用 Focus 的访问策略和审计：
浏览器可以访问本地页面并产生副作用，因此观察/动作声明为 opaque 工具，受限文件模式沿用
既有审批机制，完全访问按既有规则放行；释放租约和只读下载状态查询不需要扩大访问权限。
`busy`、`foreground:false`、`command_processed` 和 `result_unknown` 都保留原义。
工具失败返回 `status=error` 的 ToolMessage 及结构化原生错误，结果未知时不重放。

导航成为附件时可能返回 `download_started`，也可能先收到原生 `CONNECTION_ABORTED(9)`。
这两者都不能用来判断文件下载失败；应调用 `browser_list_downloads` 查询传输结果，禁止重复导航 / 点击。
四类交互事件会通知共享同一浏览器的活跃 Run，其他 Context 的独立浏览器不受影响。
模型在人工控制期间等待，交还提示带上最近下载 / 权限结果，明确要求新租约、新观察；用户取消或拒绝不是重试要求。

Windows Context 的 `browser_*` 名称由此适配层统一提供；发现池中的旧 Playwright MCP/
自定义同前缀浏览器工具不会装配进该 Context，防止操作另一个浏览器或产生同名冲突。
其他 MCP/自定义工具保持原装配流程。

## 接入位置和分发

- `backend/app/desktop/browser/transport.py`：管道线程、UTF-8 JSONL、多请求 ID 关联、交错事件、
  连接失效和超时结果未知。适配 Windows Selector 事件循环，持续读取，不按浏览器串行等响应。
- `browser/service.py`：Context 持久绑定、共享引用、Run 租约/续租/失效和事件路由。
  绑定保存于 `DesktopThread.ui_state._distinct_browser`；通用 UI 保存不能伪造或覆盖这个服务端字段。
- `DesktopService`：注入受信任的 Run 资源、内置工具目录与中间件。
  Context 装配在 durable dispatch 标记数据库 running 前确认创建成功；统一 `run_agent`
  在内存 running 前再次核对资源，finally 释放；没有新增 Run 执行器。
- `browser/routes.py`：既有会话认证下的 Context 查询、展示、更换与 SSE 快照。
  路径为 `/desktop/api/contexts/{context_id}/browser`、其 `/present`、`/pages`、`/downloads` 和 `/stream`。展示请求可带 `page_id`，只选择展示，不取得租约。
  更换使用 PUT `{ "source_context_id": "来源 Context ID" }`；`null` 表示显式新建独立实例。
- `desktop/src/renderer/features/context/context-browser.*`：Context 按钮、页面列表/展示、下载与权限面板、共享选择和实时状态；
  被任务视图、图检查器和 Loop 控制台复用。被动刷新不反复激活原生窗口。
- `desktop/resources/distinct-browser/`：固定版本 EXE 与 SHA-256 清单。
  `npm --prefix desktop run prepare:browser` 验证产物；托管安装/更新调用同一命令。

维护者重建产物使用 `npm --prefix desktop run build:browser`，需要 MSVC x64 与 Windows SDK。
脚本检出固定基线并调用上游构建脚本（包括其控制状态机测试），然后更新 EXE 和摘要。
也可向 `desktop/scripts/build-browser.ps1` 传 `-MsvcRoot`、`-WindowsSdkRoot`。
开发时可用 `FOCUS_DISTINCT_BROWSER_EXE` 指定兼容协议 v1 的本地 EXE。

## v0.6 可选恢复：连接原宿主

Windows 随包提供 v0.6.1 EXE，不需要额外下载或设置外部产物路径。显式开启恢复：

```powershell
$env:FOCUS_DISTINCT_BROWSER_RECOVERY = '1'
npm --prefix desktop start
```

此前设置过 `FOCUS_DISTINCT_BROWSER_EXE` 的开发环境可先移除该覆盖，确保使用内置版本。
不开启恢复时仍兼容原有 JSONL v1；已有 v0.5 匿名管道绑定显示不可恢复，需要用户显式更换，
不会因安装了新版而创建替代页面或继续旧 Run。恢复继续保持显式启用；物理键鼠与真实 IME 验收尚待完成。

启动器把所选 EXE 按 SHA-256 缓存在 `%LOCALAPPDATA%\Focus\BrowserRuntime`，
然后由一个短暂的独立父进程启动 `--reconnect` 宿主。原安装路径可以更换，
存活宿主的缓存不会被更新脚本覆盖或清理；启动失败不退回受父 Job 约束的模式。
测试可通过 `FOCUS_DISTINCT_BROWSER_CACHE` 使用独立目录。不要在宿主仍存活时清理缓存。

Context 保存 `browser_id`、`host_id`、受信任的本机 endpoint 路径和产物摘要。
端点与管道 DACL 只授予账户 SID；连接前核对账户 SID 和登录 SID、受保护 ACL、DPAPI 凭据、进程创建时间、真实映像路径、
映像摘要及 Named Pipe 服务端 PID，并核对真实宿主进程的账户 SID 和登录 SID。明文凭据不写数据库、日志或前端。
管道编解码器基于上游同版本演示客户端，使用 Python 标准库与 Windows API。

后端启动时按宿主恢复一次，以 `snapshot.revision` 为基线应用同一 `host_id` / `connection_id`
的后续事件；快照期间先缓冲事件，每 5 秒发送保活。恢复同一工作区的共享绑定时，
它们仍指向同一个真实浏览器、页面及登录环境。页面列表和当前选中页来自原宿主。
旧版匿名管道绑定、退出的宿主、身份不符或缺少缓存都显示不可恢复，绝不自动重建。
竞争客户端占用时显示连接冲突，可在**浏览器绑定 → 重新连接原宿主**中显式重试。

强制终止宿主可能留下受保护的端点文件。恢复失败时，客户端只在固定宿主标识和产物摘要均匹配、
端点账户/登录身份与当前调用者一致、操作系统确认 PID 已退出且对应管道不存在时，定向清理规范端点文件与空目录。
权限不足、身份不符、活进程、占用中的管道或路径/ACL 异常均保留文件；清理使用已验证的文件句柄，
不扫描或递归删除目录。失败绑定仍报告不可恢复，不创建替代页面。EXE 版本缓存与浏览数据保留，
避免破坏其他仍在运行或尚待恢复的宿主；这里不自动回收旧版本缓存。

恢复只恢复页面及状态。原 Run 沿用现有耐久恢复规则标记中断，旧租约、截图和动作不继续。
界面显示 `interrupted_operation` 的来源页、已派发/完成步数及结果未知提示；迟到回调只更新收尾信息。
用户检查页面并在原生工具栏**交还**后，新执行才能取得新租约、重新观察。
下载/权限的断连收尾仍由基建处理；Focus 不补发下载、不代做网站权限决定。

测试入口为 `backend/tests/test_browser_recovery.py`、`backend/tests/test_browser_endpoint_cleanup.py`
和 `backend/tests/test_browser_security.py`，显式设置 `FOCUS_BROWSER_RECOVERY_REAL=1`
和 `FOCUS_BROWSER_RECOVERY_EXE` 后运行。使用独立数据库与缓存，真实 Gateway 的模型采样被固定，
其余持久化、策略、Run 调度、浏览器工具和原生宿主均为实际实现。
系统消息交还、桌面自动化与真实物理设备验收分别记录。实际旧宿主拒绝用例可额外设置
`FOCUS_BROWSER_LEGACY_EXE` 指向固定摘要的 v0.6.0 EXE；只用于创建自有旧宿主，
不作为新版本的连接实现，也不读取用户原有端点。

2026-10-25 已完成实际 Electron、外部模型、正常/异常退出、真实托管更新和双客户端竞争验收，
恢复后保留原页面身份，旧 Run 不自动继续。强制宿主退出后的端点清理也通过真实 Windows 回归。
这些结果包含 Computer Use 与进程退出注入，不代表物理键鼠/IME、触摸/笔或跨屏 DPI 验收。
v0.6.1 上游已用临时普通账户完成真实跨用户测试（无需切换桌面）：不同账户即使共享交互登录 SID，
端点与管道也均返回拒绝访问，原账户仍能恢复。Focus 随包 EXE 与该实测产物 SHA-256 相同；
客户端配套验证与端点清理由 Focus 的 Windows 回归覆盖。恢复尚不默认开启。

v0.6.0 恢复端点采用登录 SID 的旧 ACL，不能作为安全恢复来源。新版客户端拒绝这类端点，
即使对应宿主已退出，也不自动修改 ACL 或清理旧端点。界面保留断连绑定并提示显式更换；
用户可先在旧窗口保存工作，再通过浏览器绑定显式更换浏览器。旧缓存和原页面不被重建或覆盖。
移除恢复开关或设置 `FOCUS_DISTINCT_BROWSER_RECOVERY=0` 仅回退到匿名管道，
不会让不安全端点重新可恢复，也不会自动替换既有绑定。

复跑命令及旧版接入基线见 [2026-10-24 接入验收](benchmarks/context-browser-2026-10-24/README.md)；该历史记录使用 8eea976，不作为新版的物理设备验收证明。
