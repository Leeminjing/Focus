## Why

Focus 已把统一 Patrol 输入、Context 检查和真实工作读面迁入现有前端，但材质与转换仍主要依赖实色卡片、统一按压缩放和立即显隐。此次在既有业务链路上补足可感知的连续性、及时反馈和控件自身的玻璃层次，使用户能够持续表达、查看工作局面并返回输入，同时保持阅读与执行身份稳定。

## What Changes

- 原位收敛视觉 token 与样式职责，明确内容层、浮动控制层和模态遮罩；玻璃用于 Patrol 输入外壳、导航控制、浮动工具条及浮层控制，不把会话、图节点、Progress、Observation 和事实表格全部玻璃化。
- 以“安静输入 → 工作详情 → Context 抽屉 → 关闭 → 安静输入”为首个实施闭环，保持同一个输入节点，建立可反向、可打断、来源明确的展开／退出体验。
- 调整已有 controller、Context inspector、picker 和 dialog 生命周期：局部反馈即时出现；隐藏终态退出布局、命中和键盘访问；过期动画、读请求和卸载清理不能影响当前界面。
- 保持六导航、设置、任务三栏及高级能力、真实 HTTP/Live/SSE、权限、冻结版本、受理／生效区分。材质和动效不修改业务状态。
- 按需把已验证的共用控件语言推广到任务、全图、插件、无工作区模式、记忆库和设置，清理本次替代的入场动画与重复视觉声明。
- 独立验收业务、截图材质、动态连续性、性能和可访问性；透明不可用、减少动画或高对比情况下保留可用的实色反馈。

## Capabilities

### New Capabilities

- `desktop-functional-materials`：内容／控制分层、背景参与的玻璃外壳、边缘厚度与交互光感、可读性及平台降级。
- `desktop-interaction-continuity`：Patrol 输入保留、详情与检查器连续转换、来源相关浮层、反向／取消／卸载以及现有能力保留。
- `desktop-rendering-responsiveness`：本地与请求反馈、稳定增量更新、可复测性能边界、分离的验收证据和代码量核算。

### Modified Capabilities

无。当前 `openspec/specs` 只有四项图片／多模态主规格；前端迁移规格仍在未归档 change 中。本次引用其业务合同，不修改或归档旧 change，也不把 Agent 架构列为新能力。

## Impact

- 基线：本地 `main`，`628c42d9993700b34ed08c6804bd4b6e3e7ebfa5`；2026-10-10 用 `git ls-remote origin HEAD refs/heads/main` 确认远程相同。保留既有 `docs/synthesis-support-repair.md` 修改和临时目录。
- 平台：Windows 11；实际安装 Electron 43.2.0 / Chromium 150.0.7871.129；原生 JavaScript DOM、CSS custom properties、既有 CodeMirror 编辑器。无 React、无新前端框架。
- 修改范围：`desktop/styles/{tokens,base,components,shell,workspace-patrol,views}.css`、相关原有视觉声明、`workspace-patrol-{view,controller,picker}.js`、`context-inspector.js`、必要的 `app.js` 挂载衔接与 `index.html`。图谱复用 PortfolioMapView 的缓存／对账，业务和后端合同保持。
- 最多新增一个窄职责的可见性过渡 helper（理由、接口和旧逻辑替代范围见 design）；不新增控制器、业务状态源、图引擎、服务端模型、依赖或构建配置。
- 主设计依据为本机 apple-design 的 Liquid Glass／Design improvement 模式；已调用 Context7 两次 resolve、三次 query，已读取 Apple WWDC25/219 转录及相关官方参考。详细来源、实测限制和迁移表在 design。
- **审批边界：本次仅规划。artifacts 完成必须停止；只有用户明确批准本 change 才能 apply。** 不部署、不更新安装应用，不把 CLI 的 apply-ready 当作批准。
