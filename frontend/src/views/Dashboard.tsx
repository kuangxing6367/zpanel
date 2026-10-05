import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, fmtUptime, type ManagedNode } from '../api'
import { LOCAL_NODE } from '../node'
import { LiveTag, usePoll } from '../components/Live'
import { Badge, PageHeader, statusBadge } from '../components/ui'
import { useCountUp } from '../hooks/useCountUp'

const POLL_MS = 8000        // 节点状态轮询
const ENRICH_MS = 45000     // 站点/实例/资源水位轮询（比节点状态慢，避免打爆通道）

interface ResWater {
  cpu: number | null
  mem: number | null
  disk: number | null
}
interface NodeExtra {
  sites: number | null
  instances: number | null
  running: number | null
  res: ResWater | null
}
/** alerts.events 返回的事件行（扩展侧字段，宽松可选） */
interface AlertEvent {
  id?: string
  rule_name?: string
  metric?: string
  message?: string
  level?: string
  created_at?: string | number
}

/**
 * 总览（首页）—— 运维面板的第一认知：
 * 进门先看到「我管的东西」：站点、运行实例、资源水位、告警。
 * 节点是这些事的载体，卡片上直接给出水位与业务量，点击进详情做事。
 */
export default function Dashboard() {
  const nav = useNavigate()
  const [nodes, setNodes] = useState<ManagedNode[] | null>(null)
  const [extra, setExtra] = useState<Record<string, NodeExtra>>({})
  const [alerts, setAlerts] = useState<AlertEvent[] | null>(null)
  const [err, setErr] = useState('')
  const [at, setAt] = useState(0)
  const [auto, setAuto] = useState(true)
  const enrichTimer = useRef<number | null>(null)

  const load = useCallback(async () => {
    try {
      const r = await api.nodes()
      setNodes(r.nodes || [])
      setErr('')
      setAt(Date.now())
    } catch (e: any) {
      setErr(e?.message || '加载失败')
    }
  }, [])

  usePoll(load, POLL_MS, auto)

  /** 拉取每个在线节点的运维数据（站点数 / 实例数 / 资源水位）。
   *  每台节点独立解析、独立上屏 —— 一台慢不拖累其它台。 */
  const enrich = useCallback(async (list: ManagedNode[]) => {
    const online = list.filter((n) => n.status === 'online')
    if (!online.length) return
    await Promise.all(
      online.map(async (n) => {
        const [sys, sites, inst] = await Promise.all([
          api.sendCmd(n.name, 'sysres.snapshot', {}, 6).catch(() => null),
          api.sendCmd(n.name, 'sites.list', {}, 6).catch(() => null),
          api.sendCmd(n.name, 'runtime.list', {}, 6).catch(() => null),
        ])
        let res: ResWater | null = null
        const d = sys?.data
        if (d) {
          const disks = Array.isArray(d.disks) ? d.disks : []
          const disk = disks.length ? Math.max(...disks.map((x: any) => Number(x.percent) || 0)) : null
          res = {
            cpu: num(d.cpu?.percent),
            mem: num(d.memory?.percent),
            disk,
          }
        }
        // sites.list 的 count 是 {total, php, proxy, ...} 分类对象
        const rawCount = sites?.data?.count
        const sc = typeof rawCount === 'object' && rawCount != null
          ? num(rawCount.total)
          : num(rawCount ?? (Array.isArray(sites?.data?.sites) ? sites.data.sites.length : null))
        const il = Array.isArray(inst?.data?.instances) ? inst.data.instances : []
        const x: NodeExtra = {
          sites: sc,
          instances: il.length || null,
          running: il.length ? il.filter((x: any) => x.status === 'running').length : null,
          res,
        }
        setExtra((prev) => ({ ...prev, [n.name]: x }))
      }),
    )
  }, [])

  // 节点清单变化（名字/状态）→ 立即补一次运维数据；之后慢速定时刷新
  const nodesRef = useRef<ManagedNode[]>([])
  useEffect(() => { nodesRef.current = nodes || [] }, [nodes])

  const sig = (nodes || []).map((n) => `${n.name}:${n.status}`).join('|')
  useEffect(() => {
    if (!nodes) return
    enrich(nodes)
    if (enrichTimer.current) window.clearInterval(enrichTimer.current)
    enrichTimer.current = window.setInterval(() => enrich(nodesRef.current), ENRICH_MS)
    return () => { if (enrichTimer.current) window.clearInterval(enrichTimer.current) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig])

  // 告警事件（hub 侧数据，localhost 通道取一次即可）
  const loadAlerts = useCallback(async () => {
    try {
      const r = await api.sendCmd('localhost', 'alerts.events', { limit: 6 }, 8)
      // sendCmd 返回 { ok, data } 信封；事件行在 data.events
      const ev = (r as any)?.data?.events
      setAlerts(Array.isArray(ev) ? ev : [])
    } catch {
      setAlerts(null) // 扩展未启用则不显示
    }
  }, [])
  useEffect(() => {
    loadAlerts()
    const t = window.setInterval(loadAlerts, ENRICH_MS)
    return () => window.clearInterval(t)
  }, [loadAlerts])

  const list = nodes || []
  const online = list.filter((n) => n.status === 'online')
  const offline = list.length - online.length
  const totalSites = sum(extra, 'sites')
  const totalInst = sum(extra, 'instances')
  const totalRunning = sum(extra, 'running')

  return (
    <>
      <PageHeader
        title="总览"
        sub="站点 · 运行实例 · 资源水位 —— 点击节点卡片进入详情管理"
        actions={
          <>
            <LiveTag at={at} enabled={auto} intervalMs={POLL_MS}
                     onToggle={() => setAuto((v) => !v)} />
            <button className="btn" onClick={load}>刷新</button>
            <button className="btn btn-primary" onClick={() => nav('/nodes')}>管理节点</button>
          </>
        }
      />

      {err && <div className="empty">{err}</div>}

      {/* 统计卡：数字滚动 + 级联入场（zcbot 卡片化 + 1Panel 手感） */}
      <div className="metric-grid">
        <Metric label="站点" num={totalSites}
                hint={`${list.length} 台节点纳管`} onClick={() => nav(`/nodes/${LOCAL_NODE}/sites`)} delay={0} />
        <Metric label="运行实例" num={totalInst}
                hint={totalRunning == null ? '' : `其中运行中 ${totalRunning}`}
                onClick={() => nav(`/nodes/${LOCAL_NODE}/instances`)} delay={50} />
        <Metric label="在线节点" num={online.length} tone="ok" hint="探活正常" delay={100} />
        <Metric label="离线节点" num={offline}
                tone={offline > 0 ? 'warn' : undefined} hint="不可达 / 未连接" delay={150} />
      </div>

      {list.length > 0 ? (
        <div className="grid node-grid">
          {list.map((n, i) => (
            <NodeCard key={n.name} node={n} x={extra[n.name]} delay={150 + i * 60}
                      onOpen={() => nav(`/nodes/${encodeURIComponent(n.name)}`)} />
          ))}
        </div>
      ) : !err ? (
        <div className="empty-state">
          <div className="empty-title">还没有纳管任何节点</div>
          <div className="empty-hint">到「节点」页添加远端机器，或直接使用本机</div>
          <div className="empty-action">
            <button className="btn btn-primary" onClick={() => nav('/nodes')}>去添加节点</button>
          </div>
        </div>
      ) : null}

      {alerts && alerts.length > 0 && (
        <div className="section">
          <div className="section-head">
            <span className="section-title">最近告警</span>
            <button className="btn btn-sm" onClick={() => nav('/alerts')}>全部告警</button>
          </div>
          <div className="alert-list">
            {alerts.map((a, i) => (
              <div key={a.id ?? i} className="alert-row">
                <Badge tone={a.level === 'bad' || a.level === 'critical' ? 'bad'
                            : a.level === 'warn' ? 'warn' : 'info'}>{a.level || 'warn'}</Badge>
                <span className="alert-name">{a.rule_name || a.metric || '—'}</span>
                <span className="alert-msg">{a.message || ''}</span>
                <span className="alert-time">{fmtTime(a.created_at)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </>
  )
}

function num(v: any): number | null {
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}
function sum(extra: Record<string, NodeExtra>, key: 'sites' | 'instances' | 'running'): number | null {
  const vals = Object.values(extra).map((x) => x[key]).filter((v): v is number => v != null)
  if (!vals.length) return null
  return vals.reduce((a, b) => a + b, 0)
}
function fmtTime(ts: any): string {
  if (ts == null) return ''
  const d = typeof ts === 'number' ? new Date(ts * (ts < 1e12 ? 1000 : 1)) : new Date(ts)
  if (isNaN(d.getTime())) return ''
  return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function Metric({ label, num, value, hint, tone, onClick, delay = 0 }: {
  label: string; num?: number | null; value?: string; hint?: string
  tone?: 'ok' | 'warn' | 'bad'; onClick?: () => void; delay?: number
}) {
  const rolled = useCountUp(num ?? null)
  const shown = num != null ? rolled : (value ?? '—')
  return (
    <div className={'metric rise' + (tone ? ` ${tone}` : '') + (onClick ? ' clickable' : '')}
         style={{ animationDelay: `${delay}ms` }}
         role={onClick ? 'button' : undefined} tabIndex={onClick ? 0 : undefined}
         onClick={onClick} onKeyDown={(e) => { if (onClick && e.key === 'Enter') onClick() }}>
      <div className="metric-label">{label}</div>
      <div className="metric-value num">{shown}</div>
      {hint && <div className="metric-hint">{hint}</div>}
    </div>
  )
}

/** 资源水位条：数值缺失显示 —，超阈值变色 */
function ResBar({ label, pct }: { label: string; pct: number | null }) {
  const tone = pct == null ? '' : pct >= 90 ? ' bad' : pct >= 75 ? ' warn' : ''
  return (
    <div className="res-row">
      <span className="res-label">{label}</span>
      <span className="res-track">
        <span className={'res-fill' + tone} style={{ width: pct == null ? '0%' : `${Math.min(100, pct)}%` }} />
      </span>
      <span className={'res-val num' + tone}>{pct == null ? '—' : `${Math.round(pct)}%`}</span>
    </div>
  )
}

function NodeCard({ node, x, delay = 0, onOpen }: {
  node: ManagedNode; x?: NodeExtra; delay?: number; onOpen: () => void
}) {
  const b = statusBadge(node.status === 'online' ? 'online' : 'offline')
  const dead = node.status !== 'online'
  return (
    <div className={'node-card rise' + (dead ? ' offline' : '')}
         style={{ animationDelay: `${delay}ms` }}
         role="button" tabIndex={0} onClick={onOpen}
         onKeyDown={(e) => { if (e.key === 'Enter') onOpen() }}>
      <div className="node-card-top">
        <span className="node-card-name">{node.name}</span>
        {node.is_local && <span className="tag">本机</span>}
        <Badge tone={b.tone}>{b.text}</Badge>
      </div>

      <div className="node-card-meta">
        {node.platform || '—'}{node.arch ? ` · ${node.arch}` : ''}
        {node.version ? ` · v${node.version}` : ''}
      </div>

      {dead ? (
        <div className="node-card-meta" style={{ marginTop: 10 }}>
          {node.last_ok_at ? `最近在线 ${node.last_ok_at.slice(5, 16).replace('T', ' ')}` : '尚无探活记录'}
        </div>
      ) : (
        <div className="node-card-res">
          <ResBar label="CPU" pct={x?.res ? x.res.cpu : null} />
          <ResBar label="内存" pct={x?.res ? x.res.mem : null} />
          <ResBar label="磁盘" pct={x?.res ? x.res.disk : null} />
        </div>
      )}

      <div className="node-card-stats">
        <div>
          <div className="node-stat-k">站点</div>
          <div className="node-stat-v">{x?.sites ?? '—'}</div>
        </div>
        <div>
          <div className="node-stat-k">实例</div>
          <div className="node-stat-v">
            {x?.running != null && x?.instances != null ? `${x.running}/${x.instances}` : (x?.instances ?? '—')}
          </div>
        </div>
        <div>
          <div className="node-stat-k">运行时长</div>
          <div className="node-stat-v">{fmtUptime(node.uptime_seconds)}</div>
        </div>
      </div>

      <div className="node-card-go">进入详情 →</div>
    </div>
  )
}
