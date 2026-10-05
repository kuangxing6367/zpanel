import { useCallback, useState } from 'react'
import { useNode } from '../node'
import { Badge, PageHeader, Toolbar } from '../components/ui'
import Collapse, { LiveTag, usePoll } from '../components/Live'

const MS = 30000

/** 节点的运行环境：装了哪些运行时、各是哪个版本、**每个版本归在哪一层兼容层**、
 *  这个版本要注意什么。取数一律走 scope.call（localhost 只是其中一个节点）。 */
export default function Environment({ nodeName }: { nodeName?: string }) {
  const { current, call, node } = useNode()
  const name = nodeName || current

  const [env, setEnv] = useState<any>(null)
  const [cat, setCat] = useState<any>(null)
  const [at, setAt] = useState(0)
  const [auto, setAuto] = useState(true)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const load = useCallback(async () => {
    setBusy(true)
    try {
      const [e, c] = await Promise.all([
        call<any>('runtime.env', {}, 90).catch(() => null),
        call<any>('runtime.compat', {}, 30).catch(() => null),
      ])
      setEnv(e); setCat(c); setAt(Date.now()); setErr('')
    } catch (e2: any) {
      setErr(e2?.message || '读取失败')
    } finally { setBusy(false) }
  }, [call])

  usePoll(load, MS, auto)

  const items: any[] = env?.items || []
  const installed = items.filter((x) => x.available)
  const missing = items.filter((x) => !x.available)

  return (
    <>
      <PageHeader
        title="运行环境"
        sub={<>节点 <b className="mono">{name}</b> 的运行时与版本兼容层</>}
        actions={
          <Toolbar right={
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <LiveTag at={at} enabled={auto} busy={busy}
                intervalMs={MS} onToggle={() => setAuto((v) => !v)} />
              <button className="btn btn-sm btn-ghost" onClick={() => void load()}>刷新</button>
            </div>
          } />
        }
      />

      {err && (
        <div className="card" style={{ borderColor: 'var(--danger)', marginBottom: 12 }}>
          <span style={{ color: 'var(--danger)', fontSize: 12.5 }}>{err}</span>
        </div>
      )}

      {!env && !err && <div className="empty-state"><div className="empty-title">正在探测…</div></div>}

      {env && (
        <>
          <div className="env-summary">
            <div className="es-item">
              <span className="es-num" style={{ color: 'var(--ok)' }}>{installed.length}</span>
              <span className="es-lbl"><span className="es-dot" style={{ background: 'var(--ok)' }} />已安装</span>
            </div>
            <div className="es-sep" />
            <div className="es-item">
              <span className="es-num" style={{ color: 'var(--text-mute)' }}>{missing.length}</span>
              <span className="es-lbl">未安装</span>
            </div>
            <div className="es-sep" />
            <div className="es-item">
              <span className="es-num">{items.reduce((s: number, x: any) => s + (x.installed || 0), 0)}</span>
              <span className="es-lbl">已发现版本总数</span>
            </div>
            <div className="es-sep" />
            <div className="es-item" style={{ flex: 1, minWidth: 160 }}>
              <span className="es-lbl">面板能识别的版本兼容层</span>
              <span style={{ fontSize: 12.5, color: 'var(--text-dim)' }}>
                {cat?.layers?.length ? `${cat.layers.length} 层 · 覆盖 ${cat.families?.length || 0} 个家族` : '—'}
              </span>
            </div>
          </div>

          <div className="grid grid-env">
            {items.map((rt) => <RuntimeCard key={rt.kind} rt={rt} />)}
          </div>

          {cat?.layers?.length ? <Catalogue cat={cat} /> : null}
        </>
      )}
    </>
  )
}

/* ── 单个运行时 ───────────────────────────────────────── */
function RuntimeCard({ rt }: { rt: any }) {
  const cx = rt.compat || {}
  const vers: any[] = rt.versions || []
  const more = vers.filter((v) => (v.version || '') !== (rt.version || ''))
  const ok = !!rt.available

  return (
    <div className={'env-card' + (ok ? '' : ' miss')}>
      <div className="env-card-top">
        <span className="env-card-name">{rt.label}</span>
        {ok ? <Badge tone="ok">已安装</Badge> : <Badge tone="muted">未安装</Badge>}
      </div>

      {ok ? (
        <div className="env-card-ver">{rt.version}</div>
      ) : (
        <div className="env-card-ver none">未检测到</div>
      )}

      <div className="env-card-meta">
        {cx.layer && ok && (
          <span className="tag tag-accent">{cx.title || cx.layer}</span>
        )}
        {rt.installed > 1 && (
          <span className="env-card-count">{rt.installed} 个版本并存</span>
        )}
      </div>

      {(rt.checks || []).length > 0 && (
        <div className="env-card-checks">
          {(rt.checks as any[]).map((c, i) => (
            <div key={i} className="env-card-check">
              <span className={'dot ' + (c.ok ? 'dot-ok' : 'dot-bad')}
                style={{ flex: '0 0 auto', position: 'relative', top: 5 }} />
              <span className="ck-name">{c.name}</span>
              {c.detail ? <span className="ck-detail">{c.detail}</span> : null}
            </div>
          ))}
        </div>
      )}

      {ok && (cx.traits || []).length > 0 && (
        <div className="env-card-note">
          <div className="en-head">
            这个版本要注意
            {cx.compared_to ? <span style={{ color: 'var(--text-mute)', fontWeight: 400 }}> · 相对 {cx.compared_to}</span> : null}
          </div>
          <ul>
            {(cx.traits as string[]).map((t, i) => <li key={i}>{t}</li>)}
          </ul>
        </div>
      )}

      {more.length > 0 && (
        <div style={{ marginTop: 11 }}>
          <Collapse title="机器上的其它版本" count={more.length}
            storageKey={'env-vers-' + rt.kind} defaultOpen={false}>
            <div style={{ display: 'grid', gap: 5 }}>
              {(more as any[]).map((v, i) => (
                <div key={i} style={{ display: 'flex', gap: 8, alignItems: 'baseline', fontSize: 12 }}>
                  <span className="mono" style={{ color: 'var(--text)' }}>{v.version || '—'}</span>
                  <span className="env-card-count">{v.layer_title || v.layer || ''}</span>
                  <span className="ck-detail" style={{ flex: 1, textAlign: 'right',
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={v.exe || ''}>
                    {v.exe || ''}
                  </span>
                </div>
              ))}
            </div>
          </Collapse>
        </div>
      )}

      {!ok && rt.hint && (
        <div style={{ marginTop: 10, fontSize: 12, color: 'var(--text-mute)', lineHeight: 1.7 }}>{rt.hint}</div>
      )}
    </div>
  )
}

/* ── 兼容层目录：这台机器之外，面板还懂哪些版本 ─────────── */
function Catalogue({ cat }: { cat: any }) {
  const fams: any[] = cat.families || []
  const layers: any[] = cat.layers || []
  return (
    <div style={{ marginTop: 14 }}>
      <Collapse title="版本兼容层目录" count={`${layers.length} 层`}
        hint={fams.map((f) => `${f.label} ${f.layers}`).join(' · ')}
        storageKey="env-compat-catalogue" defaultOpen={false}>
        <div style={{ display: 'grid', gap: 10 }}>
          {fams.map((f) => (
            <div key={f.family}>
              <div style={{ fontSize: 12, color: 'var(--text-dim)', marginBottom: 6 }}>
                {f.label}
                <span style={{ color: 'var(--text-mute)' }}> · {f.layers} 层</span>
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {layers.filter((l) => l.family === f.family).map((l) => (
                  <span key={l.id} className="tag" title={`匹配 ${l.match}`}>
                    {l.title}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
        <div style={{ marginTop: 12, fontSize: 11.5, color: 'var(--text-mute)', lineHeight: 1.9 }}>
          一个版本对应一个兼容层：新增版本只需在机制包 <span className="mono">compat</span> 里
          加一条声明，上层操作代码不用改。
        </div>
      </Collapse>
    </div>
  )
}
