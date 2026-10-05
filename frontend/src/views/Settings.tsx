import { useCallback, useEffect, useState } from 'react'
import { PageHeader, Field, Badge, Modal } from '../components/ui'
import { api, type ExtensionRec } from '../api'

/* ============================================================
   设置 —— 内核配置的读写入口。

   写入是**文本级 patch**：只改 config.yaml 里对应键的值，注释与排版原样保留
   （运维自己的配置文件不能被程序重排成机器格式）。

   端口 / 监听地址 / 数据库这类改动**不会自动重启**内核 —— 界面上明确标出
   「需重启生效」，重启动作交给运维，不替用户决定。
   ============================================================ */

/** 本页管理的配置项（点路径 → 控件）。这是内核级/服务级配置的白名单视图 */
const FIELDS: {
  group: string
  hint?: string
  items: { path: string; label: string; hint?: string; type: 'text' | 'number' | 'bool' | 'choice' | 'list'
           choices?: string[]; restart?: boolean }[]
}[] = [
  {
    group: '常规',
    items: [
      { path: 'project.name', label: '面板名称', type: 'text' },
      { path: 'log.level', label: '日志级别', type: 'choice', choices: ['DEBUG', 'INFO', 'WARNING', 'ERROR'] },
    ],
  },
  {
    group: 'Web API',
    hint: '改完需重启内核',
    items: [
      { path: 'api.host', label: '监听地址', hint: '127.0.0.1 仅本机；0.0.0.0 对外（注意安全）', type: 'text', restart: true },
      { path: 'api.port', label: '端口', type: 'number', restart: true },
      { path: 'api.session_timeout', label: '会话有效期', hint: '秒；0 = 不过期', type: 'number' },
    ],
  },
  {
    group: '安全',
    hint: '改完需重启内核',
    items: [
      { path: 'security.encrypted', label: '加密通讯 (RSA)', hint: '关闭时走 Token 校验', type: 'bool', restart: true },
      { path: 'security.token', label: '访问 Token', hint: '留空 = 不强制校验；已设置时显示 *** （不填即不改）', type: 'text', restart: true },
    ],
  },
  {
    group: '文件管理',
    items: [
      { path: 'files.roots', label: '允许访问的根目录', hint: '逗号分隔；留空 = 所有盘符 / 根', type: 'list' },
    ],
  },
  {
    group: '终端',
    items: [
      { path: 'terminal.timeout', label: '单条命令超时', hint: '秒，超时杀进程树', type: 'number' },
    ],
  },
]

export default function Settings() {
  const [cfg, setCfg] = useState<any>(null)
  const [path, setPath] = useState('')
  const [draft, setDraft] = useState<Record<string, any>>({})
  const [exts, setExts] = useState<ExtensionRec[]>([])
  const [pwd, setPwd] = useState(false)
  const [tokens, setTokens] = useState<any[]>([])
  const [newToken, setNewToken] = useState<any>(null)
  const [tokenName, setTokenName] = useState<string | null>(null)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    try {
      const r = await api.config()
      setCfg(r.config); setPath(r.path); setDraft({})
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '读取配置失败' }) }
    try { setExts((await api.extensions()).extensions) } catch { /* 扩展清单可选 */ }
    try { setTokens((await api.apiTokens()).tokens || []) } catch { /* 令牌接口可选 */ }
  }, [])
  useEffect(() => { load() }, [load])

  /** 取配置里的值（点路径）；已改过的用草稿值 */
  const val = (p: string) => {
    if (p in draft) return draft[p]
    return p.split('.').reduce((o: any, k) => (o == null ? o : o[k]), cfg)
  }
  const setVal = (p: string, v: any) => setDraft((d) => ({ ...d, [p]: v }))
  const dirty = Object.keys(draft).length > 0

  async function save() {
    setBusy('save')
    try {
      const r = await api.saveConfig(draft)
      const miss = r.missing || []
      setMsg(miss.length
        ? { kind: 'err', text: `已保存 ${Object.keys(r.applied).length} 项；以下键在配置文件中不存在：${miss.join('、')}` }
        : { kind: 'ok', text: `已写入 config.yaml（${Object.keys(r.applied).length} 项）· ${r.restart_required ? '端口/监听类改动需重启内核生效' : ''}` })
      await load()
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '保存失败' }) }
    finally { setBusy('') }
  }

  async function toggleExt(e: ExtensionRec) {
    try {
      await api.toggleExtension(e.id, !e.enabled)
      setExts((list) => list.map((x) => (x.id === e.id ? { ...x, enabled: !x.enabled } : x)))
      setMsg({ kind: 'ok', text: `扩展「${e.name}」已${e.enabled ? '停用' : '启用'}，重启内核后生效` })
    } catch (err: any) { setMsg({ kind: 'err', text: err?.message || '操作失败' }) }
  }

  return (
    <>
      <PageHeader title="设置" sub={`内核配置读写 · 写回方式为文本级 patch，注释与排版保留 · ${path || ''}`}
        actions={<>
          <button className="btn" onClick={load}>重新读取</button>
          <button className="btn btn-primary" disabled={!dirty || busy === 'save'} onClick={save}>
            {busy === 'save' ? '保存中…' : dirty ? `保存 ${Object.keys(draft).length} 项` : '保存'}</button>
        </>} />

      {msg && <div className={msg.kind === 'ok' ? 'msg-ok' : 'msg-err'}
        style={{ marginBottom: 12, fontSize: 13, wordBreak: 'break-all' }}>{msg.text}</div>}

      {!cfg ? <div className="card" style={{ padding: 16, color: 'var(--text-dim)', fontSize: 13 }}>读取中…</div> : (
        FIELDS.map((g) => (
          <div className="card" key={g.group} style={{ padding: 16, marginBottom: 12 }}>
            <div style={{ fontSize: 13.5, fontWeight: 600, marginBottom: 2 }}>{g.group}</div>
            {g.hint && <div style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 10 }}>{g.hint}</div>}
            {g.items.map((it) => {
              const v = val(it.path)
              const changed = it.path in draft
              return (
                <Field key={it.path} label={it.label}
                  hint={[it.hint, it.restart ? '需重启生效' : ''].filter(Boolean).join(' · ')}>
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                    {it.type === 'bool' ? (
                      <label style={{ fontSize: 13, display: 'flex', gap: 8, alignItems: 'center' }}>
                        <input type="checkbox" checked={!!v} onChange={(e) => setVal(it.path, e.target.checked)} />
                        {v ? '已启用' : '已关闭'}
                      </label>
                    ) : it.type === 'choice' ? (
                      <select className="input" value={v ?? ''} onChange={(e) => setVal(it.path, e.target.value)}
                        style={{ maxWidth: 220 }}>
                        {it.choices!.map((c) => <option key={c} value={c}>{c}</option>)}
                      </select>
                    ) : it.type === 'number' ? (
                      <input className="input" type="number" style={{ maxWidth: 220 }} value={v ?? ''}
                        onChange={(e) => setVal(it.path, Number(e.target.value))} />
                    ) : it.type === 'list' ? (
                      <input className="input mono" style={{ flex: 1 }}
                        value={Array.isArray(v) ? v.join(', ') : (v ?? '')}
                        placeholder="留空 = 不限制"
                        onChange={(e) => setVal(it.path,
                          e.target.value.split(',').map((s) => s.trim()).filter(Boolean))} />
                    ) : (
                      <input className="input" style={{ flex: 1 }} value={v ?? ''}
                        onChange={(e) => setVal(it.path, e.target.value)} />
                    )}
                    {changed && <Badge tone="warn">已改</Badge>}
                  </div>
                </Field>
              )
            })}
          </div>
        ))
      )}

      {/* 官方扩展开关 */}
      <div className="card" style={{ padding: 16, marginBottom: 12 }}>
        <div style={{ fontSize: 13.5, fontWeight: 600, marginBottom: 10 }}>
          官方扩展 <span style={{ color: 'var(--text-dim)', fontWeight: 400, fontSize: 12 }}>
            （{exts.filter((e) => e.enabled).length}/{exts.length} 已启用 · 开关写入 extensions.yaml，重启生效）</span>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: 10 }}>
          {exts.map((e) => (
            <div key={e.id} style={{
              display: 'flex', gap: 10, alignItems: 'flex-start', padding: '9px 11px',
              border: '1px solid var(--border)', borderRadius: 'var(--radius-sm)',
            }}>
              <input type="checkbox" checked={e.enabled} onChange={() => toggleExt(e)}
                style={{ marginTop: 3 }} />
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 13, fontWeight: 500 }}>
                  {e.name} <span className="mono" style={{ fontSize: 11, color: 'var(--text-dim)' }}>{e.id} {e.version}</span>
                </div>
                <div style={{ fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.6 }}>{e.description}</div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* 账号与令牌 */}
      <div className="card" style={{ padding: 16, marginBottom: 12 }}>
        <div style={{ fontSize: 13.5, fontWeight: 600, marginBottom: 10 }}>账号与访问令牌</div>
        <div style={{ display: 'flex', gap: 10, marginBottom: 12 }}>
          <button className="btn" onClick={() => setPwd(true)}>修改密码</button>
          <button className="btn" onClick={() => setTokenName('')}>新建令牌</button>
        </div>
        {tokens.length === 0
          ? <div style={{ fontSize: 12.5, color: 'var(--text-dim)' }}>暂无令牌（浏览器端登录用的是会话 Token，此处是给外部程序用的长期令牌）</div>
          : (
            <table className="table">
              <thead><tr><th>名称</th><th style={{ width: 90 }}>角色</th><th style={{ width: 180 }}>创建时间</th><th style={{ width: 90 }}>操作</th></tr></thead>
              <tbody>
                {tokens.map((t: any) => (
                  <tr key={t.id}>
                    <td className="cell-main">{t.name}</td>
                    <td><Badge tone={t.role === 'admin' ? 'info' : 'muted'}>{t.role}</Badge></td>
                    <td style={{ fontSize: 12.5 }}>{t.created_at || '—'}</td>
                    <td>
                      <button className="btn btn-sm btn-danger" onClick={async () => {
                        try { await api.revokeApiToken(t.id); load() }
                        catch (e: any) { setMsg({ kind: 'err', text: e?.message || '吊销失败' }) }
                      }}>吊销</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
      </div>

      {pwd && <PasswordModal onClose={() => setPwd(false)} />}

      {tokenName !== null && (
        <TokenModal name={tokenName} setName={setTokenName}
          onCreated={(r) => { setNewToken(r); setTokenName(null); load() }}
          onError={(t) => { setMsg({ kind: 'err', text: t }); setTokenName(null) }} />
      )}

      {newToken && (
        <Modal title="令牌已创建" onClose={() => setNewToken(null)} width={560}
          footer={<button className="btn btn-primary" onClick={() => setNewToken(null)}>我已保存</button>}>
          <div style={{ fontSize: 13, lineHeight: 1.8, marginBottom: 8 }}>
            <Badge tone="warn">仅此一次显示</Badge> 关闭后无法再查看，请立即复制保存。
          </div>
          <pre className="mono" style={{ fontSize: 12, padding: 10, background: 'var(--bg-elev)',
            borderRadius: 'var(--radius-sm)', whiteSpace: 'pre-wrap', wordBreak: 'break-all', margin: 0 }}>
            {newToken.token}
          </pre>
        </Modal>
      )}
    </>
  )
}

function TokenModal({ name, setName, onCreated, onError }: {
  name: string; setName: (s: string) => void
  onCreated: (r: any) => void; onError: (t: string) => void
}) {
  const [busy, setBusy] = useState(false)
  async function go() {
    setBusy(true)
    try { onCreated(await api.createApiToken(name.trim() || 'default')) }
    catch (e: any) { onError(e?.message || '创建失败') }
    finally { setBusy(false) }
  }
  return (
    <Modal title="新建访问令牌" onClose={() => setName('')} width={440}
      footer={<>
        <button className="btn" onClick={() => setName('')}>取消</button>
        <button className="btn btn-primary" disabled={busy} onClick={go}>
          {busy ? '创建中…' : '创建'}</button>
      </>}>
      <Field label="令牌名称" hint="给谁用的（如 ci-deploy）">
        <input className="input" value={name} onChange={(e) => setName(e.target.value)}
          placeholder="default" autoFocus
          onKeyDown={(e) => { if (e.key === 'Enter') go() }} />
      </Field>
      <div style={{ fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.7 }}>
        明文只在创建时返回一次，之后只存哈希；持有者等同管理员，请妥善保管。
      </div>
    </Modal>
  )
}

function PasswordModal({ onClose }: { onClose: () => void }) {
  const [f, setF] = useState({ old: '', a: '', b: '' })
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  async function go() {
    if (f.a !== f.b) { setErr('两次输入的新密码不一致'); return }
    setBusy(true); setErr('')
    try { await api.changePassword(f.old, f.a); onClose() }
    catch (e: any) { setErr(e?.message || '修改失败') }
    finally { setBusy(false) }
  }
  return (
    <Modal title="修改密码" onClose={onClose} width={420}
      footer={<>
        <button className="btn" onClick={onClose}>取消</button>
        <button className="btn btn-primary" disabled={busy || !f.old || !f.a} onClick={go}>
          {busy ? '提交中…' : '确认修改'}</button>
      </>}>
      {err && <div className="msg-err" style={{ marginBottom: 10, fontSize: 13 }}>{err}</div>}
      <Field label="当前密码">
        <input className="input" type="password" value={f.old} onChange={(e) => setF({ ...f, old: e.target.value })} />
      </Field>
      <Field label="新密码">
        <input className="input" type="password" value={f.a} onChange={(e) => setF({ ...f, a: e.target.value })} />
      </Field>
      <Field label="确认新密码">
        <input className="input" type="password" value={f.b} onChange={(e) => setF({ ...f, b: e.target.value })} />
      </Field>
    </Modal>
  )
}
