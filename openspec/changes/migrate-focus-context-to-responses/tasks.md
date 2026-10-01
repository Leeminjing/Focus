## 1. Approval and implementation preflight

- [x] 1.1 获得用户对本 change artifacts 的明确同意后才进入 apply；验收：记录对应用户授权，不能把规划 complete 或本任务列表存在当作同意。
- [x] 1.2 改应用代码前重新发现并成功调用 Context7，核对 Responses、LangGraph state／checkpoint 和涉及的持久化合同；验收：保存新 preflight 文档与官方来源，工具缺失或不可用时立即停止并询问用户。
- [x] 1.3 采集实际依赖版本、模型配置格式及所有模型调用点，确定固定兼容范围和阶段开关；验收：提供调用角色清单、版本矩阵及从 Gate 1 到 Gate 4 的通过条件。

## 2. Typed history and lossless codecs

- [x] 2.1 建立 FocusItem envelope、已知 payload、scope、可信 provenance 和原生／unknown 保存合同；验收：schema 用例覆盖非法来源提升、未知类型和稳定身份。
- [x] 2.2 实现独立 canonical history codec，完整保存 Provider 元数据、Item 顺序、call IDs、phase、状态与 continuation；验收：reasoning＋并行 calls＋outputs＋unknown fixture roundtrip 无损。
- [x] 2.3 实现确定性 V1 adapter，保留宿主可证来源并标注 legacy-unknown；验收：相同输入身份稳定，正文标签不能产生授权，旧序列化内容不被修改。
- [x] 2.4 实现 typed call／output 关联与 repair 合同，迁移旧 H／A／T 闭合职责；验收：并行调用、孤立输出、重复 ID、缺少结果和 repair error 来源用例通过。
- [x] 2.5 将 UI display codec 与权威 persistence codec 分离；验收：隐藏／折叠内部 Items 不影响恢复，现有正式消息、compression／curation 展示合同仍通过。

## 3. V2 revisions and checkpoint bridge

- [x] 3.1 添加扩展式 V2 history payload 持久化迁移和版本解析；验收：迁移前后 V1 内容、hash、来源边及 checkpoint 不变，未知 schema 可诊断拒绝。
- [x] 3.2 更新 Revision repository／reader，为 V2 明确 authored、execution、display 和 semantic 入口；验收：checkpoint-backed V1 与 V2 混合读取、新 authored 边界和 display 兼容测试通过。
- [x] 3.3 为 execution graph 增加 typed authority 与可验证 messages bridge、稳定关联及幂等 reducer；验收：并行工具结果不丢失，投影不一致阻止运行，不存在两份独立可写历史。
- [x] 3.4 更新 shadow checkpoint writer 使用无损 bridge 并保留 synthetic／repair 来源；验收：definition prefix＋真实 suffix 写读一致，shadow namespace 不能覆盖活动 Context。
- [x] 3.5 更新结算、hash 与 publication 以精确 checkpoint 写新 V2 Revision；验收：运行不原位修改已发布版本，旧 fence 不能发布，receipt 和 pointer 仍同事务。
- [x] 3.6 验证 V1 派生为 V2、rollback、merge、compression／restore 和 Provider continuation 失效；验收：父 runtime／opaque payload 不默认继承，原始来源引用仍可解析。

## 4. Semantic selection and index compatibility

- [x] 4.1 实现统一版本化 semantic selection 与四种资格；验收：runtime／reasoning／repair 不进入任务命题，index 不绕过原 claim verification。
- [x] 4.2 建立关联工具证据单元，保留调用名、参数、状态和来源；验收：evidence_only 不独立产生 fallback，孤立或 synthetic 输出不能作为已执行证据。
- [x] 4.3 调整 segmentation、fallback、coverage、grounding 和整体 interpretation 消费资格；验收：仅追加运行控制不增加任务假设或覆盖分母，跨段解释回归通过。
- [x] 4.4 更新 quote／ordinal 映射、evidence resolution 和多源重绑定；验收：排除中间 Items 后仍引用正确原始内容，来源身份不串线。
- [x] 4.5 让 RSI、Curator source projector、检索及 Context Expansion 统一读取 semantic 合同；验收：消费者清单无无差别 execution 抽取，工具证据和 selected 约束可用。
- [x] 4.6 升级索引及依赖 fingerprint，维持局部继承和整体解释规则；验收：旧 v5 缓存不误命中，新索引冷构建，兼容前缀可复用，旧索引记录不覆盖。

## 5. Checkpoint-bound WorldState

- [x] 5.1 实现稳定 section ID、full／diff 渲染及 known／unknown／absent 基线；验收：未变化不重复 append，新增／移除／replacement 都可解释。
- [x] 5.2 保存 snapshot、retained refs 和 renderer／projection 版本，校验 delta 依赖链；验收：压缩删除基线、版本变化或引用缺失触发完整 section 重建。
- [x] 5.3 在 sampling 前同一 checkpoint 提交 Items、snapshot 和请求来源 manifest；验收：持久化前／后故障注入不产生 snapshot 超前或重复 update，prepared 不冒充已完成采样。
- [x] 5.4 迁移 permissions／environment guidance，保留工具侧当前 SecurityContext 刷新；验收：sampling 后权限收窄、一次调用批准、child capabilities 及三种 access mode 回归通过。
- [x] 5.5 迁移 tools／skills catalog、agent／collaboration mode 和实际需要的其他 sections；验收：catalog 与真正 tools 一致，移除和条目改写可见，未知基线完整重建。
- [x] 5.6 接入分支续跑、rollback、新 workspace 派生和压缩恢复；验收：旧 checkpoint 不能恢复失效权限，新分支初始化当前完整状态，Round 冻结输入不被 diff。

## 6. Scoped context assembly and durable inbox

- [x] 6.1 建立按角色的 context assembly 端口，拆分基础行为、WorldState、任务历史和冻结认知引用；验收：角色请求 fixture 显示来源和作用域，Main 与认知 Worker 输入互不泄漏。
- [x] 6.2 迁移 memory／selected skill 为精确版本绑定，区分 catalog 与选择正文；验收：磁盘或 memory 更新不静默改变已 admission 的 Run，派生引用可继承或明确排除。
- [x] 6.3 迁移材料 policy／用户正文并维护本轮图像投影、must-view 和 read 声明；验收：未选历史图像不 replay 像素，必需图像每次存在、不可读可诊断、原图与 display 保留。
- [x] 6.4 拆分 inbox 获取和投递确认，生成 collaboration Items 与唯一 delivery refs；验收：checkpoint 前故障不丢消息，checkpoint 后 ack 前故障不重复正文。
- [x] 6.5 为模型尝试保存精确 source manifest，复用现有领域引用与尝试审计；验收：可还原基础行为、工具、selected、Round 和状态版本，日志不展开敏感原生载荷。
- [x] 6.6 从 service 与旧 prompt middleware 删除已替代的动态拼接职责；验收：同一块上下文不在 instructions 与 input 重复注入，工具权限执行职责仍有效。

## 7. Responses provider projection

- [x] 7.1 建立显式 provider／protocol／capabilities 和固定 legacy config 映射；验收：未知配置拒绝、能力缺失可见、不按模型名猜图像能力、不静默 Chat fallback。
- [x] 7.2 实现 OpenAI Responses 合法 input、request instructions／tools 和手工 replay；验收：实际 payload fixture 使用 store=false，无 previous_response_id／conversation，原生 continuity 保留。
- [x] 7.3 实现 DeepSeek Responses 投影与必要窄适配；验收：policy 使用有效 system 层级，省略不支持参数，工具与内容块限制在发送前校验。
- [x] 7.4 为 Focus Registry／MCP／plugin 工具实现 function schema 与 call output 投影；验收：并行结果关联正确，不依赖不支持的 hosted／custom tools，安全 middleware 仍裁决执行。
- [x] 7.5 迁移 structured output、schema 校验、拒绝与 modality 处理；验收：Patrol／Curator／投影／评审 fixtures 使用 Responses 参数，不完整或非法结果不进入领域 publication。
- [x] 7.6 将全部角色工厂接到明确协议入口并固定依赖兼容范围；验收：Main、Teammate、Worker 和各认知调用清单逐项通过 payload 合同，不存在隐藏旧调用路径。

## 8. Stream normalization and settlement

- [x] 8.1 实现按 Item／content identity 收束的原生 event decoder；验收：交错 Items、done、重复事件和 unknown 输出 fixtures 保持顺序及去重。
- [x] 8.2 分别适配 OpenAI reasoning summary 与 DeepSeek reasoning_text 增量；验收：tokens 与 reasoning 不混入参数、工具结果、控制或承诺子图输出，原生载荷独立保存。
- [x] 8.3 组装并验证完整 function arguments 后才允许工具执行；验收：部分 JSON、无效参数、冲突 ID 和 incomplete 输出不会产生工具副作用。
- [x] 8.4 显式处理 completed／incomplete／failed／refusal／取消／断流，并接现有 Run finalizer；验收：有 token 不等于成功，迟到响应不能覆盖取消或旧 fence，重试有明确上限。
- [x] 8.5 隔离 partial audit 与可 replay 完成历史，维护工具不确定执行策略；验收：已保存结果不重复执行，副作用发生但结果未知时阻止盲目重试。
- [x] 8.6 标准化实际 attempt usage 和完整请求窗口估算；验收：重复终态不双计量，缺失指标为 unknown，instructions／tools／schema／图片计入且与压缩 gate 一致。
- [x] 8.7 连接现有 display／snapshot reducer 并保持消息身份与有界渲染；验收：最终快照不重复正文／reasoning／工具行，大输出不回到助手流通道。

## 9. Integration gates and failure recovery

- [x] 9.1 通过 Gate 1 的 V1／V2、codec／bridge、typed 协议及 semantic 集成套件后才继续状态迁移；验收：形成对 specs 场景的覆盖记录，旧 authority hash 不变。
- [x] 9.2 通过 Gate 2 的 WorldState、branch、mailbox 和 prepared checkpoint 故障注入；验收：崩溃恢复不丢状态、权限和引用不泄漏，全部精确基线可验证。
- [x] 9.3 跑真实数据库的 Revision／checkpoint／publication／delivery 集成测试；验收：事务、租约、fencing 和唯一 identity 在并发／重启情况下成立，不凭 mock 宣称原子性。
- [x] 9.4 为 OpenAI／DeepSeek 跑离线完整 wire／SSE 合同并记录受控真实 smoke；验收：工具回合、structured output、reasoning replay 和可用终态逐项有证据，未运行项目明确标注。
- [x] 9.5 回归 Loop Observation／Progress／Lineage、Kernel、CompletionGuard、Curator 质量、Compression 和 security；验收：冻结输入与发布权威不被 Provider 或新 assembler 绕过。
- [x] 9.6 回归桌面 live、材料／must-view、compression 缩略图、长会话与 stream 隔离；验收：现有用户行为和前端成本边界保持，失败／中断在 UI 可见。

## 10. Cutover, cleanup and documentation

- [x] 10.1 以阶段 gate 迁移默认 Responses 配置和新 Run admission，禁止混合 writer；验收：显式协议可查询，V2 活动执行由兼容版本处理，旧配置确定性解释。
- [x] 10.2 演练停止 V2 admission、V2 历史读取及新分支显式 legacy 回退；验收：不删除 V2 数据、不 downgrade 运行中 schema、不把 opaque continuation 塞入 Chat 历史。
- [x] 10.3 清理已替代 prompt 拼接、UI persistence 依赖和重复协议转换，审查模块依赖／单一职责／helpers 可见性；验收：没有 Desktop ORM 反向进入 Harness history，没有混合 Provider 巨型类或重复可写历史。
- [x] 10.4 更新每个修改文件的声明式头部注释和架构／配置文档；验收：头部写明公开接口、输入含义、输出含义、流程与示例，文件其余处仅必要注释，长方法按职责拆分。
- [x] 10.5 完成 OpenSpec strict 校验和六份规格场景的验收映射；验收：记录真实测试命令、结果、依赖版本与尚未验证项，所有实现任务完成前不宣告迁移完成。

## 11. Independent verification corrections

- [x] 11.1 修复 F1：统一在历史重建后准备当前 Run 的冻结上下文和耐久 checkpoint，补真实 compression interrupt/apply 首次 sampling 回归。
- [x] 11.2 修复 F2：宿主来源决定 Provider 指令层级，Curator copy/compose 不得提升引用权威，覆盖双 Provider 与混合来源。
- [x] 11.3 修复 F3：服务端 admission 绑定真实用户／Patrol 委托的 Item 来源与精确 Run／directive／Round 引用，覆盖实际启动链路。
- [x] 11.4 修复 F4：保存并校验实际输入前缀证明，前缀变化在权威 checkpoint 显式重建执行分支，覆盖图像和旧 Run 过滤。
- [x] 11.5 修复 F5：V1 semantic 从 canonical checkpoint 来源适配，保留宿主可证元数据与旧 hash，补混合旧历史回归。
- [x] 11.6 修复 F6：统一合成工具 repair 的 error 状态与来源审计，覆盖压缩拆分并行交换和 typed interruption。
- [x] 11.7 重跑必要集成／安全／桌面测试、检查模块与声明式头部，更新场景映射和独立验证报告。
