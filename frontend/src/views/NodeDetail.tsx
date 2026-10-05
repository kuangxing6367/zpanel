import { useEffect } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useNode } from '../node'
import Overview from '../parts/Overview'
import Instances from '../parts/Instances'
import Sites from '../parts/Sites'
import Database from '../parts/Database'
import Services from '../parts/Services'
import Files from '../parts/Files'
import Terminal from '../parts/Terminal'
import Environment from '../parts/Environment'
import Monitor from './Monitor'
import Scheduler from './Scheduler'
import Ssl from './Ssl'
import Backup from './Backup'
import Firewall from './Firewall'
import LogsCenter from './LogsCenter'

/**
 * 节点工作台（1Panel 详情页式布局）：
 * 左侧竖排子菜单（这台机器的全部能力），右侧内容区 —— 不再是横铺一长条。
 * 标签即 URL（/nodes/:name/:tab），刷新/分享不丢上下文；
 * 侧边栏只有全局页，想操作哪台机器，先进它的卡片。
 */
const TABS = [
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

type TabKey = typeof TABS[number]['key']
const TAB_KEYS = new Set<string>(TABS.map((t) => t.key))

export default function NodeDetail() {
  const { name = '', tab: tabParam } = useParams()
  const nav = useNavigate()
  const { nodes, current, setCurrent, node } = useNode()

  const tab: TabKey = TAB_KEYS.has(tabParam || '') ? (tabParam as TabKey) : 'overview'
  const tabLabel = TABS.find((t) => t.key === tab)?.label || '概览'

  // 进入工作台即把全局取数作用域切到该节点（不带 nodeName 参数的内嵌页面跟着走）
  useEffect(() => {
    if (name && name !== current) setCurrent(name)
  }, [name, current, setCurrent])

  const rec = node || nodes.find((n) => n.name === name) || null
  const isLocal = rec?.is_local || name === 'localhost'
  const online = rec ? (rec.status === 'online' || rec.online) : false

  return (
    <>
      {/* 面包屑（1Panel RouterButton 同款层级感） */}
      <div className="crumbs">
        <span className="crumb">
          <span className="crumb-link" onClick={() => nav('/')}>总览</span>
        </span>
        <span className="crumb"><span className="crumb-sep">›</span>
          <span className={'crumb' + (tab === 'overview' ? ' crumb-here' : ' crumb-link')}
                onClick={() => tab !== 'overview' && nav(`/nodes/${encodeURIComponent(name)}/overview`)}>{name}</span>
        </span>
        {tab !== 'overview' && (
          <span className="crumb"><span className="crumb-sep">›</span>
            <span className="crumb-here">{tabLabel}</span>
          </span>
        )}
      </div>

      <div className="page-head">
        <div>
          <div className="page-title">
            {name}
            <span className="tag" style={{ marginLeft: 10, fontSize: 10.5 }}>
              {isLocal ? '本机' : '远程节点'}
            </span>
            {rec && (
              <span style={{ marginLeft: 8, display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                <span className={'dot ' + (online ? 'dot-ok' : 'dot-bad')} />
                <span style={{ fontSize: 12, color: 'var(--text-mute)' }}>
                  {rec.status || (online ? 'online' : 'offline')}
                </span>
              </span>
            )}
          </div>
          <div className="page-sub">
            {rec?.platform || '—'}{rec?.hostname ? ` · ${rec.hostname}` : ''}
            {rec?.version ? ` · 内核 ${rec.version}` : ''}
            {' · '}左侧菜单里的所有操作都只作用于这台机器
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn btn-sm" onClick={() => nav('/')}>← 返回总览</button>
          <button className="btn btn-sm" onClick={() => nav('/nodes')}>节点管理</button>
        </div>
      </div>

      {/* 内容区直接铺开 —— 菜单已由左侧上下文侧边栏承担（Layout 按路由切换） */}
      <div>
        {tab === 'overview' && <Overview nodeName={name} />}
        {tab === 'monitor' && <Monitor nodeName={name} />}
        {tab === 'instances' && <Instances nodeName={name} />}
        {tab === 'sites' && <Sites nodeName={name} />}
        {tab === 'database' && <Database nodeName={name} />}
        {tab === 'services' && <Services nodeName={name} />}
        {tab === 'files' && <Files nodeName={name} />}
        {tab === 'terminal' && <Terminal nodeName={name} />}
        {tab === 'scheduler' && <Scheduler />}
        {tab === 'ssl' && <Ssl />}
        {tab === 'backup' && <Backup />}
        {tab === 'firewall' && <Firewall />}
        {tab === 'logs' && <LogsCenter />}
        {tab === 'env' && <Environment nodeName={name} />}
      </div>
    </>
  )
}
