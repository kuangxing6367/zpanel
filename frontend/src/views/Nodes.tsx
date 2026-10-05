import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, ApiError, fmtUptime, type ManagedNode } from '../api'
import { LiveTag, usePoll } from '../components/Live'
import { useNode } from '../node'

export default function Nodes() {
  const [nodes, setNodes] = useState<ManagedNode[]>([])
  const [mode, setMode] = useState('')
  const [loading, setLoading] = useState(true)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [adding, setAdding] = useState(false)
  const [newSecret, setNewSecret] = useState<{ name: string; secret: string } | null>(null)
  const [cmdOut, setCmdOut] = useState<{ name: string; text: string } | null>(null)
  const [auto, setAuto] = useState(true)
  const nav = useNavigate()
  const { setCurrent } = useNode()
  const [at, setAt] = useState(0)

  async function load() {
    try {
      const r = await api.nodes()
      setNodes(r.nodes || [])
      setMode(r.mode || '')
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '加载失败' })
    } finally {
      setLoading(false)
      setAt(Date.now())
    }
  }

  // 节点在线状态由心跳驱动，轮询 5s 即可（服务端另有实时连接表）
  usePoll(load, 5000, auto)

  async function runCmd(name: string, cmd: string) {
    try {
      const r = await api.sendCmd(name, cmd)
      setCmdOut({ name, text: JSON.stringify(r, null, 2) })
    } catch (e: any) {
      setCmdOut({ name, text: `失败：${e?.message || e}` })
    }
  }

  async function rotate(name: string) {
    if (!confirm(`轮换「${name}」的密钥？旧密钥立即失效，该节点需同步更新配置。`)) return
    try {
      const r = await api.rotateSecret(name)
      setNewSecret({ name: r.name, secret: r.secret })
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '轮换失败' })
    }
  }

  async function remove(name: string) {
    if (!confirm(`移除纳管「${name}」？该节点将无法再接入，需重新纳管。`)) return
    try {
      await api.removeNode(name)
      setMsg({ kind: 'ok', text: `已移除 ${name}` })
      load()
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '移除失败' })
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">节点</div>
          <div className="page-sub">
            中心机模式 <span className="mono">{mode}</span> · 节点主动外连，可在 NAT / 防火墙后 ·
            点击任意一行进入该节点的实例 / 站点 / 文件 / 终端
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <LiveTag at={at} enabled={auto} intervalMs={5000} onToggle={() => setAuto((v) => !v)} />
          <button className="btn" onClick={load}>刷新</button>
          <button className="btn btn-primary" onClick={() => setAdding(true)}>纳管节点</button>
        </div>
      </div>

      {msg && (
        <div style={{
          marginBottom: 12, padding: '8px 12px', fontSize: 12.5,
          borderRadius: 'var(--radius-sm)',
          color: msg.kind === 'ok' ? 'var(--ok)' : 'var(--danger)',
          background: msg.kind === 'ok' ? 'var(--ok-soft)' : 'var(--danger-soft)',
          border: `1px solid ${msg.kind === 'ok' ? 'rgba(74,222,128,.25)' : 'rgba(248,113,113,.25)'}`,
        }}>
          {msg.text}
        </div>
      )}

      <div className="card" style={{ padding: '4px 6px 6px' }}>
        {loading ? (
          <div className="empty"><span className="spinner" /> 加载中…</div>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: 44 }} />
                <th>节点</th>
                <th>平台</th>
                <th>版本</th>
                <th>运行时长</th>
                <th>内存</th>
                <th>最后在线</th>
                <th style={{ textAlign: 'right' }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {nodes.map((n) => (
                <tr key={n.name} onClick={() => { setCurrent(n.name); nav(`/nodes/${n.name}`) }}
                    style={{ cursor: 'pointer' }}>
                  <td>
                    <span className={
                      'dot ' + (n.status === 'online' ? 'dot-ok'
                        : n.status === 'offline' ? 'dot-bad' : '')
                    } />
                  </td>
                  <td>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
                      <span className="mono">{n.name}</span>
                      {n.is_local && <span className="tag tag-accent">本机</span>}
                    </div>
                  </td>
                  <td style={{ color: 'var(--text-dim)' }}>{n.platform || '—'}</td>
                  <td className="mono" style={{ color: 'var(--text-dim)' }}>{n.version || '—'}</td>
                  <td className="mono" style={{ color: 'var(--text-dim)' }}>
                    {fmtUptime(n.uptime_seconds)}
                  </td>
                  <td className="mono" style={{ color: 'var(--text-dim)' }}>
                    {n.memory_mb != null ? `${n.memory_mb} MB` : '—'}
                  </td>
                  <td className="mono" style={{ color: 'var(--text-mute)', fontSize: 12 }}>
                    {n.last_ok_at || '—'}
                  </td>
                  <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}
                      onClick={(e) => e.stopPropagation()}>
                    <button className="btn btn-sm btn-ghost" onClick={() => nav(`/nodes/${n.name}`)}>
                      详情
                    </button>
                    <button className="btn btn-sm btn-ghost" onClick={() => runCmd(n.name, 'ping')}>
                      探测
                    </button>
                    <button className="btn btn-sm btn-ghost" onClick={() => runCmd(n.name, 'node.info')}>
                      信息
                    </button>
                    {!n.is_local && (
                      <>
                        <button className="btn btn-sm btn-ghost" onClick={() => rotate(n.name)}>
                          换钥
                        </button>
                        <button className="btn-text danger" onClick={() => remove(n.name)}>
                          移除
                        </button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
              {nodes.length === 0 && (
                <tr><td colSpan={8}><div className="empty">暂无纳管节点</div></td></tr>
              )}
            </tbody>
          </table>
        )}
      </div>

      {adding && (
        <AddDialog
          onClose={() => setAdding(false)}
          onDone={(r) => { setAdding(false); setNewSecret(r); load() }}
        />
      )}

      {newSecret && <SecretDialog data={newSecret} onClose={() => setNewSecret(null)} />}
      {cmdOut && <OutputDialog data={cmdOut} onClose={() => setCmdOut(null)} />}
    </>
  )
}

/* ── 纳管 ─────────────────────────────────────────────── */
function AddDialog({
  onClose, onDone,
}: {
  onClose: () => void
  onDone: (r: { name: string; secret: string }) => void
}) {
  const [name, setName] = useState('')
  const [host, setHost] = useState('')
  const [tags, setTags] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit() {
    if (!name.trim()) { setErr('请填写节点名'); return }
    setBusy(true); setErr('')
    try {
      const r = await api.addNode({ name: name.trim(), host: host.trim(), tags: tags.trim() })
      onDone({ name: r.name, secret: r.secret })
    } catch (e: any) {
      setErr(e?.message || '纳管失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal title="纳管节点" onClose={onClose}>
      <Field label="节点名">
        <input className="input" autoFocus value={name} placeholder="node-1"
          onChange={(e) => setName(e.target.value)} />
      </Field>
      <Field label="地址（可选，仅备注）">
        <input className="input" value={host} placeholder="192.168.1.10"
          onChange={(e) => setHost(e.target.value)} />
      </Field>
      <Field label="标签（可选，逗号分隔）">
        <input className="input" value={tags} placeholder="web,prod"
          onChange={(e) => setTags(e.target.value)} />
      </Field>
      {err && <div style={{ color: 'var(--danger)', fontSize: 12.5, marginBottom: 10 }}>{err}</div>}
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 4 }}>
        <button className="btn" onClick={onClose}>取消</button>
        <button className="btn btn-primary" onClick={submit} disabled={busy}>
          {busy ? '纳管中…' : '生成密钥并纳管'}
        </button>
      </div>
    </Modal>
  )
}

/* ── 一次性密钥 ───────────────────────────────────────── */
function SecretDialog({
  data, onClose,
}: {
  data: { name: string; secret: string }
  onClose: () => void
}) {
  const cfg = `# 在被管机器上配置（config.yaml）
nodes:
  mode: agent
  agent:
    hub_host: <中心机地址>
    hub_port: 37010
    name: ${data.name}
    secret: ${data.secret}`
  return (
    <Modal title={`节点 ${data.name} 已纳管`} onClose={onClose} width={520}>
      <div style={{ fontSize: 12.5, color: 'var(--warn)', marginBottom: 10 }}>
        密钥仅此一次显示，请立即保存到被管机器。
      </div>
      <pre style={pre}>{cfg}</pre>
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 12 }}>
        <button className="btn" onClick={() => navigator.clipboard?.writeText(cfg)}>复制配置</button>
        <button className="btn btn-primary" onClick={onClose}>我已保存</button>
      </div>
    </Modal>
  )
}

/* ── 输出 ─────────────────────────────────────────────── */
function OutputDialog({ data, onClose }: { data: { name: string; text: string }; onClose: () => void }) {
  return (
    <Modal title={`${data.name} · 执行结果`} onClose={onClose} width={560}>
      <pre style={pre}>{data.text}</pre>
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 12 }}>
        <button className="btn" onClick={onClose}>关闭</button>
      </div>
    </Modal>
  )
}

/* ── 基础件 ───────────────────────────────────────────── */
function Modal({
  title, children, onClose, width = 420,
}: {
  title: string
  children: React.ReactNode
  onClose: () => void
  width?: number
}) {
  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, background: 'rgba(6,8,11,.62)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        zIndex: 50, padding: 20, backdropFilter: 'blur(2px)',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width, maxWidth: '100%', maxHeight: '85vh', overflow: 'auto',
          background: 'var(--panel)', border: '1px solid var(--border-strong)',
          borderRadius: 'var(--radius-lg)', padding: '18px 20px 16px',
          boxShadow: 'var(--shadow-pop)',
        }}
      >
        <div style={{
          fontSize: 14, fontWeight: 600, marginBottom: 16,
          letterSpacing: 'var(--track-normal)',
        }}>
          {title}
        </div>
        {children}
      </div>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{
        fontSize: 11, color: 'var(--text-mute)',
        letterSpacing: 'var(--track-wide)', textTransform: 'uppercase', marginBottom: 5,
      }}>
        {label}
      </div>
      {children}
    </div>
  )
}

const pre: React.CSSProperties = {
  background: 'var(--bg)',
  border: '1px solid var(--border)',
  borderRadius: 'var(--radius-sm)',
  padding: '12px 14px',
  fontFamily: 'var(--font-mono)',
  fontSize: 12,
  lineHeight: 1.65,
  color: 'var(--text-dim)',
  overflow: 'auto',
  maxHeight: 380,
  margin: 0,
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-all',
}
