import { useEffect, type ReactNode } from 'react'

/* ============================================================
   页面骨架零件 —— 每个管理页都用同一套结构：

   <PageHeader title="..." sub="..." actions={<button className="btn btn-primary">新建</button>} />
   <Toolbar>筛选 / 搜索 | 刷新</Toolbar>
   <div className="card">表格</div>

   对齐的是常见管理后台（1Panel / Element 系）的信息次序：
   「我在哪、我能干什么」放最显眼，主操作只有一个，永远在右上角。
   ============================================================ */

/** 页头：大标题 + 一句话说明 + 右侧操作区（主按钮放这） */
export function PageHeader({ title, sub, actions, back }: {
  title: ReactNode; sub?: ReactNode; actions?: ReactNode; back?: string
}) {
  return (
    <div className="page-head">
      <div>
        {back && (
          <button className="btn-back" onClick={() => history.back()}>← 返回</button>
        )}
        <div className="page-title">{title}</div>
        {sub && <div className="page-sub">{sub}</div>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  )
}

/** 工具栏：左边筛选/搜索，右边刷新类次级操作 */
export function Toolbar({ left, right }: { left?: ReactNode; right?: ReactNode }) {
  return (
    <div className="toolbar">
      <div className="toolbar-left">{left}</div>
      <div className="toolbar-right">{right}</div>
    </div>
  )
}

/** 状态徽章：带底色，一眼分清 运行/停止/过渡 */
const BADGE: Record<string, string> = {
  ok: 'badge-ok', warn: 'badge-warn', bad: 'badge-bad', info: 'badge-info', muted: 'badge-muted',
}
export function Badge({ tone = 'muted', children }: { tone?: keyof typeof BADGE; children: ReactNode }) {
  return <span className={'badge ' + (BADGE[tone] || BADGE.muted)}>{children}</span>
}

/** 实例/服务的状态 → 徽章文案与色调（多处共用，只写一遍） */
export function statusBadge(status: string): { tone: keyof typeof BADGE; text: string } {
  switch (status) {
    case 'running': return { tone: 'ok', text: '运行中' }
    case 'starting': return { tone: 'warn', text: '启动中' }
    case 'stopping': return { tone: 'warn', text: '停止中' }
    case 'busy': return { tone: 'warn', text: '处理中' }
    case 'stopped': return { tone: 'muted', text: '已停止' }
    case 'online': return { tone: 'ok', text: '在线' }
    case 'offline': return { tone: 'muted', text: '离线' }
    case 'error': return { tone: 'bad', text: '异常' }
    default: return { tone: 'muted', text: status || '—' }
  }
}

/** 模态对话框：Esc 关闭、点遮罩关闭、锁页面滚动 */
export function Modal({ title, onClose, children, footer, width = 560 }: {
  title: ReactNode; onClose: () => void; children: ReactNode
  footer?: ReactNode; width?: number
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = prev
    }
  }, [onClose])

  return (
    <div className="modal-mask" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="modal" style={{ width }}>
        <div className="modal-head">
          <div className="modal-title">{title}</div>
          <button className="btn-close" onClick={onClose} aria-label="关闭">×</button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  )
}

/** 空态：说明 + 引导动作（不是干巴巴一行灰字） */
export function EmptyState({ title, hint, action }: {
  title: string; hint?: string; action?: ReactNode
}) {
  return (
    <div className="empty-state">
      <div className="empty-title">{title}</div>
      {hint && <div className="empty-hint">{hint}</div>}
      {action && <div className="empty-action">{action}</div>}
    </div>
  )
}

/** 表单字段（对话框里用）：label 在上、控件在下 */
export function Field({ label, hint, children }: {
  label: string; hint?: string; children: ReactNode
}) {
  return (
    <label className="field">
      <div className="field-label">{label}{hint && <span className="field-hint"> · {hint}</span>}</div>
      {children}
    </label>
  )
}

/** 确认对话框（代替 window.confirm：样式统一、语义清楚） */
export function ConfirmModal({ title, text, confirmText = '删除', danger = true,
                              busy = false, onConfirm, onClose }: {
  title: string; text: ReactNode; confirmText?: string; danger?: boolean
  busy?: boolean; onConfirm: () => void; onClose: () => void
}) {
  return (
    <Modal title={title} onClose={onClose} width={420}
           footer={
             <>
               <button className="btn" onClick={onClose}>取消</button>
               <button className={danger ? 'btn btn-danger' : 'btn btn-primary'}
                       disabled={busy} onClick={onConfirm}>
                 {busy ? '处理中…' : confirmText}
               </button>
             </>
           }>
      <div style={{ fontSize: 13, lineHeight: 1.8 }}>{text}</div>
    </Modal>
  )
}
