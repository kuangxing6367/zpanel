import { useCallback, useEffect, useState } from 'react'
import { PageHeader, Toolbar, Modal, Field, EmptyState, ConfirmModal, Badge } from '../components/ui'
import { useNode } from '../node'

function fmtSize(n: number) {
  if (!n) return '—'
  if (n < 1024) return `${n} B`
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1048576).toFixed(1)} MB`
}

export default function Backup() {
  const { call } = useNode()
  const [jobs, setJobs] = useState<any[]>([])
  const [defaultDir, setDefaultDir] = useState('')
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [busy, setBusy] = useState('')
  const [editing, setEditing] = useState<null | { id?: string; name: string; paths: string; target_dir: string; keep: string }>(null)
  const [removing, setRemoving] = useState<any>(null)

  const load = useCallback(async () => {
    try {
      const r = await call<any>('backup.list', {}, 20)
      setJobs(r?.jobs || []); setDefaultDir(r?.default_dir || '')
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '读取失败' }) }
  }, [call])
  useEffect(() => { load() }, [load])

  async function run(job: any) {
    setBusy(job.id)
    try {
      const r = await call<any>('backup.run', { id: job.id }, 120)
      const d = r?.data || r
      if (d.ok) setMsg({ kind: 'ok', text: `${job.name} 完成：${d.files} 个文件 / ${fmtSize(d.size)} / ${d.duration_ms}ms → ${d.archive}` })
      else setMsg({ kind: 'err', text: d.error || '执行失败' })
      load()
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '执行失败' }) }
    finally { setBusy('') }
  }

  async function save() {
    if (!editing) return
    setBusy('save')
    try {
      const body = {
        name: editing.name, keep: Number(editing.keep) || 5,
        paths: editing.paths.split('\n').map((s) => s.trim()).filter(Boolean),
        target_dir: editing.target_dir,
      }
      const r = editing.id
        ? await call<any>('backup.update', { id: editing.id, ...body }, 30)
        : await call<any>('backup.create', body, 30)
      if (r.ok) { setEditing(null); setMsg({ kind: 'ok', text: '已保存' }); load() }
      else setMsg({ kind: 'err', text: (r.data || r.error || '保存失败').toString() })
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '保存失败' }) }
    finally { setBusy('') }
  }

  async function remove() {
    if (!removing) return
    setBusy('remove')
    try {
      await call<any>('backup.remove', { id: removing.id }, 20)
      setRemoving(null); setMsg({ kind: 'ok', text: '任务已删除' }); load()
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '删除失败' }) }
    finally { setBusy('') }
  }

  return (
    <>
      <PageHeader title="备份" sub={`把目录打包成 zip，保留最近 N 份；默认目录 ${defaultDir || 'data/backups'}`}
        actions={<button className="btn btn-primary" onClick={() => setEditing({ name: '', paths: '', target_dir: '', keep: '5' })}>新建任务</button>} />
      {msg && <div className={msg.kind === 'ok' ? 'msg-ok' : 'msg-err'} style={{ marginBottom: 10, fontSize: 13, wordBreak: 'break-all' }}>{msg.text}</div>}

      <div className="card">
        {jobs.length === 0 ? (
          <EmptyState title="还没有备份任务" hint="新建任务，填源路径（可多行）与保留份数" />
        ) : (
          <table className="table">
            <thead><tr>
              <th>任务</th><th>源路径</th><th style={{ width: 80 }}>保留</th>
              <th style={{ width: 150 }}>上次执行</th><th style={{ width: 90 }}>上次大小</th>
              <th style={{ width: 210, textAlign: 'right' }}>操作</th>
            </tr></thead>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.id}>
                  <td>{j.name}{j.last_error && <div style={{ fontSize: 11.5, color: 'var(--danger, #e5484d)' }}>{j.last_error}</div>}</td>
                  <td className="mono" style={{ fontSize: 12, maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                      title={j.paths.join('\n')}>{j.paths.join(' · ')}</td>
                  <td>{j.keep}</td>
                  <td style={{ fontSize: 12.5 }}>{j.last_run_at ? new Date(j.last_run_at * 1000).toLocaleString() : '—'}</td>
                  <td style={{ fontSize: 12.5 }}>{fmtSize(j.last_size)}</td>
                  <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                    <button className="btn btn-sm btn-primary" disabled={busy === j.id} onClick={() => run(j)}>
                      {busy === j.id ? '备份中…' : '立即备份'}</button>{' '}
                    <button className="btn btn-sm" onClick={() => setEditing({
                      id: j.id, name: j.name, paths: j.paths.join('\n'), target_dir: j.target_dir, keep: String(j.keep),
                    })}>编辑</button>{' '}
                    <button className="btn btn-sm btn-danger" onClick={() => setRemoving(j)}>删除</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {editing && (
        <Modal title={editing.id ? '编辑备份任务' : '新建备份任务'} onClose={() => setEditing(null)}
          footer={<>
            <button className="btn" onClick={() => setEditing(null)}>取消</button>
            <button className="btn btn-primary" disabled={busy === 'save'} onClick={save}>{busy === 'save' ? '保存中…' : '保存'}</button>
          </>}>
          <Field label="任务名">
            <input className="input" value={editing.name} onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
          </Field>
          <Field label="源路径" hint="每行一个，需在面板允许的路径白名单内">
            <textarea className="input mono" rows={4} value={editing.paths}
              onChange={(e) => setEditing({ ...editing, paths: e.target.value })} />
          </Field>
          <Field label="备份目录" hint="留空用默认目录">
            <input className="input mono" value={editing.target_dir} onChange={(e) => setEditing({ ...editing, target_dir: e.target.value })} />
          </Field>
          <Field label="保留份数" hint="超出自动清理最旧的">
            <input className="input" type="number" value={editing.keep} onChange={(e) => setEditing({ ...editing, keep: e.target.value })} />
          </Field>
        </Modal>
      )}

      {removing && (
        <ConfirmModal title="删除备份任务" text={<>确定删除任务 <b>{removing.name}</b>？已生成的备份压缩包不会被删除。</>}
          busy={busy === 'remove'} onConfirm={remove} onClose={() => setRemoving(null)} />
      )}
    </>
  )
}
