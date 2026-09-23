/**
 * Screenshot harness for the direct-confirm Artifact Deploy surfaces (#12816).
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * and answers every /api/** call from fixtures via Playwright route interception
 * (gateway-free — no kiro-cli, no live backend, no pod token).
 *
 * The Deploy button previews, acknowledges and confirms in place without leaving
 * the page, and every refusal renders as a plain banner with the technical text
 * behind a Details toggle.
 *
 * The whole surface sits behind the `artifact-deploy` Feature Preview, so the
 * harness sets that flag before the app boots; without it these doors are
 * withheld by design and the frames would be empty.
 *
 * Frames:
 *   01-ready          Ready-to-deploy row: profile, Expiry select, Deploy
 *   02-ack            the acknowledgment, pinned to the previewed digest
 *   03-refused        auto-cleanup refusal — plain banner, Details collapsed
 *   04-refused-open   the same refusal with Details open, runnable command shown
 *   06-card-deployed  the same card once a public URL exists
 *   07-card-flag-off  the same card WITHOUT the opt-in: no deploy affordance
 *   08-settings       Settings -> Feature Previews: the toggle and its link
 *   09-done           the success state: URL, copy button, propagation note
 *   10-scan-blocked   the scan block and its explicit override
 *
 * Usage: node scripts/capture-artifact-deploy-direct-confirm.mjs [outDir] [prefix] [distDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/artifact-deploy-direct-confirm'
const PREFIX = process.argv[3] || 'after'
const DIST = process.argv[4] || DEFAULT_DIST
// Committed evidence is captured at 2x for a reviewer zooming in; CAPTURE_SCALE=1
// emits a smaller copy for reading inside a session, where a frame past ~2000px
// on an edge is rejected.
const SCALE = Number(process.env.CAPTURE_SCALE || 2)

mkdirSync(OUT, { recursive: true })

const PREVIEW_FLAG = 'mc-preview-artifact-deploy'

const DRAFT = {
  slug: 'quarterly-dashboard',
  name: 'Quarterly dashboard',
  kind: 'webapp',
  source: 'chat',
  description: 'Fixture webapp artifact for the deploy-state capture',
  tags: [],
  version: 1,
  pinned: false,
  created_at: '2026-09-01T10:00:00.000000+00:00',
  updated_at: '2026-09-20T21:00:00.000000+00:00',
  content: 'A built single-page app.',
  webapp_metadata: {
    slug: 'quarterly-dashboard',
    lifecycle: { status: 'draft' },
    architecture: 'static',
    cost: { estimates: [{ usd: 0.4 }] },
    deploy_target: { provider: 'aws', account: '1234', region: 'us-east-1' },
  },
}

const DEPLOYED = {
  ...DRAFT,
  slug: 'revenue-explorer',
  name: 'Revenue explorer',
  webapp_metadata: {
    ...DRAFT.webapp_metadata,
    slug: 'revenue-explorer',
    lifecycle: { status: 'live' },
    deploy_target: {
      provider: 'aws',
      account: '1234',
      region: 'us-east-1',
      public_url: 'https://kirocrew-web-revenue-explorer.s3-website-us-east-1.amazonaws.com',
    },
  },
}

/** Which artifacts /api/artifacts answers with for the current frame. */
let artifacts = [DRAFT]
/**
 * What POST /api/deploy/deploy answers. 'preview' offers the acknowledgment,
 * 'refusal' the auto-cleanup precondition, 'scan' the secret-scan block, and
 * 'done' a completed deploy carrying its public URL.
 */
let deployMode = 'preview'

const REAPER_REFUSAL = {
  code: 'reaper_required',
  error: 'An expiring deployment needs the auto-cleanup job installed first.',
  details:
    'TTL 24h was requested for site quarterly-dashboard, but no reaper schedule '
    + 'was found for profile default in us-east-1. Without it the bucket would '
    + 'outlive its expiry with no process to remove it.',
  remediation: '/local/home/example/.kirocrew/skills/artifact-deploy/install-reaper.sh --profile default',
}

const PREVIEW = {
  requires_confirm: true,
  content_digest: 'sha256:9f2b41c7e5a8',
  profile: 'default',
  region: 'us-east-1',
  bytes: 184320,
  scan: 'no findings',
  site_id: 'quarterly-dashboard',
}

const extra = async (path, route) => {
  if (path === '/api/deploy/config') {
    return json(route, {
      cloudDeploymentEnabled: true,
      reaperInstallScript:
        '/local/home/example/.kirocrew/skills/artifact-deploy/install-reaper.sh',
    }), true
  }
  if (path === '/api/deploy/profiles') {
    // ProfileEntry objects, not bare names: the saved-profiles table reads
    // name/account/region off each entry, so strings render a blank row.
    return json(route, {
      profiles: [{
        name: 'default', region: 'us-east-1', account: '123456789012',
        verified_at: '2026-09-20T09:00:00+00:00', note: '',
      }],
      default: 'default',
      available: ['default'],
    }), true
  }
  if (path === '/api/deploy/list') {
    return json(route, { sites: [], configured: true, profile_errors: [] }), true
  }
  if (path === '/api/deploy/deploy') {
    if (deployMode === 'refusal') return json(route, REAPER_REFUSAL, 409), true
    if (deployMode === 'scan') {
      return json(route, {
        blocked: true, reason: 'scan', count: 2, credential: true,
        findings: 'aws-access-key-id in config/settings.json:14\nprivate-key in build/key.pem:1',
        content_digest: PREVIEW.content_digest, profile: 'default', region: 'us-east-1',
      }, 409), true
    }
    if (deployMode === 'done') {
      // The flow is two calls. The PREVIEW must answer `requires_confirm` or the
      // hook has nothing to acknowledge and falls into its 'Unexpected response'
      // branch; only the confirmed call carries the URL. Answering both with the
      // URL is what made this frame show a red failure under a success caption.
      let confirming = false
      try { confirming = JSON.parse(route.request().postData() || '{}').confirm === true } catch { /* preview */ }
      if (!confirming) return json(route, PREVIEW), true
      return json(route, {
        url: 'https://kirocrew-web-quarterly-dashboard.s3-website-us-east-1.amazonaws.com',
        profile: 'default', region: 'us-east-1',
      }), true
    }
    return json(route, PREVIEW), true
  }
  // useCloudDeploymentEnabled withholds every deploy control unless the core's
  // own provider is present, matched on id AND endpoint. Left unstubbed this
  // answers [], which hides the buttons for a reason that has nothing to do with
  // the preview flag -- and makes the frames misrepresent the UI.
  if (path === '/api/publish-providers' || path === '/api/artifacts/publish-providers') {
    return json(route, {
      providers: [{
        id: 'deploy-web-aws', endpoint: '/api/deploy/deploy',
        label: 'Publish to public web (your AWS)', kind: 'public_web',
      }],
    }), true
  }
  if (path === '/api/artifacts') return json(route, { artifacts }), true
  const one = /^\/api\/artifacts\/([^/]+)$/.exec(path)
  if (one) {
    const hit = artifacts.find(a => a.slug === one[1])
    if (hit) return json(route, hit), true
  }
  if (path === '/api/artifact-folders') return json(route, { folders: [] }), true
  if (path === '/api/artifacts/session-docs') return json(route, { docs: [] }), true
  return false
}


/**
 * Horizontal clipping is visible in a frame and invisible to a jsdom assertion,
 * which has no layout. Measuring the box keeps this harness honest on its own:
 * a banner extending past the viewport's right edge fails the run.
 */
const assertFits = async (page, label) => {
  const box = await page.locator('[role="alert"], .error-notice').first().boundingBox()
  if (!box) { console.log(`CHECK ${label}: no banner found`); return }
  const vw = page.viewportSize().width
  const right = Math.round(box.x + box.width)
  const ok = right <= vw
  console.log(`CHECK ${label}: banner right edge ${right} vs viewport ${vw} -> ${ok ? 'FITS' : 'CLIPPED'}`)
  if (!ok) process.exitCode = 1
}

const shot = async (page, name, height = 900) => {
  await page.screenshot({
    path: `${OUT}/${PREFIX}-${name}.png`,
    clip: { x: 0, y: 0, width: 1500, height },
  })
  console.log('wrote', `${OUT}/${PREFIX}-${name}.png`)
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 1000 },
    deviceScaleFactor: SCALE,
  })
  // The surface is gated, so opt in before any app code runs.
  await context.addInitScript(flag => {
    window.localStorage.setItem(flag, '1')
  }, PREVIEW_FLAG)
  const page = await context.newPage()

  await stubDashboardApi(page, { extra })
  logPageProblems(page)

  // ── Frame 1: the Ready-to-deploy row, with the Expiry control ──
  await page.goto(base + '/deploy', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  await shot(page, '01-ready')

  // ── Frame 2: the acknowledgment, pinned to the previewed digest ──
  deployMode = 'preview'
  const deployBtn = page.getByRole('button', { name: /quarterly-dashboard/i }).first()
  if (await deployBtn.count()) {
    await deployBtn.click()
    await page.waitForTimeout(1200)
    await shot(page, '02-ack')
  }

  // ── Frame 3: the auto-cleanup refusal — plain banner, Details collapsed ──
  deployMode = 'refusal'
  await page.goto(base + '/deploy', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2000)
  const deployBtn2 = page.getByRole('button', { name: /quarterly-dashboard/i }).first()
  if (await deployBtn2.count()) {
    await deployBtn2.click()
    await page.waitForTimeout(1500)
    await shot(page, '03-refused')

    // ── Frame 4: the same refusal with Details open ──
    const details = page.getByRole('button', { name: /Details/i }).first()
    if (await details.count()) {
      await details.click()
      await page.waitForTimeout(600)
      await shot(page, '04-refused-open')
      await assertFits(page, '04-refused-open')
    }
  }

  // The opted-in card's own in-page button is covered by
  // WebAppArtifactCard.test.tsx rather than by a frame here: this harness's
  // stubbed detail route does not reach that branch, and a frame captioned as
  // a state it does not show is worse than no frame at all.
  deployMode = 'preview'

  // ── Frame 6: the same card once a public URL exists ──
  artifacts = [DEPLOYED]
  await page.goto(base + `/artifacts/${DEPLOYED.slug}`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2500)
  await shot(page, '06-card-deployed', 820)
  const urlSeen = await page.getByText(/kirocrew-web-revenue-explorer/).count()
  console.log(`CHECK 06-card-deployed: public URL rendered -> ${urlSeen > 0 ? 'YES' : 'NO'}`)
  if (urlSeen === 0) process.exitCode = 1

  // ---- Frame 7: the card without the opt-in -- the agent hand-off remains ----
  const plain = await browser.newContext({
    viewport: { width: 1500, height: 1000 }, deviceScaleFactor: SCALE,
  })
  const plainPage = await plain.newPage()
  await stubDashboardApi(plainPage, { extra })
  logPageProblems(plainPage)
  artifacts = [DRAFT]
  await plainPage.goto(base + `/artifacts/${DRAFT.slug}`, { waitUntil: 'domcontentloaded' })
  await plainPage.waitForTimeout(2500)
  await plainPage.screenshot({
    path: `${OUT}/${PREFIX}-07-card-flag-off.png`,
    clip: { x: 0, y: 0, width: 1500, height: 820 },
  })
  console.log('wrote', `${OUT}/${PREFIX}-07-card-flag-off.png`)
  // Without the opt-in the feature is hidden outright: no route by either path,
  // and no copy naming a deploy the reader has no control for.
  const offDirect = await plainPage.getByRole('button', { name: /^Deploy$/ }).count()
  const offAgent = await plainPage.getByRole('button', { name: 'Deploy via agent' }).count()
  const offInvite = await plainPage.getByText(/deploy to your own AWS account/i).count()
  const offPointer = await plainPage.getByText(/available under Settings/i).count()
  const clean = offDirect === 0 && offAgent === 0 && offInvite === 0 && offPointer > 0
  console.log(`CHECK 07-card-flag-off: direct=${offDirect} agent=${offAgent} invite=${offInvite} pointer=${offPointer} -> ${clean ? 'HIDDEN' : 'LEAKS'}`)
  if (!clean) process.exitCode = 1
  await plain.close()

  // ---- Frame 8: the Feature Previews card that carries the opt-in ----
  // Feature Previews is a section of the Developer settings tab, so /settings
  // alone lands on Overview and shoots a frame that does not contain what the
  // caption claims. Navigate straight to the tab and assert the toggle and its
  // ingress link before the shot.
  await page.goto(base + '/settings/developer', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1800)
  const fpHeading = await page.getByText(/Feature Previews/i).count()
  const sw = page.getByRole('switch', { name: 'Artifact Deploy' }).first()
  if (!(await sw.count())) {
    throw new Error('08-settings: the Artifact Deploy preview toggle did not render')
  }
  // Shoot the card in its ON state: the ingress link only exists once the
  // preview is enabled, and the toggle plus that link together are what a
  // reader needs to see. Clicking it here also exercises the real control
  // rather than trusting the seeded flag.
  if ((await sw.getAttribute('aria-checked')) !== 'true') {
    await sw.click()
    await page.waitForTimeout(600)
  }
  const fpChecked = await sw.getAttribute('aria-checked')
  const fpLink = await page.getByText(/Open Artifact Deploy/i).count()
  console.log(`CHECK 08-settings: section=${fpHeading} toggle=on(${fpChecked}) openLink=${fpLink}`)
  if (fpChecked !== 'true' || !fpLink) {
    throw new Error('08-settings: the enabled Artifact Deploy card must show its ingress link')
  }
  await shot(page, '08-settings')

  // ---- Frame 9: the success state, with the URL and the propagation note ----
  deployMode = 'done'
  await page.goto(base + '/deploy', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2000)
  const d1 = page.getByRole('button', { name: /quarterly-dashboard/i }).first()
  if (await d1.count()) {
    await d1.click()
    await page.waitForTimeout(1200)
    // Acknowledge, which is the only route to the confirmed call.
    const ack = page.getByRole('button', { name: /Deploy|Publish|Confirm/i }).last()
    if (await ack.count()) { await ack.click().catch(() => {}); await page.waitForTimeout(1800) }
    await shot(page, '09-done')
    const urlSeen = await page.getByText(/kirocrew-web-quarterly-dashboard/).count()
    const failSeen = await page.getByText(/Unexpected response|Deploy failed/i).count()
    console.log(`CHECK 09-done: url=${urlSeen} failure=${failSeen}`)
    if (urlSeen === 0 || failSeen > 0) process.exitCode = 1
  }

  // ---- Frame 10: the scan block and its explicit override ----
  deployMode = 'scan'
  await page.goto(base + '/deploy', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2000)
  const d2 = page.getByRole('button', { name: /quarterly-dashboard/i }).first()
  if (await d2.count()) {
    await d2.click()
    await page.waitForTimeout(1800)
    await shot(page, '10-scan-blocked')
    const findingSeen = await page.getByText(/aws-access-key-id|private-key/).count()
    const overrideSeen = await page.getByRole('button', { name: /Deploy anyway|Cancel/i }).count()
    console.log(`CHECK 10-scan-blocked: findings=${findingSeen} controls=${overrideSeen}`)
    if (findingSeen === 0 || overrideSeen === 0) process.exitCode = 1
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
