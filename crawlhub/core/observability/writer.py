"""
================================================================================
 R7 Observability — Background Writer
================================================================================

后台线程从 Queue 取记录批量 fsync 落 jsonl。

设计（spec §3.5）：
  - 队列 maxsize=4096，满 → drop（不阻塞业务）+ counter
  - 每 256 条或 1s flush 一次
  - close() 等待 flush 完成（不留尾部数据）
  - 任何异常吞掉 + log warn（不能拖垮业务）

================================================================================
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class _RequestsWriter:
    """每个 TaskContext 一个实例；后台线程批量写。"""

    BATCH_SIZE = 256
    FLUSH_INTERVAL = 1.0  # seconds
    QUEUE_MAX = 4096

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._q: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=self.QUEUE_MAX)
        self._stop = threading.Event()
        self._dropped = 0
        self._written = 0
        self._fh = open(self._path, "a", encoding="utf-8")
        self._thread = threading.Thread(
            target=self._run,
            name=f"obs-writer-{self._path.name}",
            daemon=True,
        )
        self._thread.start()

    def put(self, record: dict[str, Any]) -> None:
        """非阻塞投递；队列满直接 drop + counter."""
        try:
            self._q.put_nowait(record)
        except queue.Full:
            self._dropped += 1

    def _run(self) -> None:
        batch: list[dict[str, Any]] = []
        last_flush = time.monotonic()
        while not self._stop.is_set() or not self._q.empty():
            timeout = max(0.05, self.FLUSH_INTERVAL - (time.monotonic() - last_flush))
            try:
                item = self._q.get(timeout=timeout)
            except queue.Empty:
                if batch:
                    self._flush(batch)
                    batch.clear()
                    last_flush = time.monotonic()
                continue
            if item is None:  # sentinel
                break
            batch.append(item)
            if len(batch) >= self.BATCH_SIZE:
                self._flush(batch)
                batch.clear()
                last_flush = time.monotonic()
        if batch:
            self._flush(batch)
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            logger.exception("[obs-writer] close failed for %s", self._path)

    def _flush(self, batch: list[dict[str, Any]]) -> None:
        try:
            for rec in batch:
                self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._fh.flush()
            self._written += len(batch)
        except Exception:
            logger.exception("[obs-writer] flush failed for %s", self._path)

    def close(self, timeout: float = 5.0) -> dict[str, int]:
        """优雅关闭：投递 sentinel 等线程退出。返回统计."""
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=timeout)
        return {"written": self._written, "dropped": self._dropped}

    @property
    def stats(self) -> dict[str, int]:
        return {"written": self._written, "dropped": self._dropped}
