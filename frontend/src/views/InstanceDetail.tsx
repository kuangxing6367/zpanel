import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import type { LogLine, RuntimeInstance } from '../api'
import { useNode } from '../node'
import Collapse, { LiveTag, usePoll } from '../components/Live'
import { useEventStream } from '../hooks/useEventStream'

const MS = 2000
const MAX_LOGS = 600

const KIND_LABEL: Record<string, string> = {
  node: 'Node.js', java: 'Java', php: 'PHP', python: 'Python', generic: '通用',
}

/** 实例详情：一个被托管的服务进程**自身**的状态、配置、输出与操作。
 *  注意这里是「实例」视角，不是「本机」视角 —— 它可能跑在任意节点上。 */
export default function InstanceDetail() {
  const { id = '' } = useParams()
  const nav = useNavigate()
  const { current, call, nodes, setCurrent, node } = useNode()

  const [it, setIt] = useState<RuntimeInstance | null>(null)
  const [logs, setLogs] = useState<LogLine[]>([])
  const [at, setAt] = useState(0)
  const [auto, setAuto] = useState(true)
  const [autoLog, setAutoLog] = useState(true)
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [notHere, setNotHere] = useState(false)

  const load = useCallback(async () => {
    try {
      const d = await call<{ instances: RuntimeInstance[] }>('runtime.list', {}, 15)
      const found = (d.instances || []).find((x) => x.id === id) || null
      setIt(found)
      setNotHere(!found)
      setAt(Date.now())
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '读取实例失败' })
    }
  }, [call, id])

  const loadLogs = useCallback(async () => {
    if (!it) return
    try {
      const lines = await call<LogLine[]>('runtime.logs', { id, limit: 300 }, 20)
      setLogs((lines || []).slice(-MAX_LOGS))
    } catch { /* 读取输出失败不打断页面 */ }
  }, [call, id, it])

  usePoll(load, MS, auto)
  // 历史输出进页面时拉一次；之后的增量走实时推送（不再轮询）
  useEffect(() => { loadLogs() }, [loadLogs])

  // ── 实时输出（内核 WebSocket；票据一次性，断线自动重连重取票）──
  const [wsState, setWsState] = useState<'idle' | 'connecting' | 'open' | 'closed'>('idle')
  useEventStream(id ? `instance.${id}` : null, auto && autoLog && !!it, (ev) => {
    if (ev.event !== 'output') return
    const d = ev.data || {}
    setLogs((prev) => {
      const next = [...prev, { ts: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
                               stream: d.stream || 'stdout', line: String(d.line ?? '') }]
      return next.length > MAX_LOGS ? next.slice(-MAX_LOGS) : next
    })
    setAt(Date.now())
  }, setWsState)

  async function act(op: 'start' | 'stop' | 'restart') {
    setBusy(op)
    try {
      await call(`runtime.${op}`, { id }, 45)
      await load()
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || `${op} 失败` }) }
    finally { setBusy('') }
  }

  async function remove() {
    if (!it) return
    if (!confirm(`删除实例「${it.name}」？运行中的进程会先被停止。`)) return
    try {
      await call('runtime.remove', { id }, 45)
      nav(`/nodes/${current}/instances`)
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '删除失败' }) }
  }

  if (notHere && !msg) {
    return (
      <>
        <div className="page-head">
          <div>
            <div className="page-title">实例详情</div>
            <div className="page-sub">实例 {id}</div>
          </div>
        </div>
        <div className="card">
          <div className="empty" style={{ padding: '26px 0' }}>
            当前节点（<span className="mono">{current}</span>）上没有这个实例。<br />
            <span style={{ fontSize: 12, color: 'var(--text-mute)' }}>
              实例是**节点上的**实体，切到它所在的节点即可查看。
            </span>
            <div style={{ marginTop: 14, display: 'flex', gap: 8, justifyContent: 'center',
                          flexWrap: 'wrap' }}>
              {nodes.map((n) => (
                <button key={n.name} className="btn btn-sm"
                        onClick={() => setCurrent(n.name)}>
                  {n.name}{n.is_local ? '（本机）' : ''}
                </button>
              ))}
            </div>
          </div>
        </div>
      </>
    )
  }

  const st = it?.status || 'stopped'
  const trans = st === 'starting' || st === 'stopping' || st === 'busy'

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">
            {it?.name || '实例详情'}
            <span className="tag" style={{ marginLeft: 10, fontSize: 10.5 }}>
              {KIND_LABEL[it?.kind || ''] || it?.kind || '—'}
            </span>
          </div>
          <div className="page-sub">
            节点 <Link to={`/nodes/${current}`} className="mono"
                      style={{ color: 'var(--accent)' }}>{current}</Link>
            {node?.is_local ? '（本机）' : ''} · 实例 ID <span className="mono">{id.slice(0, 12)}</span>
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <LiveTag at={at} enabled={auto} intervalMs={MS} onToggle={() => setAuto((v) => !v)} />
          <button className="btn btn-sm" onClick={() => nav(`/nodes/${current}/instances`)}>返回列表</button>
        </div>
      </div>

      {msg && (
        <div style={{
          marginBottom: 12, padding: '8px 12px', fontSize: 12.5,
          borderRadius: 'var(--radius-sm)',
          color: msg.kind === 'ok' ? 'var(--ok)' : 'var(--danger)',
          background: msg.kind === 'ok' ? 'var(--ok-soft)' : 'var(--danger-soft)',
        }}>{msg.text}</div>
      )}

      <div className="grid grid-4" style={{ marginBottom: 14 }}>
        <Card label="状态" value={st === 'running' ? '运行中' : st === 'stopped' ? '已停止'
          : st === 'starting' ? '启动中' : st === 'stopping' ? '停止中' : '处理中'}
          hint={`退出码 ${it?.exit_code ?? '—'}`}
          dot={st === 'running' ? 'dot-ok' : st === 'stopped' ? 'dot-bad' : 'dot-warn'} />
        <Card label="PID" value={String(it?.pid ?? '—')} hint={`监听 ${it?.port || '—'}`} />
        <Card label="运行时长" value={fmtDur(it?.uptime_seconds)} hint={`启动 ${it?.start_count || 0} 次`} />
        <Card label="重启" value={String(it?.restart_count || 0)}
              hint={it?.auto_restart ? '已开启自愈' : '未开启自愈'} />
      </div>

      <div style={{ display: 'flex', gap: 8, marginBottom: 14 }}>
        {st === 'running' ? (
          <>
            <button className="btn" disabled={!!busy} onClick={() => act('restart')}>重启</button>
            <button className="btn" disabled={!!busy} onClick={() => act('stop')}>停止</button>
          </>
        ) : (
          <button className="btn btn-primary" disabled={trans || !!busy} onClick={() => act('start')}>
            启动
          </button>
        )}
        <span style={{ flex: 1 }} />
        <button className="btn btn-danger" disabled={!!busy} onClick={remove}>删除实例</button>
      </div>

      <div className="grid grid-2" style={{ marginBottom: 14 }}>
        <Collapse title="配置" defaultOpen storageKey={'inst.cfg.' + id}>
          <div className="kv"><span className="kv-k">启动命令</span>
            <span className="kv-v mono" style={{ fontSize: 11.5, wordBreak: 'break-all' }}>
              {it?.start_command || '—'}</span></div>
          <div className="kv"><span className="kv-k">停止命令</span>
            <span className="kv-v mono" style={{ fontSize: 11.5 }}>{it?.stop_command || '（发送终止信号）'}</span></div>
          <div className="kv"><span className="kv-k">工作目录</span>
            <span className="kv-v mono" style={{ fontSize: 11.5 }}>{it?.cwd || '—'}</span></div>
          <div className="kv"><span className="kv-k">端口</span>
            <span className="kv-v mono">{it?.port || '—'}</span></div>
          <div className="kv"><span className="kv-k">自动重启</span>
            <span className="kv-v">{it?.auto_restart ? '开' : '关'}（上限 {it?.max_restarts ?? '—'}）</span></div>
          <div className="kv"><span className="kv-k">开机自启</span>
            <span className="kv-v">{it?.auto_start ? '开' : '关'}</span></div>
          <div className="kv"><span className="kv-k">标签</span>
            <span className="kv-v">{(it?.tags || []).join(', ') || '—'}</span></div>
          {it?.env && Object.keys(it.env).length > 0 && (
            <div style={{ marginTop: 8, fontSize: 11.5, color: 'var(--text-mute)', lineHeight: 1.7 }}>
              注入的环境变量：{Object.keys(it.env).join(', ')}
            </div>
          )}
          {it?.last_error && (
            <div style={{ marginTop: 8, fontSize: 11.5, color: 'var(--danger)' }}>
              最近错误：{it.last_error}
            </div>
          )}
        </Collapse>

        <Collapse title="进程事实" defaultOpen storageKey={'inst.facts.' + id}>
          <div className="kv"><span className="kv-k">启动时间</span>
            <span className="kv-v mono">{it?.started_at || '—'}</span></div>
          <div className="kv"><span className="kv-k">状态码</span>
            <span className="kv-v mono">{it?.status_code ?? '—'}</span></div>
          <div className="kv"><span className="kv-k">缓冲输出</span>
            <span className="kv-v mono">{it?.output_lines ?? 0} 行</span></div>
          <div style={{ marginTop: 10, fontSize: 11.5, color: 'var(--text-mute)', lineHeight: 1.75 }}>
            实例是「可持久化配置 + 易失运行态」两面：配置落 SQLite，运行态在内存。
            崩溃后按 <span className="mono">auto_restart</span> 策略自愈（带次数上限，避免疯狂重启）。
          </div>
        </Collapse>
      </div>

      <Collapse title="输出" count={`${logs.length} 行`} defaultOpen
                storageKey={'inst.logs.' + id}
                right={
                  <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}
                       onClick={(e) => e.stopPropagation()}>
                    <span style={{
                      fontSize: 11, display: 'inline-flex', alignItems: 'center', gap: 5,
                      color: wsState === 'open' ? 'var(--ok)' : 'var(--text-mute)',
                    }}>
                      <span className={'dot ' +
                        (wsState === 'open' ? 'dot-ok' : wsState === 'connecting' ? 'dot-warn' : 'dot-bad')} />
                      实时{wsState === 'open' ? '已连接' : wsState === 'connecting' ? '连接中'
                        : wsState === 'closed' ? '未连接' : '已停用'}
                    </span>
                    <button className={'btn btn-sm ' + (autoLog ? 'btn-primary' : 'btn-ghost')}
                            onClick={() => setAutoLog((v) => !v)}>
                      {autoLog ? '跟随' : '已暂停'}
                    </button>
                    <button className="btn btn-sm btn-ghost" onClick={loadLogs}>刷新</button>
                  </div>
                }>
        <pre className="mono" style={{
          margin: 0, maxHeight: 380, overflow: 'auto', fontSize: 12, lineHeight: 1.7,
          background: 'var(--bg-elev)', border: '1px solid var(--border)',
          borderRadius: 'var(--radius-sm)', padding: 12, color: 'var(--text-dim)',
          whiteSpace: 'pre-wrap',
        }}>
          {logs.length === 0 ? '(暂无输出)' : logs.map((l, i) => (
            <div key={i}>
              <span style={{ color: 'var(--text-mute)' }}>{l.ts} </span>
              <span style={{ color: l.stream === 'stderr' ? 'var(--danger)' : 'var(--text-dim)' }}>
                {l.line}
              </span>
            </div>
          ))}
        </pre>
      </Collapse>
    </>
  )
}

function Card({ label, value, hint, dot }: {
  label: string; value: string; hint?: string; dot?: string
}) {
  return (
    <div className="card" style={{ padding: '15px 17px' }}>
      <div style={{
        fontSize: 11, color: 'var(--text-mute)',
        letterSpacing: 'var(--track-wider)', textTransform: 'uppercase',
      }}>{label}</div>
      <div className="num" style={{
        fontSize: 21, marginTop: 5, letterSpacing: 'var(--track-tight)',
        display: 'flex', alignItems: 'center', gap: 8,
      }}>
        {dot && <span className={'dot ' + dot} />}
        {value}
      </div>
      {hint && <div style={{ fontSize: 11.5, color: 'var(--text-mute)', marginTop: 3 }}>{hint}</div>}
    </div>
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
