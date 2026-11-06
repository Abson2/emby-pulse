from dataclasses import dataclass
from typing import Callable, List, Optional


ServiceCallback = Callable[[], None]


@dataclass
class BootstrapService:
    name: str
    start: ServiceCallback
    stop: Optional[ServiceCallback] = None
    started: bool = False


class BootstrapServiceRegistry:
    """Process-local lifecycle registry for bootstrap-started services."""

    def __init__(self) -> None:
        self._services: List[BootstrapService] = []

    def register(self, name: str, start: ServiceCallback, stop: Optional[ServiceCallback] = None) -> None:
        self._services.append(BootstrapService(name=name, start=start, stop=stop))

    def start_all(self) -> None:
        import logging
        _log = logging.getLogger("uvicorn")
        for service in self._services:
            if service.started:
                continue
            try:
                service.start()
                service.started = True
            except Exception as e:
                # 单个服务启动失败不应中断其它服务（尤其是 notifications 事件订阅），
                # 否则会出现「求片等直连功能正常、但所有基于事件总线的通知静默失效」。
                _log.error(f"[启动] 服务 {service.name} 启动失败（已跳过，不影响其它服务）: {e}")

    def stop_all(self) -> None:
        for service in reversed(self._services):
            if not service.started:
                continue
            try:
                if service.stop:
                    service.stop()
            finally:
                service.started = False

    def started_names(self) -> List[str]:
        return [service.name for service in self._services if service.started]

    def clear(self) -> None:
        self._services.clear()
