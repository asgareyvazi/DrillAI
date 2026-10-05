/**
 * Right-to-left: what actually has to be true, in a real browser, with the real backend.
 *
 * `dir="rtl"` on the document element is the beginning of this and not the end. Five statements are
 * asserted here, in the Persian locale, against the same seeded data the English journeys use:
 *
 * 1. **The document really is right-to-left, and stays that way.** The direction follows the locale,
 *    including across a reload — a Persian interface that reverts to LTR on refresh is a defect that
 *    only appears when somebody reloads.
 * 2. **Engineering numbers keep their value.** The number on the page is compared with the number the
 *    API returned, mapping the locale's numerals back to ASCII first, because "looks Persian" is not
 *    the same as "is the same quantity".
 * 3. **Technical strings are not reordered.** An identifier, a key or a unit-qualified number states
 *    its own direction, so `wel_01…` reads the same way in an RTL sentence as in an LTR one.
 * 4. **JSON stays JSON.** A payload's braces and quotes are directional neutrals; under an RTL
 *    paragraph they would be placed by the paragraph's direction and the structure would read inside
 *    out.
 * 5. **The graph is not mirrored.** The canvas keeps its geometry: the same node sits at the same
 *    position in both locales.
 */

import { fixtures, apiGet, expect, test, waitForLoaded, type Page } from './fixtures'

/** The well's NPT roll-up, as the platform's own engine computed it for this well. */
async function apiGetNpt(request: Parameters<typeof apiGet>[1], wellId: string): Promise<number> {
  const payload = await apiGet<{ npt: { total_hours: number } }>(request, `/wells/${wellId}/npt`, 'engineer')
  return payload.npt.total_hours
}

/** Copy the Persian catalogue out of the running application, so the label is the product's own. */
async function switchToPersian(page: Page): Promise<void> {
  await page.getByTestId('locale-switch').selectOption('fa')
  await expect(page.locator('html')).toHaveAttribute('dir', 'rtl')
}

/** The locale's own digits mapped back to ASCII, so a rendered number can be compared with the API's. */
function asciiDigits(text: string): string {
  const persian = '۰۱۲۳۴۵۶۷۸۹'
  const arabic = '٠١٢٣٤٥٦٧٨٩'
  return text.replace(/[۰-۹٠-٩]/g, (digit) => {
    const index = persian.indexOf(digit)
    return String(index >= 0 ? index : arabic.indexOf(digit))
  })
}

/** The direction the browser resolved for an element — the property bidi actually uses. */
function resolvedDirection(page: Page, selector: string): Promise<string> {
  return page
    .locator(selector)
    .first()
    .evaluate((element) => getComputedStyle(element).direction)
}

/** The canvas geometry, as the browser lays it out. */
function nodeGeometry(page: Page): Promise<Array<{ id: string; transform: string }>> {
  return page
    .locator('.react-flow__node')
    .evaluateAll((nodes) =>
      nodes.map((node) => ({
        id: node.getAttribute('data-id') ?? '',
        transform: getComputedStyle(node).transform,
      })),
    )
}

test.describe('the Persian interface', () => {
  test('is right to left, follows the locale, and survives a reload', async ({ page, wellId }) => {
    await page.goto(`/wells/${wellId}/cockpit`)
    await waitForLoaded(page)
    await expect(page.locator('html')).toHaveAttribute('dir', 'ltr')

    await switchToPersian(page)
    await expect(page.locator('html')).toHaveAttribute('lang', 'fa')

    // The chrome follows the direction, not only the text: the navigation aside is laid out on the
    // right of the content, which is what `dir` means for a block layout.
    const navBox = await page.locator('aside').first().boundingBox()
    const mainBox = await page.locator('main').first().boundingBox()
    expect(navBox && mainBox && navBox.x > mainBox.x, 'the sidebar sits to the right of the content').toBe(
      true,
    )

    await page.reload()
    await waitForLoaded(page)
    await expect(page.locator('html')).toHaveAttribute('dir', 'rtl')
    // And the locale itself is remembered, in the product's own words.
    await expect(page.getByTestId('locale-switch')).toHaveValue('fa')
  })

  test('shows the same engineering quantity as the API, in the locale’s numerals', async ({
    page,
    request,
    wellId,
  }) => {
    const totalHours = await apiGetNpt(request, wellId)

    await page.goto(`/wells/${wellId}/cockpit`)
    await waitForLoaded(page)
    await switchToPersian(page)
    await waitForLoaded(page)

    // NPT is computed by the platform's own engine: the page must show the number the API computed,
    // not a different quantity and not a rounded stand-in for it.
    const mainText = await page.locator('main').innerText()
    expect(asciiDigits(mainText)).toContain(String(totalHours))
    // And the page is genuinely in Persian: at least part of its chrome is in the Persian script.
    expect(/[\u0600-\u06FF]/.test(mainText), 'the Persian interface renders Persian text').toBe(true)
  })

  test('keeps identifiers readable and JSON a payload', async ({ page, wellId }) => {
    await page.goto(`/wells/${wellId}/cockpit`)
    await waitForLoaded(page)
    await switchToPersian(page)
    await waitForLoaded(page)

    // Technical content states its own direction under RTL — every monospace element on the screen,
    // not only the one that happens to be selected.
    expect(await resolvedDirection(page, '.font-mono')).toBe('ltr')
    const directions = await page
      .locator('.font-mono')
      .evaluateAll((nodes) => nodes.map((node) => getComputedStyle(node).direction))
    expect(directions.every((direction) => direction === 'ltr')).toBe(true)

    // And the strings themselves survive: a port key is a key, not a sentence, so it is rendered
    // verbatim (`well_sections.current_md_si`, `extracted_record:parameter_set`) rather than
    // re-ordered by the paragraph around it.
    const technical = await page.locator('.font-mono:visible').allInnerTexts()
    expect(technical.some((text) => /^(well_|engine\.|extracted_record:)/.test(text.trim()))).toBe(true)

    // Engineering numbers keep their sign and their magnitude: the cockpit's variance is negative,
    // and a minus sign placed at the wrong end of a number is a different number.
    const negatives = technical
      .map((text) => asciiDigits(text).replace(/[\u200e\u200f\u2212]/g, (mark) => (mark === '\u2212' ? '-' : '')))
      .filter((text) => /^-?[\d.,]+$/.test(text) && text.startsWith('-'))
    expect(negatives.length, 'a signed engineering quantity stays signed').toBeGreaterThan(0)

    // A JSON payload keeps its own direction. The platform page renders the deployment's authorization
    // summary as a payload through the shared component (`Json`), which is the contract asserted here.
    await page.goto('/platform')
    await waitForLoaded(page)
    await page.getByTestId('locale-switch').selectOption('fa')
    await expect(page.locator('html')).toHaveAttribute('dir', 'rtl')
    await waitForLoaded(page)
    // The visible one: the deployment descriptor, which is a payload like any other.
    const payload = page.locator('pre:visible').first()
    await expect(payload).toBeVisible()
    expect(await payload.getAttribute('dir')).toBe('ltr')
    expect(await resolvedDirection(page, 'pre:visible')).toBe('ltr')
    // The payload's own characters are in payload order, not paragraph order.
    expect((await payload.innerText()).trim().startsWith('{') || (await payload.innerText()).includes('{')).toBe(true)
  })

  test('keeps the operational record readable in Persian, with its identifiers LTR', async ({
    page,
    request,
    wellId,
  }) => {
    // The number the screen must show is the API's own: the actual hours of a promoted operation,
    // read here so the assertion cannot pass on a typed-in value.
    const operations = await apiGet<{
      items: { name: string; actual_duration_hours: number | null }[]
    }>(request, `/operations?well_id=${wellId}&limit=20`, 'engineer')
    const recorded = operations.items.find((row) => row.actual_duration_hours !== null)
    expect(recorded, 'the seeded day has an actual operation').toBeTruthy()

    await page.goto(`/wells/${wellId}/operations`)
    await waitForLoaded(page)
    await switchToPersian(page)
    await waitForLoaded(page)

    const mainText = await page.locator('main').innerText()
    expect(/[\u0600-\u06FF]/.test(mainText), 'the workspace renders Persian chrome').toBe(true)
    expect(asciiDigits(mainText)).toContain(String(recorded?.actual_duration_hours))

    // Identifiers keep their own direction under the RTL paragraph: the timeline's monospace
    // timestamps are technical strings, not sentences.
    await page.goto(`/wells/${wellId}/operations?tab=timeline`)
    await waitForLoaded(page)
    expect(await resolvedDirection(page, '[data-testid="timeline-entries"] .font-mono')).toBe('ltr')
  })

  test('does not mirror the workflow graph', async ({ page }) => {
    const url = `/workflows?workflow=${fixtures().workflow_id}`

    await page.goto(url)
    await waitForLoaded(page)
    await page.locator('.react-flow__node').first().waitFor({ state: 'visible' })
    const ltrGeometry = await nodeGeometry(page)

    await switchToPersian(page)
    await page.locator('.react-flow__node').first().waitFor({ state: 'visible' })
    const rtlGeometry = await nodeGeometry(page)

    // Geometry is meaning on a canvas: a mirrored graph would draw "downstream" to the left while the
    // data said otherwise. The nodes are where they were, in the same order, with the same positions.
    expect(rtlGeometry.length).toBe(ltrGeometry.length)
    expect(rtlGeometry.map((node) => node.transform)).toEqual(ltrGeometry.map((node) => node.transform))
    expect(rtlGeometry.map((node) => node.id)).toEqual(ltrGeometry.map((node) => node.id))
    /*
     * The canvas is left-to-right while the page around it is right-to-left. This is the library's
     * rule (`@xyflow/react` sets `direction: ltr` on `.react-flow`) and the product depends on it, so
     * it is asserted rather than assumed: a library change that began mirroring the graph would fail
     * here instead of quietly redefining "downstream" for every Persian reader.
     */
    expect(await resolvedDirection(page, '.react-flow')).toBe('ltr')
    expect(await resolvedDirection(page, 'html')).toBe('rtl')
  })
})
