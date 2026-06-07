"""Local filesystem BlobStore implementation.

Manages task output directories under ~/.crawlhub/output/{YYYY-MM-DD}/{task_id}_{task_name}/
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from crawlhub.core.config import get_data_root
from crawlhub.core.interfaces import BlobStore


class LocalBlobStore(BlobStore):
    """Filesystem-based blob storage for task outputs."""

    def __init__(self, root: Path | None = None):
        self.root = root or get_data_root()
        self.output_root = self.root / "output"
        self.trash_root = self.root / "trash"
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.trash_root.mkdir(parents=True, exist_ok=True)
        self._ensure_readme()

    def _ensure_readme(self) -> None:
        """Write a warning README at the output root (idempotent)."""
        readme = self.output_root / "README.txt"
        content = (
            "CrawlHub Artifact Directory - DO NOT MOVE OR RENAME FILES\n"
            "==========================================================\n\n"
            "This directory is managed by CrawlHub. The web UI and downstream\n"
            "tasks reference these files by their original paths. Moving,\n"
            "renaming, or deleting anything here will:\n"
            "  - Break the task detail page (data preview will fail).\n"
            "  - Break downstream batch tasks that reference upstream run_id.\n"
            "  - Cause data to appear lost even though it still exists.\n\n"
            "If you need a copy of a task's output for your own use:\n"
            "  - CLI:  crawlhub task export <task_id> --output <your-path>\n"
            "  - MCP:  crawler_export_result(task_id, output_path=<your-path>)\n"
            "  - Web:  task detail page > Export button\n\n"
            "If you need to chain tasks, use items_from.sources.{alias}.run_id\n"
            "in the batch task definition - never point at files inside this\n"
            "directory directly.\n"
        )
        try:
            # Rewrite only if missing or content drifted; cheap O(1) check.
            if not readme.exists() or readme.read_text(encoding="utf-8") != content:
                readme.write_text(content, encoding="utf-8")
        except OSError:
            # README is best-effort; never fail BlobStore init because of it.
            pass

    def get_output_dir(self, task_id: str, task_name: str) -> str:
        """Create and return output directory: output/{YYYY-MM-DD}/{task_id}_{task_name}/"""
        date_str = time.strftime("%Y-%m-%d")
        # Sanitize task_name for filesystem
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_name)[:50]
        dir_name = f"{task_id}_{safe_name}" if safe_name else task_id
        output_dir = self.output_root / date_str / dir_name
        output_dir.mkdir(parents=True, exist_ok=True)
        return str(output_dir)

    def write_record(self, output_dir: str, record: dict) -> None:
        """Append a JSON record to data.jsonl."""
        data_path = Path(output_dir) / "data.jsonl"
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with open(data_path, "a", encoding="utf-8") as f:
            f.write(line)

    def write_asset(self, output_dir: str, filename: str, data: bytes) -> str:
        """Write binary asset to assets/ subdirectory."""
        assets_dir = Path(output_dir) / "assets"
        assets_dir.mkdir(exist_ok=True)
        filepath = assets_dir / filename
        with open(filepath, "wb") as f:
            f.write(data)
        return f"assets/{filename}"

    def read_records(
        self, output_dir: str, offset: int = 0, limit: int = 100, filter_expr: str | None = None
    ) -> list[dict]:
        """Read records from data.jsonl with pagination."""
        data_path = Path(output_dir) / "data.jsonl"
        if not data_path.exists():
            return []

        records = []
        current = 0
        with open(data_path, "r", encoding="utf-8") as f:
            for line in f:
                if current < offset:
                    current += 1
                    continue
                if len(records) >= limit:
                    break
                try:
                    record = json.loads(line.strip())
                    if filter_expr:
                        # Simple key=value filter
                        if _matches_filter(record, filter_expr):
                            records.append(record)
                    else:
                        records.append(record)
                except json.JSONDecodeError:
                    continue
                current += 1
        return records

    def write_summary(self, output_dir: str, summary: dict) -> None:
        """Write summary.json."""
        summary_path = Path(output_dir) / "summary.json"
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

    def get_summary(self, output_dir: str) -> dict | None:
        """Read summary.json."""
        summary_path = Path(output_dir) / "summary.json"
        if not summary_path.exists():
            return None
        with open(summary_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def list_files(self, output_dir: str) -> list[dict]:
        """List files in output_dir with metadata."""
        result = []
        output_path = Path(output_dir)
        if not output_path.exists():
            return result

        for item in sorted(output_path.rglob("*")):
            if item.is_file():
                rel_path = str(item.relative_to(output_path)).replace("\\", "/")
                info: dict[str, Any] = {
                    "path": rel_path,
                    "size": item.stat().st_size,
                }
                # Count rows for JSONL files
                if item.suffix == ".jsonl":
                    with open(item, "r", encoding="utf-8") as f:
                        info["rows"] = sum(1 for _ in f)
                else:
                    info["rows"] = None
                result.append(info)
        return result

    def move_to_trash(self, output_dir: str, trash_dir: str | None = None) -> str:
        """Move output_dir to trash."""
        src = Path(output_dir)
        if not src.exists():
            return ""
        dest_root = Path(trash_dir) if trash_dir else self.trash_root
        dest = dest_root / src.name
        # Handle name collision
        if dest.exists():
            dest = dest_root / f"{src.name}_{int(time.time())}"
        shutil.move(str(src), str(dest))
        return str(dest)

    def purge(self, trash_path: str) -> None:
        """Permanently delete from trash."""
        path = Path(trash_path)
        if path.exists():
            shutil.rmtree(path)

    def disk_free_bytes(self) -> int:
        """Return free disk space on the output volume."""
        usage = shutil.disk_usage(str(self.output_root))
        return usage.free


def _matches_filter(record: dict, filter_expr: str) -> bool:
    """Simple filter: key=value or key~=partial_match."""
    if "~=" in filter_expr:
        key, value = filter_expr.split("~=", 1)
        return value.lower() in str(record.get(key.strip(), "")).lower()
    elif "=" in filter_expr:
        key, value = filter_expr.split("=", 1)
        return str(record.get(key.strip(), "")) == value.strip()
    return True
