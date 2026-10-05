import { useCallback, useEffect, useState } from 'react'
import { PageHeader, Toolbar, Modal, Field, EmptyState, Badge } from '../components/ui'
import { useNode } from '../node'

export default function Ssl() {
  const { call } = useNode()
  const [local, setLocal] = useState<any>(null)
  const [checking, setChecking] = useState(false)
  const [host, setHost] = useState('')
  const [result, setResult] = useState<any>(null)
  const [selfSign, setSelfSign] = useState(false)
  const [form, setForm] = useState({ domain: '', days: '825' })
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    try { setLocal(await call<any>('ssl.local', {}, 20)) } catch { /* 本地证书可能为空 */ }
  }, [call])
  useEffect(() => { load() }, [load])

  async function check() {
    if (!host.trim()) return
    setChecking(true); setResult(null)
    try { setResult(await call<any>('ssl.check', { host: host.trim(), port: 443 }, 25)) }
    catch (e: any) { setResult({ ok: false, error: e?.message || '巡检失败' }) }
    finally { setChecking(false) }
  }

  async function doSelfSign() {
    setBusy('sign')
    try {
      const r = await call<any>('ssl.self_signed', { domain: form.domain, days: Number(form.days) || 825 }, 60)
      if (r.ok) { setMsg({ kind: 'ok', text: `已生成 ${r.cert}` }); setSelfSign(false); load() }
      else setMsg({ kind: 'err', text: (r.error || r.data || '生成失败').toString() })
    } catch (e: any) { setMsg({ kind: 'err', text: e?.message || '生成失败' }) }
    finally { setBusy('') }
  }

  const daysTone = (d: number | undefined) =>
    d === undefined || d === null ? 'muted' : d < 15 ? 'bad' : d < 30 ? 'warn' : 'ok'

  return (
    <>
      <PageHeader title="SSL 证书" sub="证书到期巡检（本地 PEM + 远端直连）与自签生成；不做 ACME 自动签发"
        actions={<button className="btn btn-primary" onClick={() => setSelfSign(true)}>自签证书</button>} />
      {msg && <div className={msg.kind === 'ok' ? 'msg-ok' : 'msg-err'} style={{ marginBottom: 10, fontSize: 13, wordBreak: 'break-all' }}>{msg.text}</div>}

      <div className="card" style={{ padding: 14, marginBottom: 12 }}>
        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>远端巡检</div>
        <div style={{ display: 'flex', gap: 10 }}>
          <input className="input" placeholder="域名，如 www.example.com" value={host}
            onChange={(e) => setHost(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') check() }} style={{ flex: 1 }} />
          <button className="btn btn-primary" disabled={checking || !host.trim()} onClick={check}>
            {checking ? '巡检中…' : '检查'}</button>
        </div>
        {result && (result.ok ? (
          <div style={{ marginTop: 12, fontSize: 13, display: 'flex', gap: 18, flexWrap: 'wrap', alignItems: 'center' }}>
            <span><b>{result.host}</b></span>
            <Badge tone={daysTone(result.days_left) as any}>剩余 {result.days_left} 天</Badge>
            <span>到期：{result.expire_str}</span>
            <span style={{ color: 'var(--text-dim)' }}>签发：{result.issuer}</span>
            <span style={{ color: 'var(--text-dim)' }}>延迟 {result.latency_ms}ms</span>
          </div>
        ) : (
          <div style={{ marginTop: 12, fontSize: 13, color: 'var(--danger, #e5484d)' }}>{result.error}</div>
        ))}
      </div>

      <Toolbar left={<span style={{ fontSize: 13, color: 'var(--text-dim)' }}>本地证书（data/ssl，{local?.count ?? 0} 张）</span>} />
      <div className="card">
        {!local || local.certs.length === 0 ? (
          <EmptyState title="本地暂无证书" hint="用右上角「自签证书」生成，或把 PEM 放进 data/ssl/" />
        ) : (
          <table className="table">
            <thead><tr><th>文件</th><th> subject</th><th style={{ width: 170 }}>到期时间</th><th style={{ width: 110 }}>剩余</th></tr></thead>
            <tbody>
              {local.certs.map((c: any) => (
                <tr key={c.file}>
                  <td className="mono" style={{ fontSize: 12.5 }}>{c.file}</td>
                  <td className="mono" style={{ fontSize: 12, maxWidth: 280, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.subject}</td>
                  <td style={{ fontSize: 12.5 }}>{c.expire_str}</td>
                  <td>{c.days_left === null ? <Badge tone="bad">解析失败</Badge>
                    : <Badge tone={daysTone(c.days_left) as any}>{c.days_left} 天</Badge>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {selfSign && (
        <Modal title="自签证书" onClose={() => setSelfSign(false)} width={460}
          footer={<>
            <button className="btn" onClick={() => setSelfSign(false)}>取消</button>
            <button className="btn btn-primary" disabled={busy === 'sign' || !form.domain.trim()} onClick={doSelfSign}>
              {busy === 'sign' ? '生成中…' : '生成'}</button>
          </>}>
          <Field label="域名 (CN)">
            <input className="input" value={form.domain} onChange={(e) => setForm({ ...form, domain: e.target.value })} />
          </Field>
          <Field label="有效天数" hint="默认 825 天（浏览器信任链上限）">
            <input className="input" type="number" value={form.days} onChange={(e) => setForm({ ...form, days: e.target.value })} />
          </Field>
          <div style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 4 }}>
            自签证书浏览器会告警，仅用于内网 / 回环加密；需要系统 openssl。
          </div>
        </Modal>
      )}
    </>
  )
}
