// Rehearses the 90-second demo against the running app (real backend), in Microsoft Edge by
// default (set REHEARSE_CHANNEL=chrome to use Google Chrome, e.g. on macOS):
// walks every presenter step, takes screenshots, checks the layout fits without scrolling,
// and measures confirmation latency.
//
//   (backend on 127.0.0.1:8000 and `npm run dev` running)
//   npm run rehearse -- <outDir> [width] [height]      e.g. npm run rehearse -- ../docs/screenshots 1366 768
//   REHEARSE_CHANNEL=chrome npm run rehearse -- /tmp/rehearsal 1366 768   (macOS, Google Chrome)
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

const channel = process.env.REHEARSE_CHANNEL || 'msedge'
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
await shot('3-approval')

const replyTimes = []
for (let i = 1; i <= 3; i++) {
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
await browser.close()
