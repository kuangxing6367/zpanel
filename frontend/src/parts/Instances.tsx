import { useCallback, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import type { RuntimeInstance } from '../api'
import { useNode } from '../node'
import { LiveTag, usePoll } from '../components/Live'
import { Badge, statusBadge, ConfirmModal, EmptyState, Field, Modal, PageHeader, Toolbar } from '../components/ui'

const POLL_MS = 3000

const KIND_LABEL: Record<string, string> = {
  node: 'Node.js', java: 'Java', php: 'PHP', python: 'Python', generic: '通用',
}

type Filter = 'all' | 'running' | 'stopped'

/**
 * 运行时 · 实例托管
 *
 * 页面结构（所有管理页同款）：
 *   页头（标题 + 主按钮「新建实例」）
 *   工具栏（状态筛选 / 搜索 ｜ 自动刷新 + 手动刷新）
 *   卡片表格（名称列可点进详情，操作列靠右）
 *   新建 = 模态对话框；删除 = 确认对话框
 */
export default function Instances({ nodeName }: { nodeName?: string }) {
  const nav = useNavigate()
  const { current, call } = useNode()
  const node = nodeName || current

  const [insts, setInsts] = useState<RuntimeInstance[]>([])
  const [probes, setProbes] = useState<{ kind: string; label: string; version: string; available: boolean }[]>([])
  const [filter, setFilter] = useState<Filter>('all')
  const [keyword, setKeyword] = useState('')
  const [at, setAt] = useState(0)
  const [auto, setAuto] = useState(true)
  const [busyId, setBusyId] = useState('')
  const [creating, setCreating] = useState(false)
  const [removing, setRemoving] = useState<RuntimeInstance | null>(null)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)

  const load = useCallback(async () => {
    try {
      const d = await call<{ instances: RuntimeInstance[] }>('runtime.list', {}, 15)
      setInsts(d.instances || [])
      setAt(Date.now())
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '读取实例失败' })
    }
  }, [call])

  usePoll(load, POLL_MS, auto)

  // 运行时探测：版本不会秒变，进页面取一次就够
  usePoll(async () => {
    try {
      const r = await call<any>('runtime.detect', {}, 30)
      setProbes(r?.runtimes || [])
    } catch { /* 探测失败不影响列表 */ }
  }, 0, true)

  /** 筛选 + 搜索（纯前端过滤：实例量级到不了服务端分页） */
  const shown = useMemo(() => {
    const kw = keyword.trim().toLowerCase()
    return insts.filter((it) => {
      if (filter === 'running' && it.status !== 'running') return false
      if (filter === 'stopped' && it.status === 'running') return false
      if (kw && !(it.name.toLowerCase().includes(kw) || (it.tags || []).join(',').toLowerCase().includes(kw))) return false
      return true
    })
  }, [insts, filter, keyword])

  const nRunning = insts.filter((i) => i.status === 'running').length

  async function act(id: string, op: 'start' | 'stop' | 'restart') {
    setBusyId(id + op)
    try {
      await call(`runtime.${op}`, { id }, 40)
      await load()
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || `${op} 失败` })
    } finally { setBusyId('') }
  }

  async function doRemove() {
    if (!removing) return
    setBusyId(removing.id + 'del')
    try {
      await call('runtime.remove', { id: removing.id }, 40)
      setMsg({ kind: 'ok', text: `已删除 ${removing.name}` })
      setRemoving(null)
      load()
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '删除失败' })
    } finally { setBusyId('') }
  }

  return (
    <>
      <PageHeader
        title="运行时"
        sub={<>
          节点 <span className="mono" style={{ color: 'var(--accent)' }}>{node}</span> 上的实例 ·
          {' '}{nRunning} 运行 / {insts.length} 总数
          {probes.length > 0 && (
            <span style={{ marginLeft: 8 }}>
              {probes.filter((p) => p.available).map((p) => (
                <span key={p.kind} className="tag tag-mono" style={{ marginRight: 5, fontSize: 10.5 }}
                      title={p.version}>
                  {p.label}
                </span>
              ))}
            </span>
          )}
        </>}
        actions={<>
          <LiveTag at={at} enabled={auto} intervalMs={POLL_MS} onToggle={() => setAuto((v) => !v)} />
          <button className="btn" onClick={load}>刷新</button>
          <button className="btn btn-primary" onClick={() => setCreating(true)}>新建实例</button>
        </>}
      />

      {msg && (
        <div style={{
          marginBottom: 12, padding: '8px 12px', fontSize: 12.5,
          borderRadius: 'var(--radius-sm)',
          color: msg.kind === 'ok' ? 'var(--ok)' : 'var(--danger)',
          background: msg.kind === 'ok' ? 'var(--ok-soft)' : 'var(--danger-soft)',
        }}>
          {msg.text}
        </div>
      )}

      <Toolbar left={
        <>
          <div className="seg">
            {([['all', '全部', insts.length],
               ['running', '运行中', nRunning],
               ['stopped', '已停止', insts.length - nRunning]] as const).map(([k, label, n]) => (
              <button key={k} className={filter === k ? 'on' : ''} onClick={() => setFilter(k)}>
                {label}<span className="seg-count">{n}</span>
              </button>
            ))}
          </div>
          <input className="input" style={{ width: 200 }} placeholder="按名称 / 标签搜索…"
                 value={keyword} onChange={(e) => setKeyword(e.target.value)} />
        </>
      } />

      <div className="card" style={{ padding: '6px 16px 10px' }}>
        {shown.length === 0 ? (
          <EmptyState
            title={insts.length === 0 ? '还没有实例' : '没有匹配的实例'}
            hint={insts.length === 0
              ? '实例是一个被托管的常驻进程：Node 应用、Java jar、PHP 服务都可以。'
              : '换个关键词，或切回「全部」。'}
            action={insts.length === 0
              ? <button className="btn btn-primary" onClick={() => setCreating(true)}>新建第一个实例</button>
              : <button className="btn" onClick={() => { setKeyword(''); setFilter('all') }}>清除筛选</button>}
          />
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>实例</th>
                <th style={{ width: 96 }}>状态</th>
                <th style={{ width: 76 }}>端口</th>
                <th style={{ width: 84 }}>PID</th>
                <th style={{ width: 90 }}>运行时长</th>
                <th style={{ width: 170, textAlign: 'right' }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((it) => {
                const b = statusBadge(it.status)
                const busy = busyId.startsWith(it.id)
                return (
                  <tr key={it.id} className="row-link" onClick={() => nav(`/instances/${it.id}`)}>
                    <td>
                      <div className="cell-main mono">{it.name}</div>
                      <div className="cell-sub">
                        {KIND_LABEL[it.kind] || it.kind}
                        {it.auto_restart && ' · 崩溃自愈'}
                        {it.tags?.length ? ` · ${it.tags.join(', ')}` : ''}
                      </div>
                    </td>
                    <td><Badge tone={b.tone}>{b.text}</Badge></td>
                    <td className="mono num" style={{ color: 'var(--text-dim)' }}>{it.port || '—'}</td>
                    <td className="mono num" style={{ color: 'var(--text-dim)' }}>{it.pid || '—'}</td>
                    <td className="mono num" style={{ color: 'var(--text-dim)' }}>{fmtDur(it.uptime_seconds)}</td>
                    <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}
                        onClick={(e) => e.stopPropagation()}>
                      {it.status === 'running' ? (
                        <>
                          <button className="btn-text dim" disabled={busy}
                                  onClick={() => act(it.id, 'restart')}>重启</button>
                          <button className="btn-text dim" disabled={busyId === it.id + 'stop'}
                                  onClick={() => act(it.id, 'stop')}>停止</button>
                        </>
                      ) : (
                        <button className="btn-text" disabled={busy || b.tone === 'warn'}
                                onClick={() => act(it.id, 'start')}>启动</button>
                      )}
                      <button className="btn-text danger" disabled={busy}
                              onClick={() => setRemoving(it)}>删除</button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      {creating && (
        <CreateModal node={node} onClose={() => setCreating(false)}
                     onDone={(t) => { setCreating(false); setMsg({ kind: 'ok', text: t }); load() }} />
      )}

      {removing && (
        <ConfirmModal
          title="删除实例"
          text={<>确定删除 <b className="mono">{removing.name}</b>？<br />
            运行中的进程会先被停止，配置与输出记录会一并清除，<b>不可恢复</b>。</>}
          busy={busyId === removing.id + 'del'}
          onConfirm={doRemove}
          onClose={() => setRemoving(null)} />
      )}
    </>
  )
}

/* ── 新建实例（模态对话框）───────────────────────────────── */
function CreateModal({ node, onClose, onDone }: {
  node: string; onClose: () => void; onDone: (t: string) => void
}) {
  const { call } = useNode()
  const [kind, setKind] = useState('node')
  const [name, setName] = useState('')
  const [cwd, setCwd] = useState('')
  const [entry, setEntry] = useState('')
  const [cmd, setCmd] = useState('')
  const [port, setPort] = useState('')
  const [autoRestart, setAutoRestart] = useState(true)
  const [autoStart, setAutoStart] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  async function submit() {
    if (!name.trim()) { setErr('实例名不能为空'); return }
    if (!cwd.trim()) { setErr('工作目录不能为空'); return }
    setBusy(true); setErr('')
    try {
      const spec: Record<string, any> = {
        name: name.trim(), kind, cwd: cwd.trim(), entry: entry.trim(),
        args: cmd.trim(), port: Number(port) || 0,
        auto_restart: autoRestart, auto_start: autoStart,
      }
      if (cmd.trim() && kind === 'generic') spec.start_command = cmd.trim()
      await call('runtime.create', spec, 40)
      onDone(`已创建 ${name.trim()}（节点 ${node}）`)
    } catch (e: any) {
      setErr(e?.message || '创建失败')
    } finally { setBusy(false) }
  }

  return (
    <Modal title="新建实例" onClose={onClose} width={620}
           footer={
             <>
               <button className="btn" onClick={onClose}>取消</button>
               <button className="btn btn-primary" disabled={busy} onClick={submit}>
                 {busy ? '创建中…' : '创建'}
               </button>
             </>
           }>
      {err && (
        <div style={{
          marginBottom: 12, padding: '7px 11px', fontSize: 12.5,
          borderRadius: 'var(--radius-sm)', color: 'var(--danger)',
          background: 'var(--danger-soft)',
        }}>{err}</div>
      )}
      <div className="form-grid">
        <Field label="类型">
          <select className="select" value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="node">Node.js</option>
            <option value="java">Java</option>
            <option value="php">PHP</option>
            <option value="python">Python</option>
            <option value="generic">通用命令</option>
          </select>
        </Field>
        <Field label="实例名" hint="唯一标识">
          <input className="input" value={name} placeholder="web-api"
                 onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="工作目录" hint="绝对路径">
          <input className="input" value={cwd} placeholder="E:\apps\myapp"
                 onChange={(e) => setCwd(e.target.value)} />
        </Field>
        <Field label="端口" hint="可选，只用于展示">
          <input className="input" value={port} placeholder="8080"
                 onChange={(e) => setPort(e.target.value)} />
        </Field>
        <Field label="入口文件" hint="jar / 脚本，可选">
          <input className="input" value={entry} placeholder="app.jar 或 index.js"
                 onChange={(e) => setEntry(e.target.value)} />
        </Field>
        <Field label="附加参数">
          <input className="input" value={cmd} placeholder="--verbose"
                 onChange={(e) => setCmd(e.target.value)} />
        </Field>
        <div className="span-2" style={{ display: 'flex', gap: 18 }}>
          <label className="check-line">
            <input type="checkbox" checked={autoRestart}
                   onChange={(e) => setAutoRestart(e.target.checked)} />
            崩溃自动重启（自愈）
          </label>
          <label className="check-line">
            <input type="checkbox" checked={autoStart}
                   onChange={(e) => setAutoStart(e.target.checked)} />
            面板启动时自动拉起
          </label>
        </div>
      </div>
    </Modal>
  )
}

function fmtDur(sec?: number): string {
  const s = Math.max(0, Math.floor(sec || 0))
  if (!s) return '—'
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m${s % 60}s`
  if (s < 86400) return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`
  return `${Math.floor(s / 86400)}d${Math.floor((s % 86400) / 3600)}h`
}
