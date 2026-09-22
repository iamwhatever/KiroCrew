/**
 * Screenshot evidence for the account modal's Refresh button and the pill's
 * automatic /usage fallback surface.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server and
 * answers every /api/** call from fixtures through `stubDashboardApi`. No
 * gateway, no dashboard auth, no kiro-cli. Only two routes are scene-specific:
 *
 *   GET  /api/sessions/usage          the cached reading the top-bar pill polls.
 *                                     Per scene: a reading, a stale reading, or a
 *                                     500 (the `failed` state). Once a POST has
 *                                     answered a reading the GET serves that
 *                                     reading, as the gateway does -- it replaces
 *                                     its cache before answering the POST, so the
 *                                     pill's refetch after a click sees the result.
 *   POST /api/sessions/usage/refresh  the click. Per scene it answers the reading
 *                                     (200 `{usage}`), the same reading dimmed
 *                                     `stale`, the parked marker, a 500, 409
 *                                     `refresh_in_flight`, or hangs until released
 *                                     so the pending state can be photographed.
 *
 * Frames, each in light and dark:
 *   01-reading         meter with the compact Refresh beside it
 *   02-failed          the no-reading notice with Refresh as the primary action
 *   03-pending         Refresh disabled, spinner, "Refreshing…", POST in flight
 *   04-stale           a dimmed reading with the assertive stale line + Refresh
 *   05-in-flight       the 409 as a quiet status line: progress, not an error
 *   06-display         Settings > Display > View with no credit-meter control
 *   07-checked-at      the "Checked at" row after a refresh returned a reading
 *   08-paused          the parked scrape as an ErrorNotice with the hand-off
 *   09-failed-refresh  a refresh that failed (500) as an ErrorNotice with the hand-off
 *   10-stale-refresh   a refresh that returned the earlier reading: dimmed meter,
 *                      no checked-at stamp, the outcome reported with the hand-off
 *
 * Every frame is ASSERTED before it is shot: exactly one Refresh, no cost words,
 * the pending button disabled, the meter reads the refreshed numbers, exactly one
 * POST per click, the hand-off present on every error and absent on the 409, and
 * no switch on Display -- claims about the DOM that a picture alone cannot settle.
 *
 * Usage: node scripts/capture-usage-refresh.mjs [outDir]
 *   (npm run build first; the harness serves website/dist)
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || join(process.env.KIROCREW_SCRATCH || '/tmp', 'usage-refresh-evidence')
mkdirSync(OUT, { recursive: true })

const REFRESH = { name: /^Refresh$/ }
const REFRESHING = { name: 'Refreshing…' }
const NOTICE = 'Could not read your balance.'
const STALE_LINE = 'Showing an earlier reading; the latest refresh did not return one.'
const IN_FLIGHT_LINE = 'A refresh is already running.'
const PAUSED_LINE = 'Refreshes are paused because recent ones failed. Try again in about 60 min.'
const FAILED_REFRESH_LINE = 'Could not refresh the balance.'
const STALE_REFRESH_LINE = 'The refresh did not return a new reading. Showing the earlier one.'
const COST_WORDS = /spends? a few|spent|Check balance|owner only|only the person/i

const READING = {
  usage: {
    credits_plan: 2000, credits_used: 636, credits_overage: 0, plan: 'KIRO PRO+',
    resets: '2026-10-01', overage_rate: 0.04, cost_usd: 0,
    email: 'owner@example.com', account_type: 'SocialGoogle',
    bonus_credits: [{ name: 'Welcome bonus', used: 500, total: 500, days_left: 13 }],
  },
}
const STALE = { usage: { ...READING.usage, stale: true } }

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, older than the
// system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const { srv, base } = await serveDist()
const browser = await chromium.launch({ env: browserEnv, executablePath: chromiumExecutable() })
let failures = 0
const fail = msg => { console.error(`FAIL: ${msg}`); failures += 1 }
const shot = async (page, name) => {
  const out = join(OUT, `${name}.png`)
  await page.screenshot({ path: out })
  console.log('wrote', out)
}

/**
 * One page per scene.
 *   `get`     what GET /api/sessions/usage serves first: 'reading' | 'stale' | 'failed' (500)
 *   `refresh` what the POST answers: 'reading' (200) | 'stale' (200, dimmed prior reading)
 *             | 'parked' (200 + skipped marker) | 'error' (500) | 'in-flight' (409) | 'hang' (held open)
 * Returns the page, the POST count, and the release handle for 'hang'.
 */
async function scene(theme, get, refresh) {
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 2 })
  page.on('pageerror', e => fail(`page error: ${e.message}`))
  let posts = 0
  let cache = get === 'stale' ? STALE : get === 'reading' ? READING : null
  let release = () => {}
  const held = new Promise(resolve => { release = resolve })
  const extra = async (path, route) => {
    if (path === '/api/sessions/usage/refresh') {
      posts += 1
      if (refresh === 'hang') await held
      if (refresh === 'in-flight') {
        await json(route, { error: 'A refresh is already running', code: 'refresh_in_flight' }, 409)
      } else if (refresh === 'error') {
        await json(route, { error: 'usage store unreadable' }, 500)
      } else if (refresh === 'parked') {
        await json(route, { usage: { available: false }, skipped: 'scrape_parked', retry_after: 3600 })
      } else if (refresh === 'stale') {
        cache = STALE
        await json(route, STALE)
      } else {
        cache = READING
        await json(route, READING)
      }
      return true
    }
    if (path === '/api/sessions/usage') {
      if (cache) await json(route, cache)
      else await json(route, { error: 'usage store unreadable' }, 500)
      return true
    }
    return false
  }
  await stubDashboardApi(page, { theme, extra })
  // Pin the locale: without it the SPA picks one from the environment and the
  // shot comes out in whatever language the runner happens to negotiate.
  await page.addInitScript(() => { localStorage.setItem('mc-lang', 'en') })
  return { page, count: () => posts, release: () => release() }
}

/**
 * Open the account modal from the top-bar pill and return its dialog locator.
 * The pill lives in the shell's top bar, so any dashboard route carries it; the
 * Display settings page is used because it needs no chat slots to render and is
 * the same page the 06 frames photograph.
 */
async function openModal(page) {
  await page.goto(base + '/settings/display', { waitUntil: 'domcontentloaded' })
  const pill = page.getByRole('button', { name: /Kiro credit usage/ }).first()
  await pill.waitFor({ state: 'visible', timeout: 15000 })
  // Let the shell's boot-time reflows (container-query rungs, first metric
  // poll, the query's own retry on a 500) finish so the click lands on a
  // settled pill.
  await page.waitForTimeout(2500)
  await pill.click()
  const dialog = page.getByRole('dialog', { name: 'Kiro Account' })
  await dialog.waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(400)
  return dialog
}

async function assertOneRefreshNoCostWords(dialog, theme, frame) {
  if ((await dialog.getByRole('button', REFRESH).count()) !== 1) fail(`${theme} ${frame}: expected exactly one Refresh`)
  const text = (await dialog.textContent()) || ''
  if (COST_WORDS.test(text)) fail(`${theme} ${frame}: cost or ownership words on the modal: ${text.slice(0, 200)}`)
  if (/usage_text_scrape_enabled|config\.json/.test(text)) fail(`${theme} ${frame}: a config key is named`)
}

for (const theme of ['light', 'dark']) {
  /* ── 01 reading: meter + compact Refresh ────────────────────────────────── */
  {
    const { page, count } = await scene(theme, 'reading', 'reading')
    const dialog = await openModal(page)
    const meter = dialog.getByRole('progressbar', { name: 'Kiro credit usage' })
    if ((await meter.getAttribute('aria-valuenow')) !== '636') fail(`${theme}: meter does not read 636`)
    await assertOneRefreshNoCostWords(dialog, theme, '01')
    if (!(await dialog.getByRole('button', REFRESH).isEnabled())) fail(`${theme}: Refresh not enabled beside the meter`)
    if ((await dialog.getByText(STALE_LINE).count()) !== 0) fail(`${theme}: stale line shown on a fresh reading`)
    if (count() !== 0) fail(`${theme}: a POST was issued before any click`)
    await shot(page, `01-reading-${theme}`)
    await page.close()
  }

  /* ── 02 failed: notice + primary Refresh; then the click fills the meter ── */
  {
    const { page, count } = await scene(theme, 'failed', 'reading')
    const dialog = await openModal(page)
    if ((await dialog.getByText(NOTICE).count()) !== 1) fail(`${theme}: no-reading notice missing`)
    if ((await dialog.getByRole('progressbar').count()) !== 0) fail(`${theme}: a meter rendered with no reading`)
    await assertOneRefreshNoCostWords(dialog, theme, '02')
    await shot(page, `02-failed-${theme}`)

    await dialog.getByRole('button', REFRESH).click()
    const meter = dialog.getByRole('progressbar', { name: 'Kiro credit usage' })
    await meter.waitFor({ state: 'visible', timeout: 10000 })
    if ((await meter.getAttribute('aria-valuenow')) !== '636') fail(`${theme}: meter does not read the refreshed 636`)
    if ((await dialog.getByText(NOTICE).count()) !== 0) fail(`${theme}: notice still shown next to a live meter`)
    if ((await dialog.getByText(/Checked at/).count()) !== 1) fail(`${theme}: check time missing after refresh`)
    if (count() !== 1) fail(`${theme}: expected exactly one POST for one click, got ${count()}`)
    // The pill outside the modal reads the same numbers: one query feeds both.
    // It pools the bonus grant into its compact readout (636 + 500 used of
    // 2000 + 500), so the expected text is the compacted total.
    const pillText = await page.getByRole('button', { name: /Kiro credit usage/ }).first().textContent()
    if (!/1\.1K\s*\/\s*2\.5K/.test(pillText || '')) fail(`${theme}: top-bar pill did not update (${pillText})`)
    await page.close()
  }

  /* ── 03 pending: disabled, spinner, "Refreshing…" ───────────────────────── */
  {
    const { page, count, release } = await scene(theme, 'failed', 'hang')
    const dialog = await openModal(page)
    await dialog.getByRole('button', REFRESH).click()
    const pending = dialog.getByRole('button', REFRESHING)
    await pending.waitFor({ state: 'visible', timeout: 10000 })
    if (await pending.isEnabled()) fail(`${theme}: pending button is not disabled`)
    if ((await pending.getAttribute('aria-busy')) !== 'true') fail(`${theme}: pending button lacks aria-busy`)
    // A second press cannot reach the wire: the button is disabled.
    await pending.click({ force: true }).catch(() => {})
    await page.waitForTimeout(300)
    if (count() !== 1) fail(`${theme}: expected one POST while pending, got ${count()}`)
    await shot(page, `03-pending-${theme}`)
    release()
    await dialog.getByRole('progressbar').waitFor({ state: 'visible', timeout: 10000 })
    await page.close()
  }

  /* ── 04 stale: dimmed reading, the fact stated, Refresh beside it ───────── */
  {
    const { page } = await scene(theme, 'stale', 'reading')
    const dialog = await openModal(page)
    if ((await dialog.getByText(STALE_LINE).count()) !== 1) fail(`${theme}: stale line missing`)
    if ((await dialog.getByText(/may be/).count()) !== 0) fail(`${theme}: stale copy hedges`)
    await assertOneRefreshNoCostWords(dialog, theme, '04')
    await shot(page, `04-stale-${theme}`)
    await page.close()
  }

  /* ── 05 in-flight: the 409 as quiet progress, no alarm, no hand-off ───────── */
  {
    const { page, count } = await scene(theme, 'failed', 'in-flight')
    const dialog = await openModal(page)
    await dialog.getByRole('button', REFRESH).click()
    const status = dialog.getByRole('status').filter({ hasText: IN_FLIGHT_LINE })
    await status.waitFor({ state: 'visible', timeout: 10000 })
    if ((await dialog.getByRole('alert').count()) !== 0) fail(`${theme}: a 409 rendered as an alert`)
    if ((await dialog.getByRole('button', { name: /Ask the agent/i }).count()) !== 0) {
      fail(`${theme}: a 409 offers the agent hand-off`)
    }
    if ((await dialog.getByRole('progressbar').count()) !== 0) fail(`${theme}: a meter rendered after a 409`)
    if (!(await dialog.getByRole('button', REFRESH).isEnabled())) fail(`${theme}: Refresh not re-enabled after the refusal`)
    if (count() !== 1) fail(`${theme}: expected one POST, got ${count()}`)
    await page.waitForTimeout(300)
    await shot(page, `05-in-flight-${theme}`)
    await page.close()
  }

  /* ── 06 Settings > Display: no control for the scrape ───────────────────── */
  {
    const { page } = await scene(theme, 'reading', 'reading')
    await page.goto(base + '/settings/display', { waitUntil: 'domcontentloaded' })
    const view = page.getByRole('heading', { name: /^View$/ }).first()
    await view.waitFor({ state: 'visible', timeout: 15000 })
    await page.waitForTimeout(900)
    const text = (await page.textContent('main')) || (await page.textContent('body')) || ''
    if (/credit|balance|usage_text_scrape/i.test(text)) fail(`${theme}: Settings > Display still mentions the credit meter`)
    await view.scrollIntoViewIfNeeded()
    await page.waitForTimeout(200)
    await shot(page, `06-display-${theme}`)
    await page.close()
  }

  /* ── 07 checked-at: a refresh that returned a reading is stamped ─────────── */
  {
    const { page } = await scene(theme, 'reading', 'reading')
    const dialog = await openModal(page)
    if ((await dialog.getByText(/Checked at/).count()) !== 0) fail(`${theme}: checked-at shown before any refresh`)
    await dialog.getByRole('button', REFRESH).click()
    await dialog.getByText(/Checked at/).waitFor({ state: 'visible', timeout: 10000 })
    if ((await dialog.getByRole('alert').count()) !== 0) fail(`${theme}: an alert rendered after a successful refresh`)
    if ((await dialog.getByText(STALE_LINE).count()) !== 0) fail(`${theme}: stale line shown on a fresh reading`)
    await assertOneRefreshNoCostWords(dialog, theme, '07')
    await page.waitForTimeout(300)
    await shot(page, `07-checked-at-${theme}`)
    await page.close()
  }

  /* ── 08 paused: the parked scrape as ErrorNotice with the hand-off ───────── */
  {
    const { page, count } = await scene(theme, 'failed', 'parked')
    const dialog = await openModal(page)
    await dialog.getByRole('button', REFRESH).click()
    await dialog.getByRole('alert').filter({ hasText: PAUSED_LINE }).waitFor({ state: 'visible', timeout: 10000 })
    if ((await dialog.getByRole('button', { name: /Ask the agent/i }).count()) !== 1) fail(`${theme}: paused notice has no hand-off`)
    if ((await dialog.getByRole('progressbar').count()) !== 0) fail(`${theme}: a meter rendered on a parked refresh`)
    if (count() !== 1) fail(`${theme}: expected one POST, got ${count()}`)
    await page.waitForTimeout(300)
    await shot(page, `08-paused-${theme}`)
    await page.close()
  }

  /* ── 09 failed refresh: a 500 as ErrorNotice with the hand-off ───────────── */
  {
    const { page, count } = await scene(theme, 'failed', 'error')
    const dialog = await openModal(page)
    await dialog.getByRole('button', REFRESH).click()
    await dialog.getByRole('alert').filter({ hasText: FAILED_REFRESH_LINE }).waitFor({ state: 'visible', timeout: 10000 })
    if ((await dialog.getByRole('button', { name: /Ask the agent/i }).count()) !== 1) fail(`${theme}: failed notice has no hand-off`)
    if ((await dialog.getByRole('progressbar').count()) !== 0) fail(`${theme}: a meter rendered after a failed refresh`)
    if (!(await dialog.getByRole('button', REFRESH).isEnabled())) fail(`${theme}: Refresh not re-enabled after the failure`)
    if (count() !== 1) fail(`${theme}: expected one POST, got ${count()}`)
    await page.waitForTimeout(300)
    await shot(page, `09-failed-refresh-${theme}`)
    await page.close()
  }

  /* ── 10 stale refresh: the earlier reading came back, not stamped as fresh ── */
  {
    const { page, count } = await scene(theme, 'failed', 'stale')
    const dialog = await openModal(page)
    await dialog.getByRole('button', REFRESH).click()
    await dialog.getByRole('alert').filter({ hasText: STALE_REFRESH_LINE }).waitFor({ state: 'visible', timeout: 10000 })
    const meter = dialog.getByRole('progressbar', { name: 'Kiro credit usage' })
    if ((await meter.getAttribute('aria-valuenow')) !== '636') fail(`${theme}: the earlier reading did not render`)
    if ((await dialog.getByText(/Checked at/).count()) !== 0) fail(`${theme}: a stale refresh was stamped as checked now`)
    if ((await dialog.getByText(STALE_LINE).count()) !== 1) fail(`${theme}: stale line missing under the dimmed meter`)
    if ((await dialog.getByRole('button', { name: /Ask the agent/i }).count()) !== 1) fail(`${theme}: stale-refresh notice has no hand-off`)
    if (count() !== 1) fail(`${theme}: expected one POST, got ${count()}`)
    await page.waitForTimeout(300)
    await shot(page, `10-stale-refresh-${theme}`)
    await page.close()
  }
}

await browser.close()
srv.close()
if (failures) {
  console.error(`\n${failures} assertion(s) failed -- frames are not admissible evidence.`)
  process.exit(1)
}
console.log(`wrote 20 asserted frames to ${OUT}`)
