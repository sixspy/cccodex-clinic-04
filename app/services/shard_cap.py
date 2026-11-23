"""分片上限 · 甲侧：旁路账册（side-ledger）。

择定：甲侧。分片上限钉进旁路账册 `shard_cap_ledger`，以「业务键 + 指纹」落库；
读侧只认旁路账册，不回刷、不回落到台参数 `app_settings`。
台参数页只展「分片上限口径」，不承载可写上限值。

乙侧（双写两轨 + 过期档只读归档）骨架在择定前仅作对照，未启用即已清除，
本库只保留甲侧这一条命运。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from app.db import DEFAULT_SHARD_CAP, DEFAULT_SHARD_CONCURRENCY, SHARD_CAP_KEY, get_db
from app.services import audit as audit_svc
from app.services.errors import ServiceError

ACTIVE_SIDE = "甲"
CAP_TABLE = "shard_cap_ledger"
CAP_KEY = SHARD_CAP_KEY
CAP_SOURCE = "旁路账册"
FINGERPRINT_FIELDS = ("cap_key", "shard_cap", "shard_concurrency", "version")


def _canonical(payload: dict) -> str:
    return json.dumps(
        {field: payload.get(field) for field in FINGERPRINT_FIELDS},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def compute_fingerprint(payload: dict) -> str:
    """旁路账册字段指纹：对落库字段做规范化摘要，指纹与库表必须对得上。"""
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _row_to_payload(row: sqlite3.Row) -> dict:
    return {
        "cap_key": row["cap_key"],
        "shard_cap": int(row["shard_cap"]),
        "shard_concurrency": int(row["shard_concurrency"]),
        "version": int(row["version"]),
        "fingerprint": row["fingerprint"],
        "updated_by": row["updated_by"],
        "updated_at": row["updated_at"],
    }


def ensure_ledger(conn: sqlite3.Connection) -> None:
    """幂等补齐出厂落点行；既有库升级时也会补上。"""
    payload = {
        "cap_key": CAP_KEY,
        "shard_cap": DEFAULT_SHARD_CAP,
        "shard_concurrency": DEFAULT_SHARD_CONCURRENCY,
        "version": 1,
    }
    conn.execute(
        """
        INSERT OR IGNORE INTO shard_cap_ledger(
          cap_key, shard_cap, shard_concurrency, version, fingerprint, updated_by
        ) VALUES (?, ?, ?, ?, ?, 'system')
        """,
        (
            CAP_KEY,
            payload["shard_cap"],
            payload["shard_concurrency"],
            payload["version"],
            compute_fingerprint(payload),
        ),
    )


def _fetch(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM shard_cap_ledger WHERE cap_key=?", (CAP_KEY,)
    ).fetchone()


def landing_status(db_path: str | None = None) -> dict:
    """核本题落点是否真的落进业务库，以及字段指纹是否与库表对得上。"""
    with get_db(db_path) as conn:
        row = _fetch(conn)
    if not row:
        return {
            "landed": False,
            "fingerprint_ok": False,
            "table": CAP_TABLE,
            "cap_key": CAP_KEY,
            "source": CAP_SOURCE,
        }
    payload = _row_to_payload(row)
    return {
        "landed": True,
        "fingerprint_ok": compute_fingerprint(payload) == payload["fingerprint"],
        "table": CAP_TABLE,
        "cap_key": CAP_KEY,
        "source": CAP_SOURCE,
        "version": payload["version"],
    }


def read_cap(db_path: str | None = None, strict: bool = False) -> dict:
    """读侧唯一入口：只认旁路账册，不回落到台参数。

    strict=True 时用于补采闸：账册未落库 / 指纹对不上都直接拦。
    """
    with get_db(db_path) as conn:
        row = _fetch(conn)
    if not row:
        if strict:
            raise ServiceError("分片上限旁路账册未落库，补采闸已拦截")
        return {
            "cap_key": CAP_KEY,
            "shard_cap": DEFAULT_SHARD_CAP,
            "shard_concurrency": DEFAULT_SHARD_CONCURRENCY,
            "version": 0,
            "landed": False,
            "fingerprint_ok": False,
            "source": CAP_SOURCE,
        }
    payload = _row_to_payload(row)
    payload["landed"] = True
    payload["fingerprint_ok"] = (
        compute_fingerprint(payload) == payload["fingerprint"]
    )
    payload["source"] = CAP_SOURCE
    if strict:
        if not payload["fingerprint_ok"]:
            raise ServiceError("分片上限指纹与库表对不上，补采闸已拦截")
        if payload["shard_cap"] < 1:
            raise ServiceError("分片上限台账值无效，补采闸已拦截")
        if payload["shard_concurrency"] < 1:
            raise ServiceError("分片并发数台账值无效，补采闸已拦截")
    return payload


def write_cap(
    key: str,
    shard_cap_value: int,
    shard_concurrency: int,
    expected_version: int | None = None,
    actor: str = "admin",
    db_path: str | None = None,
) -> dict:
    """一次性写完整条上限；带版本 CAS 串行，两名值班不会互相覆盖。"""
    key = (key or "").strip() or CAP_KEY
    if key != CAP_KEY:
        raise ServiceError("分片上限业务键不匹配")
    try:
        cap = int(shard_cap_value)
        conc = int(shard_concurrency)
        expected = int(expected_version) if expected_version is not None else None
    except (TypeError, ValueError) as exc:
        raise ServiceError("分片上限与并发数必须是整数") from exc
    if cap < 1:
        raise ServiceError("分片上限至少为 1")
    if conc < 1:
        raise ServiceError("分片并发数至少为 1")

    with get_db(db_path) as conn:
        row = _fetch(conn)
        if not row:
            raise ServiceError("分片上限旁路账册未落库，不能写入")
        current = _row_to_payload(row)
        if not (compute_fingerprint(current) == current["fingerprint"]):
            raise ServiceError("分片上限指纹与库表对不上，先修复账册再写")
        if expected is not None and expected != current["version"]:
            raise ServiceError(
                f"分片上限版本冲突：期望版本 {expected}，当前版本 {current['version']}，"
                "请刷新后重写",
                409,
            )
        new_version = current["version"] + 1
        payload = {
            "cap_key": CAP_KEY,
            "shard_cap": cap,
            "shard_concurrency": conc,
            "version": new_version,
        }
        cur = conn.execute(
            """
            UPDATE shard_cap_ledger
            SET shard_cap=?, shard_concurrency=?, version=?, fingerprint=?,
                updated_by=?, updated_at=datetime('now')
            WHERE cap_key=? AND version=?
            """,
            (
                cap,
                conc,
                new_version,
                compute_fingerprint(payload),
                actor or "admin",
                CAP_KEY,
                current["version"],
            ),
        )
        if cur.rowcount != 1:
            raise ServiceError("分片上限版本冲突，请刷新后重写", 409)
    audit_svc.write_event(
        "写分片上限",
        "shard_cap_ledger",
        0,
        f"上限{cap}/并发{conc}@v{new_version}",
        actor=actor or "admin",
        db_path=db_path,
    )
    return read_cap(db_path=db_path)


def cap_criterion(db_path: str | None = None) -> dict:
    """台参数页只展口径：来源/表/是否落库，不展也不承载可写上限值。"""
    status = landing_status(db_path=db_path)
    return {
        "source": CAP_SOURCE,
        "side": ACTIVE_SIDE,
        "table": CAP_TABLE,
        "cap_key": CAP_KEY,
        "landed": status["landed"],
        "fingerprint_ok": status["fingerprint_ok"],
        "writable_via": "分片上限闸（旁路账册 CAS 写入）",
    }


def active_shard_counts(conn: sqlite3.Connection) -> dict[int, int]:
    rows = conn.execute(
        """
        SELECT shard_no, COUNT(*) AS c
        FROM backfill_jobs
        WHERE status IN ('排队','执行中') AND shard_no >= 1
        GROUP BY shard_no
        """
    ).fetchall()
    return {int(r["shard_no"]): int(r["c"]) for r in rows}


def pick_shard(conn: sqlite3.Connection, cap: dict) -> int:
    """择最低仍有空位的分片；全满则拦超额补采。"""
    counts = active_shard_counts(conn)
    limit = int(cap["shard_cap"])
    for shard in range(1, int(cap["shard_concurrency"]) + 1):
        if counts.get(shard, 0) < limit:
            return shard
    raise ServiceError(
        f"分片上限已满：并发 {cap['shard_concurrency']} 片 × 上限 {limit} 条，"
        "不能再排队超额补采"
    )


def shard_summary(db_path: str | None = None) -> dict:
    """缺口补采页 / 访视批次列表 / 对账矩阵共用的一口径分片摘要。"""
    cap = read_cap(db_path=db_path)
    with get_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT b.id AS batch_id, b.batch_code, s.name AS site_name,
                   COUNT(j.id) AS active_jobs,
                   COUNT(DISTINCT CASE WHEN j.shard_no >= 1 THEN j.shard_no END)
                     AS active_shards,
                   SUM(CASE WHEN j.status='排队' THEN 1 ELSE 0 END) AS queued_jobs,
                   SUM(CASE WHEN j.status='执行中' THEN 1 ELSE 0 END) AS running_jobs
            FROM visit_batches b
            JOIN trial_sites s ON s.id=b.site_id
            LEFT JOIN visit_rows r ON r.batch_id=b.id
            LEFT JOIN backfill_jobs j
              ON j.row_id=r.id AND j.status IN ('排队','执行中')
            GROUP BY b.id
            ORDER BY b.id
            """
        ).fetchall()
        totals = conn.execute(
            """
            SELECT COUNT(*) AS active_jobs,
                   COUNT(DISTINCT CASE WHEN shard_no >= 1 THEN shard_no END)
                     AS active_shards
            FROM backfill_jobs
            WHERE status IN ('排队','执行中')
            """
        ).fetchone()
    batches = []
    for r in rows:
        item = dict(r)
        item["shard_cap"] = int(cap["shard_cap"])
        item["shard_concurrency"] = int(cap["shard_concurrency"])
        batches.append(item)
    return {
        "side": ACTIVE_SIDE,
        "source": CAP_SOURCE,
        "landed": cap["landed"],
        "fingerprint_ok": cap["fingerprint_ok"],
        "version": cap["version"],
        "shard_cap": int(cap["shard_cap"]),
        "shard_concurrency": int(cap["shard_concurrency"]),
        "totals": {
            "active_jobs": int(totals["active_jobs"]),
            "active_shards": int(totals["active_shards"]),
        },
        "batches": batches,
    }
