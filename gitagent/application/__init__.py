"""应用装配、配置和命令行入口。"""

from .bootstrap import LiveApplication, build_live_application
from .config import RuntimeConfig
from .service import GitAgentService, ServiceResult

__all__ = [
    "GitAgentService",
    "LiveApplication",
    "RuntimeConfig",
    "ServiceResult",
    "build_live_application",
]
