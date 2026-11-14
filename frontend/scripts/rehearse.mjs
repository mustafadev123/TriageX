// Rehearses the 90-second demo against the running app (real backend), in Microsoft Edge by
// default (set REHEARSE_CHANNEL=chrome to use Google Chrome, e.g. on macOS):
// walks every presenter step, takes screenshots, checks the layout fits without scrolling,
// and measures confirmation latency.
//
//   (backend on 127.0.0.1:8000 and `npm run dev` running)
//   npm run rehearse -- <outDir> [width] [height]      e.g. npm run rehearse -- ../docs/screenshots 1366 768
//   REHEARSE_CHANNEL=chrome npm run rehearse -- /tmp/rehearsal 1366 768   (macOS, Google Chrome)
//
// How the staff confirmations arrive (REHEARSE_CONFIRM):
//   simulated (default)  click the labeled "Simulate staff reply" button three times
//   live                 after approval, wait up to REHEARSE_LIVE_WAIT s (default 240) for real
//                        FaceTime / iMessage confirmations (a teammate calls the line), then fill
//                        the rest with labeled simulated replies; times each live confirmation
//   agent                drive the FaceTime agent API directly (card 9999 rejected, 1001 + "I
//                        confirm") with TRIAGEX_AGENT_TOKEN from ../.env: tests the backend path
//                        without a phone; reported as "agent API, no phone", never as a live call
// REHEARSE_BRIEF=0 skips generating the AI brief at 16:30. A JSON summary is written to outDir.
import { readFileSync, writeFileSync } from 'node:fs'
import { chromium } from 'playwright-core'

const [outDir = 'rehearsal', w = '1366', h = '768'] = process.argv.slice(2)
await import('node:fs').then((fs) => fs.mkdirSync(outDir, { recursive: true }))
const width = Number(w)
const height = Number(h)
// localhost, not 127.0.0.1: on macOS the Vite dev server listens on IPv6 (::1) only.
const URL = process.env.REHEARSE_URL || 'http://localhost:5173/'
const log = []
const t0 = Date.now()
const note = (msg) => {
  const line = `[${((Date.now() - t0) / 1000).toFixed(1)}s] ${msg}`
  log.push(line)
  console.log(line)
}

const channel = process.env.REHEARSE_CHANNEL || (process.platform === 'darwin' ? 'chrome' : 'msedge')
const MODE = process.env.REHEARSE_CONFIRM || 'simulated'
if (!['simulated', 'live', 'agent'].includes(MODE)) throw new Error(`REHEARSE_CONFIRM must be simulated, live or agent`)
const LIVE_WAIT_S = Number(process.env.REHEARSE_LIVE_WAIT || 240)
const summary = { mode: MODE, width, height, started_at: new Date().toISOString(), live: [], simulated_ms: [] }

/** TRIAGEX_AGENT_TOKEN from the environment or the repo's .env (never printed). */
function agentToken() {
  if (process.env.TRIAGEX_AGENT_TOKEN) return process.env.TRIAGEX_AGENT_TOKEN
  try {
    const line = readFileSync(new globalThis.URL('../../.env', import.meta.url), 'utf8').split('\n').find((l) => l.startsWith('TRIAGEX_AGENT_TOKEN='))
    return line ? line.slice('TRIAGEX_AGENT_TOKEN='.length).trim() : ''
  } catch {
    return ''
  }
}
const agentApi = async (path, body) => {
  const r = await fetch(`${URL.replace(/\/$/, '')}/api/agent${path}`, {
    method: body ? 'POST' : 'GET',
    headers: { 'content-type': 'application/json', 'x-triagex-agent-token': agentToken() },
    body: body ? JSON.stringify(body) : undefined,
  })
  return { status: r.status, body: await r.json().catch(() => null) }
}
const apiState = () => page.evaluate(async () => (await fetch('/api/demo/state')).json())
const browser = await chromium.launch({ channel, headless: true })
const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 1 })
const consoleErrors = []
page.on('console', (m) => m.type() === 'error' && consoleErrors.push(m.text()))
page.on('pageerror', (e) => consoleErrors.push(String(e)))

const text = (sel) => page.locator(sel).first().innerText()
const clock = () => text('[data-testid="clock"]')
const saved = () => text('[data-testid="hours-saved"]')
const shot = async (name) => {
  await page.waitForTimeout(450)
  await page.screenshot({ path: `${outDir}/${width}x${height}-${name}.png` })
  note(`screenshot ${name} @ ${await clock()} · saved ${await saved()}`)
}
const fits = async () =>
  page.evaluate(() => ({
    scrollW: document.documentElement.scrollWidth,
    scrollH: document.documentElement.scrollHeight,
    w: window.innerWidth,
    h: window.innerHeight,
  }))
const waitClock = async (value) => {
  await page.waitForFunction(
    (v) => document.querySelector('[data-testid="clock"]')?.textContent === v,
    value,
    { timeout: 20000 },
  )
}
const next = async () => page.getByTestId('next-event').click()

await page.goto(URL)
await page.getByText('Monitoring 5 emergency departments').waitFor()
await page.getByRole('button', { name: 'Reset' }).click()
await page.waitForTimeout(600)
const demoStart = Date.now()
note(`initial clock ${await clock()}`)
const layout = await fits()
note(`layout ${JSON.stringify(layout)}`)
await shot('1-initial')

await next() // Start Surge
await waitClock('16:00')
await next() // Show Prediction
await waitClock('16:15')
await page.getByRole('button', { name: 'Approve outreach', exact: true }).waitFor()
await shot('2-prediction')

await next() // moves to 16:30 (does not approve)
await waitClock('16:30')
const approveVisible = await page.getByRole('button', { name: 'Approve outreach', exact: true }).isVisible()
note(`after Next at 16:30, approve button still visible (not auto-approved): ${approveVisible}`)
await page.getByRole('button', { name: 'Approve outreach', exact: true }).click()
await page.getByTestId('confirmed-count').waitFor()
const approvedAt = Date.now()
await shot('3-approval')

if (process.env.REHEARSE_BRIEF !== '0') {
  const t = Date.now()
  await page.getByRole('button', { name: /^Generate/ }).click()
  await page.getByTestId('brief').waitFor({ timeout: 40000 })
  const badge = await page.locator('[data-testid="brief"] span').first().innerText()
  summary.brief = { provider: badge, ms: Date.now() - t }
  note(`AI brief at 16:30: ${badge} in ${Date.now() - t} ms`)
  await shot('3b-brief')
  await page.getByRole('button', { name: 'Close brief' }).click()
}

const confirmedNow = async () => Number((await page.getByTestId('confirmed-count').innerText()).trim().split('/')[0])

if (MODE === 'agent') {
  if (!agentToken()) throw new Error('REHEARSE_CONFIRM=agent needs TRIAGEX_AGENT_TOKEN (env or ../.env)')
  const call = `rehearsal-${Date.now()}`
  await agentApi('/call', { call_id: call, event: 'connected' })
  const card = (id, name) => ({ call_id: call, card_id: id, name, role: 'Registered Nurse · Emergency', availability: 'AVAILABLE', confidence: 0.99 })
  const bad = await agentApi('/card-check', card('TX-RN-9999', 'Sam (fictional RN)'))
  note(`agent API (no phone): TX-RN-9999 -> ${bad.body?.reason}`)
  const good = await agentApi('/card-check', card('TX-RN-1001', 'Avery (fictional RN)'))
  note(`agent API (no phone): TX-RN-1001 -> ${good.body?.reason}`)
  const t = Date.now()
  const conf = await agentApi('/confirm', { call_id: call, card_id: 'TX-RN-1001', verbal_confirmation: 'I confirm' })
  await page.waitForFunction(() => document.querySelector('[data-testid="confirmed-count"]')?.textContent?.startsWith('1'), null, { timeout: 5000 })
  summary.live.push({ channel: 'agent API (no phone)', confirm_to_dashboard_ms: Date.now() - t, result: conf.body?.reason })
  note(`agent API confirm -> dashboard shows 1/3 after ${Date.now() - t} ms`)
  await agentApi('/call', { call_id: call, event: 'ended' })
} else if (MODE === 'live') {
  note(`waiting up to ${LIVE_WAIT_S} s for live FaceTime / iMessage confirmations…`)
  const deadline = approvedAt + LIVE_WAIT_S * 1000
  let seen = 0
  while (Date.now() < deadline) {
    const state = await apiState()
    const req = state.staffing_requests[0]
    const live = (req?.confirmations ?? []).filter((c) => c.source !== 'simulated')
    for (const c of live.slice(seen)) {
      const feed = state.feed.filter((e) => e.origin === 'facetime').map((e) => e.title)
      summary.live.push({ channel: c.source, after_approval_s: Math.round((Date.now() - approvedAt) / 100) / 10, feed })
      note(`live confirmation via ${c.source} (${c.display_name}) ${((Date.now() - approvedAt) / 1000).toFixed(1)} s after approval; FaceTime feed: ${feed.join(' > ')}`)
    }
    seen = live.length
    if (seen >= (req?.requested_staff ?? 3) || req?.status === 'expired') break
    const notice = await page.getByTestId('fallback-notice').innerText().catch(() => null)
    if (notice && !summary.fallback_notice) {
      summary.fallback_notice = notice
      note(`dashboard fallback notice: ${notice}`)
    }
    await page.waitForTimeout(1000)
    if (seen > 0 && Date.now() - approvedAt > 60_000 + 30_000 * seen) break
  }
  if (seen === 0) note('no live confirmation arrived: the rest uses the labeled simulated fallback')
}

const replyTimes = []
for (let i = (await confirmedNow()) + 1; i <= 3; i++) {
  // Measured inside the page: click -> confirmed counter shows the new value on screen.
  const ms = await page.evaluate(
    (n) =>
      new Promise((resolve) => {
        const btn = [...document.querySelectorAll('button')].find((b) => b.textContent?.includes('Simulate staff reply'))
        const t = performance.now()
        const check = () => {
          if (document.querySelector('[data-testid="confirmed-count"]')?.textContent?.startsWith(String(n))) {
            requestAnimationFrame(() => resolve(Math.round(performance.now() - t)))
          } else requestAnimationFrame(check)
        }
        btn.click()
        check()
      }),
    i,
  )
  replyTimes.push(ms)
  const inApp = await page.getByTestId('reply-latency').innerText().catch(() => '?')
  note(`confirmation ${i}/3 visible after ${replyTimes.at(-1)} ms (browser click->DOM); app: "${inApp}"; hero still ${await saved()}`)
  if (i === 1) await shot('4-first-confirmation')
}
await shot('5-confirmed')

await next() // Activate Shift
await waitClock('17:15')
await page.getByTestId('shift-active-banner').waitFor()
await shot('6-shift-active')

await page.locator('input[aria-label="Simulation time"]').fill('25') // scrub to 18:15
await waitClock('18:15')
await shot('7-diverging')

await next() // Reveal Impact
await waitClock('20:45')
await page.getByTestId('final-result').waitFor()
const finalSaved = await saved()
const apiFinal = await page.evaluate(async () => (await (await fetch('/api/demo/state')).json()).impact.primary.delta)
await shot('8-final')
const demoSeconds = (Date.now() - demoStart) / 1000

const drainStart = Date.now()
let pending = -1
for (let k = 0; k < 60; k++) {
  pending = await page.evaluate(async () => (await (await fetch('/api/db/status')).json()).writer.pending ?? 0)
  if (pending === 0) break
  await page.waitForTimeout(500)
}
note(`Tiger Data writes drained (pending=${pending}) ${((Date.now() - drainStart) / 1000).toFixed(1)} s after the reveal`)
await page.keyboard.press('d')
await page.getByText('Technical details').first().waitFor()
await page.waitForTimeout(2500)
await page.screenshot({ path: `${outDir}/${width}x${height}-9-technical.png` })
note('screenshot 9-technical')

// Reset and replay must return to the identical initial state.
await page.keyboard.press('Escape')
await page.getByRole('button', { name: 'Reset' }).click()
await page.getByText('Monitoring 5 emergency departments').waitFor()
note(`after reset: clock ${await clock()} · saved ${await saved()}`)

note(`final hero on screen ${finalSaved} h (API impact.primary.delta captured before reset: ${apiFinal})`)
note(`machine time for the full walkthrough: ${demoSeconds.toFixed(1)} s`)
note(`reply round trips (click -> UI updated): ${replyTimes.join(', ')} ms`)
note(`console errors: ${consoleErrors.length ? consoleErrors.join(' | ') : 'none'}`)
Object.assign(summary, {
  simulated_ms: replyTimes,
  final_hero: finalSaved,
  api_delta: apiFinal,
  machine_seconds: Number(demoSeconds.toFixed(1)),
  layout_fits: layout.scrollW <= layout.w && layout.scrollH <= layout.h,
  console_errors: consoleErrors,
})
writeFileSync(`${outDir}/summary-${width}x${height}.json`, JSON.stringify(summary, null, 2))
note(`summary written to ${outDir}/summary-${width}x${height}.json`)
await browser.close()
