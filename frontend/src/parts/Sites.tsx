import { useCallback, useEffect, useState } from 'react'
import { useNode } from '../node'
import { PageHeader } from '../components/ui'
import Collapse, { LiveTag, usePoll } from '../components/Live'

const MS = 5000

interface Site {
  id: string; name: string; domains: string[]; kind: string
  root_dir?: string; target?: string; index?: string
  enable_ssl?: boolean; remark?: string
  php_version?: string; php_fastcgi?: string; php_socket?: string
}
interface PhpVer { version: string; ver: string; layer: string; layer_title: string; socket: string }

const KIND_LABEL: Record<string, string> = {
  static: '静态', php: 'PHP', proxy: '反代',
}

/** 站点托管：面板只**生成并下发 Nginx 配置**，自己不当 Web 服务器。 */
export default function Sites({ nodeName }: { nodeName?: string }) {
  const { current, call } = useNode()
  const name = nodeName || current

  const [sites, setSites] = useState<Site[]>([])
  const [st, setSt] = useState<any>(null)
  const [preview, setPreview] = useState<{ id: string; text: string } | null>(null)
  const [at, setAt] = useState(0)
  const [auto, setAuto] = useState(true)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)

  const load = useCallback(async () => {
    try {
      const d = await call<any>('sites.list', {}, 15)
      setSites(d.sites || [])
      setAt(Date.now())
      const s = await call<any>('sites.status', {}, 15).catch(() => null)
      setSt(s)
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '读取站点失败' })
    }
  }, [call])

  usePoll(load, MS, auto)

  async function remove(s: Site) {
    if (!confirm(`删除站点「${s.name}」？其 Nginx 配置会被回收（人手写的配置不受影响）。`)) return
    try {
      await call('sites.remove', { id: s.id }, 20)
      setMsg({ kind: 'ok', text: `已删除 ${s.name}` })
      load()
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '删除失败' }) }
  }

  async function showConf(s: Site) {
    try {
      // 真实预览：走节点命令 sites.render（与下发用同一套渲染参数）
      const d = await call<any>('sites.render', { id: s.id }, 15)
      setPreview({ id: s.id, text: d.nginx_conf || '# 无配置' })
    } catch {
      setPreview({ id: s.id, text: '# 预览失败' })
    }
  }

  const backend = st?.backend || '—'

  const wsActive = st?.webserver?.active || null
  const wsAvailable: any[] = st?.webserver?.available || []

  return (
    <>
      <PageHeader title="站点" sub={<>
        节点 <span className="mono" style={{ color: 'var(--accent)' }}>{name}</span>
        {' '} · 面板生成并下发配置给当前引擎，自己不做 Web 服务器
      </>} />

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
        <div style={{ flex: 1, fontSize: 12, color: 'var(--text-mute)' }}>
          引擎 <span className="mono" style={{ color: 'var(--text-dim)' }}>{backend}</span>
          {wsActive
            ? ` · ${wsActive.name} ${wsActive.version || ''}`
            : ' · 未检测到引擎：配置照常生成，待部署'}
          {wsAvailable.length > 1 && <> · 可用: <span className="mono">{wsAvailable.map((x: any) => x.id).join(', ')}</span></>}
          {st?.conf_dir && <> · 配置目录 <span className="mono">{st.conf_dir}</span></>}
        </div>
        <LiveTag at={at} enabled={auto} intervalMs={MS} onToggle={() => setAuto((v) => !v)} />
        <button className="btn btn-sm" onClick={load}>刷新</button>
      </div>

      {msg && (
        <div style={{
          marginBottom: 12, padding: '8px 12px', fontSize: 12.5,
          borderRadius: 'var(--radius-sm)',
          color: msg.kind === 'ok' ? 'var(--ok)' : 'var(--danger)',
          background: msg.kind === 'ok' ? 'var(--ok-soft)' : 'var(--danger-soft)',
        }}>{msg.text}</div>
      )}

      <Collapse title="新建站点" defaultOpen={false} storageKey={'sites.new.' + name}>
        <CreateForm onDone={(t) => { setMsg({ kind: 'ok', text: t }); load() }}
          onErr={(t) => setMsg({ kind: 'err', text: t })} />
      </Collapse>

      <div style={{ height: 12 }} />

      <Collapse title="站点" count={sites.length} defaultOpen storageKey={'sites.list.' + name}>
        {sites.length === 0 ? (
          <div className="empty" style={{ padding: '20px 0' }}>
            暂无站点 —— 展开上方「新建站点」
          </div>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>站点</th>
                <th style={{ width: 70 }}>方式</th>
                <th>根目录 / 上游</th>
                <th style={{ width: 190 }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {sites.map((s) => (
                <tr key={s.id}>
                  <td>
                    <div style={{ color: 'var(--text)' }}>{s.name}</div>
                    <div className="mono" style={{ fontSize: 11, color: 'var(--text-mute)' }}>
                      {(s.domains || []).join(', ') || '(无域名)'}
                    </div>
                  </td>
                  <td>
                    <span className="tag">{KIND_LABEL[s.kind] || s.kind}</span>
                    {s.kind === 'php' && s.php_version &&
                      <span className="tag tag-accent" style={{ marginLeft: 4 }}>PHP {s.php_version}</span>}
                  </td>
                  <td className="mono" style={{ fontSize: 11.5, color: 'var(--text-dim)' }}>
                    {s.kind === 'proxy'
                      ? (s.target || '—')
                      : (s.root_dir || '—')}
                    {s.kind === 'php' && s.php_socket &&
                      <div style={{ color: 'var(--text-mute)' }}>↳ {s.php_socket}</div>}
                  </td>
                  <td>
                    <button className="btn btn-sm btn-ghost" onClick={() => showConf(s)}>看配置</button>
                    <button className="btn-text danger" onClick={() => remove(s)}>删除</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Collapse>

      {preview && (
        <div style={{ height: 12 }}>
          <Collapse title={`Nginx 配置预览 · ${preview.id}`} defaultOpen
            storageKey={'sites.preview.' + name}>
            <pre className="mono" style={{
              margin: 0, fontSize: 11.5, lineHeight: 1.7, whiteSpace: 'pre-wrap',
              color: 'var(--text-dim)', background: 'var(--bg-elev)',
              border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)', padding: 12,
            }}>{preview.text}</pre>
            <button className="btn btn-sm btn-ghost" style={{ marginTop: 8 }}
              onClick={() => setPreview(null)}>关闭</button>
          </Collapse>
        </div>
      )}
    </>
  )
}

function CreateForm({ onDone, onErr }: { onDone: (t: string) => void; onErr: (t: string) => void }) {
  const { call } = useNode()
  const [name, setName] = useState('')
  const [domains, setDomains] = useState('')
  const [kind, setKind] = useState('static')
  const [rootDir, setRootDir] = useState('')
  const [target, setTarget] = useState('')
  const [phpVersion, setPhpVersion] = useState('')      // '' = 默认（自动）
  const [phpVersions, setPhpVersions] = useState<PhpVer[]>([])
  const [busy, setBusy] = useState(false)

  // 选 PHP 时加载本机 PHP 版本清单（下拉用）
  useEffect(() => {
    if (kind !== 'php') return
    let alive = true
    call<any>('sites.php-versions', {}, 15).then((d) => {
      if (alive) setPhpVersions(d?.versions || [])
    }).catch(() => { if (alive) setPhpVersions([]) })
    return () => { alive = false }
  }, [kind, call])

  const selVer = phpVersions.find((v) => v.version === phpVersion)

  async function submit() {
    if (!name.trim()) { onErr('站点名不能为空'); return }
    if (!domains.trim()) { onErr('至少填一个域名'); return }
    if (kind !== 'proxy' && !rootDir.trim()) { onErr('静态/PHP 站点需要根目录'); return }
    if (kind === 'proxy' && !target.trim()) { onErr('反代站点需要上游 host:port'); return }
    setBusy(true)
    try {
      await call('sites.create', {
        name: name.trim(),
        domains: domains.split(/[,\s;]+/).filter(Boolean),
        kind, root_dir: rootDir.trim(), target: target.trim(),
        php_version: kind === 'php' ? phpVersion : '',
      }, 25)
      onDone(`已创建站点 ${name}`)
      setName(''); setDomains(''); setRootDir(''); setTarget(''); setPhpVersion('')
    } catch (e: any) {
      onErr(e?.message || '创建失败')
    } finally { setBusy(false) }
  }

  return (
    <div className="grid grid-2" style={{ gap: 10 }}>
      <F label="站点名"><input className="input" value={name} placeholder="my-blog"
        onChange={(e) => setName(e.target.value)} /></F>
      <F label="域名（逗号分隔）"><input className="input" value={domains} placeholder="a.com, *.a.com"
        onChange={(e) => setDomains(e.target.value)} /></F>
      <F label="方式">
        <select className="input" value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="static">静态</option>
          <option value="php">PHP</option>
          <option value="proxy">反向代理</option>
        </select>
      </F>
      {kind === 'proxy' ? (
        <F label="上游 host:port"><input className="input" value={target} placeholder="127.0.0.1:8080"
          onChange={(e) => setTarget(e.target.value)} /></F>
      ) : (
        <F label="根目录"><input className="input" value={rootDir} placeholder="/www/wwwroot/a.com"
          onChange={(e) => setRootDir(e.target.value)} /></F>
      )}

      {kind === 'php' && (
        <F label="PHP 版本">
          <select className="input" value={phpVersion}
            onChange={(e) => setPhpVersion(e.target.value)}>
            <option value="">默认（自动 · 沿用 127.0.0.1:9000）</option>
            {phpVersions.map((v) => (
              <option key={v.version} value={v.version}>
                {v.version} · {v.layer_title}
              </option>
            ))}
          </select>
          {phpVersions.length === 0 && (
            <div style={{ fontSize: 11, color: 'var(--text-mute)', marginTop: 4 }}>
              未检测到本机 PHP 版本 —— 仍可建站，配置按所选版本约定端点生成
            </div>
          )}
          {selVer && (
            <div style={{ fontSize: 11, color: 'var(--text-mute)', marginTop: 4 }}>
              fastcgi 端点 <span className="mono">{selVer.socket}</span>
            </div>
          )}
        </F>
      )}

      <div style={{ gridColumn: '1 / -1', display: 'flex', justifyContent: 'flex-end' }}>
        <button className="btn btn-primary" disabled={busy} onClick={submit}>
          {busy ? '创建中…' : '创建'}
        </button>
      </div>
    </div>
  )
}

function F({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: 'block' }}>
      <div style={{ fontSize: 11.5, color: 'var(--text-mute)', marginBottom: 4 }}>{label}</div>
      {children}
    </label>
  )
}
