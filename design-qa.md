# 会话 Patrol 小兵视觉验收

## 同一视觉比较输入

| 参考设计 | 真实 Electron 实现（1280×840） |
| --- | --- |
| ![冷脸萌小兵参考设计](C:/Users/brubing/Downloads/冷脸萌小兵.png) | ![会话 Patrol 小兵实现](C:/Users/brubing/Desktop/ag-project/focus/openspec/changes/add-session-patrol-avatar/qa/session-patrol-avatar-1280x840.png) |

## 核对结果

- 角色主体：白色圆润机体、深色面罩、青蓝眼睛、顶部晶体、悬浮光环和阴影均保留。
- 会话呈现：小兵位于会话画布而非右侧检查器；气泡朝向小兵，且不改变原会话布局。
- 交互状态：七个状态资源与五个移动资源使用统一比例、锚点和透明画布。
- P1 修复：重新计算品红色键透明度，移除绿色边缘；为状态气泡补充对话尾巴。
- P0：无。
- P1：无未解决项。
- P2：无未解决项。

## 常驻待命 Patrol 验收

| 确定性 Electron 状态 | 真实网页零 Agent 会话 |
| --- | --- |
| ![待命 Patrol Electron 实现](C:/Users/brubing/Desktop/ag-project/focus/openspec/changes/add-session-patrol-avatar/qa/standby-patrol-1280x840.png) | ![待命 Patrol 真实网页](C:/Users/brubing/Desktop/ag-project/focus/openspec/changes/add-session-patrol-avatar/qa/standby-patrol-web-actual.png) |

- 常驻：真实网页的当前任务没有已投放 PatrolAgent，画布仍呈现一个待命小兵。
- 语义：气泡明确显示“Patrol 小兵 / 待命 / 尚未布置任务 / 布置任务”，没有伪装成运行中 Agent。
- 布局：小兵和气泡位于会话画布，不占用右侧检查器，不阻断消息与 Composer。
- 视觉：继续使用既有白色机体、深色面罩、青蓝发光、顶部晶体、光环与阴影；P0/P1/P2 无未解决项。

final result: passed
