/* Optional, offline-only synthetic OCR comparison worker. Emits transcripts only to its
 * parent process through stdout; the parent stores transcript-free metrics. Never use on
 * customer documents. Dependencies and language data must already exist on disk. */
const { createRequire } = require('node:module')
const fs = require('node:fs')
const path = require('node:path')

async function main() {
  const [prefix, langPath, pageDir] = process.argv.slice(2)
  if (!prefix || !langPath || !pageDir) throw new Error('expected module prefix, language directory and page directory')
  const requireFromPrefix = createRequire(path.resolve(prefix, 'node_modules/tesseract.js/package.json'))
  const { createWorker } = requireFromPrefix('tesseract.js')
  const worker = await createWorker('eng+fas', 1, {
    corePath: path.resolve(prefix, 'node_modules/tesseract.js-core'),
    langPath: path.resolve(langPath),
    cacheMethod: 'none',
  })
  try {
    await worker.setParameters({ tessedit_pageseg_mode: '6' })
    for (const filename of fs.readdirSync(pageDir).filter((p) => /^cp135-ddr-\d{3}\.png$/.test(p)).sort()) {
      const result = await worker.recognize(path.join(pageDir, filename))
      process.stdout.write(`${JSON.stringify({ id: filename.slice(0, -4), text: result.data.text })}\n`)
    }
  } finally {
    await worker.terminate()
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1 })
