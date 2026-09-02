import 'dotenv/config';
import path from 'path';
import express from 'express';
import cors from 'cors';
import catalogRouter from './routes/catalog';
import mbProxyRouter from './routes/metabase-proxy';

const app = express();
app.use(express.json()); // parse JSON bodies for proxy routes
const PORT = Number(process.env.PORT ?? 3000);

// ── CORS ──────────────────────────────────────────────────────────────────────
const allowedOrigins = (process.env.CORS_ORIGINS ?? '*')
  .split(',')
  .map(s => s.trim())
  .filter(Boolean);

app.use(cors({
  origin: allowedOrigins.includes('*') ? '*' : (origin, cb) => {
    if (!origin || allowedOrigins.includes(origin)) cb(null, true);
    else cb(new Error(`Origin ${origin} not allowed by CORS`));
  },
}));

// ── Health ────────────────────────────────────────────────────────────────────
app.get('/health', (_req, res) => {
  res.json({ status: 'ok', ts: new Date().toISOString() });
});

// ── Routes ────────────────────────────────────────────────────────────────────
app.use('/v1/catalog-management', catalogRouter);
app.use('/v1/mb', mbProxyRouter);

// ── Dashboard (serves the HTML at /) ─────────────────────────────────────────
const DASH = path.resolve(__dirname, '../../catalog-dashboard.html');
app.get('/', (_req, res) => res.sendFile(DASH));

// ── Start ─────────────────────────────────────────────────────────────────────
app.listen(PORT, () => {
  console.log(`catalog-api listening on http://localhost:${PORT}`);
  console.log(`  Dashboard → http://localhost:${PORT}/`);
  console.log('  GET /health');
  console.log('  GET /v1/catalog-management');
  console.log('  GET /v1/mb/session  (Metabase auth proxy)');
  console.log('  GET /v1/mb/query    (Metabase data proxy)');
});
