import { useCallback, useEffect, useState } from 'react'
import { PageHeader, Badge, EmptyState } from '../components/ui'
import { useNode } from '../node'

export default function LogsCenter() {
  const { call } = useNode()
  const [files, setFiles] = useState<any[]>([])
  const [sel, setSel] = useState('')
  const [kw, setKw] = useState('')
  const [tail, setTail] = useState<any>(null)
  const [err, setErr] = useState('')

  const loadList = useCallback(async () => {
    try {
      const r = await call<any>('logs.list', {}, 20)
      setFiles(r?.files || [])
      if (!sel && r?.files?.length) setSel(r.files[r.files.length - 1].file)
    } catch (e: any) { setErr(e?.message || '读取失败') }
  }, [call, sel])
  useEffect(() => { loadList() }, [loadList])

  const loadTail = useCallback(async () => {
    if (!sel) return
    try {
      if (kw.trim()) {
        const r = await call<any>('logs.search', { file: sel, keyword: kw.trim() }, 20)
        setTail({ ...r, lines: r.lines || [], truncated: false })
      } else {
        setTail(await call<any>('logs.tail', { file: sel, lines: 400 }, 20))
      }
      setErr('')
    } catch (e: any) { setErr(e?.message || '读取失败') }
  }, [call, sel, kw])
  useEffect(() => { loadTail() }, [loadTail])

  return (
    <>
      <PageHeader title="日志中心" sub="面板与站点日志集中查看；读取范围锁死 data/logs，超 4MB 只看末尾" />
      <div style={{ display: 'grid', gridTemplateColumns: '260px 1fr', gap: 12, alignItems: 'start' }}>
        <div className="card" style={{ padding: 0, overflow: 'auto', maxHeight: '70vh' }}>
          {files.length === 0 ? (
            <EmptyState title="暂无日志文件" />
          ) : files.map((f) => (
            <div key={f.file} onClick={() => { setSel(f.file); setKw('') }}
              style={{
                padding: '8px 12px', fontSize: 12.5, cursor: 'pointer',
                borderBottom: '1px solid var(--border)',
                background: sel === f.file ? 'var(--accent-soft)' : 'transparent',
                display: 'flex', justifyContent: 'space-between', gap: 8,
              }}>
              <span className="mono" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{f.file}</span>
              <span style={{ color: 'var(--text-dim)', flex: 'none' }}>{(f.size / 1024).toFixed(0)}K</span>
            </div>
          ))}
        </div>

        <div className="card" style={{ padding: 0 }}>
          <div style={{ padding: '8px 12px', borderBottom: '1px solid var(--border)', display: 'flex', gap: 10, alignItems: 'center', fontSize: 12.5 }}>
            <input className="input" placeholder="关键词过滤（大小写不敏感）" value={kw}
              onChange={(e) => setKw(e.target.value)} style={{ flex: 1, padding: '5px 10px' }} />
            {tail && <Badge tone={tail.truncated ? 'warn' : 'muted'}>
              {tail.truncated ? '已截断（末 4MB）' : `${tail.returned ?? tail.lines?.length ?? 0} 行`}</Badge>}
            <button className="btn btn-sm" onClick={loadTail}>刷新</button>
          </div>
          {err ? <div style={{ padding: 14, fontSize: 13, color: 'var(--danger, #e5484d)' }}>{err}</div>
            : !tail || (tail.lines || []).length === 0 ? (
              <EmptyState title={kw ? '无匹配行' : '日志为空'} hint={kw ? '换个关键词试试' : undefined} />
            ) : (
              <pre className="mono" style={{ margin: 0, padding: 14, fontSize: 12, lineHeight: 1.65, whiteSpace: 'pre-wrap', wordBreak: 'break-all', maxHeight: '64vh', overflow: 'auto' }}>
                {tail.lines.join('\n')}
              </pre>
            )}
        </div>
      </div>
    </>
  )
}
