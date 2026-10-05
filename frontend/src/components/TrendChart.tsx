/**
 * 轻量趋势折线图（纯 SVG，不引图表库）。
 * 三条 0-100% 线：CPU / 内存 / 磁盘；某指标缺值就断线，不编数据。
 * viewBox + preserveAspectRatio=none 做响应式，vector-effect 保持线宽。
 */
export interface TrendPoint {
  t: number
  cpu: number | null
  mem: number | null
  load: number | null
  disk: number | null
}

const W = 800
const H = 220
const PAD_L = 34
const PAD_R = 10
const PAD_T = 12
const PAD_B = 24

const SERIES: { key: 'cpu' | 'mem' | 'disk'; name: string; color: string }[] = [
  { key: 'cpu', name: 'CPU', color: 'var(--accent)' },
  { key: 'mem', name: '内存', color: 'var(--ok)' },
  { key: 'disk', name: '磁盘', color: 'var(--warn)' },
]

export function fmtTick(ts: number, spanHours: number): string {
  const d = new Date(ts * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}`
  return spanHours > 24 ? `${d.getMonth() + 1}-${d.getDate()} ${hm}` : hm
}

export default function TrendChart({ points, spanHours }: {
  points: TrendPoint[]
  spanHours: number
}) {
  if (points.length < 2) {
    return <div className="empty" style={{ padding: '22px 0' }}>
      历史数据积累中（每分钟一拍，稍后再看）
    </div>
  }
  const t0 = points[0].t
  const t1 = Math.max(points[points.length - 1].t, t0 + 1)
  const x = (t: number) => PAD_L + ((t - t0) / (t1 - t0)) * (W - PAD_L - PAD_R)
  const y = (v: number) => PAD_T + (1 - Math.max(0, Math.min(100, v)) / 100) * (H - PAD_T - PAD_B)

  const segsFor = (sel: (p: TrendPoint) => number | null): string[] => {
    const segs: string[] = []
    let cur: string[] = []
    for (const p of points) {
      const v = sel(p)
      if (v == null) {
        if (cur.length > 1) segs.push(cur.join(' '))
        cur = []
      } else {
        cur.push(`${x(p.t).toFixed(1)},${y(v).toFixed(1)}`)
      }
    }
    if (cur.length > 1) segs.push(cur.join(' '))
    return segs
  }

  const ticks = [t0, t0 + (t1 - t0) / 2, t1]

  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto', display: 'block' }}
         role="img" aria-label="资源历史趋势">
      {/* 横向网格 + 纵轴刻度 */}
      {[0, 25, 50, 75, 100].map((v) => (
        <g key={v}>
          <line x1={PAD_L} x2={W - PAD_R} y1={y(v)} y2={y(v)}
                stroke={v === 0 ? 'var(--border-strong)' : 'var(--border)'}
                strokeWidth={1} vectorEffect="non-scaling-stroke" />
          <text x={PAD_L - 6} y={y(v) + 3.5} textAnchor="end"
                fontSize={10} fill="var(--text-mute)">{v}</text>
        </g>
      ))}
      {/* 时间轴刻度 */}
      {ticks.map((t, i) => (
        <text key={i}
              x={i === 0 ? PAD_L : i === 1 ? (PAD_L + W - PAD_R) / 2 : W - PAD_R}
              y={H - 6}
              textAnchor={i === 0 ? 'start' : i === 1 ? 'middle' : 'end'}
              fontSize={10} fill="var(--text-mute)">
          {fmtTick(t, spanHours)}
        </text>
      ))}
      {/* 三条趋势线 */}
      {SERIES.map((s) => segsFor((p) => p[s.key]).map((seg, i) => (
        <polyline key={`${s.key}-${i}`} points={seg} fill="none"
                  stroke={s.color} strokeWidth={1.6} strokeLinejoin="round"
                  strokeLinecap="round" vectorEffect="non-scaling-stroke"
                  opacity={0.9} />
      )))}
    </svg>
  )
}
