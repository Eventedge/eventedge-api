"""ADMIN-OPS-BACKUPSTATUS-001: expose latest backup manifest + status."""

from __future__ import annotations

import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/srv/backups"))


def _newest_daily_dir() -> Path | None:
    """Return the most-recently-modified daily backup directory (YYYY-MM-DD)."""
    candidates = sorted(
        (d for d in BACKUP_DIR.iterdir()
         if d.is_dir() and d.name[:2] == "20" and len(d.name) == 10),
        key=lambda p: p.name,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _parse_manifest(path: Path) -> dict:
    """Parse a key=value MANIFEST.txt into a dict."""
    data: dict[str, str] = {}
    for line in path.read_text().strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data


def _file_list(backup_dir: Path) -> list[dict]:
    """Return a list of files in the backup directory with size."""
    files = []
    for f in sorted(backup_dir.iterdir()):
        if not f.is_file():
            continue
        stat = f.stat()
        files.append({
            "name": f.name,
            "size_bytes": stat.st_size,
            "size_human": _human_size(stat.st_size),
        })
    return files


def _human_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f}{unit}"
        nbytes /= 1024  # type: ignore[assignment]
    return f"{nbytes:.1f}TB"


def build_backup_status() -> dict:
    now = datetime.now(timezone.utc)

    if not BACKUP_DIR.is_dir():
        return {
            "ok": False,
            "generated_at": now.isoformat(),
            "error": "backup directory not found",
        }

    backup_dir = _newest_daily_dir()
    if backup_dir is None:
        return {
            "ok": False,
            "generated_at": now.isoformat(),
            "error": "no daily backup directories found",
        }

    manifest_path = backup_dir / "MANIFEST.txt"
    manifest = _parse_manifest(manifest_path) if manifest_path.exists() else {}

    # Age of backup
    ts_str = manifest.get("ts")
    age_s: float | None = None
    if ts_str:
        try:
            backup_dt = datetime.strptime(ts_str, "%Y-%m-%dT%H%M%SZ").replace(
                tzinfo=timezone.utc)
            age_s = round((now - backup_dt).total_seconds(), 0)
        except ValueError:
            pass

    # SHA256SUMS presence
    sha_path = backup_dir / "SHA256SUMS"
    has_checksums = sha_path.exists()

    # Disk usage
    disk = shutil.disk_usage(str(BACKUP_DIR))

    # File inventory
    files = _file_list(backup_dir)
    total_bytes = sum(f["size_bytes"] for f in files)

    # Health classification
    if age_s is not None:
        if age_s <= 90_000:      # ~25 hours
            health = "ok"
        elif age_s <= 180_000:   # ~50 hours
            health = "stale"
        else:
            health = "missed"
    else:
        health = "unknown"

    return {
        "ok": True,
        "generated_at": now.isoformat(),
        "health": health,
        "latest": {
            "dir": backup_dir.name,
            "ts": ts_str,
            "age_s": age_s,
            "mode": manifest.get("mode", "unknown"),
            "size": manifest.get("size"),
            "integrity": manifest.get("integrity"),
            "has_checksums": has_checksums,
            "contents": manifest.get("contents"),
            "file_count": int(manifest["files"]) if "files" in manifest else len(files),
        },
        "files": files,
        "total_bytes": total_bytes,
        "total_human": _human_size(total_bytes),
        "disk": {
            "total_gb": round(disk.total / (1024**3), 1),
            "free_gb": round(disk.free / (1024**3), 1),
            "used_pct": round((disk.used / disk.total) * 100, 1),
        },
        "retention_dirs": sorted(
            [d.name for d in BACKUP_DIR.iterdir()
             if d.is_dir() and d.name[:2] == "20" and len(d.name) == 10],
            reverse=True,
        ),
    }
