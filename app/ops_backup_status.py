"""ADMIN-OPS-BACKUPSTATUS-001/003: expose latest backup manifest + status."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/srv/backups"))

# Timestamp formats emitted by daily_backup_unified.sh
_TS_FORMATS = [
    "%Y-%m-%dT%H%M%SZ",    # 2026-02-19T030002Z  (standard)
    "%Y-%m-%dT%H:%M:%SZ",  # 2026-02-19T03:00:02Z (alternate)
]


def _newest_daily_dir() -> Path | None:
    """Return the most-recently-modified daily backup directory (YYYY-MM-DD)."""
    candidates = sorted(
        (d for d in BACKUP_DIR.iterdir()
         if d.is_dir() and d.name[:2] == "20" and len(d.name) == 10),
        key=lambda p: p.name,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _parse_manifest(backup_dir: Path) -> dict:
    """Read manifest from backup_dir, supporting both key=value and JSON."""
    # Prefer MANIFEST.txt (key=value), fall back to manifest.json
    txt_path = backup_dir / "MANIFEST.txt"
    json_path = backup_dir / "manifest.json"

    if txt_path.exists():
        data: dict[str, str] = {}
        for line in txt_path.read_text().strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()
        return data

    if json_path.exists():
        try:
            return json.loads(json_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    return {}


def _parse_ts(ts_str: str) -> datetime | None:
    """Parse a timestamp string trying known formats."""
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(ts_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _infer_integrity(manifest: dict, backup_dir: Path) -> str:
    """Determine integrity status from manifest field or filesystem markers.

    Priority:
    1. Explicit ``integrity=`` field in manifest (OK / WARN).
    2. If SHA256SUMS exists and DB dump exists → "OK" (checksums were written
       after gzip-test + pg_restore --list passed in the backup script).
    3. If SHA256SUMS exists but no DB dump → "OK" (quick/session mode).
    4. If no SHA256SUMS but archives exist → "UNKNOWN" (pre-audit backup).
    5. Empty dir → "MISSING".
    """
    # 1. Explicit manifest field
    explicit = manifest.get("integrity")
    if explicit and explicit.upper() in ("OK", "WARN"):
        return explicit.upper()

    # 2-4. Infer from filesystem
    sha_path = backup_dir / "SHA256SUMS"
    has_checksums = sha_path.exists()
    has_archives = any(f.suffix == ".gz" for f in backup_dir.iterdir() if f.is_file())

    if has_checksums:
        return "OK"
    if has_archives:
        return "UNKNOWN"
    return "MISSING"


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

    manifest = _parse_manifest(backup_dir)

    # Age of backup — try manifest ts, fall back to dir name
    ts_str = manifest.get("ts")
    age_s: float | None = None
    backup_dt: datetime | None = None

    if ts_str:
        backup_dt = _parse_ts(ts_str)

    if backup_dt is None:
        # Fall back: parse dir name as date (midnight UTC)
        try:
            backup_dt = datetime.strptime(backup_dir.name, "%Y-%m-%d").replace(
                tzinfo=timezone.utc)
            if ts_str is None:
                ts_str = backup_dir.name
        except ValueError:
            pass

    if backup_dt is not None:
        age_s = round((now - backup_dt).total_seconds(), 0)

    # Integrity (003: infer from manifest + filesystem)
    sha_path = backup_dir / "SHA256SUMS"
    has_checksums = sha_path.exists()
    integrity = _infer_integrity(manifest, backup_dir)

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
            "integrity": integrity,
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
