"""超标记录标注接口测试."""
from app.extensions import db
from app.models import AnnotationLog, Exceedance


def _make_exceedances(client, station, entry_payload, measured_at="2026-09-01 10:00"):
    return client.post(
        "/api/measurements/entries",
        json=entry_payload(
            station.id,
            measured_at=measured_at,
            entries=[
                {"pollutant": "SO2", "value": 600.0},
                {"pollutant": "NO2", "value": 300.0},
                {"pollutant": "PM25", "value": 40.0},
            ],
        ),
    ).get_json()


def test_exceedance_records_are_created_automatically(client, station, entry_payload):
    body = _make_exceedances(client, station, entry_payload)
    assert body["summary"]["exceeded_count"] == 2

    listed = client.get("/api/exceedances").get_json()
    assert listed["total"] == 2
    levels = {item["pollutant"]: item["level"] for item in listed["items"]}
    assert levels == {"SO2": "light", "NO2": "moderate"}
    assert listed["summary"]["pending"] == 2
    assert listed["summary"]["by_status"][0]["key"] == "pending"


def test_annotation_requires_note_when_not_pending(client, station, entry_payload):
    _make_exceedances(client, station, entry_payload)
    exceedance_id = Exceedance.query.first().id

    response = client.patch(
        "/api/exceedances/%d" % exceedance_id,
        json={"status": "confirmed", "annotator": "王敏"},
    )
    assert response.status_code == 422
    assert response.get_json()["error"]["fields"]["note"] == "required"


def test_annotation_requires_annotator(client, station, entry_payload):
    _make_exceedances(client, station, entry_payload)
    exceedance_id = Exceedance.query.first().id

    response = client.patch(
        "/api/exceedances/%d" % exceedance_id,
        json={"status": "confirmed", "note": "复核属实"},
    )
    assert response.status_code == 422
    assert response.get_json()["error"]["fields"]["annotator"]


def test_single_annotation_persists_note_and_annotator(client, station, entry_payload):
    _make_exceedances(client, station, entry_payload)
    exceedance_id = Exceedance.query.order_by(Exceedance.id.asc()).first().id

    response = client.patch(
        "/api/exceedances/%d" % exceedance_id,
        json={
            "status": "confirmed",
            "level": "severe",
            "note": "复核确认超标, 已通知现场核查",
            "annotator": "王敏",
        },
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "confirmed"
    assert body["status_label"] == "已确认"
    assert body["level"] == "severe"
    assert body["note"] == "复核确认超标, 已通知现场核查"
    assert body["annotator"] == "王敏"
    assert body["annotated_at"] is not None
    assert body["annotation_count"] == 1
    assert body["last_annotation"]["annotator"] == "王敏"
    assert body["last_annotation"]["action"] == "confirm"

    # 详情接口包含完整留痕: 操作人 / 时间 / 前后状态
    detail = client.get("/api/exceedances/%d" % exceedance_id).get_json()
    assert len(detail["annotations"]) == 1
    log = detail["annotations"][0]
    assert log["annotator"] == "王敏"
    assert log["created_at"] is not None
    assert log["from_status"] == "pending"
    assert log["to_status"] == "confirmed"
    assert log["from_level"] == "light"
    assert log["to_level"] == "severe"
    assert log["note"] == "复核确认超标, 已通知现场核查"


def test_reannotation_appends_log_with_before_after_diff(client, station, entry_payload):
    """同一条记录两次标注: 快照更新为最近一次, 留痕保留前后两次差别."""
    _make_exceedances(client, station, entry_payload)
    exceedance_id = Exceedance.query.order_by(Exceedance.id.asc()).first().id

    client.patch(
        "/api/exceedances/%d" % exceedance_id,
        json={"status": "confirmed", "note": "第一次: 复核属实", "annotator": "王敏"},
    )
    client.patch(
        "/api/exceedances/%d" % exceedance_id,
        json={"status": "ignored", "note": "第二次: 设备校准期数据, 改判忽略", "annotator": "李静"},
    )

    record = db.session.get(Exceedance, exceedance_id)
    assert record.status == "ignored"
    assert record.annotator == "李静"
    assert record.note.startswith("第二次")

    logs = AnnotationLog.query.filter_by(exceedance_id=exceedance_id).order_by(
        AnnotationLog.id.asc()
    ).all()
    assert len(logs) == 2
    first, second = logs
    assert first.annotator == "王敏"
    assert second.annotator == "李静"
    assert second.from_status == "confirmed"
    assert second.to_status == "ignored"
    assert second.action == "ignore"
    assert first.created_at <= second.created_at


def test_identical_resubmit_does_not_create_log(client, station, entry_payload):
    _make_exceedances(client, station, entry_payload)
    exceedance_id = Exceedance.query.first().id

    payload = {"status": "ignored", "note": "设备异常", "annotator": "王敏"}
    client.patch("/api/exceedances/%d" % exceedance_id, json=payload)
    client.patch("/api/exceedances/%d" % exceedance_id, json=payload)

    assert AnnotationLog.query.filter_by(exceedance_id=exceedance_id).count() == 1


def test_reset_clears_snapshot_but_keeps_trail(client, station, entry_payload):
    _make_exceedances(client, station, entry_payload)
    exceedance_id = Exceedance.query.first().id

    client.patch(
        "/api/exceedances/%d" % exceedance_id,
        json={"status": "ignored", "note": "误报", "annotator": "王敏"},
    )
    response = client.patch(
        "/api/exceedances/%d" % exceedance_id,
        json={"status": "pending", "annotator": "李静"},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "pending"
    assert body["note"] is None
    assert body["annotator"] is None
    assert body["annotated_at"] is None

    # 留痕仍保留两次动作, 事后可追溯谁在什么时候重置过
    assert body["last_annotation"]["action"] == "reset"
    detail = client.get("/api/exceedances/%d" % exceedance_id).get_json()
    assert [log["action"] for log in detail["annotations"]] == ["reset", "ignore"]
    reset_log = detail["annotations"][0]
    assert reset_log["annotator"] == "李静"
    assert reset_log["from_status"] == "ignored"
    assert reset_log["to_status"] == "pending"


def test_ignored_records_excluded_from_counted_statistics(client, station, entry_payload):
    """忽略后: 仍计入状态分布与 total, 但不参与等级/高发因子/倍数统计."""
    _make_exceedances(client, station, entry_payload)
    ids = {item.pollutant: item.id for item in Exceedance.query.all()}

    # 忽略 SO2(轻度) 一条
    client.patch(
        "/api/exceedances/%d" % ids["SO2"],
        json={"status": "ignored", "note": "设备校准", "annotator": "李静"},
    )

    summary = client.get("/api/exceedances/summary").get_json()
    # 全部记录口径
    assert summary["total"] == 2
    assert summary["ignored"] == 1
    assert summary["pending"] == 1
    status_counts = {item["key"]: item["count"] for item in summary["by_status"]}
    assert status_counts == {"pending": 1, "confirmed": 0, "ignored": 1}
    # 有效超标口径 (待标注 + 已确认)
    assert summary["effective_total"] == 1
    # 等级分布不再统计被忽略的 light 记录, 只剩 NO2 的 moderate
    level_counts = {item["key"]: item["count"] for item in summary["by_level"]}
    assert level_counts == {"light": 0, "moderate": 1, "severe": 0}
    # 高发因子只剩 NO2
    assert [item["key"] for item in summary["top_pollutants"]] == ["NO2"]


def test_confirmed_records_remain_in_statistics(client, station, entry_payload):
    _make_exceedances(client, station, entry_payload)
    exceedance_id = Exceedance.query.filter_by(pollutant="SO2").first().id
    client.patch(
        "/api/exceedances/%d" % exceedance_id,
        json={"status": "confirmed", "note": "属实", "annotator": "王敏"},
    )
    summary = client.get("/api/exceedances/summary").get_json()
    assert summary["effective_total"] == 2
    assert summary["confirmed"] == 1
    assert {item["key"] for item in summary["top_pollutants"]} == {"SO2", "NO2"}


def test_batch_annotation_updates_selected_records(client, station, entry_payload):
    _make_exceedances(client, station, entry_payload)
    ids = [item.id for item in Exceedance.query.all()]

    response = client.post(
        "/api/exceedances/annotations",
        json={"ids": ids, "status": "ignored", "note": "仪器校准异常值", "annotator": "李静"},
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["updated"] == 2
    assert body["missing"] == []
    assert Exceedance.query.filter_by(status="ignored").count() == 2
    # 批量动作同样逐条留痕
    assert AnnotationLog.query.filter_by(annotator="李静", action="ignore").count() == 2

    missing = client.post(
        "/api/exceedances/annotations",
        json={"ids": [9999], "status": "confirmed", "note": "不存在", "annotator": "王敏"},
    )
    assert missing.get_json()["missing"] == [9999]


def test_batch_annotation_without_note_is_rejected(client, station, entry_payload):
    _make_exceedances(client, station, entry_payload)
    ids = [item.id for item in Exceedance.query.all()]
    response = client.post(
        "/api/exceedances/annotations",
        json={"ids": ids, "status": "confirmed", "annotator": "王敏"},
    )
    assert response.status_code == 422


def test_batch_annotation_without_annotator_is_rejected(client, station, entry_payload):
    _make_exceedances(client, station, entry_payload)
    ids = [item.id for item in Exceedance.query.all()]
    response = client.post(
        "/api/exceedances/annotations",
        json={"ids": ids, "status": "ignored", "note": "设备异常"},
    )
    assert response.status_code == 422
    assert response.get_json()["error"]["fields"]["annotator"]


def test_batch_annotation_reports_unchanged(client, station, entry_payload):
    """与当前标注完全一致的批量提交记入 unchanged, 不新增留痕."""
    _make_exceedances(client, station, entry_payload)
    ids = [item.id for item in Exceedance.query.all()]
    payload = {"ids": ids, "status": "ignored", "note": "仪器校准", "annotator": "李静"}
    client.post("/api/exceedances/annotations", json=payload)
    again = client.post("/api/exceedances/annotations", json=payload).get_json()
    assert again["updated"] == 0
    assert sorted(again["unchanged_ids"]) == sorted(ids)
    assert AnnotationLog.query.count() == 2


def test_exceedance_filters_and_summary(client, station, entry_payload):
    _make_exceedances(client, station, entry_payload)
    only_so2 = client.get("/api/exceedances?pollutant=SO2&level=light").get_json()
    assert only_so2["total"] == 1
    assert only_so2["items"][0]["pollutant"] == "SO2"
    assert only_so2["summary"]["total"] == 1

    annotated = client.get("/api/exceedances?annotated=false").get_json()
    assert annotated["total"] == 2

    detail = client.get("/api/exceedances/%d" % only_so2["items"][0]["id"]).get_json()
    assert detail["measurement"]["station"]["code"] == "TEST-001"


def test_exceedance_options_and_export(client, station, entry_payload):
    _make_exceedances(client, station, entry_payload)
    exceedance_id = Exceedance.query.first().id
    client.patch(
        "/api/exceedances/%d" % exceedance_id,
        json={"status": "confirmed", "note": "复核属实", "annotator": "王敏"},
    )

    options = client.get("/api/exceedances/options").get_json()
    assert {item["value"] for item in options["statuses"]} == {"pending", "confirmed", "ignored"}

    csv_body = client.get("/api/exceedances/export").get_data(as_text=True)
    assert csv_body.startswith("﻿站点编码")
    assert "SO₂" not in csv_body  # 导出使用标准因子代码
    assert "SO2" in csv_body
    assert "标注次数" in csv_body
    assert "王敏" in csv_body
