"""超标记录查询与人工标注.

标注动作 (确认 / 忽略 / 重置 / 修正等级) 必须留下操作人、时间与说明:
每次实际发生的标注都会向 ``annotation_logs`` 追加一条不可变留痕,
``exceedances`` 表上的 ``status / level / note / annotator / annotated_at``
只保存“最近一次标注”的快照。

统计口径:
- 状态分布 (by_status) 统计全部记录, 已忽略仍计入 ignored 分桶, 台账完整;
- 等级分布、高发因子、站点排名、均值/最大倍数等“超标统计”只统计
  待标注 + 已确认记录, **已忽略记录不参与**, 避免误报污染排名。
"""
from datetime import datetime

from sqlalchemy import func, or_

from ..domain.constants import EXCEEDANCE_LEVEL_LABELS, EXCEEDANCE_STATUS_LABELS
from ..errors import NotFoundError, ValidationError
from ..extensions import db
from ..models import AnnotationLog, Exceedance, Station
from ..models.base import iso

STATUS_CHOICES = tuple(EXCEEDANCE_STATUS_LABELS.keys())
LEVEL_CHOICES = tuple(EXCEEDANCE_LEVEL_LABELS.keys())

# 参与超标统计的状态: 已忽略视为无效记录, 不参与排名与倍数统计
COUNTED_STATUSES = ("pending", "confirmed")


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


def _require_annotator(annotator):
    annotator = (annotator or "").strip()
    if not annotator:
        raise ValidationError("标注时必须填写操作人", fields={"annotator": "required"})
    return annotator[:64]


def _is_noop(exceedance, status, level, note):
    """与当前快照完全一致的提交不产生新留痕, 避免重复标注刷历史."""
    return (
        status == exceedance.status
        and (level or exceedance.level) == exceedance.level
        and note == (exceedance.note or "")
    )


def apply_annotation(exceedance, status=None, note=None, annotator=None, level=None):
    """Apply one manual annotation action and append an audit log entry.

    - 操作人必填; 标注为确认/忽略时说明必填。
    - 每次状态或等级或说明实际发生变化都追加一条留痕 (含前后值快照)。
    - 与当前标注完全一致的提交视为无操作, 不写留痕。
    """
    note = (note or "").strip()
    target_status = status if status is not None else exceedance.status

    if target_status not in STATUS_CHOICES:
        raise ValidationError(
            "标注状态取值不合法, 可选: %s" % ", ".join(STATUS_CHOICES),
            fields={"status": "unknown"},
        )
    target_level = level if level is not None else exceedance.level
    if target_level not in LEVEL_CHOICES:
        raise ValidationError(
            "超标等级取值不合法, 可选: %s" % ", ".join(LEVEL_CHOICES),
            fields={"level": "unknown"},
        )

    if _is_noop(exceedance, target_status, target_level, note):
        return exceedance, False

    operator = _require_annotator(annotator)
    if target_status in ("confirmed", "ignored") and not note:
        reason = "确认" if target_status == "confirmed" else "忽略"
        raise ValidationError(
            "标注为\"%s\"时必须填写%s原因" % (EXCEEDANCE_STATUS_LABELS[target_status], reason),
            fields={"note": "required"},
        )

    from_status, from_level = exceedance.status, exceedance.level
    now = datetime.now()

    if target_status == "pending":
        # 重置: 快照恢复为待标注, 操作留痕保留以便事后追溯
        action = "reset"
        exceedance.status = "pending"
        exceedance.level = target_level
        exceedance.note = None
        exceedance.annotator = None
        exceedance.annotated_at = None
    else:
        if target_status == "ignored":
            action = "ignore"
        elif from_status != target_status:
            action = "confirm"
        else:
            action = "adjust_level"
        exceedance.status = target_status
        exceedance.level = target_level
        exceedance.note = note
        exceedance.annotator = operator
        exceedance.annotated_at = now

    db.session.add(
        AnnotationLog(
            exceedance_id=exceedance.id,
            action=action,
            from_status=from_status,
            to_status=exceedance.status,
            from_level=from_level,
            to_level=exceedance.level,
            note=note or None,
            annotator=operator,
            created_at=now,
        )
    )
    return exceedance, True


def annotate(exceedance, status=None, note=None, annotator=None, level=None):
    """Apply a manual annotation to an exceedance record."""
    exceedance, _changed = apply_annotation(
        exceedance, status=status, note=note, annotator=annotator, level=level
    )
    db.session.commit()
    return exceedance


def annotate_batch(ids, status, note=None, annotator=None, level=None):
    """Batch annotation used by the exceedance work bench."""
    ids = list(dict.fromkeys(int(item) for item in ids))
    if not ids:
        raise ValidationError("请至少选择一条超标记录", fields={"ids": "empty"})
    if status not in STATUS_CHOICES:
        raise ValidationError(
            "标注状态取值不合法, 可选: %s" % ", ".join(STATUS_CHOICES),
            fields={"status": "unknown"},
        )
    note = (note or "").strip()
    if status in ("confirmed", "ignored") and not note:
        raise ValidationError(
            "批量标注为\"%s\"时必须填写标注说明" % EXCEEDANCE_STATUS_LABELS[status],
            fields={"note": "required"},
        )
    # 批量动作同样必须留下操作人
    operator = _require_annotator(annotator)

    records = Exceedance.query.filter(Exceedance.id.in_(ids)).all()
    found = {record.id for record in records}
    missing = [item for item in ids if item not in found]

    updated, unchanged = [], []
    for record in records:
        record, changed = apply_annotation(
            record, status=status, note=note, annotator=operator, level=level
        )
        (updated if changed else unchanged).append(record.id)

    db.session.commit()
    return {
        "updated": len(updated),
        "updated_ids": updated,
        "unchanged": len(unchanged),
        "unchanged_ids": unchanged,
        "missing": missing,
    }


def _scope_subquery(base, counted_only):
    """Return a subquery used by summary aggregates.

    ``counted_only=True`` 剔除已忽略记录, 用于超标统计与排名;
    ``counted_only=False`` 保留全部状态, 用于状态分布。
    """
    query = base.with_entities(
        Exceedance.id,
        Exceedance.station_id,
        Exceedance.status,
        Exceedance.level,
        Exceedance.pollutant,
        Exceedance.exceed_ratio,
    )
    if counted_only:
        query = query.filter(Exceedance.status.in_(COUNTED_STATUSES))
    return query.subquery()


def summary(args):
    """Dashboard counters for the annotation work bench.

    ``total`` 为筛选范围内的全部超标记录 (含已忽略, 保证台账完整);
    ``effective_total`` 及等级/排名/倍数统计仅统计待标注 + 已确认记录。
    """
    base = exceedance_query(args)
    all_scope = _scope_subquery(base, counted_only=False)
    counted_scope = _scope_subquery(base, counted_only=True)

    by_status = {
        status: {"key": status, "label": label, "count": 0}
        for status, label in EXCEEDANCE_STATUS_LABELS.items()
    }
    for status, count in (
        db.session.query(all_scope.c.status, func.count())
        .group_by(all_scope.c.status)
        .all()
    ):
        if status in by_status:
            by_status[status]["count"] = int(count)

    # 已忽略不参与超标等级统计
    by_level = {
        level: {"key": level, "label": label, "count": 0}
        for level, label in EXCEEDANCE_LEVEL_LABELS.items()
    }
    for level, count in (
        db.session.query(counted_scope.c.level, func.count())
        .group_by(counted_scope.c.level)
        .all()
    ):
        if level in by_level:
            by_level[level]["count"] = int(count)

    # 高发因子排名: 已忽略记录剔除
    top_pollutants = [
        {"key": pollutant, "count": int(count), "avg_ratio": round(float(avg_ratio or 0), 3)}
        for pollutant, count, avg_ratio in (
            db.session.query(
                counted_scope.c.pollutant,
                func.count(),
                func.avg(counted_scope.c.exceed_ratio),
            )
            .group_by(counted_scope.c.pollutant)
            .order_by(func.count().desc())
            .all()
        )
    ]

    # 站点排名: 已忽略记录剔除
    top_stations = [
        {"station_id": station_id, "station_name": name, "count": int(count)}
        for station_id, name, count in (
            db.session.query(
                counted_scope.c.station_id,
                Station.name,
                func.count(),
            )
            .join(Station, Station.id == counted_scope.c.station_id)
            .group_by(counted_scope.c.station_id, Station.name)
            .order_by(func.count().desc())
            .limit(5)
            .all()
        )
    ]

    totals_all = db.session.query(func.count(all_scope.c.id)).one()
    totals_counted = db.session.query(
        func.count(counted_scope.c.id),
        func.max(counted_scope.c.exceed_ratio),
        func.avg(counted_scope.c.exceed_ratio),
    ).one()

    return {
        "total": int(totals_all[0] or 0),
        "effective_total": int(totals_counted[0] or 0),
        "ignored_total": by_status["ignored"]["count"],
        "pending": by_status["pending"]["count"],
        "confirmed": by_status["confirmed"]["count"],
        "ignored": by_status["ignored"]["count"],
        "by_status": list(by_status.values()),
        "by_level": list(by_level.values()),
        "top_pollutants": top_pollutants,
        "top_stations": top_stations,
        "max_ratio": round(float(totals_counted[1] or 0), 3),
        "avg_ratio": round(float(totals_counted[2] or 0), 3),
        "counted_statuses": list(COUNTED_STATUSES),
        "generated_at": iso(datetime.now()),
    }
