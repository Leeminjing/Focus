# Follow-up repair verification

2026-10-01。用户“全部修复”授权后完成 V1／V2／V3。新 Context7 调用在修改应用代码前完成，依据见 follow-up-repair-preflight.md。当前任务 76/76；本次修复范围没有未解决的 WARNING／SUGGESTION，没有提交、推送或归档。

## Changes and evidence

| Finding | Fundamental correction | Regression |
|---|---|---|
| V1 | semantic_grounding 统一检查来源资格；局部 projection、整体 interpretation、最终 index 使用相同规则。仅 reference_only 不能独立生成 unit；evidence_only 保留 claim／hypothesis／implementation／verification／failure 结果，不能独立定义 decision／question；index 与 reference 的联合支持仍可用，confirmed 仍需要 verdict | test_semantic_eligibility_repairs.py：有意违反 prompt 的 confirmed／hypothesis drafts、三层处置一致、正常证据结果、联合任务支持 |
| V1 compatibility | 新索引为 revision-semantic-index-v8，局部及整体 fingerprint 纳入 grounding v2。proof 另存可读取的 grounding_version，旧缺失版本 payload 按原算法与序列化读取，旧 index／proof 身份不变；旧规则结果不能进入新 v8 projected inventory | 旧 local／whole proof roundtrip；v7 index 只读兼容与 v8 拒绝；原增量／缓存／综合套件及配置 fingerprint 变更冷构建 |
| V2 | history/task_contract.py 只负责 canonical 合同镜像；新增 NotRequired typed／legacy 来源标记。交付、TypedHistory、shadow writer 与压缩历史手术复用该合同；删除／替代同时清空 scalar，恢复从保留合同再生成，非空正文冲突仍拒绝，旧 string-only 保持明确 legacy | test_contract_history_repairs.py：真实 CompressionGate interrupt／apply，delete／replace／restore，重新构图续跑，内存及 PostgreSQL 精确 checkpoint，旧状态兼容；合同交付／publication 套件 |
| V3 | display 从 retained trigger 只读深拷贝 files 到合同替换行；canonical 原输入、合同身份及请求材料绑定不变 | Reader checkpoint／definition 两种视图与实际 events live snapshot 相同；编辑展示结果不会改变原消息或合同 |

三个原 strict xfail 已转为 backend 下的正式回归，旧 xfail 文件移除，不留下永久预期失败。原审计结论及日志保留在 follow-up-independent-verification.md 与 .tmp/independent-boundary-reproductions.txt；当前回归入口为 test_semantic_eligibility_repairs.py 和 test_contract_history_repairs.py，共 25 个新增用例。

模块边界：Desktop semantic 模块负责命题资格；Harness history 负责兼容镜像与展示；Commitment 仍负责批准生产，Compression 仍负责显式历史手术，publication 沿用既有权威。新增通用 history 模块不反向依赖 Desktop、Provider HTTP 或承诺 workflow。未添加新的批准 UI、权限来源或合同数据库。

## Actual final validation

Python 3.12；PYTHONPATH 为 repository 和 backend/packages/harness。

| Batch | Actual final result | Log |
|---|---|---|
| 合同／实际 Lead／恢复／PostgreSQL／publication／Reader／Inbox／Responses／安全／typed history／原压缩与快捷压缩，18 个模块 | 222 passed，11.05s | .tmp/follow-up-repair-history.txt |
| interpretation／evidence corpus／缓存／增量／派生／Reader／Curator／新宿主资格，8 个模块 | 151 passed，79.76s | .tmp/follow-up-repair-semantic-final.txt |
| Revision lifecycle／publisher／authority／DAG／Loop architecture，6 个模块 | 153 passed，4.67s | .tmp/follow-up-repair-publication.txt |
| desktop conversation events／reconciler／live consistency／render budgets／DOM／run-ui／compression／Curator，9 个文件 | 72 passed，387ms | .tmp/follow-up-repair-display.txt |
| 17 个本次修改／新增 Python 文件的语法、声明式头部、Harness 依赖方向 | passed | .tmp/follow-up-repair-checks.json |
| OpenSpec strict、git diff --check、场景／测试入口核对 | passed | 本次最终工具输出与 verify-report.json |

各批次有重叠，不能相加为全量数量。实施中首次运行发现新增 static helper 误标 classmethod 和旧测试固定 v7 名称；纠正后还发现 fingerprint VERSION 与 proof schema 混用。已区分 RECORD_VERSION 和缓存 VERSION，并重跑完整语义批次，最终 151 项通过；失败日志保留，未当作基线失败忽略。

## Limits

本次未执行全量 backend、真实 OpenAI／DeepSeek smoke 或交互 Electron E2E。实际 SDK wire／SSE 使用无网络 transport，数据库测试使用隔离 PostgreSQL；未操作用户生产 Context。此次结果只覆盖上述修复与必要相邻回归，不改变原 F1–F6、前期 migration 或仓库无关失败的历史证据。
