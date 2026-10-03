const { test } = require('node:test');
const assert = require('node:assert/strict');
const { spawn } = require('node:child_process');
const http = require('node:http');
const path = require('node:path');

test('dashboard requires browser authentication and redacts job credentials', async () => {
  const upstream = http.createServer((req, res) => {
    assert.equal(req.headers.authorization, 'Bearer worker-secret');
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({spec: {env: {HF_TOKEN:'private-value'}}, spec_json:'private-value', process_marker:'private-marker'}));
  });
  await new Promise(resolve => upstream.listen(0, '127.0.0.1', resolve));
  const portProbe = http.createServer();
  await new Promise(resolve => portProbe.listen(0, '127.0.0.1', resolve));
  const dashboardPort = portProbe.address().port;
  await new Promise(resolve => portProbe.close(resolve));
  const child = spawn(process.execPath, [path.resolve(__dirname, '../server.js')], {env: {...process.env, DASHBOARD_PORT:String(dashboardPort), DASHBOARD_HOST:'127.0.0.1', GPUHARBOR_DASHBOARD_PASSWORD:'browser-secret', GPUHARBOR_AUTH_TOKEN:'worker-secret', WORKER_URL:`http://127.0.0.1:${upstream.address().port}`}, stdio:'pipe'});
  const base = `http://127.0.0.1:${dashboardPort}`;
  try {
    let ready = false;
    for (let i=0;i<50;i++) {
      try { ready = (await fetch(base+'/api/health')).ok; if (ready) break; } catch {}
      await new Promise(resolve=>setTimeout(resolve,50));
    }
    assert.ok(ready);
    assert.equal((await fetch(base+'/api/jobs/job_test')).status,401);
    const response = await fetch(base+'/api/jobs/job_test', {headers:{Authorization:'Basic '+Buffer.from('admin:browser-secret').toString('base64')}});
    assert.equal(response.status,200);
    const body = await response.text();
    assert.ok(body.includes('[redacted]'));
    assert.ok(!body.includes('private-value'));
    assert.ok(!body.includes('private-marker'));
  } finally {
    child.kill('SIGTERM');
    await new Promise(resolve => child.on('exit',resolve));
    await new Promise(resolve => upstream.close(resolve));
  }
});
