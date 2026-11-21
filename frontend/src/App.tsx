import { useEffect, useState } from 'react'

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

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000/api/v1'

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

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="/" aria-label="Forge home">
          <span className="brand-mark">F</span>
          <span>forge</span>
        </a>

        <div className="sidebar-label">WORKSPACE</div>
        <nav className="navigation" aria-label="Main navigation">
          <a className="nav-item active" href="#overview" aria-current="page">
            <span className="nav-icon">⌂</span>
            Overview
          </a>
          <span className="nav-item disabled" aria-disabled="true">
            <span className="nav-icon">◷</span>
            Sessions
            <span className="nav-soon">Soon</span>
          </span>
          <span className="nav-item disabled" aria-disabled="true">
            <span className="nav-icon">⚙</span>
            Settings
            <span className="nav-soon">Soon</span>
          </span>
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
            <strong>Overview</strong>
          </div>
          <a className="docs-link" href="https://github.com/Rufidatul726/forge">
            Project repository <span aria-hidden="true">↗</span>
          </a>
        </header>

        <div className="content">
          <section className="welcome">
            <div className="eyebrow"><span className="eyebrow-line" /> YOUR ENGINEERING SPACE</div>
            <h1>Your ideas,<br /><span>under construction.</span></h1>
            <p className="welcome-copy">
              Forge brings your models and codebase together. The workspace is taking shape;
              the agent runtime is the next milestone.
            </p>
            <div className="runtime-note">
              <span className="runtime-indicator" />
              <span><strong>Agent runtime</strong> is not available yet</span>
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
      </main>
    </div>
  )
}

export default App
