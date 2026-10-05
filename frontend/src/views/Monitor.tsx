import { useCallback, useEffect, useState } from 'react'
import { fmtUptime, type HostSnapshot, type LogLine, type ProcessRow } from '../api'
import Collapse, { LiveTag, usePoll } from '../components/Live'
import TrendChart, { type TrendPoint } from '../components/TrendChart'
import { useNode } from '../node'

const REFRESH_MS = 2000
const HIST_REFRESH_MS = 60000
const RANGES: [number, string][] = [[1, '1小时'], [6, '6小时'], [24, '24小时'], [168, '7天'], [720, '30天']]

export default function Monitor({ nodeName }: { nodeName?: string } = {}) {
  const { current, call } = useNode()
  const node = nodeName || current
  const [host, setHost] = useState<HostSnapshot | null>(null)
  const [procs, setProcs] = useState<ProcessRow[]>([])
  const [sort, setSort] = useState<'cpu' | 'mem'>('cpu')
  const [procsAvailable, setProcsAvailable] = useState(true)
  const [auto, setAuto] = useState(true)
  const [at, setAt] = useState(0)
  const [err, setErr] = useState('')

  // 历史趋势：范围切换立即重拉，之后慢速轮询（与实时 2s 轮询分离）
  const [hist, setHist] = useState<TrendPoint[] | null>(null)
  const [histErr, setHistErr] = useState('')
  const [range, setRange] = useState(24)
  // 数据刚积累时，大范围会被降采样成 1 个点 —— 允许自动降档；用户手动点过范围就不再干预
  const [autoRange, setAutoRange] = useState(true)
  const [rangeRef] = useState({ v: 24, auto: true })
  rangeRef.v = range
  rangeRef.auto = autoRange

  // 用 ref 承载 sort，load 身份保持稳定 —— 定时器不会被反复重建
  const [sortRef] = useState({ v: sort as 'cpu' | 'mem' })
  sortRef.v = sort

  const load = useCallback(async () => {
    try {
      // 跟随选中的节点：本机也好、远端也好，同一条命令通道
      const [h, t] = await Promise.all([
        call<any>('sysres.snapshot', {}, 25),
        call<any>('sysres.top', { n: 10, sort: sortRef.v }, 25),
      ])
      setHost(h as unknown as HostSnapshot)
      setProcs((t as any)?.processes || [])
      setProcsAvailable((t as any)?.available !== false)
      setAt(Date.now())
      setErr('')
    } catch (e: any) {
      setErr(e?.message || '读取失败（该节点的 monitor 扩展不可用？）')
    }
  }, [call, sortRef])

  usePoll(load, REFRESH_MS, auto)

  const loadHist = useCallback(async () => {
    try {
      const fetchPts = async (h: number) => {
        const r = await call<any>('monitor.history', { hours: h }, 30)
        return (r?.points || []) as TrendPoint[]
      }
      let hours = rangeRef.v
      let pts = await fetchPts(hours)
      if (pts.length < 2 && rangeRef.auto) {
        for (const [h] of RANGES) {
          if (h >= hours) continue
          const smaller = await fetchPts(h)
          if (smaller.length >= 2) { hours = h; pts = smaller; break }
        }
        setRange(hours)
      }
      setHist(pts)
      setHistErr(pts.length ? '' : '暂无数据')
    } catch {
      // 节点侧 monitor 扩展还没更新到带 monitor.history 的版本
      setHist(null)
      setHistErr('该节点的 monitor 扩展未提供历史查询')
    }
  }, [call, rangeRef])

  useEffect(() => { loadHist() }, [loadHist, range])
  usePoll(loadHist, HIST_REFRESH_MS, auto && hist !== null)

  if (err) return <div className="empty">{err}</div>
  if (!host) return <div className="empty"><span className="spinner" /> 正在采样…</div>

  const cpu = host.cpu?.percent
  const mem = host.memory
  const swap = host.swap
  const net = host.net
  const disks = host.disks || []

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">监控</div>
          <div className="page-sub">
            {host.info.system} {host.info.release} · {host.info.cpu_count ?? '—'} 核 ·
            采集后端 {host.info.backend}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <LiveTag at={at} enabled={auto} intervalMs={REFRESH_MS}
                   onToggle={() => setAuto((v) => !v)} />
          <button className="btn btn-sm" onClick={() => load()}>刷新</button>
        </div>
      </div>

      <div className="grid grid-4" style={{ marginBottom: 14 }}>
        <Stat label="CPU" value={cpu == null ? '—' : `${cpu}%`}
              hint={`${host.info.cpu_count ?? '—'} 逻辑核`} pct={cpu} />
        <Stat label="内存" value={mem?.percent == null ? '—' : `${mem.percent}%`}
              hint={`${gb(mem?.used)} / ${gb(mem?.total)}`} pct={mem?.percent} />
        <Stat label="交换" value={swap?.percent == null ? '—' : `${swap.percent}%`}
              hint={swap?.total ? `${gb(swap.used)} / ${gb(swap.total)}` : '未启用'}
              pct={swap?.percent} />
        <Stat label="运行时长" value={fmtUptime(host.uptime ?? undefined)}
              hint={host.load ? `负载 ${host.load.map((x) => x.toFixed(2)).join(' / ')}` : '负载 —'} />
      </div>

      {/* 历史趋势：范围切换 + 三条 0-100% 线（数据来自节点本地 SQLite） */}
      <div className="card" style={{ marginBottom: 14 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                      flexWrap: 'wrap', gap: 8, marginBottom: 8 }}>
          <div className="card-title" style={{ marginBottom: 0 }}>历史趋势</div>
          <div style={{ display: 'flex', gap: 6 }}>
            {RANGES.map(([h, label]) => (
              <button key={h}
                      className={'btn btn-sm ' + (range === h ? 'btn-primary' : 'btn-ghost')}
                      onClick={() => { setRange(h); setAutoRange(false) }}>{label}</button>
            ))}
          </div>
        </div>
        {hist && hist.length >= 2 ? (
          <>
            <TrendChart points={hist} spanHours={range} />
            <div style={{ display: 'flex', gap: 14, marginTop: 8, fontSize: 11.5,
                          color: 'var(--text-mute)' }}>
              <LegendDot color="var(--accent)" name={`CPU ${last(hist, 'cpu')}`} />
              <LegendDot color="var(--ok)" name={`内存 ${last(hist, 'mem')}`} />
              <LegendDot color="var(--warn)" name={`磁盘 ${last(hist, 'disk')}`} />
              {last(hist, 'load') != null && (
                <span>负载 {last(hist, 'load')}</span>
              )}
            </div>
          </>
        ) : (
          <div className="empty" style={{ padding: '20px 0' }}>
            {histErr || '暂无历史数据'}{histErr.includes('扩展') ? '' : '（采样积累中）'}
          </div>
        )}
      </div>

      <div className="grid grid-2" style={{ marginBottom: 14 }}>
        <Collapse title="磁盘" count={disks.length ? `${disks.length}` : '—'}
                  defaultOpen storageKey="monitor.disks">
          {disks.length === 0 ? (
            <div className="empty" style={{ padding: '14px 0' }}>
              采集后端拿不到磁盘信息
            </div>
          ) : (
            disks.map((d) => (
              <div key={d.mountpoint} style={{ marginBottom: 10 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12 }}>
                  <span className="mono" style={{ color: 'var(--text-dim)' }}>{d.mountpoint}</span>
                  <span className="mono" style={{ color: 'var(--text-mute)' }}>
                    {gb(d.used)} / {gb(d.total)} · {d.percent}%
                  </span>
                </div>
                <Bar pct={d.percent} />
              </div>
            ))
          )}
        </Collapse>

        <Collapse title="网络（自开机累计）" hint={net?.bytes_recv == null ? '不可用' : ''}
                  defaultOpen={false} storageKey="monitor.net">
          {net?.bytes_sent == null && net?.bytes_recv == null ? (
            <div className="empty" style={{ padding: '14px 0' }}>采集后端未提供网络计数</div>
          ) : (
            <>
              <div className="kv">
                <span className="kv-k">发送</span>
                <span className="kv-v mono">{gb(net?.bytes_sent, 1)} · {net?.packets_sent ?? '—'} 包</span>
              </div>
              <div className="kv">
                <span className="kv-k">接收</span>
                <span className="kv-v mono">{gb(net?.bytes_recv, 1)} · {net?.packets_recv ?? '—'} 包</span>
              </div>
              <div className="kv">
                <span className="kv-k">架构</span>
                <span className="kv-v mono">{host.info.machine || '—'}</span>
              </div>
              <div className="kv">
                <span className="kv-k">Python</span>
                <span className="kv-v mono">{host.info.python}</span>
              </div>
              <div style={{ marginTop: 8, fontSize: 11.5, color: 'var(--text-mute)', lineHeight: 1.7 }}>
                累计值不做速率换算 —— 要速率请接网卡级采样。
              </div>
            </>
          )}
        </Collapse>
      </div>

      <Collapse title="进程 TOP 10" defaultOpen storageKey="monitor.procs"
                right={
                  <div style={{ display: 'flex', gap: 6 }} onClick={(e) => e.stopPropagation()}>
                    <button className={'btn btn-sm ' + (sort === 'cpu' ? 'btn-primary' : 'btn-ghost')}
                            onClick={() => setSort('cpu')}>CPU</button>
                    <button className={'btn btn-sm ' + (sort === 'mem' ? 'btn-primary' : 'btn-ghost')}
                            onClick={() => setSort('mem')}>内存</button>
                  </div>
                }>
        {!procsAvailable ? (
          <div className="empty" style={{ padding: '18px 0' }}>
            采集后端（{host.info.backend}）不提供进程明细
          </div>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: 80 }}>PID</th>
                <th>进程</th>
                <th style={{ width: 80, textAlign: 'right' }}>CPU</th>
                <th style={{ width: 80, textAlign: 'right' }}>内存</th>
                <th style={{ width: 150 }}>用户</th>
              </tr>
            </thead>
            <tbody>
              {procs.map((p) => (
                <tr key={p.pid}>
                  <td className="mono" style={{ color: 'var(--text-mute)' }}>{p.pid}</td>
                  <td className="mono" style={{ color: 'var(--text)' }}>{p.name}</td>
                  <td className="mono" style={{ textAlign: 'right', color: 'var(--text-dim)' }}>
                    {p.cpu.toFixed(1)}%
                  </td>
                  <td className="mono" style={{ textAlign: 'right', color: 'var(--text-dim)' }}>
                    {p.mem.toFixed(1)}%
                  </td>
                  <td className="mono" style={{ color: 'var(--text-mute)' }}>{p.user || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Collapse>

      <div style={{ marginTop: 14 }}>
        <Collapse title="说明" defaultOpen={false} storageKey="monitor.notes">
          <div style={{ fontSize: 11.5, color: 'var(--text-mute)', lineHeight: 1.85 }}>
            · 数据来自 <span className="mono">sysres</span> 机制包（psutil 优先、stdlib 兜底）。
            拿不到的字段显示 <span className="mono">—</span>，不填 0 假装正常。<br />
            · 进程 CPU 已归一到「整机口径」（单核满负荷 × 核数 ≈ 100%），与任务管理器一致；
            已排除 PID 0（System Idle）。<br />
            · 刷新间隔 {REFRESH_MS / 1000}s，服务端另有 1s 采样缓存兜底。
          </div>
        </Collapse>
      </div>
    </>
  )
}

/* ── 小组件 ───────────────────────────────────────────── */

/** 图例小圆点 + 名称（value 已在 name 里拼好） */
function LegendDot({ color, name }: { color: string; name: string }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
      <span style={{ width: 8, height: 8, borderRadius: 4, background: color, display: 'inline-block' }} />
      {name}
    </span>
  )
}

function last(points: TrendPoint[], key: 'cpu' | 'mem' | 'disk' | 'load'): string {
  for (let i = points.length - 1; i >= 0; i--) {
    const v = points[i][key]
    if (v != null) return key === 'load' ? String(v) : `${Math.round(v)}%`
  }
  return '—'
}

function Stat({ label, value, hint, pct }: {
  label: string; value: string; hint?: string; pct?: number | null
}) {
  return (
    <div className="card" style={{ padding: '15px 17px' }}>
      <div style={{
        fontSize: 11, color: 'var(--text-mute)',
        letterSpacing: 'var(--track-wider)', textTransform: 'uppercase',
      }}>
        {label}
      </div>
      <div className="num" style={{ fontSize: 21, marginTop: 5, letterSpacing: 'var(--track-tight)' }}>
        {value}
      </div>
      {hint && <div style={{ fontSize: 11.5, color: 'var(--text-mute)', marginTop: 3 }}>{hint}</div>}
      {pct != null && <Bar pct={pct} />}
    </div>
  )
}

function Bar({ pct }: { pct?: number | null }) {
  const v = Math.max(0, Math.min(100, Number(pct ?? 0)))
  const color = v >= 90 ? 'var(--danger)' : v >= 70 ? 'var(--warn)' : 'var(--accent)'
  return (
    <div style={{
      marginTop: 9, height: 3, borderRadius: 2,
      background: 'var(--border)', overflow: 'hidden',
    }}>
      <div style={{
        width: `${v}%`, height: '100%', background: color,
        transition: 'width var(--dur) var(--ease)',
      }} />
    </div>
  )
}

function gb(bytes?: number | null, digits = 1): string {
  if (bytes == null) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  let v = Number(bytes)
  let i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(i === 0 ? 0 : digits)} ${units[i]}`
}
