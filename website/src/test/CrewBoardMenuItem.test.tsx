/**
 * "Crew board" — the work-item board's entry point in the session menu.
 *
 * The contract worth locking is visibility, not navigation: the board is keyed
 * on a conductor, so the entry must not exist for a session that conducts
 * nothing. That is the common case, and a dead entry leading to an empty page
 * is exactly what SendToInstanceSubmenu's self-hiding contract avoids.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const mocks = vi.hoisted(() => ({
  crewBoard: vi.fn(),
}))
vi.mock('../api/client', () => ({
  SEARCH_MIN_CHARS: 2,
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))

import CrewBoardMenuItem from '../components/CrewBoardMenuItem'

/** Plain Item stub — jsdom cannot drive a real Radix menu, so the row list is
 *  rendered against a button, the same way InstanceSendItems is tested. */
function ItemStub({ onSelect, children }: {
  readonly onSelect?: (e: Event) => void
  readonly children?: React.ReactNode
}) {
  return <button onClick={() => onSelect?.(new Event('select'))}>{children}</button>
}

function renderItem(slotKey = 'chat-1750-1') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <CrewBoardMenuItem slotKey={slotKey} Item={ItemStub} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('CrewBoardMenuItem', () => {
  beforeEach(() => { mocks.crewBoard.mockReset() })

  it('renders no menu entry for a session that owns no work ledger', async () => {
    // What /api/crew-board answers for a session with no ledger: 404 no_ledger.
    mocks.crewBoard.mockRejectedValue(Object.assign(new Error('no_ledger'), { status: 404 }))
    renderItem()
    // Awaiting the rejection first, so this asserts a settled absence rather
    // than merely catching the query still in flight.
    await expect(mocks.crewBoard.mock.results[0]?.value).rejects.toThrow()
    expect(screen.queryByText('Crew board')).toBeNull()
  })

  it('renders the entry for a conductor session, linking to its own board', async () => {
    mocks.crewBoard.mockResolvedValue({
      conductor: 'chat-1750-1', goal: 'ship it', round: 2, items: [],
      channels_available: false,
    })
    renderItem()
    expect(await screen.findByText('Crew board')).toBeTruthy()
  })
})
