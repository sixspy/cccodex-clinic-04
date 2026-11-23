"""分片上限链路验收用例（先于改码落笔）。

题面落点：
- 分片上限钉进旁路账册（甲侧），读侧只认旁路账册，不回刷台参数。
- 分片上限要能落库（落点行）、指纹要与库表对齐、超额补采要拦。
- 两名值班抢写同一分片上限须串行，不能互相覆盖。
- 失败重试不得把队列拖进死循环。
- 缺口补采页、访视批次列表、对账矩阵都要挂分片摘要与分片并发数。
- 择定甲侧后，乙侧闲置骨架必须清掉。
"""

from __future__ import annotations

import importlib.util

import pytest

from app.db import get_db
from app.services import backfill, protocols, reconcile, settings, shard_cap, sites, visits
from app.services.errors import ServiceError


def _version(db_path: str) -> int:
    return shard_cap.read_cap(db_path=db_path)["version"]


def _gap_batch(db_path: str, rows: int = 3):
    site = next(s for s in sites.list_sites(db_path) if s["status"] == "启用")
    proto = protocols.active_protocol(db_path)
    batch = visits.create_batch(
        site["id"],
        protocol_id=proto["id"] if proto else None,
        note="分片上限用例批",
        db_path=db_path,
    )
    created = []
    for i in range(rows):
        created.append(
            visits.add_visit_row(
                batch["id"],
                f"SH-{i:03d}",
                "V1",
                payload="{}",
                quality="缺字段",
                db_path=db_path,
            )
        )
    return batch, created


def _clear_active_jobs(db_path: str) -> None:
    """清掉种子等历史在册任务，让分片上限用例自带确定性起点。"""
    with get_db(db_path) as conn:
        conn.execute("DELETE FROM backfill_jobs")


def test_landing_point_persisted_in_business_db(db_path):
    status = shard_cap.landing_status(db_path=db_path)
    assert status["landed"] is True
    assert status["table"] == "shard_cap_ledger"
    assert status["cap_key"] == shard_cap.CAP_KEY
    assert status["fingerprint_ok"] is True
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM shard_cap_ledger WHERE cap_key=?", (shard_cap.CAP_KEY,)
        ).fetchone()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(backfill_jobs)")}
    assert row is not None
    assert row["shard_cap"] >= 1
    assert row["shard_concurrency"] >= 1
    assert row["version"] >= 1
    # 分片落点必须真的进业务库：backfill_jobs 有 shard_no 列
    assert "shard_no" in cols


def test_read_side_only_trusts_side_ledger(db_path):
    # 台参数里塞一个诱饵值，读侧不得回刷/回落到它
    with get_db(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_settings(key, value, label) VALUES (?,?,?)",
            ("backfill_shard_cap", "999", "诱饵：不得被读侧采用"),
        )
    cap = shard_cap.read_cap(db_path=db_path)
    assert cap["source"] == "旁路账册"
    assert cap["shard_cap"] != 999
    assert cap["shard_concurrency"] != 999


def test_missing_landing_blocks_backfill_even_with_badge(db_path):
    with get_db(db_path) as conn:
        conn.execute("DELETE FROM shard_cap_ledger")
    assert shard_cap.landing_status(db_path=db_path)["landed"] is False
    # 非严格读只返回未落库信号，但补采闸必须严格拦截
    relaxed = shard_cap.read_cap(db_path=db_path)
    assert relaxed["landed"] is False
    with pytest.raises(ServiceError) as excinfo:
        shard_cap.read_cap(db_path=db_path, strict=True)
    assert "账册" in excinfo.value.message or "落" in excinfo.value.message
    _batch, rows = _gap_batch(db_path, 1)
    with pytest.raises(ServiceError) as excinfo:
        backfill.enqueue_from_row(rows[0]["id"], db_path=db_path)
    assert "账册" in excinfo.value.message or "落" in excinfo.value.message


def test_fingerprint_mismatch_is_rejected(db_path):
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE shard_cap_ledger SET fingerprint='deadbeef' WHERE cap_key=?",
            (shard_cap.CAP_KEY,),
        )
    status = shard_cap.landing_status(db_path=db_path)
    assert status["landed"] is True
    assert status["fingerprint_ok"] is False
    with pytest.raises(ServiceError) as excinfo:
        shard_cap.read_cap(db_path=db_path, strict=True)
    assert "指纹" in excinfo.value.message
    # 指纹对不上仍放行补采 = 失格
    _batch, rows = _gap_batch(db_path, 1)
    with pytest.raises(ServiceError):
        backfill.enqueue_from_row(rows[0]["id"], db_path=db_path)


def test_cap_write_serialized_by_version_cas(db_path):
    base = _version(db_path)
    updated = shard_cap.write_cap(
        shard_cap.CAP_KEY,
        shard_cap_value=9,
        shard_concurrency=2,
        expected_version=base,
        db_path=db_path,
    )
    assert updated["shard_cap"] == 9
    assert updated["shard_concurrency"] == 2
    assert updated["version"] == base + 1
    # 另一名值班拿着旧版本写入：必须 409，不能覆盖
    with pytest.raises(ServiceError) as excinfo:
        shard_cap.write_cap(
            shard_cap.CAP_KEY,
            99,
            2,
            expected_version=base,
            db_path=db_path,
        )
    assert excinfo.value.status_code == 409
    assert "版本" in excinfo.value.message
    assert shard_cap.read_cap(db_path=db_path)["shard_cap"] == 9


def test_cap_write_is_one_shot_and_speaks_plain_chinese(db_path):
    cap = shard_cap.read_cap(db_path=db_path)
    with pytest.raises(ServiceError) as excinfo:
        shard_cap.write_cap(
            shard_cap.CAP_KEY, 0, 1, expected_version=cap["version"], db_path=db_path
        )
    assert "分片上限" in excinfo.value.message
    with pytest.raises(ServiceError) as excinfo:
        shard_cap.write_cap(
            shard_cap.CAP_KEY, 3, 0, expected_version=cap["version"], db_path=db_path
        )
    assert "并发" in excinfo.value.message
    # 失败不得半写：值与版本都不动
    after = shard_cap.read_cap(db_path=db_path)
    assert after["shard_cap"] == cap["shard_cap"]
    assert after["version"] == cap["version"]


def test_over_cap_backfill_is_blocked(db_path):
    _clear_active_jobs(db_path)
    shard_cap.write_cap(
        shard_cap.CAP_KEY, 2, 1, expected_version=_version(db_path), db_path=db_path
    )
    _batch, rows = _gap_batch(db_path, 3)
    first = backfill.enqueue_from_row(rows[0]["id"], db_path=db_path)
    second = backfill.enqueue_from_row(rows[1]["id"], db_path=db_path)
    assert first["shard_no"] == 1
    assert second["shard_no"] == 1
    with pytest.raises(ServiceError) as excinfo:
        backfill.enqueue_from_row(rows[2]["id"], db_path=db_path)
    assert "分片" in excinfo.value.message
    with get_db(db_path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM backfill_jobs WHERE row_id IN (?,?,?)",
            (rows[0]["id"], rows[1]["id"], rows[2]["id"]),
        ).fetchone()["c"]
    assert n == 2


def test_shards_spill_until_concurrency_then_block(db_path):
    _clear_active_jobs(db_path)
    shard_cap.write_cap(
        shard_cap.CAP_KEY, 2, 2, expected_version=_version(db_path), db_path=db_path
    )
    _batch, rows = _gap_batch(db_path, 5)
    jobs = [backfill.enqueue_from_row(r["id"], db_path=db_path) for r in rows[:4]]
    assert [j["shard_no"] for j in jobs] == [1, 1, 2, 2]
    with pytest.raises(ServiceError) as excinfo:
        backfill.enqueue_from_row(rows[4]["id"], db_path=db_path)
    assert "分片" in excinfo.value.message


def test_failed_job_is_not_duplicated_into_retry_loop(db_path):
    _batch, rows = _gap_batch(db_path, 1)
    job = backfill.enqueue_from_row(rows[0]["id"], db_path=db_path)
    failed = backfill.run_job(job["id"], succeed=False, db_path=db_path)
    assert failed["status"] == "失败"
    # 再排队必须被拦，不能无限新增任务把队列拖死
    with pytest.raises(ServiceError):
        backfill.enqueue_from_row(rows[0]["id"], db_path=db_path)
    with get_db(db_path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM backfill_jobs WHERE row_id=?",
            (rows[0]["id"],),
        ).fetchone()["c"]
    assert n == 1
    # 走重试是允许的，且仍受最大尝试次数约束
    requeued = backfill.retry_job(job["id"], db_path=db_path)
    assert requeued["status"] == "排队"


def test_shard_summary_attached_to_pages(db_path):
    batch, rows = _gap_batch(db_path, 2)
    backfill.enqueue_from_row(rows[0]["id"], db_path=db_path)
    summary = shard_cap.shard_summary(db_path=db_path)
    assert summary["side"] == "甲"
    assert summary["source"] == "旁路账册"
    entry = next(b for b in summary["batches"] if b["batch_id"] == batch["id"])
    assert entry["shard_cap"] >= 1
    assert entry["shard_concurrency"] >= 1
    assert entry["active_shards"] >= 1
    assert summary["totals"]["active_shards"] >= 1
    # 访视批次列表挂分片并发数
    listed = next(x for x in visits.list_batches(db_path) if x["id"] == batch["id"])
    assert listed["shard_concurrency"] >= 1
    assert listed["active_shards"] >= 1
    assert listed["shard_cap"] >= 1
    # 对账矩阵挂分片摘要
    matrix = reconcile.matrix_view(db_path)
    assert matrix["items"]
    assert matrix["shard"]["side"] == "甲"
    report = reconcile.run_reconcile(db_path)
    assert "shard" in report["detail"]


def test_settings_only_expose_cap_criterion(db_path):
    keys = {s["key"] for s in settings.list_settings(db_path)}
    assert "backfill_shard_cap" not in keys
    criterion = shard_cap.cap_criterion(db_path=db_path)
    assert criterion["source"] == "旁路账册"
    assert criterion["table"] == "shard_cap_ledger"
    assert criterion["landed"] is True


def test_only_jia_side_active_and_idle_skeleton_removed():
    assert shard_cap.ACTIVE_SIDE == "甲"
    # 择定甲侧后，乙侧闲置骨架必须清掉
    assert importlib.util.find_spec("app.services.shard_cap_dual") is None
