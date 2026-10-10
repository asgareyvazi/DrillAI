/**
 * The configuration editor's contract with the person using it.
 *
 * Two things are asserted here, and both were defects in the previous implementation: text being
 * typed must not be thrown away while it is not yet valid JSON, and an edit must reach the graph as
 * the value the user meant — a number as a number, not as the string "6".
 */

import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { I18nProvider } from '../../i18n'
import { NodeConfigEditor, toConfigSchema } from './NodeConfigEditor'

function Harness({
  initial = {},
  schema,
  onChange,
}: {
  initial?: Record<string, unknown>
  schema?: Parameters<typeof NodeConfigEditor>[0]['schema']
  onChange?: (config: Record<string, unknown>) => void
}) {
  const [config, setConfig] = useState(initial)
  return (
    <I18nProvider>
      <NodeConfigEditor
        config={config}
        schema={schema}
        onChange={(next) => {
          setConfig(next)
          onChange?.(next)
        }}
      />
      <output data-testid="config">{JSON.stringify(config)}</output>
    </I18nProvider>
  )
}

describe('NodeConfigEditor: raw JSON editing', () => {
  it('keeps half-typed JSON on screen instead of discarding it', async () => {
    render(<Harness initial={{ engine_key: 'hydraulics.laminar' }} />)

    const area = screen.getByLabelText('Configuration (JSON)')
    // Typed character by character: `{` alone is the state this test is about.
    fireEvent.change(area, { target: { value: '{' } })
    expect(area).toHaveValue('{')
    fireEvent.change(area, { target: { value: '{"depth": 2412' } })

    // The text stays exactly as typed, and the editor says why it is not applied yet.
    expect(area).toHaveValue('{"depth": 2412')
    expect(await screen.findByText(/not valid JSON yet/i)).toBeInTheDocument()
  })

  it('applies the edit on blur once the text parses, and reports the failure before that', async () => {
    const onChange = vi.fn()
    render(<Harness initial={{}} onChange={onChange} />)

    const area = screen.getByLabelText('Configuration (JSON)')
    fireEvent.change(area, { target: { value: '{"depth": 2412, "unit": "m"}' } })
    expect(onChange).not.toHaveBeenCalled()

    fireEvent.blur(area)
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('config')).toHaveTextContent('{"depth":2412,"unit":"m"}')
  })

  it('refuses an array or a scalar as the whole configuration, and keeps what the node already had', async () => {
    const onChange = vi.fn()
    render(<Harness initial={{ keep: true }} onChange={onChange} />)

    const area = screen.getByLabelText('Configuration (JSON)')
    fireEvent.change(area, { target: { value: '[1, 2, 3]' } })
    fireEvent.blur(area)

    expect(await screen.findByText(/must be a JSON object/i)).toBeInTheDocument()
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByTestId('config')).toHaveTextContent('{"keep":true}')
  })

  it('clears the error once the text parses again', async () => {
    render(<Harness initial={{}} />)

    const area = screen.getByLabelText('Configuration (JSON)')
    fireEvent.change(area, { target: { value: '{"a":' } })
    expect(await screen.findByText(/not valid JSON yet/i)).toBeInTheDocument()

    fireEvent.change(area, { target: { value: '{"a": 1}' } })
    expect(screen.queryByText(/not valid JSON yet/i)).not.toBeInTheDocument()
  })
})

describe('NodeConfigEditor: schema-driven fields', () => {
  const schema = {
    type: 'object',
    required: ['engine_key'],
    properties: {
      engine_key: { type: 'string', title: 'Engine key', description: 'registry key of the engine to run' },
      depth: { type: 'number', minimum: 0, maximum: 12000 },
      basis: { type: 'string', enum: ['events', 'operations'], default: 'events' },
      include_offsets: { type: 'boolean', default: false },
    },
  }

  it('renders a field per schema property, with the required marker and the default', () => {
    render(<Harness initial={{}} schema={schema} />)
    expect(screen.getByLabelText('Engine key')).toBeInTheDocument()
    expect(screen.getByText(/required/)).toBeInTheDocument()
    expect(screen.getAllByText(/default/).length).toBeGreaterThan(0)
  })

  it('writes a number as a number, not as a string', async () => {
    const onChange = vi.fn()
    render(<Harness initial={{}} schema={schema} onChange={onChange} />)

    const depth = screen.getByLabelText('Depth')
    // One keystroke at a time, so the assertions can see the partial values too.
    for (const partial of ['2', '24', '241', '2412']) {
      fireEvent.change(depth, { target: { value: partial } })
    }
    expect(onChange).toHaveBeenLastCalledWith({ depth: 2412 })
    expect(typeof (onChange.mock.calls.at(-1)?.[0] as { depth: unknown }).depth).toBe('number')
    expect(screen.getByTestId('config')).toHaveTextContent('{"depth":2412}')
  })

  it('offers the enum values the schema declares', () => {
    render(<Harness initial={{ basis: 'events' }} schema={schema} />)
    const select = screen.getByLabelText('Basis')
    expect(select).toHaveValue('events')
    expect(Array.from(select.querySelectorAll('option')).map((option) => option.textContent)).toEqual([
      '— not set —',
      'events',
      'operations',
    ])
  })

  it('represents an unset boolean as "not set" rather than as false', async () => {
    const user = userEvent.setup()
    render(<Harness initial={{}} schema={schema} />)
    const select = screen.getByLabelText('Include offsets')
    expect(select).toHaveValue('')

    await user.selectOptions(select, 'false')
    // Choosing false is a decision; leaving it unset is not the same decision.
    expect(screen.getByTestId('config')).toHaveTextContent('{"include_offsets":false}')
  })

  it('lets a field be removed again', async () => {
    const user = userEvent.setup()
    render(<Harness initial={{ engine_key: 'hydraulics.laminar' }} schema={schema} />)
    await user.click(screen.getByTitle('remove engine_key'))
    expect(screen.getByTestId('config')).toHaveTextContent('{}')
  })

  it('keeps the JSON view and the fields in step', async () => {
    const user = userEvent.setup()
    render(<Harness initial={{ engine_key: 'hydraulics.laminar' }} schema={schema} />)

    await user.type(screen.getByLabelText('Engine key'), '-v2')
    expect(screen.getByLabelText('Configuration (JSON)')).toHaveValue(
      JSON.stringify({ engine_key: 'hydraulics.laminar-v2' }, null, 2),
    )
  })

  it('accepts a real node-type schema from the registry', () => {
    const schema = toConfigSchema({
      type: 'object',
      title: 'ReportConfig',
      properties: {
        title: { title: 'Title', type: 'string', minLength: 3 },
        sections: { title: 'Sections', type: 'object', default: {} },
      },
      required: ['title'],
      additionalProperties: false,
    })
    expect(schema?.properties?.title).toMatchObject({ title: 'Title', minLength: 3 })
    expect(schema?.required).toEqual(['title'])
  })

  it('answers null for a schema that is not a property map, so raw JSON stays available', () => {
    expect(toConfigSchema(null)).toBeNull()
    expect(toConfigSchema('{}')).toBeNull()
    expect(toConfigSchema([])).toBeNull()
    expect(toConfigSchema({})).toBeNull()
    expect(toConfigSchema({ properties: [] })).toBeNull()
    // A schema without a property map is still a schema; it simply has no fields to render.
    expect(toConfigSchema({ type: 'object', properties: {}, required: 'title' })?.required).toEqual([])
  })
})
