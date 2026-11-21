import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App'

function jsonResponse(payload: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  } as Response
}

describe('Forge dashboard and chat', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  afterEach(() => {
    cleanup()
  })

  it('shows an honest offline state when dashboard requests fail', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new Error('Network unavailable')
    }))

    render(<App />)

    expect(await screen.findByText('Not connected')).toBeInTheDocument()
    expect(screen.getByText('Network unavailable')).toBeInTheDocument()
  })

  it('can configure a key, create a session, and submit a read-only task', async () => {
    const sessionId = 'a'.repeat(32)
    const fetchMock = vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
      const url = String(input)
      const method = options?.method ?? 'GET'

      if (url.endsWith('/health')) return jsonResponse({ status: 'ok', service: 'forge-api' })
      if (url.endsWith('/providers')) {
        return jsonResponse({
          default_model: 'ollama',
          providers: [{ name: 'ollama', kind: 'local', configured: true }],
        })
      }
      if (url.endsWith('/sessions') && method === 'POST') {
        return jsonResponse({
          id: sessionId,
          title: 'New chat',
          created_at: '2026-11-21T00:00:00Z',
          updated_at: '2026-11-21T00:00:00Z',
          turn_count: 0,
        })
      }
      if (url.endsWith(`/sessions/${sessionId}`) && method === 'GET') {
        return jsonResponse({
          id: sessionId,
          title: 'New chat',
          created_at: '2026-11-21T00:00:00Z',
          updated_at: '2026-11-21T00:00:00Z',
          turn_count: 2,
          turns: [
            { role: 'user', content: 'Explain main.py', created_at: '2026-11-21T00:00:00Z' },
            { role: 'assistant', content: 'main.py starts the application.', created_at: '2026-11-21T00:00:01Z' },
          ],
          outcomes: [],
        })
      }
      if (url.endsWith('/sessions') && method === 'GET') {
        return jsonResponse([{
          id: sessionId,
          title: 'New chat',
          created_at: '2026-11-21T00:00:00Z',
          updated_at: '2026-11-21T00:00:00Z',
          turn_count: 2,
        }])
      }
      if (url.endsWith('/agent/tasks') && method === 'POST') {
        return jsonResponse({
          generation: { model: 'qwen2.5-coder:7b', content: 'main.py starts the application.', done: true },
          workspace_root: '/workspace',
          context_paths: ['main.py'],
          session_id: sessionId,
          read_only: true,
        })
      }
      throw new Error(`Unexpected request: ${method} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    render(<App />)

    await user.click(screen.getByRole('button', { name: 'Settings' }))
    await user.type(screen.getByLabelText('Forge API key'), 'b'.repeat(40))
    await user.click(screen.getByRole('button', { name: 'Save and open sessions' }))

    await waitFor(() => expect(screen.getByRole('heading', { name: 'New conversation' })).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: '+' }))

    await waitFor(() => expect(screen.getByRole('heading', { name: 'New chat' })).toBeInTheDocument())
    await user.type(screen.getByLabelText('Task prompt'), 'Explain main.py')
    await user.click(screen.getByRole('button', { name: 'Run' }))

    await waitFor(() => expect(screen.getByText('main.py starts the application.')).toBeInTheDocument())

    const taskCall = fetchMock.mock.calls.find(([url, options]) =>
      String(url).endsWith('/agent/tasks') && options?.method === 'POST'
    )
    expect(taskCall).toBeDefined()
  })

  it('shows a sanitized API error after session loading fails', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/health')) return jsonResponse({ status: 'ok', service: 'forge-api' })
      if (url.endsWith('/providers')) {
        return jsonResponse({ default_model: 'ollama', providers: [] })
      }
      if (url.endsWith('/sessions')) {
        return jsonResponse({
          error: { message: 'A valid API key is required.' },
        }, 401)
      }
      throw new Error(`Unexpected request: ${url}`)
    }))

    const user = userEvent.setup()
    render(<App />)

    await user.click(screen.getByRole('button', { name: 'Settings' }))
    await user.type(screen.getByLabelText('Forge API key'), 'b'.repeat(40))
    await user.click(screen.getByRole('button', { name: 'Save and open sessions' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('A valid API key is required.')
  })

  it('can cancel an in-progress task', async () => {
    const sessionId = 'c'.repeat(32)
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
      const url = String(input)
      const method = options?.method ?? 'GET'
      if (url.endsWith('/health')) return jsonResponse({ status: 'ok', service: 'forge-api' })
      if (url.endsWith('/providers')) {
        return jsonResponse({ default_model: 'ollama', providers: [] })
      }
      if (url.endsWith('/sessions') && method === 'GET') return jsonResponse([])
      if (url.endsWith('/sessions') && method === 'POST') {
        return jsonResponse({
          id: sessionId,
          title: 'Review request',
          created_at: '2026-11-21T00:00:00Z',
          updated_at: '2026-11-21T00:00:00Z',
          turn_count: 0,
        })
      }
      if (url.endsWith(`/sessions/${sessionId}`) && method === 'GET') {
        return jsonResponse({
          id: sessionId,
          title: 'Review request',
          created_at: '2026-11-21T00:00:00Z',
          updated_at: '2026-11-21T00:00:00Z',
          turn_count: 0,
          turns: [],
          outcomes: [],
        })
      }
      if (url.endsWith('/agent/tasks') && method === 'POST') {
        return new Promise<Response>((_resolve, reject) => {
          options?.signal?.addEventListener('abort', () => {
            reject(new DOMException('Aborted', 'AbortError'))
          })
        })
      }
      throw new Error(`Unexpected request: ${method} ${url}`)
    }))

    const user = userEvent.setup()
    render(<App />)

    await user.click(screen.getByRole('button', { name: 'Settings' }))
    await user.type(screen.getByLabelText('Forge API key'), 'b'.repeat(40))
    await user.click(screen.getByRole('button', { name: 'Save and open sessions' }))
    await user.click(await screen.findByRole('button', { name: '+' }))
    await user.type(screen.getByLabelText('Task prompt'), 'Review this repository')
    await user.click(screen.getByRole('button', { name: 'Run' }))

    await user.click(await screen.findByRole('button', { name: 'Cancel' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Task cancelled.')
  })
})
