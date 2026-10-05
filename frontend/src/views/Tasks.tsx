import { useCallback, useState } from 'react'
import { api, type ManagedNode, type PanelTask } from '../api'
import { Badge, EmptyState, PageHeader, Toolbar } from '../components/ui'
import Collapse, { LiveTag, usePoll } from '../components/Live'

const MS = 3000

const STATE_BADGE: Record<string, { tone: 'ok' | 'warn' | 'muted' | 'danger' | 'info'; text: string }> = {
  pending: { tone: 'muted', text: '排队中' },
  running: { tone: 'info', text: '执行中' },
  done: { tone: 'ok', text: '完成' },
  failed: { tone: 'danger', text: '失败' },
  cancelled: { tone: 'warn', text: '已取消' },
}

/** 回环地址上的节点与 hub 共用同一个进程、同一个任务队列 —— 别去查第二遍 */
const SELF_HOSTS = new Set(['', '127.0.0.1', '::1', 'localhost'])

type TaskRow = PanelTask & { node: string }

function fmtTime(ts: number | null): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function fmtDur(t: PanelTask): string {
  if (!t.started_at) return '—'
  const end = t.finished_at ?? Date.now() / 1000
  const s = Math.max(0, Math.round(end - t.started_at))
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m${s % 60}s`
}

/**
 * 任务中心（全局页）：面板操作的后台任务。
 * 聚合范围 = hub 本机队列 + **每台真实远程节点**的队列（命令通道 task.list）；
 * 回环自连的节点与 hub 同进程同队列，不重复查。
 * 取消只对"排队中"的生效 —— 执行中的进程型任务无法中断，如实拒绝。
 */
export default function Tasks() {
  const [tasks, setTasks] = useState<TaskRow[] | null>(null)
  const [stats, setStats] = useState<Record<string, number>>({})
  const [state, setState] = useState('')
  const [auto, setAuto] = useState(true)
  const [at, setAt] = useState(0)
  const [open, setOpen] = useState<string | null>(null)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [err, setErr] = useState('')

  const load = useCallback(async () => {
    try {
      const [hub, nodesR] = await Promise.all([
        api.tasks(state || undefined),
        api.nodes().catch(() => null),
      ])
      const merged: TaskRow[] = (hub.tasks || []).map((t) => ({ ...t, node: '本机' }))
      const nodes: ManagedNode[] = nodesR?.nodes || []
      const remotes = nodes.filter(
        (n) => n.status === 'online' && !SELF_HOSTS.has((n.host || '').trim()))
      await Promise.all(remotes.map(async (n) => {
        try {
          const r = await api.sendCmd(n.name, 'task.list', { limit: 50 }, 20)
          // sendCmd 返回 { ok, data } 信封
          for (const t of (r?.data?.tasks || [])) merged.push({ ...t, node: n.name })
        } catch { /* 节点扩展过旧 / 瞬时不可达：跳过，不拖垮整页 */ }
      }))
      merged.sort((a, b) => (b.submitted_at || 0) - (a.submitted_at || 0))
      setTasks(merged)
      setStats(hub.stats || {})
      setErr('')
      setAt(Date.now())
    } catch (e: any) {
      setErr(e?.message || '加载失败')
    }
  }, [state])

  usePoll(load, MS, auto)

  async function cancel(t: TaskRow) {
    try {
      if (t.node === '本机') {
        await api.cancelTask(t.id)
      } else {
        await api.sendCmd(t.node, 'task.cancel', { id: t.id }, 20)
      }
      setMsg({ kind: 'ok', text: `已取消：${t.name}` })
      load()
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '取消失败' })
    }
  }

  const list = tasks || []
  const running = (stats.running || 0) + (stats.pending || 0)

  return (
    <>
      <PageHeader
        title="任务"
        sub={<>面板操作的后台任务：安装、备份、巡检……每一条都能看到状态与日志
          {running > 0 && <> · <span style={{ color: 'var(--accent)' }}>{running} 个进行中</span></>}
        </>}
        actions={
          <Toolbar right={
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <LiveTag at={at} enabled={auto} intervalMs={MS} onToggle={() => setAuto((v) => !v)} />
              <button className="btn btn-sm" onClick={() => load()}>刷新</button>
            </div>
          } />
        }
      />

      {err && <div className="empty">{err}</div>}
      {msg && (
        <div style={{
          marginBottom: 10, padding: '8px 12px', fontSize: 12.5,
          borderRadius: 'var(--radius-sm)',
          color: msg.kind === 'ok' ? 'var(--ok)' : 'var(--danger)',
          background: msg.kind === 'ok' ? 'var(--ok-soft)' : 'var(--danger-soft)',
        }}>{msg.text}</div>
      )}

      <Toolbar left={
        <div style={{ display: 'flex', gap: 6 }}>
          {[['', '全部'], ['running', '执行中'], ['pending', '排队'], ['done', '完成'], ['failed', '失败']].map(([k, label]) => (
            <button key={k} className={'btn btn-sm ' + (state === k ? 'btn-primary' : 'btn-ghost')}
                    onClick={() => setState(k)}>{label}</button>
          ))}
        </div>
      } right={<span className="badge badge-muted">{list.length} 条</span>} />

      <div className="card" style={{ padding: '4px 16px 8px' }}>
        {list.length === 0 ? (
          <EmptyState title="还没有任务"
                      hint="在工作台的「服务」页安装一个服务，或执行其它长操作试试" />
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>任务</th>
                <th style={{ width: 110 }}>节点</th>
                <th style={{ width: 100 }}>状态</th>
                <th style={{ width: 90 }}>提交</th>
                <th style={{ width: 90 }}>耗时</th>
                <th style={{ width: 150, textAlign: 'right' }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {list.map((t) => {
                const b = STATE_BADGE[t.state] || STATE_BADGE.pending
                return (
                  <tr key={`${t.node}:${t.id}`}>
                    <td>
                      <span className="cell-main">{t.name}</span>
                      {t.error && (
                        <div className="mono" style={{ fontSize: 11, color: 'var(--danger)', maxWidth: 420, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                          {t.error}
                        </div>
                      )}
                    </td>
                    <td className="mono" style={{ color: t.node === '本机' ? 'var(--text-mute)' : 'var(--accent)' }}>
                      {t.node}
                    </td>
                    <td><Badge tone={b.tone}>{b.text}</Badge></td>
                    <td className="mono" style={{ color: 'var(--text-mute)' }}>{fmtTime(t.submitted_at)}</td>
                    <td className="mono" style={{ color: 'var(--text-mute)' }}>{fmtDur(t)}</td>
                    <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                      {t.state === 'pending' && (
                        <button className="btn-text danger" onClick={() => cancel(t)}>取消</button>
                      )}
                      <button className="btn-text" onClick={() => setOpen(open === t.id ? null : t.id)}>
                        {open === t.id ? '收起日志' : '日志'}
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      {open && (() => {
        const t = list.find((x) => x.id === open)
        if (!t) return null
        return (
          <div className="card" style={{ marginTop: 12 }}>
            <Collapse title={`日志 · ${t.name}（${t.node}）`} count={String((t.log || []).length)}
                      defaultOpen storageKey={`task.${t.id}`}>
              {(t.log || []).length ? (
                <pre className="mono" style={{
                  margin: 0, padding: 12, fontSize: 11.5, maxHeight: 300, overflow: 'auto',
                  whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                  background: 'var(--bg-elev)', border: '1px solid var(--border)',
                  borderRadius: 'var(--radius-sm)', color: 'var(--text-dim)',
                }}>{t.log!.join('\n')}</pre>
              ) : (
                <div className="empty" style={{ padding: 12 }}>暂无进度日志</div>
              )}
              {t.error && (
                <div style={{ marginTop: 10, fontSize: 12.5, color: 'var(--danger)' }}>
                  {t.error}
                </div>
              )}
            </Collapse>
          </div>
        )
      })()}
    </>
  )
}
