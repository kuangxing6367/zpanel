import type { ReactNode } from 'react'
import { NavLink, matchPath, useLocation, useNavigate } from 'react-router-dom'
import type { SessionUser } from '../api'

const NAV: { to?: string; label: string; end?: boolean; soon?: boolean }[] = [
  { to: '/', label: '总览', end: true },
  { to: '/nodes', label: '节点', end: true },   // end：/nodes/localhost/* 是工作台，不算命中全局"节点"
  { to: '/tasks', label: '任务' },
  { to: '/alerts', label: '告警' },
  { to: '/settings', label: '设置' },
]

/** 节点工作台的竖排菜单（与 NodeDetail 保持同一份定义） */
const WS_TABS = [
  { key: 'overview', label: '概览' },
  { key: 'monitor', label: '监控' },
  { key: 'instances', label: '实例' },
  { key: 'sites', label: '站点' },
  { key: 'database', label: '数据库' },
  { key: 'services', label: '服务' },
  { key: 'files', label: '文件' },
  { key: 'terminal', label: '终端' },
  { key: 'scheduler', label: '计划任务' },
  { key: 'ssl', label: 'SSL' },
  { key: 'backup', label: '备份' },
  { key: 'firewall', label: '防火墙' },
  { key: 'logs', label: '日志' },
  { key: 'env', label: '环境' },
] as const

// 设计约定：侧边栏是**上下文**的 ——
// 全局页面显示全局导航；进入节点工作台 /nodes/:name/* 后，最左侧
// 直接变成这台机器的菜单（1Panel 就是一个侧边栏到底），全局页收进底部。

export default function Layout({
  user, onLogout, children,
}: {
  user: SessionUser
  onLogout: () => void
  children: ReactNode
}) {
  const location = useLocation()
  const nav = useNavigate()
  // /nodes/:name/:tab 命中 = 在节点工作台里：侧边栏切换为这台机器的菜单
  const ws = matchPath('/nodes/:name/:tab', location.pathname)
  const wsName = ws?.params?.name ? decodeURIComponent(ws.params.name) : ''

  return (
    <div className="shell">
      {/* ── 侧边栏（上下文式）── */}
      <aside className="shell-sidebar">
        <div className="shell-brand">
          <Mark />
          <div>
            <div className="shell-brand-text">ZPanel</div>
            <div className="shell-brand-sub">运维面板</div>
          </div>
        </div>

        {wsName ? (
          /* ── 工作台上下文：这台机器的菜单 + 底部全局入口 ── */
          <nav className="shell-nav ws-nav">
            <div className="ws-nav-node">
              <span className="mono">{wsName}</span>
            </div>
            {WS_TABS.map((t) => {
              const to = `/nodes/${encodeURIComponent(wsName)}/${t.key}`
              const active = location.pathname === to
              return (
                <button key={t.key}
                        className={'shell-nav-item' + (active ? ' active' : '')}
                        onClick={() => nav(to)}>
                  {t.label}
                </button>
              )
            })}
            <div className="ws-nav-divider" />
            <div className="ws-nav-label">全局</div>
            {NAV.map((item) => (
              <NavLink key={item.label} to={item.to as string} end={item.end}
                       className={({ isActive }) =>
                         'shell-nav-item' + (isActive ? ' active' : '')}>
                {item.label}
              </NavLink>
            ))}
          </nav>
        ) : (
          /* ── 全局上下文 ── */
          <nav className="shell-nav">
            {NAV.map((item) => (
              <NavLink key={item.label} to={item.to as string} end={item.end}
                       className={({ isActive }) =>
                         'shell-nav-item' + (isActive ? ' active' : '')}>
                {item.label}
              </NavLink>
            ))}
          </nav>
        )}

        <div className="shell-footer">内核级多机管理 · 0.1</div>
      </aside>

      {/* ── 主区 ── */}
      <div className="shell-main">
        <header className="shell-header">
          <div className="shell-header-spacer" />

          <div className="shell-user">
            <span className="dot dot-ok" />
            <span className="mono" style={{ fontSize: 12, color: 'var(--text-dim)' }}>
              {user.username}
            </span>
            <span className="tag" style={{ marginLeft: 2 }}>{user.role}</span>
          </div>
          <button className="btn btn-sm" onClick={onLogout}>退出</button>
        </header>

        <main className="shell-content">
          {/* key=路径：每次导航重放入场动画（1Panel fade-transform 手感） */}
          <div className="shell-content-inner page-enter" key={location.pathname}>{children}</div>
        </main>
      </div>
    </div>
  )
}

/** 极简标识：四角星（与项目身份一致） */
function Mark() {
  return (
    <svg className="shell-brand-mark" width="18" height="18" viewBox="0 0 24 24" aria-hidden>
      <path
        d="M12 2.6c.5 4.1 1.4 6.3 3.2 7.4 1.4.9 3.2 1.2 6.2 1.5-3 .3-4.8.6-6.2 1.5-1.8 1.1-2.7 3.3-3.2 7.4-.5-4.1-1.4-6.3-3.2-7.4-1.4-.9-3.2-1.2-6.2-1.5 3-.3 4.8-.6 6.2-1.5 1.8-1.1 2.7-3.3 3.2-7.4Z"
        fill="var(--accent)"
        opacity=".9"
      />
    </svg>
  )
}
