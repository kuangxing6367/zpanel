import { useCallback, useEffect, useState } from 'react'
import { useNode } from '../node'
import { Badge, ConfirmModal, EmptyState, Field, Modal, PageHeader, Toolbar } from '../components/ui'
import Collapse, { LiveTag, usePoll } from '../components/Live'

const MS = 15000

interface DbConn {
  id: string
  name: string
  kind: string
  host: string
  port: number
  user: string
  database?: string
}
interface SqlResult {
  ok: boolean
  columns?: string[]
  rows?: any[][]
  raw?: string
  error?: string
}

const KIND_LABEL: Record<string, string> = {
  mysql: 'MySQL / MariaDB', postgres: 'PostgreSQL', redis: 'Redis',
}

/** 数据库页：管理这台机器上已有数据库的连接 —— 库/表浏览、只读查询、建库、导出。
 *  面板不内置数据库、不占端口；口令经 secretbox 加密入库，接口永不下发明文。 */
export default function Database({ nodeName }: { nodeName?: string }) {
  const { current, call } = useNode()
  const name = nodeName || current

  const [conns, setConns] = useState<DbConn[] | null>(null)
  const [sel, setSel] = useState<string>('')
  const [status, setStatus] = useState<any>(null)
  const [dbs, setDbs] = useState<string[]>([])
  const [tables, setTables] = useState<{ db: string; items: string[] } | null>(null)
  const [sql, setSql] = useState('SHOW DATABASES;')
  const [result, setResult] = useState<SqlResult | null>(null)
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [auto, setAuto] = useState(true)
  const [at, setAt] = useState(0)
  const [adding, setAdding] = useState(false)
  const [dropDb, setDropDb] = useState<string | null>(null)
  const [dumping, setDumping] = useState<string | null>(null)
  const [form, setForm] = useState({ name: '', kind: 'mysql', host: '127.0.0.1', port: 3306, user: 'root', password: '' })
  const [newDb, setNewDb] = useState('')

  const loadConns = useCallback(async () => {
    try {
      const r = await call<any>('db.list', {}, 20)
      const list: DbConn[] = r?.connections || []
      setConns(list)
      setSel((cur) => (cur && list.some((c) => c.id === cur) ? cur : (list[0]?.id || '')))
      setAt(Date.now())
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '读取连接失败' })
    }
  }, [call])

  useEffect(() => { loadConns() }, [loadConns])

  /** 选中连接后的全量状态：状态 + 库清单 */
  const loadAll = useCallback(async () => {
    if (!sel) { setStatus(null); setDbs([]); setTables(null); return }
    try {
      const [st, dl] = await Promise.all([
        call<any>('db.status', { id: sel }, 20).catch(() => null),
        call<any>('db.databases', { id: sel }, 30).catch(() => null),
      ])
      setStatus(st)
      const rows = dl?.rows || []
      // rows 可能是 [[name,...], ...]（取第一列）或 [{name:...}, ...]
      const names = rows.map((r: any) => Array.isArray(r) ? String(r[0]) : String(Object.values(r)[0]))
      setDbs(names)
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '读取失败' })
    }
  }, [call, sel])

  usePoll(loadAll, MS, auto && !!sel)

  async function openTables(db: string) {
    if (!sel) return
    setBusy('tables:' + db)
    try {
      const r = await call<any>('db.tables', { id: sel, database: db }, 30)
      const rows = r?.rows || []
      const items = rows.map((x: any) => Array.isArray(x) ? String(x[0]) : String(Object.values(x)[0]))
      setTables({ db, items })
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '读取表失败' })
    } finally { setBusy('') }
  }

  async function runSql() {
    if (!sel || !sql.trim()) return
    setBusy('sql')
    try {
      const r = await call<any>('db.query', { id: sel, sql }, 60)
      setResult(r)
      if (r?.ok && /^(SHOW DATABASES|SHOW TABLES)/i.test(sql.trim())) loadAll()
    } catch (e: any) {
      setResult({ ok: false, error: e?.message || '查询失败' })
    } finally { setBusy('') }
  }

  async function doCreate() {
    setBusy('create')
    try {
      await call('db.create', { ...form, port: Number(form.port) || undefined }, 30)
      setMsg({ kind: 'ok', text: `连接 ${form.name} 已创建` })
      setAdding(false)
      loadConns()
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '创建失败' })
    } finally { setBusy('') }
  }

  async function testConn(c: DbConn) {
    setBusy('test:' + c.id)
    try {
      const r = await call<any>('db.test', { id: c.id }, 30)
      setMsg(r?.ok ? { kind: 'ok', text: `${c.name} 连通正常` }
                   : { kind: 'err', text: `${c.name} 连不上：${r?.error || '?'}` })
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '测试失败' })
    } finally { setBusy('') }
  }

  async function doCreateDb() {
    if (!sel || !newDb.trim()) return
    setBusy('createdb')
    try {
      await call('db.createdb', { id: sel, name: newDb.trim() }, 30)
      setMsg({ kind: 'ok', text: `数据库 ${newDb.trim()} 已创建` })
      setNewDb('')
      loadAll()
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '建库失败' })
    } finally { setBusy('') }
  }

  async function doDropDb() {
    if (!sel || !dropDb) return
    setBusy('dropdb')
    try {
      await call('db.dropdb', { id: sel, name: dropDb, confirm: dropDb }, 30)
      setMsg({ kind: 'ok', text: `数据库 ${dropDb} 已删除` })
      setDropDb(null)
      loadAll()
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '删库失败' })
    } finally { setBusy('') }
  }

  async function doDump() {
    if (!sel || !dumping) return
    setBusy('dump')
    try {
      const path = `/opt/zpanel/data/backups/${dumping}-${Date.now().toString().slice(-6)}.sql`
      const r = await call<any>('db.dump', { id: sel, out_path: path, database: dumping }, 300)
      setMsg(r?.ok ? { kind: 'ok', text: `已导出 ${dumping} → ${r?.archive || r?.path || path}` }
                   : { kind: 'err', text: r?.error || '导出失败' })
      setDumping(null)
    } catch (e: any) {
      setMsg({ kind: 'err', text: e?.message || '导出失败' })
    } finally { setBusy('') }
  }

  const selConn = conns?.find((c) => c.id === sel) || null

  return (
    <>
      <PageHeader title="数据库" sub={<>
        节点 <span className="mono" style={{ color: 'var(--accent)' }}>{name}</span>
        {' '}· 管理机器上已有的数据库：库表浏览 / 只读查询 / 建库 / 导出
        {' '}· 口令加密存储，接口不下发明文
      </>} />

      {msg && (
        <div style={{
          marginBottom: 10, padding: '8px 12px', fontSize: 12.5,
          borderRadius: 'var(--radius-sm)', wordBreak: 'break-all',
          color: msg.kind === 'ok' ? 'var(--ok)' : 'var(--danger)',
          background: msg.kind === 'ok' ? 'var(--ok-soft)' : 'var(--danger-soft)',
        }}>{msg.text}</div>
      )}

      <Toolbar left={
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <select className="input" style={{ width: 220 }} value={sel}
                  disabled={!conns?.length}
                  onChange={(e) => { setSel(e.target.value); setTables(null); setResult(null) }}>
            {conns?.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}（{KIND_LABEL[c.kind] || c.kind} · {c.user}@{c.host}:{c.port}）
              </option>
            ))}
            {!conns?.length && <option value="">暂无连接</option>}
          </select>
          {selConn && (
            <button className="btn btn-sm" disabled={busy === 'test:' + selConn.id}
                    onClick={() => testConn(selConn)}>
              {busy === 'test:' + selConn.id ? '测试中…' : '测试连通'}
            </button>
          )}
          <button className="btn btn-sm" onClick={() => setAdding(true)}>新建连接</button>
        </div>
      } right={
        <LiveTag at={at} enabled={auto} intervalMs={MS} onToggle={() => setAuto((v) => !v)} />
      } />

      {!conns?.length ? (
        <div className="card">
          <EmptyState title="还没有数据库连接"
                      hint="新建一个连接（MySQL / PostgreSQL / Redis），面板用客户端命令行直连，不占数据库端口" />
        </div>
      ) : (
        <>
          {/* 库清单 */}
          <div className="card" style={{ marginBottom: 12 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                          flexWrap: 'wrap', gap: 8, marginBottom: 8 }}>
              <div className="card-title" style={{ marginBottom: 0 }}>
                库清单{status?.version ? ` · ${status.version}` : ''}
              </div>
              <div style={{ display: 'flex', gap: 6 }}>
                <input className="input input-sm" style={{ width: 160 }} placeholder="新库名，如 app_db"
                       value={newDb} onChange={(e) => setNewDb(e.target.value)} />
                <button className="btn btn-sm" disabled={!newDb.trim() || busy === 'createdb'}
                        onClick={doCreateDb}>
                  {busy === 'createdb' ? '创建中…' : '建库'}
                </button>
              </div>
            </div>
            {dbs.length === 0 ? (
              <div className="empty" style={{ padding: 12 }}>没有可见的数据库（或连接不可用）</div>
            ) : (
              <table className="table">
                <thead><tr><th>数据库</th><th style={{ width: 220, textAlign: 'right' }}>操作</th></tr></thead>
                <tbody>
                  {dbs.map((d) => (
                    <tr key={d}>
                      <td className="mono">{d}</td>
                      <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                        <button className="btn-text" disabled={busy === 'tables:' + d}
                                onClick={() => openTables(d)}>
                          {tables?.db === d ? `已载入 ${tables.items.length} 张表` : '看表'}
                        </button>
                        <button className="btn-text dim" disabled={!!busy}
                                onClick={() => setDumping(d)}>导出</button>
                        <button className="btn-text danger" disabled={!!busy}
                                onClick={() => setDropDb(d)}>删除</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {tables && (
              <div style={{ marginTop: 10, fontSize: 12.5, color: 'var(--text-dim)' }}>
                <span className="mono">{tables.db}</span> 的表：
                {tables.items.length
                  ? tables.items.map((t) => <span key={t} className="mono" style={{ marginRight: 10 }}>{t}</span>)
                  : '（空库）'}
              </div>
            )}
          </div>

          {/* SQL 查询 */}
          <Collapse title="SQL 查询（只读模式，SELECT/SHOW/DESC/EXPLAIN）" defaultOpen storageKey="db.sql">
            <textarea className="input mono" rows={4} value={sql} onChange={(e) => setSql(e.target.value)}
                      style={{ width: '100%', marginBottom: 8, resize: 'vertical' }} />
            <div style={{ marginBottom: 10 }}>
              <button className="btn btn-primary btn-sm" disabled={busy === 'sql' || !sql.trim()}
                      onClick={runSql}>{busy === 'sql' ? '执行中…' : '执行（Ctrl+Enter）'}</button>
            </div>
            {result && (result.ok ? (
              result.columns?.length ? (
                <div style={{ overflowX: 'auto' }}>
                  <table className="table">
                    <thead><tr>{result.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
                    <tbody>
                      {result.rows?.map((row, i) => (
                        <tr key={i}>{row.map((v: any, j: number) => (
                          <td key={j} className="mono" style={{ fontSize: 11.5 }}>{String(v ?? 'NULL')}</td>
                        ))}</tr>
                      ))}
                    </tbody>
                  </table>
                  <div style={{ fontSize: 11.5, color: 'var(--text-mute)', marginTop: 6 }}>
                    {result.rows?.length || 0} 行
                  </div>
                </div>
              ) : (
                <pre className="mono" style={{
                  padding: 12, fontSize: 11.5, whiteSpace: 'pre-wrap', wordBreak: 'break-all',
                  background: 'var(--bg-elev)', border: '1px solid var(--border)',
                  borderRadius: 'var(--radius-sm)', color: 'var(--text-dim)',
                }}>{result.raw || '（无返回行）'}</pre>
              )
            ) : (
              <div style={{ color: 'var(--danger)', fontSize: 12.5 }}>{result.error}</div>
            ))}
          </Collapse>
        </>
      )}

      {/* 新建连接 */}
      {adding && (
        <Modal title="新建数据库连接" onClose={() => setAdding(false)} width={520}
               footer={<>
                 <button className="btn" onClick={() => setAdding(false)}>取消</button>
                 <button className="btn btn-primary" disabled={busy === 'create'} onClick={doCreate}>
                   {busy === 'create' ? '创建中…' : '创建'}
                 </button>
               </>}>
          <Field label="类型">
            <select className="input" value={form.kind}
                    onChange={(e) => setForm({ ...form, kind: e.target.value,
                                               port: e.target.value === 'postgres' ? 5432 : e.target.value === 'mysql' ? 3306 : 6379 })}>
              <option value="mysql">MySQL / MariaDB</option>
              <option value="postgres">PostgreSQL</option>
              <option value="redis">Redis</option>
            </select>
          </Field>
          <Field label="名称"><input className="input" value={form.name}
                                     onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
          {form.kind !== 'redis' && <>
            <Field label="主机">
              <input className="input" value={form.host}
                     onChange={(e) => setForm({ ...form, host: e.target.value })} />
            </Field>
            <Field label="端口">
              <input className="input" type="number" value={form.port}
                     onChange={(e) => setForm({ ...form, port: Number(e.target.value) })} />
            </Field>
            <Field label="用户">
              <input className="input" value={form.user}
                     onChange={(e) => setForm({ ...form, user: e.target.value })} />
            </Field>
            <Field label="密码">
              <input className="input" type="password" value={form.password}
                     onChange={(e) => setForm({ ...form, password: e.target.value })} />
            </Field>
          </>}
        </Modal>
      )}

      {/* 删库确认 */}
      {dropDb && (
        <ConfirmModal title="删除数据库" busy={busy === 'dropdb'}
                      onConfirm={doDropDb} onClose={() => setDropDb(null)}
                      text={<>确定删除数据库 <b className="mono">{dropDb}</b>？
                        库内所有数据将丢失，此操作不可撤销。</>} />
      )}

      {/* 导出确认 */}
      {dumping && (
        <ConfirmModal title={`导出 ${dumping}`} busy={busy === 'dump'}
                      onConfirm={doDump} onClose={() => setDumping(null)}
                      text={<>将用 <b className="mono">mysqldump</b> 把 <b className="mono">{dumping}</b> 导出
                        到 data/backups/ 下的 SQL 文件，继续？</>} />
      )}
    </>
  )
}
