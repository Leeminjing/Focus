# Responses 客户端复用与本地测量

2026-10-17，Windows 11、Python 3.12.10、OpenAI Python 2.43.0。完整逐次采样见 [local.json](local.json)。

## 实现范围

Gateway 的 `langgraph_runtime` 为同一个 AppConfig 绑定 `response_client_session`。多个短生命周期的
`StructuredWorkerModel` 和普通 Responses 模型共享 SDK 及其 HTTP 连接池。模型、消息、schema 绑定、
请求选项、callback 和用量收据仍逐请求创建；估算不创建 SDK 或 TLS 资源。legacy Chat 路径沿用现有行为。

共享键包括 Provider，以及有效连接选项和环境的 SHA-256 摘要：地址、凭据、代理及 NO_PROXY、
TLS 证书路径、SDK 的租户/自定义头环境、超时和重试设置。模型名、输出上限与 schema 不参与连接身份。
缓存键不包含明文密钥，连接池不参与配置或模型序列化。

每个模型配置条目记录其当前连接身份；该身份变化且没有其它条目继续使用旧身份时，将旧客户端从可复用
缓存移出。旧客户端保留到应用退出统一关闭，允许在途调用完成，也意味着频繁变更连接设置时会暂存多个旧客户端。
同步与异步资源独立懒创建，异步客户端禁止跨事件循环使用，关闭借用端口不会关闭共享连接。
显式注入 HTTP transport 的调用继续由调用方管理端口。AppConfig 的深拷贝保留同一连接所有者，供设置验证使用。

应用退出时先停止运行任务，再在原事件循环内关闭缓存和淘汰客户端；未使用的端口不会触发 SDK 创建。
SDK 的关闭要求参见 [OpenAI Python 资源管理](https://github.com/openai/openai-python#managing-http-resources)。

## 测量方法

```powershell
python backend/scripts/benchmark_response_clients.py --requests 20 --report docs/benchmarks/response-client-reuse-2026-10-17/local.json
```

脚本只使用本地 HTTP/1.1 持久连接服务和虚构凭据，不加载应用配置。服务端统计实际 accept 的 TCP 连接数，
客户端统计 AsyncOpenAI 创建数。每组串行执行 20 次，关闭重试；每次重新构造模型。关闭 SDK 的耗时在计时之外。

结构化组每次新建 Worker，执行真实请求估算和 schema 调用，计时截至校验后的结果返回。
流式组每次新建模型，计时截至首个非空正文 chunk，并继续读完终态以归还连接。
两组都从模型构造之前开始计时。warm P50/P95 使用除首个样本以外的 19 个样本，冷样本另存于 JSON。
最终数据在相关测试结束后单独运行获得。

| 场景 | 复用 | 请求数 | SDK 创建 | TCP 连接 | warm P50 | warm P95 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 结构化结果完成 | 关闭 | 20 | 20 | 20 | 278.940 ms | 412.799 ms |
| 结构化结果完成 | 开启 | 20 | 1 | 1 | 5.477 ms | 7.796 ms |
| 流式首字 | 关闭 | 20 | 20 | 20 | 307.891 ms | 380.489 ms |
| 流式首字 | 开启 | 20 | 1 | 1 | 3.648 ms | 5.152 ms |

连接数证明该路径可以跨模型实例复用真实 HTTP 连接。延迟数据仅反映本机 SDK/HTTP 初始化和本地请求开销，
没有远端 TLS 握手、Provider 排队或模型推理，不能换算为真实 Provider 固定节省的毫秒数。
每组首个请求仍支付初始化成本；不同组的冷样本还受到进程导入和系统缓存影响，不用于比较冷启动收益。

## 验证

148 个不同的相关测试用例通过，覆盖结构化预算、双 Provider/协议、代理回退、配置应用、设置保存和真实 Gateway 生命周期。
新增 17 个用例覆盖懒创建、并发 Worker 的输出和用量/收据隔离、凭据/地址/代理/NO_PROXY/租户/超时/重试变更、
多模型共享及独立凭据、跨事件循环拒绝、调用方 transport、失败与取消恢复、跨线程同步借用、退出清理和真实 TCP 复用。
连接回归使用 3 次请求并断言创建/连接次数，不对耗时设置不稳定的阈值。
