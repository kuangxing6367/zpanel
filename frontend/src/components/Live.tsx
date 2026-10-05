import { useEffect, useRef, useState, type ReactNode } from 'react'

/* ── 可折叠卡片 ─────────────────────────────────────────
   面板天然是「长列表」：磁盘、进程、数据源、命令… 全都平铺会淹掉重点。
   统一用这个：标题行可点，右侧显示数量/摘要，展开状态按 storageKey 记忆。 */
export default function Collapse({
  title, count, hint, right, defaultOpen = true, storageKey, children,
}: {
  title: string
  count?: number | string
  hint?: string
  right?: ReactNode
  defaultOpen?: boolean
  storageKey?: string
  children: ReactNode
}) {
  const key = storageKey ? `zp-fold:${storageKey}` : ''
  const [open, setOpen] = useState(() => {
    if (!key) return defaultOpen
    const v = localStorage.getItem(key)
    return v == null ? defaultOpen : v === '1'
  })
  useEffect(() => {
    if (key) localStorage.setItem(key, open ? '1' : '0')
  }, [key, open])

  return (
    <div className="card" style={{ padding: '4px 6px 6px' }}>
      <div
        role="button"
        tabIndex={0}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') setOpen((v) => !v) }}
        style={{
          display: 'flex', alignItems: 'center', gap: 10,
          padding: '13px 12px', cursor: 'pointer', userSelect: 'none',
        }}
      >
        <span
          className="mono"
          style={{
            width: 12, fontSize: 10, color: 'var(--text-mute)',
            transform: open ? 'rotate(90deg)' : 'none',
            transition: 'transform var(--dur) var(--ease)',
          }}
        >
          ▶
        </span>
        <span className="card-title" style={{ marginBottom: 0 }}>{title}</span>
        {count != null && count !== '' && (
          <span className="tag tag-mono" style={{ fontSize: 10.5 }}>{count}</span>
        )}
        <span style={{ flex: 1 }} />
        {hint && (
          <span style={{ fontSize: 11.5, color: 'var(--text-mute)' }}>{hint}</span>
        )}
        {right}
      </div>
      {open && <div style={{ padding: '0 8px 8px' }}>{children}</div>}
    </div>
  )
}

/* ── 实时轮询 ───────────────────────────────────────────
   把回调放进 ref，避免调用方每次 render 产生新函数导致定时器被反复重建
   （这正是「以为在刷新其实没刷」的经典成因）。 */
export function usePoll(load: () => void | Promise<void>, ms: number, enabled = true) {
  const ref = useRef(load)
  ref.current = load
  useEffect(() => {
    if (!enabled) return
    void ref.current()                       // 立即拉一次：不让首屏干等一个周期
    if (!ms) return
    const id = window.setInterval(() => { void ref.current() }, ms)
    return () => window.clearInterval(id)
  }, [ms, enabled])
}

/* ── 实时状态标签 ───────────────────────────────────────
   每秒自增「Ns 前」，让「在不在刷新」一眼可见 —— 数字静悄悄变不算实时感。 */
export function LiveTag({
  at, enabled, onToggle, intervalMs, busy,
}: {
  at: number
  enabled: boolean
  onToggle: () => void
  intervalMs: number
  busy?: boolean
}) {
  const [, tick] = useState(0)
  useEffect(() => {
    const id = window.setInterval(() => tick((v) => v + 1), 1000)
    return () => window.clearInterval(id)
  }, [])

  const ago = at ? Math.max(0, Math.round((Date.now() - at) / 1000)) : null
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <span
        title={enabled ? `每 ${intervalMs / 1000}s 自动刷新` : '已暂停自动刷新'}
        style={{
          width: 6, height: 6, borderRadius: 3,
          background: enabled ? 'var(--ok)' : 'var(--text-mute)',
          boxShadow: enabled ? '0 0 0 0 var(--ok-soft)' : 'none',
          animation: enabled && !busy ? 'zp-pulse 2s var(--ease) infinite' : 'none',
        }}
      />
      <span style={{ fontSize: 11.5, color: 'var(--text-mute)', minWidth: 66 }}>
        {ago == null ? '尚未采样' : ago < 2 ? '刚刚更新' : `${ago}s 前更新`}
      </span>
      <button className="btn btn-sm btn-ghost" onClick={onToggle}>
        {enabled ? '暂停' : '继续'}
      </button>
    </div>
  )
}
