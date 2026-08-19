"""应用装配、配置和命令行入口。"""

from .config import CLIConfig
from .factory import LiveApplication, build_live_application
from .service import GitAgentService, ServiceResult

__all__ = [
    "CLIConfig",
    "GitAgentService",
    "LiveApplication",
    "ServiceResult",
    "build_live_application",
]
