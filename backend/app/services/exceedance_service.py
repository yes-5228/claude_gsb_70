"""超标记录查询与人工标注."""
from datetime import datetime

from sqlalchemy import cast, func, or_

from ..domain.constants import (
    EXCEEDANCE_LEVEL_LABELS,
    EXCEEDANCE_STATUS_LABELS,
    label_of,
)
from ..errors import NotFoundError, ValidationError
from ..extensions import db
from ..models import Exceedance, ExceedanceAnnotation, Measurement, Station
from ..models.base import iso

STATUS_CHOICES = tuple(EXCEEDANCE_STATUS_LABELS.keys())
LEVEL_CHOICES = tuple(EXCEEDANCE_LEVEL_LABELS.keys())

ANNOTATION_FIELD_LABELS = {
    "status": "标注状态",
    "level": "超标等级",
    "note": "标注说明",
    "annotator": "标注人",
}

# 统计口径说明: 已忽略记录的去向 (summary 接口同步返回该说明)
IGNORED_SCOPE_NOTE = (
    "已忽略记录不参与超标统计(总数/最大与平均超标倍数)、等级分布与高发因子/站点排名, "
    "仅计入状态分布, 记录本身仍可在列表查询与导出中检索"
)


def _split(value):
    if not value:
        return []
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _int_list(args, name):
    values = []
    for item in _split(args.get(name)):
        try:
            values.append(int(item))
        except ValueError:
            raise ValidationError("%s 参数必须为整数" % name, fields={name: "invalid_integer"})
    return values


def _date_arg(args, name, end_of_day=False):
    from datetime import time

    from ..utils.validation import parse_date

    raw = args.get(name)
    if raw in (None, ""):
        return None
    parsed = parse_date(raw, name)
    return datetime.combine(parsed, time.max if end_of_day else time.min)


def get_exceedance(exceedance_id):
    exceedance = db.session.get(Exceedance, exceedance_id)
    if exceedance is None:
        raise NotFoundError("超标记录不存在: id=%s" % exceedance_id)
    return exceedance


def exceedance_query(args):
    query = db.session.query(Exceedance).join(Station, Exceedance.station_id == Station.id)

    statuses = _split(args.get("status"))
    if statuses:
        query = query.filter(Exceedance.status.in_(statuses))
    levels = _split(args.get("level"))
    if levels:
        query = query.filter(Exceedance.level.in_(levels))
    pollutants = _split(args.get("pollutant"))
    if pollutants:
        query = query.filter(Exceedance.pollutant.in_([item.upper() for item in pollutants]))
    station_ids = _int_list(args, "station_id")
    if station_ids:
        query = query.filter(Exceedance.station_id.in_(station_ids))
    areas = _split(args.get("area"))
    if areas:
        query = query.filter(Station.area.in_(areas))
    keyword = (args.get("keyword") or "").strip()
    if keyword:
        like = "%" + keyword + "%"
        query = query.filter(
            or_(Station.name.like(like), Station.code.like(like), Exceedance.note.like(like))
        )
    date_from = _date_arg(args, "date_from")
    if date_from:
        query = query.filter(Exceedance.measured_at >= date_from)
    date_to = _date_arg(args, "date_to", end_of_day=True)
    if date_to:
        query = query.filter(Exceedance.measured_at <= date_to)
    min_ratio = args.get("min_ratio")
    if min_ratio not in (None, ""):
        try:
            query = query.filter(Exceedance.exceed_ratio >= float(min_ratio))
        except ValueError:
            raise ValidationError("min_ratio 必须为数字", fields={"min_ratio": "invalid_number"})
    if str(args.get("annotated", "")).strip().lower() in {"1", "true", "yes"}:
        query = query.filter(Exceedance.annotated_at.isnot(None))
    elif str(args.get("annotated", "")).strip().lower() in {"0", "false", "no"}:
        query = query.filter(Exceedance.annotated_at.is_(None))

    order = (args.get("order") or "desc").lower()
    sort_key = args.get("sort") or "measured_at"
    column = {
        "measured_at": Exceedance.measured_at,
        "exceed_ratio": Exceedance.exceed_ratio,
        "level": Exceedance.level,
        "updated_at": Exceedance.updated_at,
    }.get(sort_key, Exceedance.measured_at)
    primary = column.desc() if order == "desc" else column.asc()
    return query.order_by(primary, Exceedance.id.desc())


def _annotation_snapshot(exceedance):
    return {
        "status": exceedance.status,
        "level": exceedance.level,
        "note": exceedance.note or None,
        "annotator": exceedance.annotator or None,
    }


def _diff_changes(before, after):
    """前后两次标注快照的差异, 用于接口响应与标注日志."""
    changes = []
    for field, label in ANNOTATION_FIELD_LABELS.items():
        old, new = before.get(field), after.get(field)
        if old == new:
            continue
        change = {"field": field, "label": label, "from": old, "to": new}
        if field == "status":
            change["from_label"] = label_of(EXCEEDANCE_STATUS_LABELS, old) if old else "未标注"
            change["to_label"] = label_of(EXCEEDANCE_STATUS_LABELS, new) if new else "未标注"
        elif field == "level":
            change["from_label"] = label_of(EXCEEDANCE_LEVEL_LABELS, old) if old else None
            change["to_label"] = label_of(EXCEEDANCE_LEVEL_LABELS, new) if new else None
        changes.append(change)
    return changes


def _apply_annotation(exceedance, status=None, level=None, note=None, annotator=None):
    """Apply one annotation action and return the change list (empty when no-op).

    每次有效变更都会写入一条 ExceedanceAnnotation 日志(操作人/时间/说明/前后差异);
    没有任何变化的调用视为无操作, 不要求操作人, 也不产生日志.
    """
    target_status = status or exceedance.status
    target_level = level or exceedance.level
    note = (note or "").strip() or None
    annotator = (annotator or "").strip()

    if target_status not in STATUS_CHOICES:
        raise ValidationError(
            "标注状态取值不合法, 可选: %s" % ", ".join(STATUS_CHOICES),
            fields={"status": "unknown"},
        )
    if target_level not in LEVEL_CHOICES:
        raise ValidationError(
            "超标等级取值不合法, 可选: %s" % ", ".join(LEVEL_CHOICES),
            fields={"level": "unknown"},
        )

    before = _annotation_snapshot(exceedance)
    resetting = target_status == "pending" and exceedance.status != "pending"
    note_changed = note is not None and note != before["note"]
    if (
        not resetting
        and target_status == before["status"]
        and target_level == before["level"]
        and not note_changed
    ):
        return []

    errors = {}
    if not annotator:
        errors["annotator"] = "required"
    if target_status != "pending" and not note:
        errors["note"] = "required"
    if errors:
        parts = []
        if "annotator" in errors:
            parts.append("标注人不能为空")
        if "note" in errors:
            reason = "确认" if target_status == "confirmed" else "忽略"
            parts.append(
                "标注为\"%s\"时必须填写%s原因" % (EXCEEDANCE_STATUS_LABELS[target_status], reason)
            )
        raise ValidationError(", ".join(parts), fields=errors)

    now = datetime.now()
    exceedance.status = target_status
    exceedance.level = target_level
    if resetting:
        # 重置为待标注: 清空记录上的标注快照, 操作痕迹保留在标注日志中
        exceedance.note = None
        exceedance.annotator = None
        exceedance.annotated_at = None
    else:
        if note is not None:
            exceedance.note = note
        exceedance.annotator = annotator
        exceedance.annotated_at = now

    changes = _diff_changes(before, _annotation_snapshot(exceedance))
    db.session.add(
        ExceedanceAnnotation(
            exceedance_id=exceedance.id,
            annotator=annotator,
            note=note,
            status_from=before["status"],
            status_to=exceedance.status,
            level_from=before["level"],
            level_to=exceedance.level,
            changes=changes,
            created_at=now,
        )
    )
    return changes


def annotate(exceedance, status=None, note=None, annotator=None, level=None):
    """Apply a manual annotation to an exceedance record.

    Returns ``(exceedance, changes)``: changes 为本次操作相对上一次标注的逐字段差异.
    """
    changes = _apply_annotation(
        exceedance, status=status, level=level, note=note, annotator=annotator
    )
    db.session.commit()
    return exceedance, changes


def annotate_batch(ids, status, note=None, annotator=None, level=None):
    """Batch annotation used by the exceedance work bench."""
    ids = list(dict.fromkeys(int(item) for item in ids))
    if not ids:
        raise ValidationError("请至少选择一条超标记录", fields={"ids": "empty"})

    records = Exceedance.query.filter(Exceedance.id.in_(ids)).all()
    found = {record.id for record in records}
    missing = [item for item in ids if item not in found]

    note_text = (note or "").strip()
    annotator = (annotator or "").strip()
    if status != "pending" and not note_text:
        raise ValidationError(
            "批量标注为\"%s\"时必须填写标注说明"
            % EXCEEDANCE_STATUS_LABELS.get(status, status),
            fields={"note": "required"},
        )
    if records and not annotator:
        raise ValidationError(
            "批量标注必须填写标注人, 以便事后追溯", fields={"annotator": "required"}
        )

    updated = []
    changed = 0
    for record in records:
        changes = _apply_annotation(
            record, status=status, level=level, note=note_text, annotator=annotator
        )
        if changes:
            changed += 1
        updated.append(record.id)

    db.session.commit()
    return {
        "updated": len(updated),
        "updated_ids": updated,
        "changed": changed,
        "missing": missing,
    }


def summary(args):
    """Dashboard counters for the annotation work bench.

    统计口径(已忽略记录的去向):
    - 不参与: 超标记录总数 total、最大/平均超标倍数、等级分布 by_level、
      高发因子排名 top_pollutants、高发站点排名 top_stations;
    - 仍参与: 状态分布 by_status 与 ignored 计数(标注工作量统计),
      且记录本身仍可在列表查询、详情与导出中检索;
    - 首页概览的超标卡片复用本函数, 口径一致.
    """
    base = exceedance_query(args)
    effective = base.filter(Exceedance.status != "ignored").order_by(None)
    subquery = effective.with_entities(
        Exceedance.id, Exceedance.station_id, Exceedance.level,
        Exceedance.pollutant, Exceedance.exceed_ratio,
    ).subquery()

    by_status = {
        status: {"key": status, "label": label, "count": 0}
        for status, label in EXCEEDANCE_STATUS_LABELS.items()
    }
    for status, count in (
        base.order_by(None)
        .with_entities(Exceedance.status, func.count())
        .group_by(Exceedance.status)
        .all()
    ):
        if status in by_status:
            by_status[status]["count"] = int(count)

    by_level = {
        level: {"key": level, "label": label, "count": 0}
        for level, label in EXCEEDANCE_LEVEL_LABELS.items()
    }
    for level, count in (
        db.session.query(subquery.c.level, func.count()).group_by(subquery.c.level).all()
    ):
        if level in by_level:
            by_level[level]["count"] = int(count)

    top_pollutants = [
        {"key": pollutant, "count": int(count), "avg_ratio": round(float(avg_ratio or 0), 3)}
        for pollutant, count, avg_ratio in (
            db.session.query(
                subquery.c.pollutant,
                func.count(),
                func.avg(subquery.c.exceed_ratio),
            )
            .group_by(subquery.c.pollutant)
            .order_by(func.count().desc())
            .all()
        )
    ]

    top_stations = [
        {"station_id": station_id, "station_name": name, "count": int(count)}
        for station_id, name, count in (
            db.session.query(
                subquery.c.station_id,
                Station.name,
                func.count(),
            )
            .join(Station, Station.id == subquery.c.station_id)
            .group_by(subquery.c.station_id, Station.name)
            .order_by(func.count().desc())
            .limit(5)
            .all()
        )
    ]

    totals = db.session.query(
        func.count(subquery.c.id),
        func.max(subquery.c.exceed_ratio),
        func.avg(subquery.c.exceed_ratio),
    ).one()

    return {
        "total": int(totals[0] or 0),
        "ignored": by_status["ignored"]["count"],
        "pending": by_status["pending"]["count"],
        "by_status": list(by_status.values()),
        "by_level": list(by_level.values()),
        "top_pollutants": top_pollutants,
        "top_stations": top_stations,
        "max_ratio": round(float(totals[1] or 0), 3),
        "avg_ratio": round(float(totals[2] or 0), 3),
        "ignored_excluded": True,
        "scope_note": IGNORED_SCOPE_NOTE,
        "generated_at": iso(datetime.now()),
    }
