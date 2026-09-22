/**
 * Crewmates page create flow — evidence frames for the PR.
 *
 *   01-empty-<theme>    zero crewmates: the hero in the chat column
 *   02-dialog-<theme>   the real New crewmate dialog, opened from the hero,
 *                       Name + job typed into the real form
 *   02b-dialog-advanced-<theme>  the same dialog with Advanced unfolded
 *                       (workspace / model / triggers / session colour)
 *   03-created-<theme>  Radar is the only row; its chat is open with the
 *                       seeded first turn and Radar's greeting
 *   04-empty-mobile-<theme>  390x844: the hero inside the roster list, the
 *                       below-md copy (the chat column is hidden there)
 *   05-post-create-error-<theme>  the create landed but the roster re-read
 *                       failed: the notice + Try again above the chat column
 *
 * Real components against stubbed /api (Vite + Playwright); the backend is
 * exercised by CI only.
 *
 * Usage:
 *   cd website && node scripts/capture-crewmates-page-create.mjs http://127.0.0.1:6931 ../temp-screenshots/crewmates-page-create
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6931'
const OUT = process.argv[3] || '../temp-screenshots/crewmates-page-create'
mkdirSync(OUT, { recursive: true })

const RADAR = {
  name: 'Radar', slug: 'radar', bound: true, slot_key: 'member-radar', running: false,
  kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'radar', memory_version: 2, memory_owner: 'Radar',
  model: '', description: 'Triage new GitHub issues every morning', source: 'kirocrew',
  last_active_ts: Math.floor(Date.now() / 1000) - 30,
  last_message: "Hi! I'm Radar. Each morning I'll read the new issues, sort them by what they need, and bring you only the ones that need a decision.",
}

const browser = await chromium.launch()
let failed = false
function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

async function newPage(theme, scene, viewport = { width: 1440, height: 900 }) {
  const page = await browser.newPage({ viewport, deviceScaleFactor: 1 })
  const members = scene === 'done' ? [RADAR] : []
  let rosterReads = 0
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    const json = (body) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
    if (path === '/api/members') {
      rosterReads += 1
      // roster-fail: the first read (arrival) is empty and fine; the re-read
      // after the create rejects, which is the failure the notice is about.
      if (scene === 'roster-fail' && rosterReads > 1) return route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"roster unavailable"}' })
      return json({ members, default_agent: 'kirocrew' })
    }
    if (path === '/api/agents' && route.request().method() === 'POST') return json({ ok: true })
    if (path === '/api/crons') return json({ jobs: [] })
    if (path === '/api/webhooks') return json({ tokens: [] })
    if (path === '/api/agents') return json({ agents: [], default_agent: 'kirocrew' })
    if (path === '/api/agents/installed') return json([
      { name: 'kirocrew' }, { name: 'kirocrew-autofix' }, { name: 'kirocrew-research' }, { name: 'kirocrew-lite' },
    ])
    if (path === '/api/workspaces') return json({ workspaces: [{ name: 'default' }] })
    if (path === '/api/config/default-agent') return json({ default_agent: 'kirocrew' })
    if (path === '/api/autonudge') return json({ enabled: true, loops: [] })
    const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
    if (thread) {
      const slug = decodeURIComponent(thread[1]).toLowerCase()
      return json({ slot_key: `member-${slug}`, slug, member: 'Radar', created: false })
    }
    if (/^\/api\/members\/[^/]+\/activity$/.test(path)) return json({ slug: 'radar', member: 'Radar', capped: false, entries: [] })
    if (/^\/api\/members\/[^/]+\/panel$/.test(path)) return json({ panel: null, html: null })
    if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) {
      return json({ key: 'member-radar', title: 'Radar', running: false, messages: [] })
    }
    if (/\/api\/chat\/(tags|pins|folders|tag-columns)$/.test(path)) return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    const isList = /commands|skills|agents$|sessions|files|history|models|artifacts|folders|slots$/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/crewmates-page-create.html?theme=${theme}&scene=${scene}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByText('Crewmates', { exact: true }).first().waitFor()
  return page
}

async function assertTheme(page, theme, tag) {
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)
  const m = bg.match(/\d+/g) || []
  const lum = m.length >= 3 ? (Number(m[0]) + Number(m[1]) + Number(m[2])) / 3 : -1
  check(`${tag} theme ${theme}`, theme === 'light' ? lum > 180 : lum < 90, `body bg=${bg}`)
}

async function assertClean(page, tag) {
  const dialogs = await page.getByRole('dialog').count()
  check(`${tag} no stray dialog`, dialogs === 0, `dialogs=${dialogs}`)
  const rawKeys = await page.getByText(/pages\.[a-zA-Z]+\./).count()
  check(`${tag} no raw i18n keys`, rawKeys === 0, `raw=${rawKeys}`)
  const legacy = await page.getByText(/crew member|agent template|pick a member/i).count()
  check(`${tag} vocabulary`, legacy === 0, `legacy-noun hits=${legacy}`)
}

for (const theme of ['dark', 'light']) {
  // 01 — empty
  {
    const page = await newPage(theme, 'empty')
    const hero = page.locator('section [data-testid=crewmate-empty-hero]')
    await hero.waitFor()
    check(`01-empty-${theme} headline`, await hero.getByText('No crewmates yet', { exact: true }).isVisible(), 'hero headline')
    check(`01-empty-${theme} sentence`, await hero.getByText(/keeps working while you are away/).isVisible(), 'hero sentence')
    check(`01-empty-${theme} cta`, await hero.getByTestId('crewmate-empty-cta').isVisible(), 'New crewmate button')
    check(`01-empty-${theme} count`, (await page.getByTestId('member-count').textContent()) === '0 crewmates', `count=${await page.getByTestId('member-count').textContent()}`)
    // Below-md copy of the hero must not show on a wide viewport.
    const rosterHero = await page.locator('ul [data-testid=crewmate-empty-hero]').isVisible().catch(() => false)
    check(`01-empty-${theme} one hero`, !rosterHero, `roster copy visible=${rosterHero}`)
    await assertClean(page, `01-empty-${theme}`)
    await assertTheme(page, theme, `01-empty-${theme}`)
    await page.screenshot({ path: `${OUT}/01-empty-${theme}.png` })
    await page.close()
  }
  // 02 — dialog, opened from the hero's real button, filled through the real form
  {
    const page = await newPage(theme, 'empty')
    await page.locator('section [data-testid=crewmate-empty-cta]').click()
    const dlg = page.getByRole('dialog')
    await dlg.waitFor()
    await dlg.getByLabel('Name').fill('Radar')
    await dlg.getByLabel('What it looks after').fill('Triage new GitHub issues every morning')
    check(`02-dialog-${theme} title`, await dlg.getByText('New crewmate', { exact: true }).isVisible(), 'dialog title')
    check(`02-dialog-${theme} built from`, await dlg.getByText('Built from', { exact: true }).isVisible() && await dlg.getByRole('combobox', { name: 'Built from' }).getByText('Default agent (kirocrew)').isVisible(), 'Built from + default option')
    check(`02-dialog-${theme} advanced`, (await dlg.getByTestId('crewmate-create-advanced-toggle').getAttribute('aria-expanded')) === 'false', 'Advanced folded')
    check(`02-dialog-${theme} submit`, await dlg.getByRole('button', { name: 'Create crewmate' }).isVisible(), 'primary button')
    check(`02-dialog-${theme} no options error`, (await dlg.getByTestId('crewmate-create-options-error').count()) === 0, 'options loaded')
    await page.waitForTimeout(400) // modal entrance motion
    const rawKeys = await page.getByText(/pages\.[a-zA-Z]+\./).count()
    check(`02-dialog-${theme} no raw i18n keys`, rawKeys === 0, `raw=${rawKeys}`)
    await assertTheme(page, theme, `02-dialog-${theme}`)
    await page.screenshot({ path: `${OUT}/02-dialog-${theme}.png` })
    await page.close()
  }
  // 02b — the same dialog, Advanced unfolded. Taller viewport: the unfolded
  // dialog is ~810px and the modal caps at 90vh, so at 900 the last field
  // (session colour) sits below the fold inside the modal's own scroll.
  {
    const page = await newPage(theme, 'empty', { width: 1440, height: 1100 })
    await page.locator('section [data-testid=crewmate-empty-cta]').click()
    const dlg = page.getByRole('dialog')
    await dlg.waitFor()
    await dlg.getByLabel('Name').fill('Radar')
    await dlg.getByLabel('What it looks after').fill('Triage new GitHub issues every morning')
    await dlg.getByTestId('crewmate-create-advanced-toggle').click()
    const adv = dlg.getByTestId('crewmate-create-advanced')
    await adv.waitFor()
    check(`02b-dialog-advanced-${theme} expanded`, (await dlg.getByTestId('crewmate-create-advanced-toggle').getAttribute('aria-expanded')) === 'true', 'aria-expanded=true')
    check(`02b-dialog-advanced-${theme} workspace`, await adv.getByRole('combobox', { name: 'Workspace' }).isVisible(), 'workspace field')
    check(`02b-dialog-advanced-${theme} model`, await adv.getByRole('combobox', { name: 'Edit default model' }).isVisible(), 'model field')
    check(`02b-dialog-advanced-${theme} triggers`, await adv.getByLabel('Triggers').isVisible(), 'triggers field')
    const colour = adv.getByLabel('Session color hex value')
    check(`02b-dialog-advanced-${theme} colour`, await colour.isVisible(), 'session colour field')
    const box = await colour.boundingBox()
    check(`02b-dialog-advanced-${theme} colour in frame`, !!box && box.y + box.height <= 1100, `colour bottom=${box ? Math.round(box.y + box.height) : 'n/a'}`)
    const legacy = await dlg.getByText(/this member|member color|crew member/i).count()
    check(`02b-dialog-advanced-${theme} vocabulary`, legacy === 0, `legacy-noun hits=${legacy}`)
    await page.waitForTimeout(500) // disclosure + modal motion
    await assertTheme(page, theme, `02b-dialog-advanced-${theme}`)
    await page.screenshot({ path: `${OUT}/02b-dialog-advanced-${theme}.png` })
    await page.close()
  }
  // 03 — created: Radar's chat open with the greeting
  {
    const page = await newPage(theme, 'done')
    await page.getByTestId('member-thread-header').waitFor()
    await page.getByText(/I'm Radar\./).first().waitFor()
    const rows = await page.locator('[data-testid=member-roster] li').count()
    check(`03-created-${theme} one row`, rows === 1, `rows=${rows}`)
    check(`03-created-${theme} count`, (await page.getByTestId('member-count').textContent()) === '1 crewmate', `count=${await page.getByTestId('member-count').textContent()}`)
    check(`03-created-${theme} greeting`, await page.getByText(/I'm Radar\./).first().isVisible(), 'greeting visible')
    check(`03-created-${theme} seed`, await page.getByText(/Your job: Triage new GitHub issues/).first().isVisible(), 'seeded first turn visible')
    await assertClean(page, `03-created-${theme}`)
    await page.waitForTimeout(400)
    await assertTheme(page, theme, `03-created-${theme}`)
    await page.screenshot({ path: `${OUT}/03-created-${theme}.png` })
    await page.close()
  }
}

// 05 — post-create failure: the create landed, the roster re-read did not
for (const theme of ['dark', 'light']) {
  const page = await newPage(theme, 'roster-fail')
  await page.locator('section [data-testid=crewmate-empty-cta]').click()
  const dlg = page.getByRole('dialog')
  await dlg.waitFor()
  await dlg.getByLabel('Name').fill('Radar')
  await dlg.getByLabel('What it looks after').fill('Triage new GitHub issues every morning')
  await dlg.getByRole('button', { name: 'Create crewmate' }).click()
  const notice = page.getByTestId('member-post-create-error')
  await notice.waitFor()
  await page.getByRole('dialog').waitFor({ state: 'detached' }) // exit motion
  check(`05-post-create-error-${theme} text`, await notice.getByText("Radar was created, but the list didn't refresh.").isVisible(), 'notice text')
  check(`05-post-create-error-${theme} retry`, await notice.getByTestId('member-post-create-retry').isVisible(), 'Try again')
  check(`05-post-create-error-${theme} no hero`, (await page.locator('section [data-testid=crewmate-empty-hero]').count()) === 0, 'hero yields to the notice')
  check(`05-post-create-error-${theme} no dialog`, (await page.getByRole('dialog').count()) === 0, 'dialog closed')
  await page.waitForTimeout(400)
  await assertTheme(page, theme, `05-post-create-error-${theme}`)
  await page.screenshot({ path: `${OUT}/05-post-create-error-${theme}.png` })
  await page.close()
}

// 04 — below md: the hero inside the roster list (dark only is enough for the
// layout question, light proves the tokens; shoot both).
for (const theme of ['dark', 'light']) {
  const page = await newPage(theme, 'empty', { width: 390, height: 844 })
  const hero = page.locator('ul [data-testid=crewmate-empty-hero]')
  await hero.waitFor()
  check(`04-empty-mobile-${theme} hero`, await hero.getByText('No crewmates yet', { exact: true }).isVisible(), 'hero in the roster list')
  check(`04-empty-mobile-${theme} cta`, await hero.getByTestId('crewmate-empty-cta').isVisible(), 'New crewmate button')
  const desktopHero = await page.locator('section [data-testid=crewmate-empty-hero]').isVisible().catch(() => false)
  check(`04-empty-mobile-${theme} one hero`, !desktopHero, `chat-column copy visible=${desktopHero}`)
  await assertTheme(page, theme, `04-empty-mobile-${theme}`)
  await page.screenshot({ path: `${OUT}/04-empty-mobile-${theme}.png` })
  await page.close()
}

await browser.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log('all frames verified')
