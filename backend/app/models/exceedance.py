"""超标记录 (含人工标注与标注日志)."""
from datetime import datetime

from ..domain.constants import (
    EXCEEDANCE_LEVEL_LABELS,
    EXCEEDANCE_STATUS_LABELS,
    PERIOD_LABELS,
    label_of,
)
from ..extensions import db
from .base import TimestampMixin, iso


class Exceedance(TimestampMixin, db.Model):
    __tablename__ = "exceedances"

    id = db.Column(db.Integer, primary_key=True)
    measurement_id = db.Column(
        db.Integer,
        db.ForeignKey("measurements.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    station_id = db.Column(
        db.Integer, db.ForeignKey("stations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pollutant = db.Column(db.String(16), nullable=False, index=True)
    period = db.Column(db.String(16), nullable=False, default="hourly")
    value = db.Column(db.Float, nullable=False)
    limit_value = db.Column(db.Float, nullable=False)
    exceed_ratio = db.Column(db.Float, nullable=False)
    level = db.Column(db.String(16), nullable=False, default="light", index=True)
    status = db.Column(db.String(16), nullable=False, default="pending", index=True)
    note = db.Column(db.Text)
    annotator = db.Column(db.String(64))
    annotated_at = db.Column(db.DateTime)
    measured_at = db.Column(db.DateTime, nullable=False, index=True)

    measurement = db.relationship("Measurement", back_populates="exceedance")
    station = db.relationship("Station", back_populates="exceedances")
    annotations = db.relationship(
        "ExceedanceAnnotation",
        back_populates="exceedance",
        cascade="all, delete-orphan",
        order_by=lambda: (
            ExceedanceAnnotation.created_at.desc(),
            ExceedanceAnnotation.id.desc(),
        ),
    )

    def to_dict(self, include_relations=False):
        payload = {
            "id": self.id,
            "measurement_id": self.measurement_id,
            "station_id": self.station_id,
            "pollutant": self.pollutant,
            "pollutant_label": self.measurement.pollutant_label() if self.measurement else self.pollutant,
            "period": self.period,
            "period_label": label_of(PERIOD_LABELS, self.period),
            "value": self.value,
            "limit_value": self.limit_value,
            "exceed_ratio": self.exceed_ratio,
            "level": self.level,
            "level_label": label_of(EXCEEDANCE_LEVEL_LABELS, self.level),
            "status": self.status,
            "status_label": label_of(EXCEEDANCE_STATUS_LABELS, self.status),
            "note": self.note,
            "annotator": self.annotator,
            "annotated_at": iso(self.annotated_at),
            "measured_at": iso(self.measured_at),
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
            "station_name": self.station.name if self.station else None,
            "station_code": self.station.code if self.station else None,
            "unit": self.measurement.unit if self.measurement else None,
        }
        if include_relations and self.measurement:
            payload["measurement"] = self.measurement.to_dict(include_station=True)
        if include_relations:
            payload["annotations"] = [entry.to_dict() for entry in self.annotations]
            payload["annotation_count"] = len(self.annotations)
        return payload

    def __repr__(self):
        return "<Exceedance %s %s %.2f>" % (self.station_id, self.pollutant, self.value)


class ExceedanceAnnotation(db.Model):
    """超标记录标注日志: 每次有效标注操作的操作人/时间/说明与前后差异."""

    __tablename__ = "exceedance_annotations"

    id = db.Column(db.Integer, primary_key=True)
    exceedance_id = db.Column(
        db.Integer,
        db.ForeignKey("exceedances.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    annotator = db.Column(db.String(64), nullable=False)
    note = db.Column(db.Text)
    status_from = db.Column(db.String(16))
    status_to = db.Column(db.String(16), nullable=False)
    level_from = db.Column(db.String(16))
    level_to = db.Column(db.String(16))
    changes = db.Column(db.JSON, nullable=False, default=list)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    exceedance = db.relationship("Exceedance", back_populates="annotations")

    def to_dict(self):
        return {
            "id": self.id,
            "exceedance_id": self.exceedance_id,
            "annotator": self.annotator,
            "note": self.note,
            "status_from": self.status_from,
            "status_to": self.status_to,
            "status_from_label": label_of(EXCEEDANCE_STATUS_LABELS, self.status_from)
            if self.status_from
            else None,
            "status_to_label": label_of(EXCEEDANCE_STATUS_LABELS, self.status_to),
            "level_from": self.level_from,
            "level_to": self.level_to,
            "level_from_label": label_of(EXCEEDANCE_LEVEL_LABELS, self.level_from)
            if self.level_from
            else None,
            "level_to_label": label_of(EXCEEDANCE_LEVEL_LABELS, self.level_to)
            if self.level_to
            else None,
            "changes": list(self.changes or []),
            "created_at": iso(self.created_at),
        }

    def __repr__(self):
        return "<ExceedanceAnnotation %s %s->%s by %s>" % (
            self.exceedance_id,
            self.status_from,
            self.status_to,
            self.annotator,
        )
