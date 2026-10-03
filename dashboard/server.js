const express = require("express");
const path = require("path");
const http = require("http");
const https = require("https");
const crypto = require("crypto");

const app = express();

const DASHBOARD_PORT = parseInt(process.env.DASHBOARD_PORT || "5001", 10);
const DASHBOARD_HOST = process.env.DASHBOARD_HOST || "127.0.0.1";
const DASHBOARD_PASSWORD = process.env.GPUHARBOR_DASHBOARD_PASSWORD;
if (!DASHBOARD_PASSWORD) {
  throw new Error("Set GPUHARBOR_DASHBOARD_PASSWORD before starting the dashboard");
}
app.use((req, res, next) => {
  if (req.path === "/api/health") return next();
  const expected = Buffer.from("Basic " + Buffer.from("admin:" + DASHBOARD_PASSWORD).toString("base64"));
  const supplied = Buffer.from(req.headers.authorization || "");
  if (supplied.length === expected.length && crypto.timingSafeEqual(supplied, expected)) return next();
  res.set("WWW-Authenticate", 'Basic realm="GPUHarbor", charset="UTF-8"');
  return res.status(401).send("Authentication required");
});
const WORKER_URL = process.env.WORKER_URL || "http://localhost:5000";
const AUTH_TOKEN = process.env.GPUHARBOR_AUTH_TOKEN || "";

// Try to load token from worker's .env if not set
if (!AUTH_TOKEN) {
  try {
    const fs = require("fs");
    const envPath = path.join(process.env.GPUHARBOR_STORAGE_ROOT || "/workspace/gpuharbor", "worker.env");
    if (fs.existsSync(envPath)) {
      const envContent = fs.readFileSync(envPath, "utf-8");
      const match = envContent.match(
        /^GPUHARBOR_AUTH_TOKEN=["']?(.+?)["']?\s*$/m
      );
      if (match) process.env.GPUHARBOR_AUTH_TOKEN = match[1];
    }
  } catch {}
}

const getToken = () => process.env.GPUHARBOR_AUTH_TOKEN || "";

// Serve static files
app.use(express.static(path.join(__dirname, "public")));

// Proxy helper — streams the worker response back to the browser
function proxyToWorker(req, res) {
  const workerPath = req.originalUrl.replace(/^\/api/, "/v1");
  const target = new URL(workerPath, WORKER_URL);
  const lib = target.protocol === "https:" ? https : http;

  const headers = { Accept: "application/json" };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  // For SSE requests, pass through Accept header
  if (req.query.follow === "true") {
    headers["Accept"] = "text/event-stream";
  }

  const proxyReq = lib.get(target.href, { headers }, (proxyRes) => {
    // For SSE, stream through
    if (
      proxyRes.headers["content-type"] &&
      proxyRes.headers["content-type"].includes("text/event-stream")
    ) {
      res.writeHead(proxyRes.statusCode, {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        Connection: "keep-alive",
      });
      proxyRes.pipe(res);
      return;
    }

    let body = "";
    proxyRes.on("data", (chunk) => {
      body += chunk;
      if (Buffer.byteLength(body) > 8 * 1024 * 1024) proxyRes.destroy(new Error("Response too large"));
    });
    proxyRes.on("error", () => { if (!res.headersSent) res.status(502).json({error: "Worker response interrupted"}); else res.destroy(); });
    proxyRes.on("end", () => {
      res.status(proxyRes.statusCode);
      res.set("Content-Type", "application/json");
      try {
        const data = JSON.parse(body);
        if (data.spec) data.spec.env = Object.fromEntries(Object.keys(data.spec.env || {}).map(key => [key, "[redacted]"]));
        delete data.spec_json;
        delete data.process_marker;
        res.json(data);
      } catch { res.send(body); }
    });
  });

  proxyReq.on("error", (err) => {
    if (!res.headersSent) res.status(502).json({ error: "Worker unreachable", detail: err.message });
    else res.destroy();
  });

  // Handle client disconnect for SSE
  res.on("close", () => proxyReq.destroy());
}

// API proxy routes
app.get("/api/status", proxyToWorker);
app.get("/api/jobs", proxyToWorker);
app.get("/api/jobs/:id", proxyToWorker);
app.get("/api/jobs/:id/logs", proxyToWorker);
app.get("/api/jobs/:id/artifacts", proxyToWorker);

// Health check for the dashboard itself
app.get("/api/health", (_req, res) => {
  res.json({ status: "ok", dashboard: true });
});

// SPA fallback
app.get("*", (_req, res) => {
  res.sendFile(path.join(__dirname, "public", "index.html"));
});

app.listen(DASHBOARD_PORT, DASHBOARD_HOST, () => {
  console.log(`GPUHarbor Dashboard running at http://${DASHBOARD_HOST}:${DASHBOARD_PORT}`);
  console.log(`Proxying to worker at ${WORKER_URL}`);
});
