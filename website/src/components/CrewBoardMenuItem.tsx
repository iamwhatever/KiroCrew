import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { LayoutList } from 'lucide-react'

import { api } from '../api/client'
import { crewBoardQueryKey } from '../api/crewBoard'
import { i18nT } from '../i18n/t'

interface CrewBoardMenuItemProps {
  /** The session whose board this opens — also the conductor the board is keyed on. */
  readonly slotKey: string
  /** The Radix menu-item primitive of the hosting menu family; items must match their parent menu. */
  readonly Item: React.ComponentType<{
    readonly onSelect?: (e: Event) => void
    readonly children?: React.ReactNode
  }>
}

/**
 * "Crew board" — the entry point to one conductor's work-item board.
 *
 * Renders NOTHING for a session that owns no work ledger, which is most of
 * them: the board is keyed on a conductor and a session that never conducted
 * anything has no items, so an always-present entry would lead almost everyone
 * to an empty page. `/api/crew-board` answers 404 `no_ledger` for such a
 * session and the query simply has no data, the same self-hiding contract
 * SendToInstanceSubmenu uses for an unconfigured feature.
 *
 * The probe is also the cheapest available existence test: it is the very
 * response the page then renders, so opening the menu warms the page's cache
 * instead of adding a round trip of its own. A menu's Content only mounts while
 * it is open (Radix), so nothing is fetched until a user actually opens the menu.
 */
export default function CrewBoardMenuItem({ slotKey, Item }: CrewBoardMenuItemProps) {
  const navigate = useNavigate()

  const { data } = useQuery({
    queryKey: crewBoardQueryKey(slotKey),
    queryFn: () => api.crewBoard(slotKey),
    // The board's own page polls; for a menu the last read is fresh enough, and
    // a ledger does not appear and vanish between two openings of one menu.
    staleTime: 30_000,
    retry: false,
  })

  if (!data) return null

  return (
    <Item onSelect={() => navigate(`/crew-board?conductor=${encodeURIComponent(slotKey)}`)}>
      <LayoutList size={13} className="shrink-0 text-muted" /> {i18nT('components.sessionActionsMenu.crew_board')}
    </Item>
  )
}
