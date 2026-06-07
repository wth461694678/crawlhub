"""Global flux counter — system-wide download throughput, decoupled from tasks.

Design rationale (see also schema comment near `global_flux_counter`):

  Per-task `tasks.record_count` is a "进度 / 终态" gauge: it represents how
  many records a single run has produced so far. It is reset to 0 on retry,
  and the row disappears entirely on purge. That's the right semantic for
  the task list / task card UI.

  But the dashboard "system download speed" chart and the "lifetime
  downloaded count" stat card want pure FLUX semantics:
    * every record ever written counts;
    * retry / archive / purge MUST NOT reduce the value;
    * the curve must be monotonically non-decreasing so the speed chart
      (which differentiates this series) never shows a phantom dip.

  The previous implementation derived the global series from
  `sum_atomic_record_count() + in_flight_delta`, which dropped 100 records
  the instant a task was retried (record_count: 100 -> 0). That's the bug
  this module fixes.

How it works:

  GlobalFluxCounter is a thin in-memory counter (single int + lock) that
  is loaded from `global_flux_counter` at daemon boot and incremented by
  every TaskContext.write_record call (the precise leaf of the data path).

  The daemon sampler thread persists the snapshot every ~5s into:
    * `global_flux_counter` (single row)  -> recovery on restart
    * `global_flux_samples` (timeseries)  -> dashboard speed chart source

  Worst-case loss on hard kill: ~5s of writes (acceptable; this is observability,
  not accounting).

Thread safety:
  tick() and snapshot() take a coarse lock. Contention is negligible:
  write_record fires at most ~100s of times/sec across all tasks combined,
  and the lock holds for a single int add.
"""

from __future__ import annotations

import threading


class GlobalFluxCounter:
    """In-memory, lock-protected counter mirroring `global_flux_counter` table.

    One instance per Daemon. Inject into every TaskContext at construction
    so write_record can call .tick(1) on the leaf write path.

    Lifecycle:
      __init__   — load persisted total from store
      tick(n)    — increment by n records (called per write_record)
      snapshot() — return current total (called by sampler each tick)

    Persistence is the SAMPLER's job (every ~5s), not tick's, to keep
    write_record on the absolute hot path lock-free w.r.t. SQLite.
    """

    def __init__(self, store) -> None:
        self._store = store
        self._lock = threading.Lock()
        row = store.get_global_flux_counter()
        self._records = int(row.get("record_count", 0))

    def tick(self, records: int = 1) -> None:
        """Increment the running total. Negative values are clamped at 0
        delta (defensive — flux must never decrease)."""
        if records <= 0:
            return
        with self._lock:
            self._records += int(records)

    def snapshot(self) -> int:
        """Return the current total record count. O(1), lock-protected."""
        with self._lock:
            return self._records
