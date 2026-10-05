import { useEffect, useState } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { api, clearToken, getToken, onUnauthorized, type SessionUser } from './api'
import { LOCAL_NODE, NodeScopeProvider } from './node'
import Layout from './views/Layout'
import Login from './views/Login'
import Dashboard from './views/Dashboard'
import Nodes from './views/Nodes'
import NodeDetail from './views/NodeDetail'
import InstanceDetail from './views/InstanceDetail'
import Alerts from './views/Alerts'
import Settings from './views/Settings'
import Tasks from './views/Tasks'

/**
 * 导航范式（对标 MCSManager + 哪吒）：
 * 侧边栏只有全局页（总览卡片墙 / 节点管理 / 告警 / 设置）；
 * 一切操作能力都在节点工作台 /nodes/:name/:tab —— 先进卡片，才操控。
 * 节点上下文 = URL，旧的全局页路由重定向到本机工作台的对应标签（保书签可用）。
 */
export default function App() {
  const [user, setUser] = useState<SessionUser | null>(null)
  const [ready, setReady] = useState(false)

  // 启动时用已有令牌恢复会话；401 时统一退回登录页
  useEffect(() => {
    onUnauthorized(() => setUser(null))
    if (!getToken()) {
      setReady(true)
      return
    }
    api.me()
      .then((r) => setUser(r.user))
      .catch(() => setUser(null))
      .finally(() => setReady(true))
  }, [])

  const handleLogout = async () => {
    try { await api.logout() } catch { /* 忽略 */ }
    clearToken()
    setUser(null)
  }

  if (!ready) {
    return (
      <div style={{
        height: '100%', display: 'flex', alignItems: 'center',
        justifyContent: 'center', gap: 10, color: 'var(--text-mute)',
      }}>
        <span className="spinner" /> 正在连接内核…
      </div>
    )
  }

  if (!user) return <Login onLogin={setUser} />

  return (
    <NodeScopeProvider>
      <Layout user={user} onLogout={handleLogout}>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/nodes" element={<Nodes />} />
          <Route path="/nodes/:name" element={<Navigate to="overview" replace />} />
          <Route path="/nodes/:name/:tab" element={<NodeDetail />} />
          <Route path="/instances/:id" element={<InstanceDetail />} />
          <Route path="/alerts" element={<Alerts />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/settings" element={<Settings />} />

          {/* 旧全局页 → 本机工作台对应标签 */}
          <Route path="/monitor" element={<Nav to={`/nodes/${LOCAL_NODE}/monitor`} />} />
          <Route path="/runtime" element={<Nav to={`/nodes/${LOCAL_NODE}/instances`} />} />
          <Route path="/sites" element={<Nav to={`/nodes/${LOCAL_NODE}/sites`} />} />
          <Route path="/files" element={<Nav to={`/nodes/${LOCAL_NODE}/files`} />} />
          <Route path="/terminal" element={<Nav to={`/nodes/${LOCAL_NODE}/terminal`} />} />
          <Route path="/scheduler" element={<Nav to={`/nodes/${LOCAL_NODE}/scheduler`} />} />
          <Route path="/ssl" element={<Nav to={`/nodes/${LOCAL_NODE}/ssl`} />} />
          <Route path="/backup" element={<Nav to={`/nodes/${LOCAL_NODE}/backup`} />} />
          <Route path="/firewall" element={<Nav to={`/nodes/${LOCAL_NODE}/firewall`} />} />
          <Route path="/logs" element={<Nav to={`/nodes/${LOCAL_NODE}/logs`} />} />

          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Layout>
    </NodeScopeProvider>
  )
}

/** 重定向小包装（保持 replace，不污染历史栈） */
function Nav({ to }: { to: string }) {
  return <Navigate to={to} replace />
}
