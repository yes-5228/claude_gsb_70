import { useCallback, useEffect, useState } from 'react'
import { annotateExceedance, getExceedance } from '../../../api/exceedances.js'
import Modal from '../../../components/common/Modal.jsx'
import Tag from '../../../components/common/Tag.jsx'
import { Field, Input, Textarea } from '../../../components/common/FormField.jsx'
import { Alert, ErrorState, Loading } from '../../../components/common/Feedback.jsx'
import { useToast } from '../../../components/common/ToastProvider.jsx'
import { EXCEEDANCE_LEVEL_TONE, EXCEEDANCE_STATUS_TONE } from '../../../constants/index.js'
import { useAsyncData } from '../../../hooks/useAsyncData.js'
import { formatDateTime, formatNumber, formatRatio } from '../../../utils/format.js'

const STATUS_CHOICES = [
  { value: 'confirmed', label: '确认超标', hint: '经复核确属超标, 需记录处置说明' },
  { value: 'ignored', label: '忽略记录', hint: '设备异常 / 校准期数据等, 需说明原因' },
  { value: 'pending', label: '保持待标注', hint: '暂不处理, 保留在待办列表' }
]

const FIELD_ERROR_TEXT = { required: '此项不能为空' }
const fieldError = (code) => FIELD_ERROR_TEXT[code] || code

const changeText = (change) =>
  `${change.label}: ${change.from_label ?? change.from ?? '空'} → ${change.to_label ?? change.to ?? '空'}`

export default function AnnotationModal({ exceedanceId, onClose, onSaved }) {
  const toast = useToast()
  const loader = useCallback(() => getExceedance(exceedanceId), [exceedanceId])
  const { data, loading, error } = useAsyncData(loader, { immediate: Boolean(exceedanceId) })
  const [form, setForm] = useState({ status: 'confirmed', level: '', note: '', annotator: '' })
  const [errors, setErrors] = useState({})
  const [message, setMessage] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!data) return
    setForm({
      status: data.status,
      level: data.level,
      note: data.note || '',
      annotator: data.annotator || ''
    })
    setErrors({})
    setMessage(null)
  }, [data])

  const submit = async () => {
    if (!form.annotator.trim()) {
      setErrors((prev) => ({ ...prev, annotator: 'required' }))
      setMessage('标注动作必须留下操作人, 请填写标注人')
      return
    }
    setBusy(true)
    setMessage(null)
    try {
      const result = await annotateExceedance(exceedanceId, {
        status: form.status,
        level: form.level || null,
        note: form.note || null,
        annotator: form.annotator.trim()
      })
      const changes = result?.changes || []
      const diffText = changes.map(changeText).join('; ')
      toast.success(diffText ? `标注已保存 (${diffText})` : '标注已保存, 标注内容无变化')
      onSaved?.()
    } catch (err) {
      setErrors(err.fields || {})
      setMessage(err.message)
    } finally {
      setBusy(false)
    }
  }

  const measurement = data?.measurement

  return (
    <Modal
      open={Boolean(exceedanceId)}
      wide
      title={data ? `超标记录标注 · ${data.station_name}` : '超标记录标注'}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose} disabled={busy}>
            取消
          </button>
          <button type="button" className="btn btn-primary" onClick={submit} disabled={busy || !data}>
            {busy ? '保存中...' : '保存标注'}
          </button>
        </>
      }
    >
      {loading && !data ? <Loading /> : null}
      {error && !data ? <ErrorState error={error} /> : null}
      {data ? (
        <div className="stack">
          <div className="stat-grid">
            <div className="stat-card">
              <div className="stat-label">监测值 / 限值</div>
              <div className="stat-value danger-text">
                {formatNumber(data.value)} <small>/ {formatNumber(data.limit_value)} {data.unit || ''}</small>
              </div>
            </div>
            <div className="stat-card">
              <div className="stat-label">超标倍数</div>
              <div className="stat-value">{formatRatio(data.exceed_ratio)}</div>
            </div>
            <div className="stat-card">
              <div className="stat-label">当前状态</div>
              <div style={{ marginTop: 8 }}>
                <Tag tone={EXCEEDANCE_STATUS_TONE[data.status]}>{data.status_label}</Tag>
              </div>
              <div className="stat-foot">
                <Tag tone={EXCEEDANCE_LEVEL_TONE[data.level]}>{data.level_label}</Tag>
              </div>
            </div>
          </div>

          <dl className="kv">
            <dt>监测点</dt>
            <dd>
              {data.station_name} <span className="mono muted">{data.station_code}</span>
            </dd>
            <dt>监测时间</dt>
            <dd>
              {formatDateTime(data.measured_at)} · {data.period_label}
            </dd>
            <dt>监测因子</dt>
            <dd>{measurement?.pollutant_label || data.pollutant_label}</dd>
            <dt>数据录入</dt>
            <dd>
              {measurement?.recorder || '-'} · {measurement?.data_source_label || '-'}
            </dd>
            <dt>最近标注</dt>
            <dd>
              {data.annotator
                ? `${data.annotator} · ${formatDateTime(data.annotated_at)} · ${data.status_label}`
                : '尚未标注'}
            </dd>
          </dl>

          <div className="stack" style={{ gap: 8 }}>
            <span className="field-label">
              标注历史{data.annotations?.length ? ` (${data.annotations.length} 次, 最新在前)` : ''}
            </span>
            {data.annotations?.length ? (
              data.annotations.map((entry) => (
                <div key={entry.id} className="card" style={{ boxShadow: 'none' }}>
                  <div className="card-body tight stack" style={{ gap: 6 }}>
                    <div className="inline" style={{ gap: 8 }}>
                      <Tag tone={EXCEEDANCE_STATUS_TONE[entry.status_to]}>{entry.status_to_label}</Tag>
                      <span className="strong">{entry.annotator}</span>
                      <span className="small muted">{formatDateTime(entry.created_at)}</span>
                    </div>
                    {entry.changes?.length ? (
                      <div className="small">
                        {entry.changes.map((change) => (
                          <div key={change.field}>{changeText(change)}</div>
                        ))}
                      </div>
                    ) : null}
                    {entry.note ? <div className="small muted">说明: {entry.note}</div> : null}
                  </div>
                </div>
              ))
            ) : (
              <Alert tone="info">本条记录尚无标注历史, 首次标注后将在此留下操作人、时间与说明。</Alert>
            )}
          </div>

          {message ? <Alert tone="error">{message}</Alert> : null}

          <Field label="标注结论" required error={errors.status}>
            <div className="stack">
              {STATUS_CHOICES.map((choice) => (
                <label key={choice.value} className="checkbox" style={{ alignItems: 'flex-start' }}>
                  <input
                    type="radio"
                    name="annotation-status"
                    checked={form.status === choice.value}
                    onChange={() => setForm({ ...form, status: choice.value })}
                  />
                  <span>
                    <span className="strong">{choice.label}</span>
                    <span className="small muted" style={{ display: 'block' }}>
                      {choice.hint}
                    </span>
                  </span>
                </label>
              ))}
            </div>
          </Field>

          <div className="form-grid">
            <Field label="超标等级 (可人工修正)" error={errors.level}>
              <select
                className="select"
                value={form.level || ''}
                onChange={(event) => setForm({ ...form, level: event.target.value })}
              >
                <option value="light">轻度超标</option>
                <option value="moderate">中度超标</option>
                <option value="severe">重度超标</option>
              </select>
            </Field>
            <Field label="标注人" required error={fieldError(errors.annotator)}>
              <Input
                value={form.annotator}
                onChange={(event) => setForm({ ...form, annotator: event.target.value })}
                invalid={Boolean(errors.annotator)}
                placeholder="如: 王敏"
              />
            </Field>
          </div>

          <Field
            label="标注说明"
            required={form.status !== 'pending'}
            error={fieldError(errors.note)}
            hint="确认或忽略时必须填写原因, 便于后续追溯"
          >
            <Textarea
              value={form.note}
              onChange={(event) => setForm({ ...form, note: event.target.value })}
              invalid={Boolean(errors.note)}
              placeholder="如: 数据经复核属实, 已通知运维排查周边排放源"
            />
          </Field>
        </div>
      ) : null}
    </Modal>
  )
}
