"""超标记录标注接口测试."""
from app.models import Exceedance, ExceedanceAnnotation


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

    response = client.patch("/api/exceedances/%d" % exceedance_id, json={"status": "confirmed"})
    assert response.status_code == 422
    assert response.get_json()["error"]["fields"]["note"] == "required"


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

    missing = client.post(
        "/api/exceedances/annotations",
        json={"ids": [9999], "status": "confirmed", "note": "不存在", "annotator": "李静"},
    )
    assert missing.get_json()["missing"] == [9999]


def test_batch_annotation_without_note_is_rejected(client, station, entry_payload):
    _make_exceedances(client, station, entry_payload)
    ids = [item.id for item in Exceedance.query.all()]
    response = client.post(
        "/api/exceedances/annotations", json={"ids": ids, "status": "confirmed"}
    )
    assert response.status_code == 422


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
    options = client.get("/api/exceedances/options").get_json()
    assert {item["value"] for item in options["statuses"]} == {"pending", "confirmed", "ignored"}

    csv_body = client.get("/api/exceedances/export").get_data(as_text=True)
    assert csv_body.startswith("\ufeff站点编码")
    assert "SO₂" not in csv_body  # 导出使用标准因子代码
    assert "SO2" in csv_body


def test_annotation_requires_annotator(client, station, entry_payload):
    _make_exceedances(client, station, entry_payload)
    exceedance_id = Exceedance.query.first().id

    response = client.patch(
        "/api/exceedances/%d" % exceedance_id,
        json={"status": "confirmed", "note": "复核确认"},
    )
    assert response.status_code == 422
    assert response.get_json()["error"]["fields"]["annotator"] == "required"

    ids = [item.id for item in Exceedance.query.all()]
    batch = client.post(
        "/api/exceedances/annotations",
        json={"ids": ids, "status": "ignored", "note": "校准异常"},
    )
    assert batch.status_code == 422
    assert batch.get_json()["error"]["fields"]["annotator"] == "required"


def test_annotation_writes_audit_log_with_operator_and_time(client, station, entry_payload):
    _make_exceedances(client, station, entry_payload)
    exceedance_id = Exceedance.query.first().id

    client.patch(
        "/api/exceedances/%d" % exceedance_id,
        json={"status": "ignored", "note": "仪器校准异常值", "annotator": "李静"},
    )
    logs = ExceedanceAnnotation.query.filter_by(exceedance_id=exceedance_id).all()
    assert len(logs) == 1
    log = logs[0]
    assert log.annotator == "李静"
    assert log.note == "仪器校准异常值"
    assert log.created_at is not None
    assert (log.status_from, log.status_to) == ("pending", "ignored")

    detail = client.get("/api/exceedances/%d" % exceedance_id).get_json()
    assert detail["annotation_count"] == 1
    assert detail["annotations"][0]["annotator"] == "李静"
    assert detail["annotations"][0]["status_to_label"] == "已忽略"


def test_reannotation_returns_diff_and_keeps_history(client, station, entry_payload):
    _make_exceedances(client, station, entry_payload)
    exceedance_id = Exceedance.query.first().id

    client.patch(
        "/api/exceedances/%d" % exceedance_id,
        json={"status": "confirmed", "note": "复核确认超标", "annotator": "王敏"},
    )
    response = client.patch(
        "/api/exceedances/%d" % exceedance_id,
        json={"status": "ignored", "note": "复核后确认为校准异常", "annotator": "李静"},
    )
    assert response.status_code == 200
    changes = {item["field"]: item for item in response.get_json()["changes"]}
    assert changes["status"]["from"] == "confirmed"
    assert changes["status"]["to"] == "ignored"
    assert changes["status"]["from_label"] == "已确认"
    assert changes["annotator"]["from"] == "王敏"
    assert changes["annotator"]["to"] == "李静"

    detail = client.get("/api/exceedances/%d" % exceedance_id).get_json()
    assert detail["annotation_count"] == 2
    latest, previous = detail["annotations"]  # 按时间倒序
    assert latest["annotator"] == "李静"
    assert latest["status_from"] == "confirmed"
    assert latest["status_to"] == "ignored"
    assert previous["annotator"] == "王敏"
    assert previous["status_from"] == "pending"
    assert previous["status_to"] == "confirmed"


def test_reset_to_pending_clears_snapshot_but_keeps_log(client, station, entry_payload):
    _make_exceedances(client, station, entry_payload)
    exceedance_id = Exceedance.query.first().id

    client.patch(
        "/api/exceedances/%d" % exceedance_id,
        json={"status": "ignored", "note": "误报", "annotator": "王敏"},
    )
    response = client.patch(
        "/api/exceedances/%d" % exceedance_id,
        json={"status": "pending", "note": "证据不足, 重新复核", "annotator": "李静"},
    )
    body = response.get_json()
    assert body["status"] == "pending"
    assert body["annotator"] is None
    assert body["annotated_at"] is None
    assert body["note"] is None

    logs = ExceedanceAnnotation.query.filter_by(exceedance_id=exceedance_id).all()
    assert len(logs) == 2
    reset_log = [log for log in logs if log.status_to == "pending"][0]
    assert reset_log.annotator == "李静"
    assert reset_log.note == "证据不足, 重新复核"


def test_noop_annotation_skips_log_and_annotator(client, station, entry_payload):
    _make_exceedances(client, station, entry_payload)
    exceedance_id = Exceedance.query.first().id

    response = client.patch("/api/exceedances/%d" % exceedance_id, json={})
    assert response.status_code == 200
    assert response.get_json()["changes"] == []
    assert ExceedanceAnnotation.query.count() == 0


def test_ignored_records_excluded_from_exceedance_statistics(client, station, entry_payload):
    _make_exceedances(client, station, entry_payload)
    so2 = Exceedance.query.filter_by(pollutant="SO2").first()
    client.patch(
        "/api/exceedances/%d" % so2.id,
        json={"status": "ignored", "note": "仪器校准异常值", "annotator": "李静"},
    )

    summary = client.get("/api/exceedances/summary").get_json()
    assert summary["total"] == 1  # 只剩 NO2 计入超标统计
    assert summary["ignored"] == 1
    assert summary["ignored_excluded"] is True
    assert "已忽略" in summary["scope_note"]
    by_status = {item["key"]: item["count"] for item in summary["by_status"]}
    assert by_status == {"pending": 1, "confirmed": 0, "ignored": 1}

    # 高发因子排名不再包含已忽略的 SO2
    assert [item["key"] for item in summary["top_pollutants"]] == ["NO2"]
    by_level = {item["key"]: item["count"] for item in summary["by_level"]}
    assert sum(by_level.values()) == 1

    # 被忽略的记录本身仍可查询
    ignored = client.get("/api/exceedances?status=ignored").get_json()
    assert ignored["total"] == 1
    assert ignored["items"][0]["pollutant"] == "SO2"


def test_batch_annotation_logs_each_record(client, station, entry_payload):
    _make_exceedances(client, station, entry_payload)
    ids = [item.id for item in Exceedance.query.all()]

    response = client.post(
        "/api/exceedances/annotations",
        json={"ids": ids, "status": "ignored", "note": "仪器校准异常值", "annotator": "李静"},
    )
    body = response.get_json()
    assert body["updated"] == 2
    assert body["changed"] == 2

    logs = ExceedanceAnnotation.query.all()
    assert len(logs) == 2
    assert {log.annotator for log in logs} == {"李静"}
    assert all(log.status_to == "ignored" for log in logs)

    # 重复提交相同标注: 无变化, 不再产生日志
    again = client.post(
        "/api/exceedances/annotations",
        json={"ids": ids, "status": "ignored", "note": "仪器校准异常值", "annotator": "李静"},
    )
    assert again.get_json()["changed"] == 0
    assert ExceedanceAnnotation.query.count() == 2
