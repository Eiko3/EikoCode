"""传输抽象（v6）：一次请求-应答的往返。

stdio 是持续管道（需要后台读线程），HTTP 是请求-响应（一次往返一条）。
两者对外统一为同一接口，客户端不关心底下是哪一种。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Transport(ABC):
    """一次「发请求、收应答」的通道。"""

    @abstractmethod
    def start(self) -> None:
        """建立连接（stdio 起进程，HTTP 无状态故为空操作）。"""

    @abstractmethod
    def request(self, payload: dict, timeout: float) -> dict:
        """发出请求并等待对应的应答（按标识关联）。"""

    @abstractmethod
    def notify(self, payload: dict) -> None:
        """发出通知，不等待应答。"""

    @abstractmethod
    def close(self) -> None:
        """温和关闭；必要时强杀，不留孤儿。"""

    @abstractmethod
    def kill(self) -> None:
        """强制终止本次在途操作（超时由执行器调用）。"""

    def drain_notifications(self) -> list[dict]:
        """取走已收到的通知（无标识报文），供上层处理。"""
        return []
