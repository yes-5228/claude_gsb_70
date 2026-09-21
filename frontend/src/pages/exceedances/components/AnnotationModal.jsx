import { useCallback, useEffect, useMemo, useState } from 'react'
import { annotateExceedance, getExceedance } from '../../../api/exceedances.js'
import Modal from '../../../components/common/Modal.jsx'
import Tag from '../../../components/common/Tag.jsx'
import { Field, Input, Select, Textarea } from '../../../components/common/FormField.jsx'
import { Alert, ErrorState, Loading } from '../../../components/common/Feedback.jsx'
import { useToast } from '../../../components/common/ToastProvider.jsx'
import { EXCEEDANCE_LEVEL_TONE, EXCEEDANCE_STATUS_TONE } from '../../../constants/index.js'
import { useAsyncData } from '../../../hooks/useAsyncData.js'
import { formatDateTime, formatNumber, formatRatio } from '../../../utils/format.js'

const STATUS_CHOICES = [
  { value: 'confirmed', label: '确认超标', hint: '经复核确属超标, 需记录处置说明; 计入超标统计与高发因子排名' },
  { value: 'ignored', label: '忽略记录', hint: '设备异常 / 校准期数据等, 需说明原因; 不参与超标等级、高发因子与站点排名' },
  { value: 'pending', label: '重置为待标注', hint: '清空当前标注快照回到待办; 操作留痕仍保留, 可随时追溯' }
]

const ACTION_TONE = {
  confirm: 'danger',
  ignore: 'neutral',
  reset: 'warning',
  adjust_level: 'info'
}

function Diff({ before, after }) {
  if (before === after) return <span>{after}</span>
  return (
    <span>
      <span className="muted" style={{ textDecoration: 'line-through' }}>{before ?? '—'}</span>
      {' → '}
      <span className="strong">{after ?? '—'}</span>
    </span>
  )
}

function AnnotationHistory({ logs }) {
  if (!logs?.length) {
    return <Alert tone="info">该记录尚未标注, 本次提交将留下第一条操作留痕。</Alert>
  }
  return (
    <div className="stack" style={{ gap: 8 }}>
      {logs.map((log, index) => (
        <div
          key={log.id}
          className="card"
          style={{
            boxShadow: 'none',
            borderLeft: '3px solid var(--border-strong, #c7ccd6)',
            margin: 0
          }}
        >
          <div className="card-body tight">
            <div className="inline" style={{ justifyContent: 'space-between' }}>
              <span className="inline" style={{ gap: 8 }}>
                <Tag tone={ACTION_TONE[log.action] || 'neutral'}>{log.action_label}</Tag>
                {index === 0 ? <Tag tone="primary">最近一次</Tag> : null}
              </span>
              <span className="small muted mono">{formatDateTime(log.created_at)}</span>
            </div>
            <div className="small muted" style={{ marginTop: 6 }}>
              操作人: <span className="strong">{log.annotator}</span>
            </div>
            <div className="small" style={{ marginTop: 4 }}>
              状态: <Diff before={log.from_status_label} after={log.to_status_label} />
              {log.from_level !== log.to_level ? (
                <>
                  {' · '}等级: <Diff before={log.from_level_label} after={log.to_level_label} />
                </>
              ) : null}
            </div>
            {log.note ? (
              <div className="small" style={{ marginTop: 4 }}>
                说明: {log.note}
              </div>
            ) : null}
          </div>
        </div>
      ))}
    </div>
  )
}

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

  const isUnchanged = useMemo(() => {
    if (!data) return false
    return (
      form.status === data.status &&
      (form.level || data.level) === data.level &&
      (form.note || '') === (data.note || '')
    )
  }, [form, data])

  const alreadyAnnotated = Boolean(data?.annotated_at)

  const submit = async () => {
    if (isUnchanged) {
      toast.info('标注内容与当前记录一致, 未产生新的操作留痕')
      return
    }
    setBusy(true)
    setMessage(null)
    try {
      await annotateExceedance(exceedanceId, {
        status: form.status,
        level: form.level || null,
        note: form.note || null,
        annotator: form.annotator || null
      })
      toast.success(alreadyAnnotated ? '已更新标注, 上一次标注保留在操作留痕中' : '标注已保存')
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
          <button
            type="button"
            className="btn btn-primary"
            onClick={submit}
            disabled={busy || !data || isUnchanged}
          >
            {busy ? '保存中...' : alreadyAnnotated ? '再次标注 (追加留痕)' : '保存标注'}
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

          {alreadyAnnotated ? (
            <Alert tone="warning">
              该记录此前由 <span className="strong">{data.annotator}</span> 于{' '}
              {formatDateTime(data.annotated_at)} 标注为「{data.status_label}」, 已标注 {data.annotation_count} 次。
              再次提交会保留上次留痕, 并记录本次操作人与时间。
            </Alert>
          ) : null}

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
          </dl>

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

          {form.status === 'ignored' ? (
            <Alert tone="warning">
              忽略后该记录仍保留在超标台账与状态统计中, 但不再计入超标等级分布、平均/最大超标倍数、高发因子与站点排名。
            </Alert>
          ) : null}

          <div className="form-grid">
            <Field label="超标等级 (可人工修正)" error={errors.level}>
              <Select
                value={form.level || ''}
                onChange={(event) => setForm({ ...form, level: event.target.value })}
              >
                <option value="light">轻度超标</option>
                <option value="moderate">中度超标</option>
                <option value="severe">重度超标</option>
              </Select>
            </Field>
            <Field label="标注人" required error={errors.annotator} hint="留痕需要, 提交后不可匿名">
              <Input
                value={form.annotator}
                onChange={(event) => setForm({ ...form, annotator: event.target.value })}
                placeholder="如: 王敏"
              />
            </Field>
          </div>

          <Field
            label="标注说明"
            required={form.status !== 'pending'}
            error={errors.note}
            hint={form.status === 'pending'
              ? '重置为待标注将清空说明快照, 本说明可选'
              : '确认或忽略时必须填写原因, 连同操作人与时间一起留痕, 便于后续追溯'}
          >
            <Textarea
              value={form.note}
              onChange={(event) => setForm({ ...form, note: event.target.value })}
              invalid={Boolean(errors.note)}
              placeholder="如: 数据经复核属实, 已通知运维排查周边排放源"
            />
          </Field>

          <div>
            <div className="field-label" style={{ marginBottom: 8 }}>
              标注留痕 ({data.annotations?.length || 0})
            </div>
            <AnnotationHistory logs={data.annotations} />
          </div>
        </div>
      ) : null}
    </Modal>
  )
}
