"""Context 策展 Patrol 的安全来源、自由计划、合法消息编译与单次模型调用边界。"""

from .contract import (
    CurationContractError,
    CompiledCuratedContext,
    ComposeMessage,
    CopyMessage,
    CuratedContextPlan,
    CurationSourceSnapshot,
    ToolExchange,
    ToolExchangeCall,
    build_curation_input,
    compile_curated_context,
    estimate_curation_tokens,
)
from .engine import (
    CURATOR_SYSTEM_CONTRACT,
    CurationEngine,
    CurationEngineError,
    CurationEngineResult,
    require_curation_model,
)
from .projector import CurationSourceProjector

__all__ = [
    "CurationContractError",
    "CompiledCuratedContext",
    "ComposeMessage",
    "CopyMessage",
    "CURATOR_SYSTEM_CONTRACT",
    "CurationEngine",
    "CurationEngineError",
    "CurationEngineResult",
    "CuratedContextPlan",
    "CurationSourceProjector",
    "CurationSourceSnapshot",
    "build_curation_input",
    "ToolExchange",
    "ToolExchangeCall",
    "compile_curated_context",
    "estimate_curation_tokens",
    "require_curation_model",
]
