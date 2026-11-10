// Run an explicit static-analysis job list through the existing REA launcher.
// Keeps its JADX worker alive within one batch; never launches the APK.
import { spawn } from 'node:child_process';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { createInterface } from 'node:readline';
import path from 'node:path';

const launcher = process.env.REA_LAUNCHER;
if (!launcher) throw new Error('Set REA_LAUNCHER to the existing public rea.ps1 launcher');
const packageDir = path.resolve(path.dirname(launcher), '../rea-agents');
const packageInfo = JSON.parse(await readFile(path.join(packageDir, 'package.json'), 'utf8'));
const bin = typeof packageInfo.bin === 'string' ? packageInfo.bin : packageInfo.bin.rea;
const child = spawn(process.execPath, [path.join(packageDir, bin), '--mcp'], {
  stdio: ['pipe', 'pipe', 'pipe'], env: process.env, windowsHide: true,
});
let nextId = 1;
const pending = new Map();
const lines = createInterface({ input: child.stdout });
lines.on('line', line => {
  let message;
  try { message = JSON.parse(line); } catch { return; }
  const request = pending.get(message.id);
  if (!request) return;
  pending.delete(message.id);
  clearTimeout(request.timeout);
  if (message.error) request.reject(new Error(JSON.stringify(message.error)));
  else request.resolve(message.result);
});
child.stderr.on('data', () => {});
child.on('exit', code => {
  for (const request of pending.values()) {
    clearTimeout(request.timeout);
    request.reject(new Error(`REA exited: ${code}`));
  }
  pending.clear();
});
function call(method, params) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => { pending.delete(id); reject(new Error(`${method} timed out`)); }, 300000);
    pending.set(id, { resolve, reject, timeout });
    child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
  });
}
try {
  await call('initialize', { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'bololo-static-analysis', version: '1' } });
  child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
  const catalog = await call('tools/list', {});
  await mkdir('analysis/lan', { recursive: true });
  await writeFile('analysis/lan/rea-tools.json', JSON.stringify(catalog, null, 2));
  const jobs = JSON.parse(await readFile(process.argv[2], 'utf8'));
  for (const job of jobs) {
    if (!catalog.tools.some(tool => tool.name === job.name)) throw new Error(`Unadvertised REA tool: ${job.name}`);
    const result = await call('tools/call', { name: job.name, arguments: job.args });
    let evidence = result;
    const text = result.content?.find(item => item.type === 'text')?.text;
    if (text) { try { evidence = JSON.parse(text); } catch {} }
    await mkdir(path.dirname(job.file), { recursive: true });
    await writeFile(job.file, JSON.stringify(evidence, null, 2));
    console.log(JSON.stringify({ file: job.file, evidence_id: evidence.evidence_id, error: evidence.error ?? evidence.message, keys: Object.keys(evidence.normalized_result ?? {}) }));
  }
} finally {
  child.stdin.end();
  child.kill();
  lines.close();
}
