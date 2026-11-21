// 每个请求独占解释器与 Deno 状态，仅加载已安装的 Pyodide。
import { loadPyodide } from 'npm:pyodide@0.27.8'

const encoder = new TextEncoder()
const stdout: string[] = [], stderr: string[] = []
let textBytes = 0
function capture(lines: string[], line: string) {
  textBytes += encoder.encode(line).length + 1
  if (textBytes > 1024 * 1024) throw new Error('沙箱文字输出超过 1 MiB')
  lines.push(line)
}

async function send(result: unknown) {
  const bytes = encoder.encode(JSON.stringify(result))
  let offset = 0
  while (offset < bytes.length) offset += await Deno.stdout.write(bytes.subarray(offset))
}

try {
  const python = await loadPyodide({
    jsglobals: Object.create(null),
    stdout: (line: string) => capture(stdout, line),
    stderr: (line: string) => capture(stderr, line),
  })
  python.globals.set('_avatar_load_packages', async (names: string) => {
    if (names) await python.loadPackage(names.split(','), { messageCallback: () => {}, errorCallback: () => {} })
  })
  const value = await python.runPythonAsync(await Deno.readTextFile(Deno.args[0]))
  try {
    if (textBytes > 1024 * 1024) throw new Error('沙箱文字输出超过 1 MiB')
    const exports = value?.toJs ? value.toJs({dict_converter: Object.fromEntries}) : value
    await send({success: true, stdout, stderr, files: exports?.files ?? []})
  } finally {
    value?.destroy?.()
  }
} catch (error) {
  await send({success: false, stdout, stderr, error: String(error).slice(-4000)})
}
