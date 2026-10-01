// Load a session log through DeepSeek Harness's own reader and print the
// model-visible transcript the harness would send.
// Usage: node _rt_realcheck.mjs <logfile> [--full]
import fs from 'node:fs';
import zlib from 'node:zlib';
import { createRequire } from 'node:module';

const require = createRequire('D:/Work/ZCodehelp/_dshrt/dsh/node_modules/@deepseek-ai/dsh-session-format-catalog/lib/index.js');
const logFile = process.argv[2];
const full = process.argv.includes('--full');

const buf = fs.readFileSync(logFile);
const text = (logFile.endsWith('.zstd') ? zlib.zstdDecompressSync(buf) : buf).toString('utf8');
const lines = text.split('\n').filter(l => l.trim());
const header = JSON.parse(lines[0]);
const events = lines.slice(1).map(l => JSON.parse(l));

const session = require('@deepseek-ai/dsh-session');
const catalog = require('@deepseek-ai/dsh-session-format-catalog');

console.log('header  :', header.id, '| v' + header.version, '|', header.cwd);
console.log('events  :', events.length);

// 1. Header validation, exactly as the format catalog performs it.
const restore = catalog.sessionFormatCatalog.createRestore(
  header, { validation: 'current', recovery: undefined });
try {
  restore.restoreArtifact({ events, inheritedEventCount: 0 });
  console.log('artifact:', 'OK');
} catch (e) {
  console.log('artifact: FAIL —', e.message);
}

// 2. Session construction: envelope, surface ops, message invariants, and the
//    projection into the model-visible message list.
const restored = { header, events, inheritedEventCount: 0 };
try {
  const s = session.Session.fromRestore(
    session.SessionId(restored.header.id), restored.events, restored.header,
    session.SessionLogOffset(0), 'detached',
    catalog.currentSessionMessageProjections);
  console.log('session :', 'OK');

  const msgs = typeof s.deriveMessages === 'function' ? s.deriveMessages() : null;
  if (!msgs) {
    console.log('surface :', Object.keys(s.surface ?? {}).join(', ') || '(none)');
    process.exit(0);
  }
  console.log('surface :', msgs.length, 'model-visible messages');

  let calls = 0, results = 0, texts = 0, reason = 0;
  for (const m of msgs) {
    const c = m.content;
    if (typeof c === 'string') continue;
    for (const b of c ?? []) {
      if (b.type === 'tool-call' || b.type === 'tool_use') calls++;
      else if (b.type === 'tool-result' || b.type === 'tool_result') results++;
      else if (b.type === 'text') texts++;
      else if (b.type === 'reasoning' || b.type === 'thinking') reason++;
    }
  }
  console.log('  tool calls  :', calls, '| tool results:', results);
  console.log('  text blocks :', texts, '| reasoning  :', reason);

  if (full) {
    console.log('\n--- transcript ---');
    msgs.forEach((m, i) => {
      const c = m.content;
      if (typeof c === 'string') {
        console.log(String(i).padStart(3), m.role.padEnd(9), '|', c.slice(0, 100).replace(/\n/g, ' '));
        return;
      }
      const kinds = (c ?? []).map(b => b.type).join('+');
      const t = (c ?? []).find(b => b.type === 'text');
      console.log(String(i).padStart(3), m.role.padEnd(9), '|', kinds.padEnd(20), '|',
                  (t?.text ?? '').slice(0, 70).replace(/\n/g, ' '));
    });
  }
} catch (e) {
  console.log('session : FAIL —', e.message);
}
