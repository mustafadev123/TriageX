// Runs vitest with an upper-case drive letter in the working directory.
// On Windows, a shell whose cwd is "c:\..." (lower case) makes Node load two copies of
// vitest ("Vitest failed to find the current suite"). Normalising the cwd avoids that.
import { spawnSync } from 'node:child_process'
import path from 'node:path'

const cwd = process.cwd().replace(/^[a-z]:/, (drive) => drive.toUpperCase())
const result = spawnSync(
  process.execPath,
  [path.join(cwd, 'node_modules', 'vitest', 'vitest.mjs'), ...process.argv.slice(2)],
  { cwd, stdio: 'inherit' },
)
process.exit(result.status ?? 1)
