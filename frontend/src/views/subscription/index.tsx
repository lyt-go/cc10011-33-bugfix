import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/subscription'
const columns = [
  "订阅编号",
  "订阅内容",
  "接收人",
  "推送渠道",
  "推送频率",
  "数据粒度",
  "静默时段",
  "下次推送时间"
]
const actions = [
  "提交订阅评审",
  "启用订阅",
  "暂停订阅"
]
const statuses = [
  "草稿",
  "订阅评审中",
  "已启用",
  "已暂停"
]
const channelOptions = ["邮件", "企业微信", "短信", "Webhook"]
const frequencyOptions = ["实时", "每小时", "每日", "每周", "每月"]

type Row = Record<string, string | number | boolean | null>

/** 操作人放在请求头里；HTTP 头只能 latin-1，中文必须先编码。 */
function operatorHeaders(operator: string): Record<string, string> {
  return operator ? { 'X-Operator': encodeURIComponent(operator) } : {}
}

function formatSilence(value: unknown): string {
  if (!value || typeof value !== 'object') return '—'
  const judge = value as Record<string, unknown>
  if (judge['是否静默']) {
    const conflict = judge['冲突'] ? '（两时区冲突，按任一命中处理）' : ''
    return `静默${conflict}｜${String(judge['判定依据'] ?? '')}`
  }
  return `可推送｜${String(judge['判定依据'] ?? '')}`
}

export default function SubscriptionView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [operator, setOperator] = useState('王芳')

  const [selected, setSelected] = useState<Row | null>(null)
  const [editValues, setEditValues] = useState<Record<string, string>>({})
  const [deliveries, setDeliveries] = useState<Row[]>([])
  const [deliveryTotal, setDeliveryTotal] = useState(0)
  const [versionFilter, setVersionFilter] = useState('')
  const [trace, setTrace] = useState<Record<string, unknown> | null>(null)
  const [mergeResult, setMergeResult] = useState<Record<string, unknown> | null>(null)
  const [recovery, setRecovery] = useState<Record<string, unknown> | null>(null)

  const filterFields = useMemo(() => ["订阅编号", "订阅内容", "推送渠道"], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`, { headers: operatorHeaders(operator) })
      if (!response.ok) {
        throw new Error('订阅配置列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '订阅推送列表读取失败')
    }
  }, [filters, operator])

  const reloadDeliveries = useCallback(async () => {
    const params = new URLSearchParams()
    if (versionFilter) params.set('insight_version', versionFilter)
    params.set('size', '50')
    const response = await request(`${ENDPOINT}/deliveries?${params.toString()}`)
    if (!response.ok) return
    const payload = await response.json()
    setDeliveries(payload.items ?? [])
    setDeliveryTotal(payload.total ?? 0)
  }, [versionFilter])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    void reloadDeliveries()
  }, [reloadDeliveries])

  function selectRow(row: Row) {
    setSelected(row)
    setMergeResult(null)
    setTrace(null)
    const next: Record<string, string> = {}
    for (const field of [...columns, '渠道时区', '接收人时区']) {
      next[field] = String(row[field] ?? '')
    }
    setEditValues(next)
  }

  async function readJson(response: Response): Promise<Record<string, unknown>> {
    const payload = await response.json().catch(() => ({}))
    if (!response.ok) {
      const detail = (payload as Record<string, unknown>)?.detail
      throw new Error(typeof detail === 'string' ? detail : `接口返回 ${response.status}`)
    }
    return payload as Record<string, unknown>
  }

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        headers: operatorHeaders(operator),
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await readJson(response)
      setNotice(String(payload.message ?? '动作已执行'))
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '订阅推送操作失败')
    }
  }

  async function saveConfig() {
    if (!selected) return
    setErrorMessage('')
    setNotice('')
    const values: Record<string, string> = {}
    for (const field of [...columns, '渠道时区', '接收人时区']) {
      // 只提交有值的字段：受保护的字段留空时后端会保留原值，不会被清空。
      if (editValues[field]) values[field] = editValues[field]
    }
    try {
      const response = await request(`${ENDPOINT}/${selected.id}`, {
        method: 'PATCH',
        headers: operatorHeaders(operator),
        body: JSON.stringify({ values }),
      })
      const payload = await readJson(response)
      setNotice(String(payload.message ?? '已保存'))
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '订阅配置保存失败')
    }
  }

  async function mergePending() {
    if (!selected) return
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${selected.id}/merge`, {
        method: 'POST',
        headers: operatorHeaders(operator),
        body: JSON.stringify({ values: {} }),
      })
      const payload = await readJson(response)
      setMergeResult(payload.entry as Record<string, unknown>)
      setNotice(String(payload.message ?? '已合并'))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '合并失败')
    }
  }

  async function dispatchNow(force: boolean) {
    if (!selected) return
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(`${ENDPOINT}/${selected.id}/dispatch`, {
        method: 'POST',
        headers: operatorHeaders(operator),
        body: JSON.stringify({ values: { force } }),
      })
      const payload = await readJson(response)
      setNotice(String(payload.message ?? '已触发推送'))
      setMergeResult((payload.entry as Record<string, unknown>) ?? null)
      await Promise.all([reload(), reloadDeliveries()])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '推送失败')
    }
  }

  async function loadTrace() {
    if (!selected) return
    const params = new URLSearchParams({ subscription_id: String(selected.id) })
    try {
      const payload = await readJson(await request(`${ENDPOINT}/trace?${params.toString()}`))
      setTrace(payload)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '追溯链路读取失败')
    }
  }

  async function loadRecovery() {
    try {
      setRecovery(await readJson(await request(`${ENDPOINT}/recovery`)))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '恢复信息读取失败')
    }
  }

  async function retryDelivery(record: Row) {
    setErrorMessage('')
    try {
      const payload = await readJson(
        await request(`${ENDPOINT}/deliveries/${record.id}/retry`, {
          method: 'POST',
          headers: operatorHeaders(operator),
          body: JSON.stringify({ values: {} }),
        }),
      )
      setNotice(String(payload.message ?? '已重试'))
      await reloadDeliveries()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '重试失败')
    }
  }

  const stats = useMemo(() => ([
    { label: '订阅配置总数', value: total },
    { label: '已启用订阅', value: rows.filter((row) => row.status === '已启用').length },
    { label: '评审中订阅', value: rows.filter((row) => row.status === '订阅评审中').length },
    { label: '推送数据记录', value: deliveryTotal },
  ]), [rows, total, deliveryTotal])

  return (
    <section className="page" data-module="subscription">
      <header className="page-head">
        <div>
          <h2>订阅推送管理</h2>
          <p className="page-desc">管理数据报告的订阅和推送配置，按频率合并待推送数据，定时向指定接收人推送分析结果。</p>
        </div>
        <div className="page-actions">
          <label className="filter-item">
            <span>当前操作人</span>
            <input value={operator} onChange={(event) => setOperator(event.target.value)} />
          </label>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出订阅推送清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <label className="filter-item">
          <span>状态</span>
          <select
            value={filters.status ?? ''}
            onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}
          >
            <option value="">全部</option>
            {statuses.map((status) => <option key={status} value={status}>{status}</option>)}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>静默判定</th>
            <th>归属</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} className={selected?.id === row.id ? 'row-selected' : undefined}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="cell-detail">{formatSilence(row['静默判定'])}</td>
              <td>{String(row['归属人'] ?? '—')}{row['共享只读'] ? '（只读）' : ''}</td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => selectRow(row)}>详情</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 3}>暂无订阅推送数据，可先登记订阅配置</td>
            </tr>
          )}
        </tbody>
      </table>

      {selected && (
        <section className="panel">
          <h3>订阅配置 {String(selected['订阅编号'] ?? selected.id)}</h3>
          <p className="page-desc">
            字段留空保存时，接收人、推送渠道、静默时段和下次推送时间会保留原值；暂停后不补发，恢复只重算下次推送时间。
          </p>
          <div className="filter-bar">
            {["接收人", "推送渠道", "推送频率", "静默时段", "下一次推送时间"].map((field) => {
              const key = field === '下一次推送时间' ? '下次推送时间' : field
              const isSelect = field === '推送频率' || field === '推送渠道'
              return (
                <label key={field} className="filter-item">
                  <span>{field}</span>
                  {isSelect ? (
                    <select
                      value={editValues[key] ?? ''}
                      disabled={Boolean(selected['共享只读'])}
                      onChange={(event) => setEditValues((prev) => ({ ...prev, [key]: event.target.value }))}
                    >
                      <option value="">保留原值</option>
                      {(field === '推送频率' ? frequencyOptions : channelOptions).map((option) => (
                        <option key={option} value={option}>{option}</option>
                      ))}
                    </select>
                  ) : (
                    <input
                      value={editValues[key] ?? ''}
                      disabled={Boolean(selected['共享只读'])}
                      placeholder={field === '静默时段' ? '22:00-07:00' : ''}
                      onChange={(event) => setEditValues((prev) => ({ ...prev, [key]: event.target.value }))}
                    />
                  )}
                </label>
              )
            })}
          </div>
          <div className="page-actions">
            <button className="btn primary" type="button" disabled={Boolean(selected['共享只读'])} onClick={() => void saveConfig()}>
              保存配置
            </button>
            <button className="btn" type="button" disabled={Boolean(selected['共享只读'])} onClick={() => void mergePending()}>
              按频率合并待推送
            </button>
            <button className="btn" type="button" disabled={Boolean(selected['共享只读'])} onClick={() => void dispatchNow(false)}>
              按计划调度推送
            </button>
            <button className="btn ghost" type="button" disabled={Boolean(selected['共享只读'])} onClick={() => void dispatchNow(true)}>
              立即推送（忽略下次推送时间）
            </button>
            <button className="btn ghost" type="button" onClick={() => void loadTrace()}>查看投递链</button>
            <button className="btn ghost" type="button" onClick={() => void loadRecovery()}>恢复信息</button>
          </div>
          <dl className="kv-list">
            <dt>静默判定</dt><dd>{formatSilence(selected['静默判定'])}</dd>
            <dt>下次推送时间</dt><dd>{String(selected['下次推送时间'] ?? '—')}</dd>
            <dt>接收人时区 / 渠道时区</dt>
            <dd>{String(editValues['接收人时区'] || '—')} / {String(editValues['渠道时区'] || '—')}</dd>
            <dt>暂停后不补发</dt><dd>{selected['暂停后不补发'] ? '是' : '否'}</dd>
          </dl>

          {mergeResult && (
            <div className="code-block">
              <strong>合并结果</strong>
              <pre>{JSON.stringify(mergeResult['merged'] ?? mergeResult, null, 2)}</pre>
            </div>
          )}
          {recovery && (
            <div className="code-block">
              <strong>恢复信息（中断后从上一完整版本继续）</strong>
              <pre>{JSON.stringify(recovery, null, 2)}</pre>
            </div>
          )}
          {trace && (
            <div className="code-block">
              <strong>投递链（静默判定可追溯：{String(trace['静默判定可追溯'])}）</strong>
              <pre>{JSON.stringify(trace['投递链'], null, 2)}</pre>
            </div>
          )}
        </section>
      )}

      <section className="panel">
        <h3>订阅推送数据记录</h3>
        <div className="filter-bar">
          <label className="filter-item">
            <span>内容版本</span>
            <input
              value={versionFilter}
              placeholder="如 USER-0001@v1"
              onChange={(event) => setVersionFilter(event.target.value)}
            />
          </label>
          <button className="btn" type="button" onClick={() => void reloadDeliveries()}>查询</button>
        </div>
        <table className="data-table">
          <thead>
            <tr>
              <th>记录</th><th>内容版本</th><th>接收人</th><th>渠道</th><th>状态</th>
              <th>重复触发</th><th>静默判定</th><th>下次推送时间</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            {deliveries.map((record) => (
              <tr key={String(record.id)}>
                <td>{String(record.id)}</td>
                <td>{String(record['内容版本'] ?? '—')}</td>
                <td>{String(record['接收人'] ?? '—')}</td>
                <td>{String(record['推送渠道'] ?? '—')}</td>
                <td>{String(record['状态'] ?? '—')}</td>
                <td>{record['重复触发'] ? `已合并 ${String(record['重复触发次数'] ?? 0)} 次` : '—'}</td>
                <td className="cell-detail">{formatSilence(record['静默判定'])}</td>
                <td>{String(record['下次推送时间'] ?? '—')}</td>
                <td>
                  {record['状态'] === '投递失败' && (
                    <button className="link" type="button" onClick={() => void retryDelivery(record)}>重试</button>
                  )}
                </td>
              </tr>
            ))}
            {deliveries.length === 0 && (
              <tr><td className="empty-state" colSpan={9}>暂无订阅推送数据记录</td></tr>
            )}
          </tbody>
        </table>
      </section>

      <footer className="page-foot">
        <span>共 {total} 条订阅配置，{deliveryTotal} 条推送数据记录</span>
        {notice && <span className="ok-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
