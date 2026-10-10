## Purpose

让 Focus 桌面操作的本地反馈及时可见，并使真实业务状态保持可信；通过可比较的硬件基线、指定数据负载、动态证据和增量更新约束，独立验证响应与流畅度，避免把时长参数、软件截图或功能通过当作 Fast 的完成证明。

## ADDED Requirements

### Requirement: Local feedback does not wait for a request or animation
The frontend SHALL 对按压、选择、展开、关闭及时提供本地反馈，MUST NOT 为播放完整动效而锁住用户。真实请求 SHALL 分别呈现请求中、已受理、失败和实际生效，不能提前显示成功或伪造执行进展。

#### Scenario: The backend response is delayed
- **WHEN** 用户展开检查或触发真实控制请求，而网络延迟
- **THEN** 本地操作反馈立即可见，已存在的输入继续可用；相关请求显示 pending，业务状态仍取最后可信投影，返回失败时显示错误而非成功

#### Scenario: The server accepts input before its effect is committed
- **WHEN** 用户输入已获得受理回执，但目标或任务状态尚未提交
- **THEN** 界面明确表达已受理，Task Progress、Observation 和 LoopFact 不因动画或回执被替换为尚未生效的结果

### Requirement: Visual effects preserve bounded incremental rendering
The frontend SHALL 复用按需读取、稳定节点、布局缓存和请求隔离；材质／动效 MUST NOT 新增不必要的常驻渲染循环、全页重建、每事件重复入场、重复连接或每帧大量布局读写。

#### Scenario: A burst of work events arrives
- **WHEN** 在已展开详情中接收指定压力规模的事件，且关系拓扑未变
- **THEN** 图坐标和输入节点继续稳定，事件按原序列规则处理，未变内容不重复创建，材质反馈不会对整页重新播放

#### Scenario: The interface settles after repeated open and close actions
- **WHEN** 用户完成50次开关并停止操作
- **THEN** 隐藏区域不继续绘制，无增长中的覆盖层、监听或未完成动画，也无新增装饰性后台循环

### Requirement: Fast is verified against a comparable baseline
The delivery SHALL 在改造前后使用同一硬件、窗口、缩放、数据和渲染模式记录本地反馈、关键切换、滚动及实时更新的性能；采用 design D8 中已批准的场景和拟定标准。报告 MUST 区分实际呈现延迟、rAF proxy、网络时间和软件／硬件路径，不承诺环境无关的绝对帧率。

#### Scenario: Only a software offscreen test is available
- **WHEN** 取得软件截图及点击→下一rAF时间，但无法测量实际硬件呈现
- **THEN** 报告保存该诊断与环境，并将硬件 Fast 验收列为未验证，不能据此宣布达标

#### Scenario: The refactor is measured under the approved load
- **WHEN** 同一环境下完成本地反馈、32／128 Context 负载、滚动与事件突发的前后采样
- **THEN** 报告记录数据规模、采样数、trace、分位数与异常，按已批数值和可观察连续性分别判定，不用“页面能打开”或“CSS时长短”替代结果

### Requirement: Evidence categories remain independent
The delivery SHALL 分别提供业务正确性、真实截图材质、真实动态转换和性能记录；生成效果图、测试 fixture 或终态截图 MUST NOT 冒充真实运行结果或过渡录屏。所有证据 SHALL 被实际查看或分析，未完成项明确标记。

#### Scenario: Business assertions pass but no dynamic capture exists
- **WHEN** 接口、身份和草稿断言通过，而关键过渡没有实际录屏或连续帧
- **THEN** 业务可报告通过，Fluid 动态验收仍标未验证，不能互相代替

#### Scenario: Material screenshots are compared
- **WHEN** 提供材质改造前后的截图
- **THEN** 窗口尺寸、缩放、数据与模式可比，并注明真实接口／fixture 范围；查看控制自身、文字和背景关系后独立判定，截图不能证明操作延迟

### Requirement: Delivery accounts for preserved scope and code growth
The delivery SHALL 对照已批准迁移清单分别列出完成、未完成和未验证范围，并分别报告业务代码与测试代码新增、删除和净增行数。规划、生成、第三方以及既有用户修改 SHALL 排除；删除有调用证据，兼容有保留理由，增长对应必要能力。

#### Scenario: A shared display helper is introduced
- **WHEN** 实施引入已批准的可见性 helper 并替代旧显隐／入场逻辑
- **THEN** 交付说明其实际职责、替换的调用与旧代码删除依据，确认没有第二业务状态源、控制器、图引擎或长期双重效果
