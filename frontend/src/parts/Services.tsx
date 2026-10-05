import { useCallback, useState } from 'react'
import { useNode } from '../node'
import { Badge, ConfirmModal, PageHeader, Toolbar } from '../components/ui'
import Collapse, { LiveTag, usePoll } from '../components/Live'

const MS = 10000

interface SvcItem {
  id: string
  name: string
  role: string
  exe: string
  version: string
  unit: string
  state: 'running' | 'stopped' | 'bare' | 'missing'
  running: boolean | null
  unit_error: string
  pkg_owner: string
  installable: boolean
}

// 状态语义与后端一一对应：running/stopped 只在 systemd 真查到单元时才下结论；
// bare = 二进制在但没注册服务（运行状态未知，可能是手工/docker 在跑）——不推断、不给启停按钮。
const STATE_BADGE: Record<string, { tone: 'ok' | 'warn' | 'muted' | 'info'; text: string }> = {
  running: { tone: 'ok', text: '运行中' },
  stopped: { tone: 'warn', text: '已停止' },
  bare: { tone: 'info', text: '未纳管' },
  missing: { tone: 'muted', text: '未安装' },
}

/** 服务页：这台机器上 nginx/数据库/缓存等服务的探测、一键安装与启停。
 *  安装/启停只允许目录白名单里的服务 —— 后端 catalog 收口，前端不传包名。 */
export default function Services({ nodeName }: { nodeName?: string }) {
  const { current, call } = useNode()
  const name = nodeName || current

  const [items, setItems] = useState<SvcItem[] | null>(null)
  const [pkg, setPkg] = useState<any>(null)
  const [osInfo, setOsInfo] = useState<any>(null)
  const [warns, setWarns] = useState<string[]>([])
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [log, setLog] = useState('')
  const [busy, setBusy] = useState('')
  const [auto, setAuto] = useState(true)
  const [at, setAt] = useState(0)
  const [installing, setInstalling] = useState<SvcItem | null>(null)
  const [uninstalling, setUninstalling] = useState<SvcItem | null>(null)
  const [compiling, setCompiling] = useState<SvcItem | null>(null)

  const load = useCallback(async () => {
    try {
      const r = await call<any>('svc.list', {}, 60)
      setItems(r?.items || [])
      setPkg(r?.pkg || null)
      setOsInfo(r?.os || null)
      setWarns(Array.isArray(r?.warnings) ? r.warnings : [])
      setAt(Date.now())
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '服务探测失败' })
    }
  }, [call])

  usePoll(load, MS, auto)

  async function act(it: SvcItem, action: string) {
    setBusy(`${it.id}:${action}`)
    try {
      const r = await call<any>('svc.act', { id: it.id, action }, 60)
      if (r?.ok === false) throw new Error(r?.error || '操作失败')
      setMsg({ kind: 'ok', text: `${it.name} ${action} 成功` })
      load()
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '操作失败' })
    } finally { setBusy('') }
  }

  async function doCompile() {
    const it = compiling
    if (!it) return
    setBusy(`compile:${it.id}`)
    setMsg({ kind: 'ok', text: `已提交编译安装任务：${it.name}（源码编译约几分钟，进度见任务中心）` })
    setLog('')
    try {
      const r = await call<any>('svc.compile', { id: it.id }, 30)
      const taskId = r?.task_id
      if (!taskId) throw new Error(r?.error || '提交失败')
      for (;;) {
        await new Promise((res) => setTimeout(res, 3000))
        const t = await call<any>('task.get', { id: taskId }, 20).catch(() => null)
        if (!t) continue
        setLog((t?.log || []).join('\n'))
        if (t?.state === 'done' || t?.state === 'failed') {
          const res = t?.result || {}
          setMsg(res.ok ? { kind: 'ok', text: `${it.name} 编译安装完成（${res.prefix || ''}，单元 ${res.unit || ''}）` }
                        : { kind: 'err', text: res.error || t?.error || '编译失败' })
          break
        }
      }
      load()
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '编译安装失败' })
    } finally { setBusy(''); setCompiling(null) }
  }

  async function doUninstall() {
    const it = uninstalling
    if (!it) return
    setBusy(`uninstall:${it.id}`)
    try {
      const r = await call<any>('svc.uninstall', { id: it.id }, 600)
      if (r?.ok) {
        setMsg({ kind: 'ok', text: `已卸载 ${it.name}（${(r.removed_pkgs || []).join(', ')}）` })
      } else {
        setMsg({ kind: 'err', text: r?.error || '卸载失败' })
      }
      load()
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '卸载失败' })
    } finally { setBusy(''); setUninstalling(null) }
  }

  async function doInstall() {
    const it = installing
    if (!it) return
    setBusy(`install:${it.id}`)
    setMsg({ kind: 'ok', text: `已提交安装任务：${it.name}…` })
    setLog('')
    try {
      const r = await call<any>('svc.install', { id: it.id }, 30)
      if (r?.ok === false) throw new Error(r?.error || '提交失败')
      const taskId = r?.task_id
      if (!taskId) {
        // 无任务队列的旧版本退回了同步结果
        setMsg(r?.ok ? { kind: 'ok', text: `${it.name} 安装完成` }
                     : { kind: 'err', text: r?.error || '安装失败' })
        setLog(r?.install_log || '')
        load()
        return
      }
      // 轮询任务直到结束（安装在这台节点的队列里，走同一条命令通道）；上限 10 分钟
      let waited = 0
      for (;;) {
        await new Promise((res) => setTimeout(res, 2000))
        waited += 2000
        const t = await call<any>('task.get', { id: taskId }, 20).catch(() => null)
        if (!t) {
          if (waited > 20000) throw new Error('该节点的 monitor/services 扩展过旧，查询不到任务状态')
          continue
        }
        setLog((t?.log || []).join('\n'))
        if (t?.state === 'done') {
          const res = t?.result || {}
          setMsg(res.ok
            ? { kind: 'ok', text: `${it.name} 安装完成（${res.pkg}，已校验）` }
            : { kind: 'err', text: res.error || '安装失败' })
          break
        }
        if (t?.state === 'failed') {
          setMsg({ kind: 'err', text: t?.error || '安装任务失败' })
          break
        }
        setMsg({ kind: 'ok', text: `正在安装 ${it.name}…（${(t?.log || []).length} 条进度）` })
      }
      load()
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '安装失败' })
    } finally { setBusy(''); setInstalling(null) }
  }

  return (
    <>
      <PageHeader title="服务" sub={<>
        节点 <span className="mono" style={{ color: 'var(--accent)' }}>{name}</span>
        {' · '}{osInfo?.pretty || '系统识别中'}
        {' · 包管理器 '}
        <b className="mono">{pkg?.backend || '…'}</b>
        {pkg?.family ? <span className="mono"> ({pkg.family})</span> : null}
      </>} />

      {/* 兼容性 WARN：来自后端真实检查（未识别发行版 / 无包管理器 / 旧版 Windows），
          有一条亮一条，绝不静默降级 */}
      {warns.length > 0 && (
        <div style={{
          marginBottom: 10, padding: '9px 12px', fontSize: 12.5, lineHeight: 1.8,
          borderRadius: 'var(--radius-sm)', wordBreak: 'break-all',
          color: 'var(--warn)', background: 'var(--warn-soft)',
          border: '1px solid rgba(251, 191, 36, .35)',
        }}>
          {warns.map((w, i) => <div key={i}>⚠ {w}</div>)}
        </div>
      )}

      {msg && (
        <div style={{
          marginBottom: 10, padding: '8px 12px', fontSize: 12.5,
          borderRadius: 'var(--radius-sm)', wordBreak: 'break-all',
          color: msg.kind === 'ok' ? 'var(--ok)' : 'var(--danger)',
          background: msg.kind === 'ok' ? 'var(--ok-soft)' : 'var(--danger-soft)',
        }}>{msg.text}</div>
      )}
      {log && (
        <pre className="mono" style={{
          margin: '0 0 12px', padding: 12, fontSize: 11.5, maxHeight: 200,
          overflow: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-all',
          background: 'var(--bg-elev)', border: '1px solid var(--border)',
          borderRadius: 'var(--radius-sm)', color: 'var(--text-dim)',
        }}>{log}</pre>
      )}

      <Toolbar left={null} right={<>
        <LiveTag at={at} enabled={auto} intervalMs={MS} onToggle={() => setAuto((v) => !v)} />
        <button className="btn btn-sm" disabled={busy === 'list'} onClick={() => load()}>刷新</button>
      </>} />

      <Collapse title="服务清单" count={items?.length ? String(items.length) : '—'}
                defaultOpen storageKey="services.list">
        {!items ? (
          <div className="empty" style={{ padding: 14 }}><span className="spinner" /> 探测中…</div>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>服务</th>
                <th style={{ width: 110 }}>角色</th>
                <th style={{ width: 120 }}>版本</th>
                <th style={{ width: 130 }}>服务单元</th>
                <th style={{ width: 90 }}>状态</th>
                <th style={{ width: 200, textAlign: 'right' }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((it) => {
                const b = STATE_BADGE[it.state] || STATE_BADGE.missing
                return (
                  <tr key={it.id}>
                    <td>
                      <span className="mono cell-main">{it.name}</span>
                      <div className="mono" style={{ fontSize: 11, color: 'var(--text-mute)' }}>
                        {it.exe || '—'}
                      </div>
                      {it.exe && (
                        <div style={{ fontSize: 11, color: 'var(--text-mute)' }}>
                          {it.pkg_owner
                            ? <>来自包 <span className="mono">{it.pkg_owner}</span></>
                            : '非包管理器安装（源码/手动）'}
                        </div>
                      )}
                    </td>
                    <td style={{ color: 'var(--text-dim)', fontSize: 12 }}>{it.role}</td>
                    <td className="mono" style={{ color: 'var(--text-dim)' }}>{it.version || '—'}</td>
                    <td className="mono" style={{ color: 'var(--text-mute)' }}>{it.unit || '—'}</td>
                    <td><Badge tone={b.tone}>{b.text}</Badge></td>
                    <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                      {it.state === 'missing' ? (
                        <>
                          {it.installable && (
                            <button className="btn-text" disabled={!!busy}
                                    onClick={() => setInstalling(it)}>安装</button>
                          )}
                          {it.id === 'nginx' && (
                            <button className="btn-text dim" disabled={!!busy}
                                    title="源码编译到 /usr/local/nginx（约几分钟）"
                                    onClick={() => setCompiling(it)}>编译安装</button>
                          )}
                        </>
                      ) : it.state === 'bare' ? (
                        <span style={{ color: 'var(--text-mute)', fontSize: 11.5 }}
                              title="有二进制但未注册为系统服务（可能手工/docker 在跑），无法通过服务管理器启停">
                          状态未知
                        </span>
                      ) : (
                        <>
                          {it.state === 'stopped' && (
                            <button className="btn-text" disabled={!!busy}
                                    onClick={() => act(it, 'start')}>启动</button>
                          )}
                          {it.state === 'running' && (
                            <button className="btn-text" disabled={!!busy}
                                    onClick={() => act(it, 'stop')}>停止</button>
                          )}
                          <button className="btn-text dim" disabled={!!busy}
                                  onClick={() => act(it, 'restart')}>重启</button>
                          <button className="btn-text danger" disabled={!!busy}
                                  onClick={() => setUninstalling(it)}>卸载</button>
                        </>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </Collapse>

      {uninstalling && (
        <ConfirmModal title={`卸载 ${uninstalling.name}`} busy={!!busy}
                      onConfirm={doUninstall} onClose={() => setUninstalling(null)}
                      text={<>将停止服务并通过包管理器卸载 <b className="mono">{uninstalling.name}</b>
                        （保留 /etc 配置）。源码/手动安装的会被如实拒绝。继续？</>} />
      )}

      {compiling && (
        <ConfirmModal title={`编译安装 ${compiling.name}`} busy={!!busy}
                      onConfirm={doCompile} onClose={() => setCompiling(null)}
                      text={<>将从 <b className="mono">nginx.org</b> 下载源码编译到
                        <b className="mono"> /usr/local/nginx</b>（配置复用 /etc/nginx/nginx.conf），
                        全程约几分钟，进度见任务中心。要求未安装包管理器版 nginx。继续？</>} />
      )}

      {installing && (
        <ConfirmModal title={`安装 ${installing.name}`} busy={!!busy}
                      onConfirm={doInstall} onClose={() => setInstalling(null)}
                      text={<>将通过系统包管理器安装 <b className="mono">{installing.name}</b>，
                        安装期间面板可以继续使用但不能再发起其它安装。继续？</>} />
      )}
    </>
  )
}
