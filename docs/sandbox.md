# 沙箱机制

[返回 README](../README.zh-CN.md) · [English README](../README.md)

Focus 使用工具准入、文件访问策略和平台 Shell 后端共同约束工具执行。文件模式分为 `read-only`、`workspace-write` 和 `danger-full-access`；工具能力授权与文件模式分别检查。

## 工具准入与执行路径

普通会话与 Patrol Loop 的工具调用都经过准入检查。Shell 启动时冻结本次调用的身份、工作区和模式；单次审批只改变该次调用的上下文。

```mermaid
flowchart TD
    UI["会话文件模式设置"] --> DB["保存到会话"]
    DB --> CTX["服务端 SecurityContext"]
    CTX --> MW["工具准入中间件<br/>刷新模式、检查身份与工具能力"]

    MW --> FX{"工具效果类型"}

    FX -->|结构化文件工具| PATH["解析真实路径"]
    PATH --> POLICY["检查读写策略<br/>敏感路径另行审批"]
    POLICY --> IO["宿主进程执行文件操作"]

    FX -->|内置 Shell| BIND["冻结本次调用绑定<br/>身份、工作区、模式、审批"]
    BIND --> OS{"宿主平台"}
    OS -->|Windows| WIN["Windows ACL 后端"]
    OS -->|Linux / macOS| POSIX{"完全访问模式？"}
    POSIX -->|否| DENY["拒绝启动<br/>SANDBOX_UNAVAILABLE"]
    POSIX -->|是| FULL["宿主权限执行<br/>管理进程组"]

    FX -->|不透明本地工具| ASK["受限模式逐次审批"]
    ASK --> THIRD["工具原执行路径"]

    MW -->|显式申请更宽模式| APPROVE["单次人工审批"]
    APPROVE --> COPY["获批后复制本次调用上下文<br/>不修改会话模式或工具能力"]
    COPY --> FX
```

- **结构化文件工具**：如 `read_file`、`discover_files`、`write_file`、`edit_file`，可以在执行前解析真实读写目标。敏感路径审批指这一层的逐路径检查；Shell 不会预先解析命令涉及的每个路径。
- **敏感路径**：Focus 明确保护的凭据、配置、技能、工具和插件位置，例如 `~/.focus/.env` 与工作区 `.agents/skills`。凭据读取需要审批，其他这些位置通常允许读取；写入先受文件模式限制，通过后还需业务审批。
- **不透明本地工具**：Focus 无法从认可的效果契约确定其本机副作用的工具，未声明效果的工具默认属于这一类。受限模式逐次审批，完全访问模式在文件模式这一层直接放行；获批后仍沿工具原执行路径运行。
- **POSIX 后端**：这里主要指 Linux / macOS。当前仅支持完全访问 Shell；受限模式在命令执行前返回 `SANDBOX_UNAVAILABLE`，文件工具仍可使用。系统不会自动放宽模式。

## Windows Shell 执行生命周期

Windows 受限执行由 Focus 自有 Node / Koffi 桥调用 Win32，通过目录 ACL、Low 完整性标签、逐调用受限令牌和 Windows Job 实施约束。Node runner 是每次 Shell 调用的管理进程，真正的命令由它启动的 CMD、PowerShell 等目标进程执行。

```mermaid
sequenceDiagram
    participant B as WindowsAclBackend
    participant G as 授权与临时目录注册表
    participant R as Node runner
    participant W as Win32
    participant P as 目标进程及普通后代

    B->>B: 校验运行时、模块及真实路径

    opt workspace-write
        B->>G: 登记工作区准备尝试，拒绝父子根重叠
        B->>G: 按会话与真实工作区取得授权
        alt 首次准备
            G->>G: 创建 UUID 私有临时目录
            G->>W: 为临时目录及工作区设置能力 ACL 和 Low 标签
        else 已有授权
            G->>G: 复用私有临时目录及 SID
        end
        G-->>B: 工作区 SID、临时目录 SID
    end

    B->>R: 传入模式、边界、SID、命令及状态通道
    R->>R: 重验真实目录与 SID

    alt read-only 或 workspace-write
        R->>W: 创建 Low、WRITE_RESTRICTED 令牌
        Note over R,W: 只读不携带两个写入能力 SID
    else danger-full-access
        R->>R: 使用原用户权限
    end

    R->>W: 创建 kill-on-close Job
    R->>W: 挂起创建目标，仅显式继承标准句柄
    R->>W: 将目标加入 Job
    R->>R: 写入 started 状态与 PID
    R->>W: 恢复目标线程
    W->>P: 开始执行

    opt 超时或取消
        B->>R: 写入 cancel 控制文件
        R->>W: 终止整个 Job
    end

    P-->>R: 主进程退出
    R->>W: 终止遗留后代，等待活动进程归零
    R->>R: 写入退出结果及 jobDrained
    R-->>B: 标准流及退出结果
    B->>B: 校验状态回执、执行身份和清理证据

    opt 后端关闭或宿主正常退出
        B->>G: 撤销私有临时授权并删除目录
        Note over G,W: 工作区 ACL、Low 标签和准备清单保留
    end
```

`cancel` 控制文件用于进程间通知：Python 后端写入取消或超时原因，runner 轮询发现后终止 Job。写入文件表示请求停止，`jobDrained` 回执才证明整个 Job 已清空。

私有临时目录按“会话 + 真实工作区”在后端实例内复用，正常退出时统一释放。结构化文件工具的可写根包含共享平台临时区，与 Shell 的私有临时目录不同；显式授权时还可加入全局 Focus 配置目录。

## 约束范围

- Windows 受限执行标记为 `partial`，不保证读取保密、网络隔离、进程隐藏、独立用户身份或虚拟机级隔离。
- NTFS 硬链接可能让工作区内外路径指向同一文件对象；结构化路径检查仍存在检查到使用之间的竞态。
- 工作区 ACL 和 Low 标签可能持久保留。强杀宿主不能依赖正常退出清理；恢复核验也不等于自动清扫旧临时目录或还原 ACL。
- 启动准备失败不会自动降级执行。清理证据为 `unknown` 时保留 Run 清理屏障，待可靠回执或系统重启证据核验后再结算。

## 源码入口

- [工具准入](../backend/packages/harness/focus/security/middleware.py)、[文件策略](../backend/packages/harness/focus/security/policy.py)、[敏感路径](../backend/packages/harness/focus/security/authority.py)
- [Windows 后端](../backend/packages/harness/focus/sandbox/windows.py)、[POSIX 后端](../backend/packages/harness/focus/sandbox/posix.py)
- [Node runner](../desktop/src/main/windows-sandbox-run.mjs)、[Win32 进程管理](../desktop/src/main/windows-sandbox/process.mjs)
- [私有临时目录](../backend/packages/harness/focus/sandbox/session_temp.py)、[清理证据恢复](../backend/packages/harness/focus/sandbox/recovery.py)
