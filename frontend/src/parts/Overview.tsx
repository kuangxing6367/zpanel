import { useCallback, useState } from 'react'
import { fmtUptime } from '../api'
import { useNode } from '../node'
import { PageHeader } from '../components/ui'
import Collapse, { LiveTag, usePoll } from '../components/Live'

const MS = 3000

/** 节点概览：这台机器（无论本机还是远端）现在什么样、上面跑了什么。 */
export default function Overview({ nodeName }: { nodeName?: string }) {
  const { current, call, node } = useNode()
  const name = nodeName || current

  const [host, setHost] = useState<any>(null)
  const [counts, setCounts] = useState<{ inst: any; sites: any; files: any }>({
    inst: null, sites: null, files: null,
  })
  const [at, setAt] = useState(0)
  const [auto, setAuto] = useState(true)
  const [err, setErr] = useState('')

  const load = useCallback(async () => {
    try {
      const [h, inst, sites, files] = await Promise.all([
        call<any>('sysres.snapshot', {}, 25).catch(() => null),
        call<any>('runtime.list', {}, 15).catch(() => null),
        call<any>('sites.list', {}, 15).catch(() => null),
        call<any>('files.roots', {}, 15).catch(() => null),
      ])
      setHost(h); setCounts({ inst, sites, files })
      setAt(Date.now()); setErr('')
    } catch (e: any) {
      setErr(e?.message || '读取失败')
    }
  }, [call])

  usePoll(load, MS, auto)

  const mem = host?.memory
  const cpu = host?.cpu?.percent
  const disks: any[] = host?.disks || []

  return (
    <>
      <PageHeader title="节点概览" sub={<>
        节点 <span className="mono" style={{ color: 'var(--accent)' }}>{name}</span>
        {' '} · 负载、实例、站点与文件的概况
      </>} />

      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <LiveTag at={at} enabled={auto} intervalMs={MS} onToggle={() => setAuto((v) => !v)} />
      </div>

      {err && <div className="empty" style={{ marginBottom: 12 }}>{err}</div>}

      <div className="grid grid-4" style={{ marginBottom: 14 }}>
        <Stat label="CPU" value={cpu == null ? '—' : `${cpu}%`}
              hint={host ? `${host.info?.cpu_count ?? '—'} 核` : '—'} pct={cpu} />
        <Stat label="内存" value={mem?.percent == null ? '—' : `${mem.percent}%`}
              hint={mem ? `${gb(mem.used)} / ${gb(mem.total)}` : '—'} pct={mem?.percent} />
        <Stat label="实例" value={counts.inst ? `${counts.inst.count?.running || 0} / ${counts.inst.count?.total || 0}` : '—'}
              hint="运行 / 总数" />
        <Stat label="站点" value={counts.sites ? String(counts.sites.count?.total ?? counts.sites.count ?? 0) : '—'}
              hint="该节点配置的站点" />
      </div>

      <div className="grid grid-2" style={{ marginBottom: 14 }}>
        <Collapse title="节点信息" defaultOpen storageKey={'ov.info.' + name}>
          <div className="kv"><span className="kv-k">名称</span><span className="kv-v mono">{name}</span></div>
          <div className="kv"><span className="kv-k">类型</span>
            <span className="kv-v">{node?.is_local ? '本机（localhost）' : '远程节点'}</span></div>
          <div className="kv"><span className="kv-k">状态</span>
            <span className="kv-v">{node?.status || (node?.online ? 'online' : '—')}</span></div>
          {node?.platform && (
            <div className="kv"><span className="kv-k">平台</span><span className="kv-v">{node.platform}</span></div>
          )}
          {node?.hostname && (
            <div className="kv"><span className="kv-k">主机名</span><span className="kv-v">{node.hostname}</span></div>
          )}
          {host?.info && (
            <>
              <div className="kv"><span className="kv-k">系统</span>
                <span className="kv-v">{host.info.system} {host.info.release}</span></div>
              <div className="kv"><span className="kv-k">Python</span>
                <span className="kv-v mono">{host.info.python}</span></div>
              <div className="kv"><span className="kv-k">运行时长</span>
                <span className="kv-v">{fmtUptime(host.uptime ?? undefined)}</span></div>
              <div className="kv"><span className="kv-k">采集后端</span>
                <span className="kv-v">{host.info.backend}</span></div>
            </>
          )}
          {!host && <div className="empty" style={{ padding: '12px 0' }}>该节点的监控能力不可用</div>}
        </Collapse>

        <Collapse title="磁盘" count={disks.length || '—'} defaultOpen storageKey={'ov.disk.' + name}>
          {disks.length === 0 ? (
            <div className="empty" style={{ padding: '12px 0' }}>拿不到磁盘信息</div>
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
      </div>

      <Collapse title="该节点已注册的能力" defaultOpen={false} storageKey={'ov.caps.' + name}
                hint="由节点上的服务层 / 软件层注册">
        <div style={{ fontSize: 11.5, color: 'var(--text-mute)', lineHeight: 1.85 }}>
          · 运行时：{host ? `${counts.inst?.count?.total ?? 0} 个实例` : '不可用'}<br />
          · 站点：{counts.sites ? `${counts.sites.count?.total ?? counts.sites.count ?? 0} 个` : '不可用'}<br />
          · 文件根目录：{counts.files?.roots ? counts.files.roots.length + ' 个' : '不可用'}<br />
          · 监控：{host ? `可用（${host.info?.backend}）` : '不可用'}<br />
          · 终端/文件：同时受开关与路径白名单约束
        </div>
      </Collapse>
    </>
  )
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
    <div style={{ marginTop: 9, height: 3, borderRadius: 2, background: 'var(--border)', overflow: 'hidden' }}>
      <div style={{ width: `${v}%`, height: '100%', background: color }} />
    </div>
  )
}

function gb(bytes?: number | null): string {
  if (bytes == null) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  let v = Number(bytes)
  let i = 0
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}
