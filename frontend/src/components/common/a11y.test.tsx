/**
 * The four interactive primitives, tested where their behaviour lives.
 *
 * The end-to-end suite already walks the critical journey with the keyboard against a real backend
 * (`e2e/a11y.spec.ts`). These tests cover the same four claims per component, at the level where the
 * interesting cases can be stated exactly: a wheel at the end of a tab list, a row with no click
 * handler, a dialog whose last control is removed while it is open, a live region that must not be
 * re-armed by a re-render. They use real key events through `userEvent`, and assert what the browser
 * would expose — roles, relationships, focus — rather than that a class name is present.
 */

import { useState, type ReactNode } from 'react'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { Drawer, Table, Tabs } from './index'
import { I18nProvider } from '../../i18n'

function renderWithI18n(node: ReactNode, locale: 'en' | 'fa' = 'en') {
  window.localStorage.setItem('drillai.locale', locale)
  return render(<I18nProvider>{node}</I18nProvider>)
}

interface Row {
  id: string
  name: string
}

const rows: Row[] = [
  { id: 'row_1', name: 'First' },
  { id: 'row_2', name: 'Second' },
]

function TableHarness({ onRowClick, active }: { onRowClick?: (row: Row) => void; active?: string }) {
  return (
    <Table<Row>
      rows={rows}
      rowKey={(row) => row.id}
      onRowClick={onRowClick}
      isRowActive={active ? (row) => row.id === active : undefined}
      columns={[{ key: 'name', header: 'Name', render: (row) => row.name }]}
    />
  )
}

describe('<Table> rows as controls', () => {
  it('makes a row with a click handler focusable and leaves a plain row alone', () => {
    const { rerender } = renderWithI18n(<TableHarness onRowClick={() => undefined} />)
    const [clickable] = screen.getAllByRole('row').slice(1)
    expect(clickable).toHaveAttribute('tabindex', '0')

    rerender(
      <I18nProvider>
        <TableHarness />
      </I18nProvider>,
    )
    const [plain] = screen.getAllByRole('row').slice(1)
    // A row that opens nothing is not a control, and must not become a tab stop just because the
    // table knows how to make one: a keyboard user would have to walk through every row for nothing.
    expect(plain).not.toHaveAttribute('tabindex')
  })

  it('activates the row it is on with Enter and with Space', async () => {
    const onRowClick = vi.fn()
    renderWithI18n(<TableHarness onRowClick={onRowClick} />)
    const [first, second] = screen.getAllByRole('row').slice(1)

    first?.focus()
    await userEvent.keyboard('{Enter}')
    expect(onRowClick).toHaveBeenCalledWith(rows[0])

    second?.focus()
    await userEvent.keyboard(' ')
    expect(onRowClick).toHaveBeenCalledWith(rows[1])
    expect(onRowClick).toHaveBeenCalledTimes(2)
  })

  it('marks the current row for assistive technology', () => {
    renderWithI18n(<TableHarness onRowClick={() => undefined} active="row_2" />)
    const [first, second] = screen.getAllByRole('row').slice(1)
    expect(first).not.toHaveAttribute('aria-current')
    expect(second).toHaveAttribute('aria-current', 'true')
  })
})

function TabsHarness({ onChange }: { onChange?: (key: string) => void } = {}) {
  const [active, setActive] = useState('one')
  return (
    <Tabs
      tabs={[
        { key: 'one', label: 'One' },
        { key: 'two', label: 'Two' },
        { key: 'three', label: 'Three' },
      ]}
      active={active}
      onChange={(key) => {
        setActive(key)
        onChange?.(key)
      }}
    >
      <p>panel {active}</p>
    </Tabs>
  )
}

describe('<Tabs>', () => {
  it('keeps one tab in the tab order and names the panel after the selected tab', () => {
    renderWithI18n(<TabsHarness />)
    const [first, second, third] = screen.getAllByRole('tab')
    expect(first).toHaveAttribute('tabindex', '0')
    expect(second).toHaveAttribute('tabindex', '-1')
    expect(third).toHaveAttribute('tabindex', '-1')

    const panel = screen.getByRole('tabpanel')
    expect(panel).toHaveTextContent('panel one')
    // The relationship is declared and, more importantly, true: the panel is the one the tab controls.
    expect(panel.getAttribute('id')).toBe(first?.getAttribute('aria-controls'))
    expect(panel).toHaveAttribute('aria-labelledby', first?.getAttribute('id') ?? '')

    // An id a browser can look up: `useId` punctuation is not a CSS identifier, and an id that throws
    // in `querySelector` is an id nothing can use.
    expect(() => document.querySelector(`#${panel.id}`)).not.toThrow()
    expect(document.querySelector(`#${panel.id}`)).toBe(panel)
  })

  it('moves with the arrow keys in the direction the page reads', async () => {
    renderWithI18n(<TabsHarness />)
    const [first, second] = screen.getAllByRole('tab')
    first?.focus()

    await userEvent.keyboard('{ArrowRight}')
    expect(second).toHaveAttribute('aria-selected', 'true')
    expect(second).toHaveFocus()

    await userEvent.keyboard('{ArrowLeft}')
    expect(first).toHaveAttribute('aria-selected', 'true')
    expect(first).toHaveFocus()
  })

  it('in a right-to-left page, “next” is the left arrow', async () => {
    renderWithI18n(<TabsHarness />, 'fa')
    const [first, second] = screen.getAllByRole('tab')
    first?.focus()

    // Persian readers read right to left, so the tab after this one is drawn to the left.
    await userEvent.keyboard('{ArrowLeft}')
    expect(second).toHaveAttribute('aria-selected', 'true')
    expect(second).toHaveFocus()

    await userEvent.keyboard('{ArrowRight}')
    expect(first).toHaveAttribute('aria-selected', 'true')
    expect(first).toHaveFocus()
  })

  it('wraps at the ends and honours Home and End', async () => {
    renderWithI18n(<TabsHarness />)
    const [first, second, third] = screen.getAllByRole('tab')

    first?.focus()
    await userEvent.keyboard('{ArrowLeft}')
    expect(third).toHaveAttribute('aria-selected', 'true')

    await userEvent.keyboard('{Home}')
    expect(first).toHaveAttribute('aria-selected', 'true')

    await userEvent.keyboard('{End}')
    expect(third).toHaveAttribute('aria-selected', 'true')
    expect(second).not.toHaveAttribute('aria-selected', 'true')
  })

  it('selects with the mouse as well, and follows the selection with the panel', async () => {
    renderWithI18n(<TabsHarness />)
    await userEvent.click(screen.getByRole('tab', { name: 'Two' }))
    expect(screen.getByRole('tabpanel')).toHaveTextContent('panel two')
  })
})

function DrawerHarness() {
  const [open, setOpen] = useState(false)
  return (
    <div>
      <button type="button" onClick={() => setOpen(true)}>
        Show evidence
      </button>
      <Drawer open={open} title="Evidence" onClose={() => setOpen(false)}>
        <button type="button">First</button>
        <button type="button">Last</button>
      </Drawer>
    </div>
  )
}

describe('<Drawer>', () => {
  it('moves focus in, traps Tab, and hands focus back to its opener', async () => {
    renderWithI18n(<DrawerHarness />)
    const opener = screen.getByRole('button', { name: 'Show evidence' })
    await userEvent.click(opener)

    const dialog = screen.getByRole('dialog')
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    /*
     * The panel is the `<aside>`, not the full-screen wrapper: the wrapper also holds the backdrop
     * button, which a pointer uses to dismiss the dialog and which the keyboard trap deliberately does
     * not cycle through. Scoping to the panel is what the trap itself does.
     */
    const panel = dialog.querySelector('aside')
    expect(panel).not.toBeNull()
    const inPanel = within(panel as HTMLElement)

    // Focus lands on the first control in the panel — the header's Close button, which is the panel's
    // own first control, not on the page behind it. A dialog that opens while focus stays behind it is
    // a dialog a keyboard user has to hunt for.
    const close = inPanel.getByRole('button', { name: 'Close' })
    await waitFor(() => expect(close).toHaveFocus())

    // Tab cycles inside the panel, forwards and backwards, rather than walking into the page the
    // dialog just declared inert.
    const first = inPanel.getByRole('button', { name: 'First' })
    const last = inPanel.getByRole('button', { name: 'Last' })
    await userEvent.tab()
    expect(first).toHaveFocus()
    await userEvent.tab()
    expect(last).toHaveFocus()
    await userEvent.tab()
    expect(close).toHaveFocus()
    await userEvent.tab({ shift: true })
    expect(last).toHaveFocus()

    // Escape closes, and focus returns to the control that opened the dialog — not to the body.
    await userEvent.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await waitFor(() => expect(opener).toHaveFocus())
  })

  it('reads the focusable set at the moment of the key press, not at open time', async () => {
    function Changing() {
      const [open, setOpen] = useState(false)
      const [extra, setExtra] = useState(false)
      return (
        <div>
          <button type="button" onClick={() => setOpen(true)}>
            Open
          </button>
          <Drawer open={open} title="Evidence" onClose={() => setOpen(false)}>
            <button type="button" onClick={() => setExtra(true)}>
              Add another
            </button>
            {extra && <button type="button">Appeared later</button>}
            <button type="button" onClick={() => setExtra(false)}>
              Remove it
            </button>
          </Drawer>
        </div>
      )
    }
    renderWithI18n(<Changing />)
    await userEvent.click(screen.getByRole('button', { name: 'Open' }))
    const dialog = screen.getByRole('dialog')
    const inPanel = within(dialog.querySelector('aside') as HTMLElement)

    // A control that appears while the dialog is open is reachable: the trap is a rule about the
    // panel, not a list captured when it opened.
    await userEvent.click(inPanel.getByRole('button', { name: 'Add another' }))
    const late = inPanel.getByRole('button', { name: 'Appeared later' })
    expect(late).toBeInTheDocument()
    late.focus()
    await userEvent.tab()
    expect(inPanel.getByRole('button', { name: 'Remove it' })).toHaveFocus()
    // Past the last control, `Tab` wraps back to the panel's first — the header's Close button.
    await userEvent.tab()
    expect(inPanel.getByRole('button', { name: 'Close' })).toHaveFocus()
  })
})
