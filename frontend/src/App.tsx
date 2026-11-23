import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'

import './App.css'

type HealthStatus = 'checking' | 'online' | 'offline'

type Provider = {
  name: 'ollama' | 'gemini' | 'openrouter'
  kind: 'local' | 'cloud'
  configured: boolean
}

type ProvidersResponse = {
  default_model: string
  providers: Provider[]
}

type SessionSummary = {
  id: string
  title: string
  created_at: string
  updated_at: string
  turn_count: number
}

type SessionTurn = {
  role: string
  content: string
  created_at: string
}

type SessionDetail = SessionSummary & {
  turns: SessionTurn[]
  outcomes: unknown[]
}

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000/api/v1'
const configuredApiKey = import.meta.env.VITE_FORGE_API_KEY ?? ''

type View = 'overview' | 'sessions' | 'settings'

const providerLabels: Record<Provider['name'], string> = {
  ollama: 'Ollama',
  gemini: 'Google Gemini',
  openrouter: 'OpenRouter',
}

function App() {
  const [health, setHealth] = useState<HealthStatus>('checking')
  const [providers, setProviders] = useState<Provider[]>([])
  const [defaultModel, setDefaultModel] = useState('ollama')
  const [error, setError] = useState<string | null>(null)
  const [view, setView] = useState<View>('overview')
  const [sessions, setSessions] = useState<SessionSummary[]>([])
  const [activeSession, setActiveSession] = useState<SessionDetail | null>(null)
  const [prompt, setPrompt] = useState('')
  const [model, setModel] = useState('qwen2.5-coder:7b')
  const [contextPaths, setContextPaths] = useState('')
  const [apiKey, setApiKey] = useState(configuredApiKey)
  const [workspaceError, setWorkspaceError] = useState<string | null>(null)
  const [running, setRunning] = useState(false)
  const [streaming, setStreaming] = useState(false)
  const [streamingContent, setStreamingContent] = useState('')
  const [streamResponse, setStreamResponse] = useState(false)
  const taskController = useRef<AbortController | null>(null)

  useEffect(() => {
    const controller = new AbortController()

    async function loadDashboard() {
      try {
        const [healthResponse, providersResponse] = await Promise.all([
          fetch(`${apiBaseUrl}/health`, { signal: controller.signal }),
          fetch(`${apiBaseUrl}/providers`, { signal: controller.signal }),
        ])

        if (!healthResponse.ok || !providersResponse.ok) {
          throw new Error('The Forge API returned an unsuccessful response.')
        }

        const providerData = (await providersResponse.json()) as ProvidersResponse
        setProviders(providerData.providers)
        setDefaultModel(providerData.default_model)
        setHealth('online')
        setError(null)
      } catch (requestError) {
        if (controller.signal.aborted) return

        setHealth('offline')
        setError(
          requestError instanceof Error
            ? requestError.message
            : 'Could not connect to the Forge API.',
        )
      }
    }

    void loadDashboard()
    return () => controller.abort()
  }, [])

  useEffect(() => {
    if (view !== 'sessions' || !apiKey) return

    const controller = new AbortController()
    void (async () => {
      try {
        const response = await fetch(`${apiBaseUrl}/sessions`, {
          signal: controller.signal,
          headers: {
            Authorization: `Bearer ${apiKey}`,
          },
        })
        if (!response.ok) {
          let message = 'Could not load sessions.'
          try {
            const payload = (await response.json()) as { error?: { message?: string } }
            if (payload.error?.message) message = payload.error.message
          } catch {
            // Keep the safe fallback for non-JSON responses.
          }
          throw new Error(message)
        }
        setSessions((await response.json()) as SessionSummary[])
        setWorkspaceError(null)
      } catch (requestError) {
        if (controller.signal.aborted) return
        setWorkspaceError(requestError instanceof Error ? requestError.message : 'Could not load sessions.')
      }
    })()
    return () => controller.abort()
  }, [view, apiKey])

  async function apiFetch(path: string, options: RequestInit = {}) {
    if (!apiKey) throw new Error('Configure a Forge API key in Settings.')

    const headers = new Headers(options.headers)
    headers.set('Content-Type', 'application/json')
    headers.set('Authorization', `Bearer ${apiKey}`)
    const response = await fetch(`${apiBaseUrl}${path}`, {
      ...options,
      headers,
    })

    if (!response.ok) {
      let message = 'The Forge API could not complete the request.'
      try {
        const payload = (await response.json()) as { error?: { message?: string } }
        if (payload.error?.message) message = payload.error.message
      } catch {
        // Keep the safe fallback for non-JSON responses.
      }
      throw new Error(message)
    }

    if (response.status === 204) return null
    return response.json()
  }

  async function fetchSessions(signal?: AbortSignal) {
    try {
      const data = (await apiFetch('/sessions', { signal })) as SessionSummary[]
      setSessions(data)
      setWorkspaceError(null)
    } catch (requestError) {
      if (signal?.aborted) return
      setWorkspaceError(requestError instanceof Error ? requestError.message : 'Could not load sessions.')
    }
  }

  async function createSession() {
    setWorkspaceError(null)
    try {
      const summary = (await apiFetch('/sessions', {
        method: 'POST',
        body: JSON.stringify({ title: 'New chat' }),
      })) as SessionSummary
      const detail = (await apiFetch(`/sessions/${summary.id}`)) as SessionDetail
      setSessions((current) => [summary, ...current.filter((s) => s.id !== summary.id)])
      setActiveSession(detail)
      setView('sessions')
    } catch (requestError) {
      setWorkspaceError(requestError instanceof Error ? requestError.message : 'Could not create a session.')
    }
  }

  async function selectSession(sessionId: string) {
    setWorkspaceError(null)
    try {
      const detail = (await apiFetch(`/sessions/${sessionId}`)) as SessionDetail
      setActiveSession(detail)
    } catch (requestError) {
      setWorkspaceError(requestError instanceof Error ? requestError.message : 'Could not open the session.')
    }
  }

  async function deleteSession(sessionId: string) {
    setWorkspaceError(null)
    try {
      await apiFetch(`/sessions/${sessionId}`, { method: 'DELETE' })
      setSessions((current) => current.filter((session) => session.id !== sessionId))
      if (activeSession?.id === sessionId) setActiveSession(null)
    } catch (requestError) {
      setWorkspaceError(requestError instanceof Error ? requestError.message : 'Could not delete the session.')
    }
  }

  async function submitPrompt(event: FormEvent) {
    event.preventDefault()
    const content = prompt.trim()
    if (!content || running) return

    setWorkspaceError(null)
    setRunning(true)
    const controller = new AbortController()
    taskController.current = controller

    try {
      let sessionId = activeSession?.id
      if (!sessionId) {
        const summary = (await apiFetch('/sessions', {
          method: 'POST',
          body: JSON.stringify({ title: content.slice(0, 120) }),
          signal: controller.signal,
        })) as SessionSummary
        sessionId = summary.id
      }

      const paths = contextPaths
        .split(',')
        .map((path) => path.trim())
        .filter(Boolean)
        .slice(0, 10)

      if (streamResponse) {
        setStreaming(true)
        setStreamingContent('')
        const response = await fetch(`${apiBaseUrl}/agent/tasks/stream`, {
          method: 'POST',
          signal: controller.signal,
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${apiKey}`,
          },
          body: JSON.stringify({
            model,
            messages: [{ role: 'user', content }],
            context_paths: paths,
            session_id: sessionId,
            max_tokens: 1024,
          }),
        })

        if (!response.ok) {
          let message = 'Could not run the streaming task.'
          try {
            const payload = (await response.json()) as { error?: { message?: string } }
            if (payload.error?.message) message = payload.error.message
          } catch {
            // Keep safe fallback for non-JSON responses.
          }
          throw new Error(message)
        }

        const reader = response.body?.getReader()
        const decoder = new TextDecoder()
        if (reader) {
          let buffer = ''

          const processSseBlock = (block: string) => {
            const trimmed = block.trim()
            if (!trimmed) return
            let event = ''
            let data = ''
            for (const rawLine of trimmed.split(/\r?\n/)) {
              const line = rawLine.trim()
              if (!line || line.startsWith(':')) continue
              if (line.startsWith('event:')) {
                event = line.slice(6).trim()
              } else if (line.startsWith('data:')) {
                const val = line.slice(5).trim()
                data = data ? `${data}\n${val}` : val
              }
            }
            if (event === 'error') {
              let errorMsg = 'Streaming failed.'
              if (data) {
                try {
                  const payload = JSON.parse(data) as { error?: { message?: string } }
                  if (payload.error?.message) errorMsg = payload.error.message
                } catch {
                  // Fallback to default
                }
              }
              throw new Error(errorMsg)
            }
            if (data) {
              try {
                const payload = JSON.parse(data) as { content?: string; done?: boolean }
                if (payload.content) {
                  setStreamingContent((prev) => prev + payload.content)
                }
              } catch {
                // Keep going on partial or non-JSON data
              }
            }
          }

          while (true) {
            const { done, value } = await reader.read()
            if (done) break
            buffer += decoder.decode(value, { stream: true })
            const blocks = buffer.split(/\r?\n\r?\n/)
            buffer = blocks.pop() ?? ''
            for (const block of blocks) {
              processSseBlock(block)
            }
          }
          buffer += decoder.decode()
          if (buffer.trim()) {
            const remaining = buffer.split(/\r?\n\r?\n/)
            for (const block of remaining) {
              processSseBlock(block)
            }
          }
        }
        await selectSession(sessionId)
        setStreaming(false)
        setStreamingContent('')
        setPrompt('')
        void fetchSessions()
      } else {
        await apiFetch('/agent/tasks', {
          method: 'POST',
          signal: controller.signal,
          body: JSON.stringify({
            model,
            messages: [{ role: 'user', content }],
            context_paths: paths,
            session_id: sessionId,
            max_tokens: 1024,
          }),
        })

        setPrompt('')
        await selectSession(sessionId)
        void fetchSessions()
      }
    } catch (requestError) {
      setStreaming(false)
      setStreamingContent('')
      if (controller.signal.aborted) {
        setWorkspaceError('Task cancelled.')
      } else {
        setWorkspaceError(requestError instanceof Error ? requestError.message : 'Could not run the task.')
      }
    } finally {
      setRunning(false)
      taskController.current = null
    }
  }

  function cancelTask() {
    taskController.current?.abort()
    setStreaming(false)
    setStreamingContent('')
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="/" aria-label="Forge home">
          <span className="brand-mark">F</span>
          <span>forge</span>
        </a>

        <div className="sidebar-label">WORKSPACE</div>
        <nav className="navigation" aria-label="Main navigation">
          <button className={`nav-item ${view === 'overview' ? 'active' : ''}`} onClick={() => setView('overview')} aria-current={view === 'overview' ? 'page' : undefined}>
            <span className="nav-icon" aria-hidden="true">⌂</span>
            Overview
          </button>
          <button className={`nav-item ${view === 'sessions' ? 'active' : ''}`} onClick={() => setView('sessions')} aria-current={view === 'sessions' ? 'page' : undefined}>
            <span className="nav-icon" aria-hidden="true">◷</span>
            Sessions
          </button>
          <button className={`nav-item ${view === 'settings' ? 'active' : ''}`} onClick={() => setView('settings')} aria-current={view === 'settings' ? 'page' : undefined}>
            <span className="nav-icon" aria-hidden="true">⚙</span>
            Settings
          </button>
        </nav>

        <div className="sidebar-footer">
          <span className="footer-orbit" aria-hidden="true">✳</span>
          <span>Build freely.<br />Code intelligently.</span>
          <span className="version">v0.1.0</span>
        </div>
      </aside>

      <main className="main-content" id="overview">
        <header className="topbar">
          <div className="breadcrumb">
            <span>Workspace</span>
            <span className="breadcrumb-separator">/</span>
            <strong>{view === 'overview' ? 'Overview' : view === 'sessions' ? 'Sessions' : 'Settings'}</strong>
          </div>
          <a className="docs-link" href="https://github.com/Rufidatul726/forge">
            Project repository <span aria-hidden="true">↗</span>
          </a>
        </header>

        {view === 'overview' ? (
        <div className="content">
          <section className="welcome">
            <div className="eyebrow"><span className="eyebrow-line" /> YOUR ENGINEERING SPACE</div>
            <h1>Your ideas,<br /><span>under construction.</span></h1>
            <p className="welcome-copy">
              Forge brings your models and codebase together. The workspace is taking shape;
              the read-only agent runtime is available now.
            </p>
            <div className="runtime-note">
              <span className="runtime-indicator" />
              <span><strong>Agent runtime</strong> is ready for read-only tasks</span>
            </div>
          </section>

          <section className="status-grid" aria-label="System status">
            <article className="status-card api-card">
              <div className="card-heading">
                <span className="card-icon api-icon" aria-hidden="true">⌘</span>
                <span className={`status-pill ${health}`}>
                  <span className="status-dot" />
                  {health === 'checking' ? 'Connecting' : health === 'online' ? 'Online' : 'Offline'}
                </span>
              </div>
              <div className="card-label">BACKEND API</div>
              <h2>{health === 'online' ? 'Connected' : health === 'checking' ? 'Checking connection' : 'Not connected'}</h2>
              <p>{health === 'online' ? 'Forge services are responding.' : 'FastAPI service at the configured address.'}</p>
              {error && <p className="error-message" role="status">{error}</p>}
            </article>

            <article className="status-card model-card">
              <div className="card-heading">
                <span className="card-icon model-icon" aria-hidden="true">✳</span>
                <span className="neutral-pill">DEFAULT</span>
              </div>
              <div className="card-label">SELECTED MODEL</div>
              <h2>{defaultModel}</h2>
              <p>Set with <code>DEFAULT_MODEL</code>.</p>
            </article>
          </section>

          <section className="providers-section">
            <div className="section-heading">
              <div>
                <div className="card-label">YOUR TOOLKIT</div>
                <h2>Model providers</h2>
              </div>
              <span className="provider-count">{providers.length || 3} providers</span>
            </div>

            <div className="provider-list">
              {providers.length > 0 ? providers.map((provider) => (
                <article className="provider-row" key={provider.name}>
                  <span className={`provider-logo ${provider.name}`} aria-hidden="true">
                    {provider.name === 'ollama' ? 'O' : provider.name === 'gemini' ? '✦' : '↗'}
                  </span>
                  <div className="provider-info">
                    <h3>{providerLabels[provider.name]}</h3>
                    <p>{provider.kind === 'local' ? 'Local inference' : 'Cloud inference'}</p>
                  </div>
                  <span className={`provider-status ${provider.configured ? 'configured' : ''}`}>
                    <span className="status-dot" />
                    {provider.configured ? 'Configured' : 'Not configured'}
                  </span>
                </article>
              )) : (
                <div className="provider-empty">
                  {health === 'offline' ? 'Start the backend to load provider status.' : 'Loading provider status…'}
                </div>
              )}
            </div>
          </section>

          <footer className="page-footer">
            <span>Early development · Built for developers, by developers.</span>
            <a href="http://127.0.0.1:8000/docs">API documentation <span aria-hidden="true">↗</span></a>
          </footer>
        </div>
        ) : view === 'sessions' ? (
          <div className="chat-layout">
            <aside className="session-list" aria-label="Chat sessions">
              <div className="session-list-heading">
                <span>SESSIONS</span>
                <button className="icon-button" type="button" onClick={() => void createSession()} title="New session">+</button>
              </div>
              <div className="session-items">
                {sessions.map((session) => (
                  <button
                    className={`session-item ${activeSession?.id === session.id ? 'active' : ''}`}
                    key={session.id}
                    type="button"
                    onClick={() => void selectSession(session.id)}
                  >
                    <strong>{session.title}</strong>
                    <span>{session.turn_count} turns</span>
                  </button>
                ))}
                {sessions.length === 0 && <p className="session-empty">No saved sessions.</p>}
              </div>
            </aside>

            <section className="chat-panel" aria-label="Agent chat">
              <div className="chat-heading">
                <div>
                  <span className="card-label">AGENT TASK</span>
                  <h1>{activeSession?.title ?? 'New conversation'}</h1>
                </div>
                {activeSession && (
                  <button className="delete-button" type="button" onClick={() => void deleteSession(activeSession.id)}>
                    Delete
                  </button>
                )}
              </div>

              {workspaceError && <p className="chat-error" role="alert">{workspaceError}</p>}

              <div className="messages" aria-live="polite">
                {activeSession?.turns.map((turn, index) => (
                  <article className={`message ${turn.role}`} key={`${turn.created_at}-${index}`}>
                    <span>{turn.role === 'assistant' ? 'Forge' : 'You'}</span>
                    <p>{turn.content}</p>
                  </article>
                ))}
                {streaming && (
                  <article className="message assistant streaming">
                    <span>Forge</span>
                    <p>{streamingContent}</p>
                    <span className="typing-indicator">Responding…</span>
                  </article>
                )}
                {!activeSession?.turns.length && !streaming && (
                  <div className="chat-empty">
                    <span className="chat-empty-mark">F</span>
                    <h2>Ask about your workspace</h2>
                    <p>Forge uses bounded repository context and a read-only task workflow.</p>
                  </div>
                )}
              </div>

              <form className="prompt-form" onSubmit={(event) => void submitPrompt(event)}>
                <div className="prompt-options">
                  <label>
                    Model
                    <input value={model} onChange={(event) => setModel(event.target.value)} placeholder="qwen2.5-coder:7b" />
                  </label>
                  <label className="context-input">
                    Context files
                    <input value={contextPaths} onChange={(event) => setContextPaths(event.target.value)} placeholder="src/main.py, README.md" />
                  </label>
                  <label className="stream-checkbox">
                    <input
                      type="checkbox"
                      checked={streamResponse}
                      onChange={(event) => setStreamResponse(event.target.checked)}
                    />
                    Stream
                  </label>
                </div>
                <div className="prompt-row">
                  <textarea
                    value={prompt}
                    onChange={(event) => setPrompt(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' && !event.shiftKey) {
                        event.preventDefault()
                        event.currentTarget.form?.requestSubmit()
                      }
                    }}
                    placeholder="Ask Forge to review or explain the codebase…"
                    aria-label="Task prompt"
                    rows={3}
                  />
                  {running ? (
                    <button className="send-button cancel" type="button" onClick={cancelTask}>Cancel</button>
                  ) : (
                    <button className="send-button" type="submit" disabled={!prompt.trim() || !apiKey}>
                      Run
                    </button>
                  )}
                </div>
              </form>
            </section>
          </div>
        ) : (
          <div className="content settings-content">
            <section className="settings-panel">
              <div className="eyebrow"><span className="eyebrow-line" /> LOCAL CONFIGURATION</div>
              <h1>Settings</h1>
              <p>Use a server API key only in a trusted local frontend environment. Vite variables are exposed to the browser.</p>
              <label className="settings-field">
                Forge API key
                <input
                  type="password"
                  value={apiKey}
                  onChange={(event) => setApiKey(event.target.value)}
                  placeholder="Enter FORGE_API_KEY"
                  autoComplete="off"
                />
              </label>
              <button className="primary-button" type="button" onClick={() => {
                setWorkspaceError(apiKey ? null : 'An API key is required.')
                if (apiKey) setView('sessions')
              }}>
                Save and open sessions
              </button>
              {workspaceError && <p className="chat-error" role="alert">{workspaceError}</p>}
            </section>
          </div>
        )}
      </main>
    </div>
  )
}

export default App
