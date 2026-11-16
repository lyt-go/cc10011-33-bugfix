import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/userinsight'
const columns = [
  "报告编号",
  "分析主题",
  "目标人群",
  "关键指标",
  "核心发现",
  "数据区间",
  "生成时间",
  "下次生成",
  "最新快照"
]
const actions = [
  "提交生成",
  "发布报告",
  "归档报告"
]
const statuses = [
  "草稿",
  "生成中",
  "已发布",
  "已归档"
]
const stats = [
  {
    "label": "洞察报告总数",
    "value": 0
  },
  {
    "label": "已发布报告",
    "value": 0
  },
  {
    "label": "生成中报告",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

export default function UserinsightView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [selected, setSelected] = useState<Row | null>(null)
  const [snapshots, setSnapshots] = useState<Row[]>([])
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('洞察报告列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '用户洞察列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(String(payload.detail ?? '用户洞察动作未生效，请稍后重试'))
      }
      await reload()
      setNotice(String(payload.message ?? ''))
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '用户洞察操作失败')
    }
  }

  async function showSnapshots(row: Row) {
    setSelected(row)
    try {
      const response = await request(`${ENDPOINT}/${row.id}`)
      if (!response.ok) throw new Error('快照读取失败')
      const payload = await response.json()
      setSnapshots(payload['快照'] ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '快照读取失败')
    }
  }

  return (
    <section className="page" data-module="userinsight">
      <header className="page-head">
        <div>
          <h2>用户洞察管理</h2>
          <p className="page-desc">管理自动生成的用户行为洞察报告，汇总关键指标变化和趋势分析。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('洞察报告登记入口尚未接入审批流')}>
            登记洞察报告
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出用户洞察清单
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
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void showSnapshots(row)}>快照</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无用户洞察数据，可先登记洞察报告</td>
            </tr>
          )}
        </tbody>
      </table>

      {selected && (
        <section className="panel">
          <h3>洞察报告快照 {String(selected['报告编号'] ?? '')}</h3>
          <p className="page-desc">
            每次发布写入一版不可变快照，订阅推送的消息正文引用这里的最新快照；订阅状态回写后也会同步到快照。
          </p>
          <table className="data-table">
            <thead>
              <tr>
                <th>洞察版本</th><th>快照摘要</th><th>生成时间</th>
                <th>订阅配置</th><th>订阅状态</th><th>投递说明</th>
              </tr>
            </thead>
            <tbody>
              {snapshots.map((snapshot) => (
                <tr key={String(snapshot['洞察版本'])}>
                  <td>{String(snapshot['洞察版本'] ?? '—')}</td>
                  <td className="cell-detail">{String(snapshot['快照摘要'] ?? '—')}</td>
                  <td>{String(snapshot['生成时间'] ?? '—')}</td>
                  <td>{Array.isArray(snapshot['订阅配置']) ? (snapshot['订阅配置'] as number[]).join('、') : '—'}</td>
                  <td>{String(snapshot['订阅状态'] ?? '—')}</td>
                  <td className="cell-detail">{String(snapshot['投递说明'] ?? '—')}</td>
                </tr>
              ))}
              {snapshots.length === 0 && (
                <tr><td className="empty-state" colSpan={6}>该报告还没有快照，发布后会生成</td></tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条用户洞察记录</span>
        {notice && <span className="ok-text">{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
