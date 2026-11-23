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

  it('can submit a streaming agent task and observe real-time output', async () => {
    const sessionId = 'd'.repeat(32)
    const encoder = new TextEncoder()
    const sseChunks = [
      'data: {"content": "Streaming ", "done": false}\n\n',
      'data: {"content": "explanation of code.", "done": false}\n\n',
      `data: {"content": "", "done": true, "session_id": "${sessionId}"}\n\n`,
    ]

    const fetchMock = vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
      const url = String(input)
      const method = options?.method ?? 'GET'

      if (url.endsWith('/health')) return jsonResponse({ status: 'ok', service: 'forge-api' })
      if (url.endsWith('/providers')) return jsonResponse({ default_model: 'ollama', providers: [] })
      if (url.endsWith('/sessions') && method === 'GET') {
        return jsonResponse([{
          id: sessionId,
          title: 'Streaming test',
          created_at: '2026-11-21T00:00:00Z',
          updated_at: '2026-11-21T00:00:00Z',
          turn_count: 0,
        }])
      }
      if (url.endsWith(`/sessions/${sessionId}`) && method === 'GET') {
        return jsonResponse({
          id: sessionId,
          title: 'Streaming test',
          created_at: '2026-11-21T00:00:00Z',
          updated_at: '2026-11-21T00:00:00Z',
          turn_count: 2,
          turns: [
            { role: 'user', content: 'Stream code review', created_at: '2026-11-21T00:00:00Z' },
            { role: 'assistant', content: 'Streaming explanation of code.', created_at: '2026-11-21T00:00:01Z' },
          ],
          outcomes: [],
        })
      }
      if (url.endsWith('/agent/tasks/stream') && method === 'POST') {
        const stream = new ReadableStream({
          start(controller) {
            for (const chunk of sseChunks) {
              controller.enqueue(encoder.encode(chunk))
            }
            controller.close()
          },
        })
        return {
          ok: true,
          status: 200,
          headers: new Headers({ 'Content-Type': 'text/event-stream' }),
          body: stream,
        } as unknown as Response
      }
      throw new Error(`Unexpected request: ${method} ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const user = userEvent.setup()
    render(<App />)

    await user.click(screen.getByRole('button', { name: 'Settings' }))
    await user.type(screen.getByLabelText('Forge API key'), 'b'.repeat(40))
    await user.click(screen.getByRole('button', { name: 'Save and open sessions' }))

    await user.click(await screen.findByRole('button', { name: /Streaming test/ }))
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Streaming test' })).toBeInTheDocument())

    await user.click(screen.getByLabelText('Stream'))
    await user.type(screen.getByLabelText('Task prompt'), 'Stream code review')
    await user.click(screen.getByRole('button', { name: 'Run' }))

    await waitFor(() => expect(screen.getByText('Streaming explanation of code.')).toBeInTheDocument())
    const streamCall = fetchMock.mock.calls.find(([url, options]) =>
      String(url).endsWith('/agent/tasks/stream') && options?.method === 'POST'
    )
    expect(streamCall).toBeDefined()
  })

  it('can delete an active session', async () => {
    const sessionId = 'e'.repeat(32)
    let sessionDeleted = false

    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
      const url = String(input)
      const method = options?.method ?? 'GET'

      if (url.endsWith('/health')) return jsonResponse({ status: 'ok', service: 'forge-api' })
      if (url.endsWith('/providers')) return jsonResponse({ default_model: 'ollama', providers: [] })
      if (url.endsWith(`/sessions/${sessionId}`) && method === 'DELETE') {
        sessionDeleted = true
        return { ok: true, status: 204 } as Response
      }
      if (url.endsWith(`/sessions/${sessionId}`) && method === 'GET') {
        return jsonResponse({
          id: sessionId,
          title: 'Session to delete',
          created_at: '2026-11-21T00:00:00Z',
          updated_at: '2026-11-21T00:00:00Z',
          turn_count: 1,
          turns: [{ role: 'user', content: 'Hello', created_at: '2026-11-21T00:00:00Z' }],
          outcomes: [],
        })
      }
      if (url.endsWith('/sessions') && method === 'GET') {
        return jsonResponse(sessionDeleted ? [] : [{
          id: sessionId,
          title: 'Session to delete',
          created_at: '2026-11-21T00:00:00Z',
          updated_at: '2026-11-21T00:00:00Z',
          turn_count: 1,
        }])
      }
      throw new Error(`Unexpected request: ${method} ${url}`)
    }))

    const user = userEvent.setup()
    render(<App />)

    await user.click(screen.getByRole('button', { name: 'Settings' }))
    await user.type(screen.getByLabelText('Forge API key'), 'b'.repeat(40))
    await user.click(screen.getByRole('button', { name: 'Save and open sessions' }))

    await user.click(await screen.findByRole('button', { name: /Session to delete/ }))
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Session to delete' })).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'Delete' }))

    await waitFor(() => expect(screen.getByRole('heading', { name: 'New conversation' })).toBeInTheDocument())
    expect(screen.getByText('No saved sessions.')).toBeInTheDocument()
    expect(sessionDeleted).toBe(true)
  })

  it('validates that an API key is required in settings', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/health')) return jsonResponse({ status: 'ok', service: 'forge-api' })
      if (url.endsWith('/providers')) return jsonResponse({ default_model: 'ollama', providers: [] })
      throw new Error(`Unexpected request: ${url}`)
    }))

    const user = userEvent.setup()
    render(<App />)

    await user.click(screen.getByRole('button', { name: 'Settings' }))
    const keyInput = screen.getByLabelText('Forge API key')
    await user.clear(keyInput)
    await user.click(screen.getByRole('button', { name: 'Save and open sessions' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('An API key is required.')
  })

  it('shows sanitized error message when streaming SSE emits error event', async () => {
    const sessionId = 'f'.repeat(32)
    const encoder = new TextEncoder()
    const errorBlock = 'event: error\r\ndata: {"error": {"code": "provider_rate_limited", "message": "Provider rate limit reached."}}\r\n\r\n'

    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
      const url = String(input)
      const method = options?.method ?? 'GET'

      if (url.endsWith('/health')) return jsonResponse({ status: 'ok', service: 'forge-api' })
      if (url.endsWith('/providers')) return jsonResponse({ default_model: 'gemini', providers: [] })
      if (url.endsWith('/sessions') && method === 'GET') {
        return jsonResponse([{
          id: sessionId,
          title: 'Error stream test',
          created_at: '2026-11-21T00:00:00Z',
          updated_at: '2026-11-21T00:00:00Z',
          turn_count: 0,
        }])
      }
      if (url.endsWith(`/sessions/${sessionId}`) && method === 'GET') {
        return jsonResponse({
          id: sessionId,
          title: 'Error stream test',
          created_at: '2026-11-21T00:00:00Z',
          updated_at: '2026-11-21T00:00:00Z',
          turn_count: 0,
          turns: [],
          outcomes: [],
        })
      }
      if (url.endsWith('/agent/tasks/stream') && method === 'POST') {
        const stream = new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode(errorBlock))
            controller.close()
          },
        })
        return {
          ok: true,
          status: 200,
          headers: new Headers({ 'Content-Type': 'text/event-stream' }),
          body: stream,
        } as unknown as Response
      }
      throw new Error(`Unexpected request: ${method} ${url}`)
    }))

    const user = userEvent.setup()
    render(<App />)

    await user.click(screen.getByRole('button', { name: 'Settings' }))
    await user.type(screen.getByLabelText('Forge API key'), 'b'.repeat(40))
    await user.click(screen.getByRole('button', { name: 'Save and open sessions' }))

    await user.click(await screen.findByRole('button', { name: /Error stream test/ }))
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Error stream test' })).toBeInTheDocument())

    await user.click(screen.getByLabelText('Stream'))
    await user.type(screen.getByLabelText('Task prompt'), 'Trigger rate limit')
    await user.click(screen.getByRole('button', { name: 'Run' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Provider rate limit reached.')
  })

  it('can cancel a streaming task in progress', async () => {
    const sessionId = '1'.repeat(32)

    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
      const url = String(input)
      const method = options?.method ?? 'GET'

      if (url.endsWith('/health')) return jsonResponse({ status: 'ok', service: 'forge-api' })
      if (url.endsWith('/providers')) return jsonResponse({ default_model: 'ollama', providers: [] })
      if (url.endsWith('/sessions') && method === 'GET') {
        return jsonResponse([{
          id: sessionId,
          title: 'Cancel stream test',
          created_at: '2026-11-21T00:00:00Z',
          updated_at: '2026-11-21T00:00:00Z',
          turn_count: 0,
        }])
      }
      if (url.endsWith(`/sessions/${sessionId}`) && method === 'GET') {
        return jsonResponse({
          id: sessionId,
          title: 'Cancel stream test',
          created_at: '2026-11-21T00:00:00Z',
          updated_at: '2026-11-21T00:00:00Z',
          turn_count: 0,
          turns: [],
          outcomes: [],
        })
      }
      if (url.endsWith('/agent/tasks/stream') && method === 'POST') {
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

    await user.click(await screen.findByRole('button', { name: /Cancel stream test/ }))
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Cancel stream test' })).toBeInTheDocument())

    await user.click(screen.getByLabelText('Stream'))
    await user.type(screen.getByLabelText('Task prompt'), 'Cancel me')
    await user.click(screen.getByRole('button', { name: 'Run' }))

    await user.click(await screen.findByRole('button', { name: 'Cancel' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Task cancelled.')
  })
})
