import { useState, type FormEvent } from 'react'
import { api, setToken, type SessionUser } from '../api'

export default function Login({ onLogin }: { onLogin: (u: SessionUser) => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (busy) return
    setErr('')
    setBusy(true)
    try {
      const r = await api.login(username.trim(), password)
      setToken(r.token)
      onLogin(r.user)
    } catch (e: any) {
      setErr(e?.message || '登录失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{
      height: '100%',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      background:
        'radial-gradient(1200px 600px at 50% -10%, #171d28 0%, var(--bg) 58%)',
      padding: 24,
    }}>
      <form
        onSubmit={submit}
        style={{
          width: 340,
          background: 'var(--panel)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--radius-lg)',
          padding: '30px 28px 26px',
          boxShadow: 'var(--shadow-md)',
        }}
      >
        {/* 标识 */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 9, marginBottom: 26 }}>
          <Mark />
          <div>
            <div style={{
              fontSize: 15, fontWeight: 600, letterSpacing: 'var(--track-wide)',
            }}>
              ZPanel
            </div>
            <div style={{
              fontSize: 11, color: 'var(--text-mute)', letterSpacing: 'var(--track-wide)',
            }}>
              运维面板
            </div>
          </div>
        </div>

        <label style={lbl}>用户名</label>
        <input
          className="input"
          autoFocus
          autoComplete="username"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder="admin"
          style={{ marginBottom: 14 }}
        />

        <label style={lbl}>密码</label>
        <input
          className="input"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="••••••••"
          style={{ marginBottom: 18 }}
        />

        {err && (
          <div style={{
            marginBottom: 14,
            padding: '7px 10px',
            fontSize: 12.5,
            color: 'var(--danger)',
            background: 'var(--danger-soft)',
            border: '1px solid rgba(248,113,113,.25)',
            borderRadius: 'var(--radius-sm)',
          }}>
            {err}
          </div>
        )}

        <button
          className="btn btn-primary"
          type="submit"
          disabled={busy || !username || !password}
          style={{ width: '100%', height: 34, justifyContent: 'center' }}
        >
          {busy ? <><span className="spinner" /> 登录中…</> : '登 录'}
        </button>

        <div style={{
          marginTop: 18,
          fontSize: 11.5,
          color: 'var(--text-mute)',
          lineHeight: 1.7,
          letterSpacing: 'var(--track-normal)',
        }}>
          首次启动会自举默认账号 <span className="mono">admin / admin123</span>
          ，请登录后立即改密。
        </div>
      </form>
    </div>
  )
}

const lbl: React.CSSProperties = {
  display: 'block',
  fontSize: 11,
  color: 'var(--text-mute)',
  letterSpacing: 'var(--track-wide)',
  textTransform: 'uppercase',
  marginBottom: 6,
}

/** 极简标识：四角星（与项目身份一致） */
function Mark() {
  return (
    <svg width="26" height="26" viewBox="0 0 24 24" aria-hidden>
      <path
        d="M12 2.6c.5 4.1 1.4 6.3 3.2 7.4 1.4.9 3.2 1.2 6.2 1.5-3 .3-4.8.6-6.2 1.5-1.8 1.1-2.7 3.3-3.2 7.4-.5-4.1-1.4-6.3-3.2-7.4-1.4-.9-3.2-1.2-6.2-1.5 3-.3 4.8-.6 6.2-1.5 1.8-1.1 2.7-3.3 3.2-7.4Z"
        fill="var(--accent)"
        opacity=".9"
      />
    </svg>
  )
}
