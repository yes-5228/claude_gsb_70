"""超标标注操作留痕 (每次人工标注动作追加一条, 不可修改)."""
from datetime import datetime

from ..domain.constants import (
    EXCEEDANCE_LEVEL_LABELS,
    EXCEEDANCE_STATUS_LABELS,
    label_of,
)
from ..extensions import db
from .base import iso

# 操作类型与展示文案
ANNOTATION_ACTION_LABELS = {
    "confirm": "确认超标",
    "ignore": "忽略记录",
    "reset": "重置为待标注",
    "adjust_level": "修正等级",
}


class AnnotationLog(db.Model):
    __tablename__ = "annotation_logs"

    id = db.Column(db.Integer, primary_key=True)
    exceedance_id = db.Column(
        db.Integer,
        db.ForeignKey("exceedances.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action = db.Column(db.String(16), nullable=False)
    # 标注前后的状态/等级快照, 便于事后看清“前后两次的差别”
    from_status = db.Column(db.String(16))
    to_status = db.Column(db.String(16))
    from_level = db.Column(db.String(16))
    to_level = db.Column(db.String(16))
    note = db.Column(db.Text)
    annotator = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    exceedance = db.relationship("Exceedance", back_populates="annotations")

    def to_dict(self):
        return {
            "id": self.id,
            "exceedance_id": self.exceedance_id,
            "action": self.action,
            "action_label": label_of(ANNOTATION_ACTION_LABELS, self.action),
            "from_status": self.from_status,
            "to_status": self.to_status,
            "from_status_label": label_of(EXCEEDANCE_STATUS_LABELS, self.from_status)
            if self.from_status
            else None,
            "to_status_label": label_of(EXCEEDANCE_STATUS_LABELS, self.to_status)
            if self.to_status
            else None,
            "from_level": self.from_level,
            "to_level": self.to_level,
            "from_level_label": label_of(EXCEEDANCE_LEVEL_LABELS, self.from_level)
            if self.from_level
            else None,
            "to_level_label": label_of(EXCEEDANCE_LEVEL_LABELS, self.to_level)
            if self.to_level
            else None,
            "note": self.note,
            "annotator": self.annotator,
            "created_at": iso(self.created_at),
        }

    def __repr__(self):
        return "<AnnotationLog %s exceedance=%s by=%s>" % (
            self.action,
            self.exceedance_id,
            self.annotator,
        )
