/**
 * Vercel Serverless Function — /api/data
 *
 * Authenticates with Metabase, queries card 12972, and returns
 * the rows as JSON. Responses are cached for 5 minutes at the edge.
 *
 * Required environment variables (set in Vercel dashboard):
 *   METABASE_URL       — e.g. https://metabase.spyne.ai
 *   METABASE_USERNAME  — your Metabase login email
 *   METABASE_PASSWORD  — your Metabase password
 *   METABASE_CARD_ID   — 12972  (or override per deployment)
 */

export default async function handler(req, res) {
  // ── CORS headers (allow the dashboard to call this from any origin) ──────────
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
  if (req.method === 'OPTIONS') { res.status(200).end(); return; }

  const METABASE_URL = process.env.METABASE_URL;
  const USERNAME     = process.env.METABASE_USERNAME;
  const PASSWORD     = process.env.METABASE_PASSWORD;
  const CARD_ID      = process.env.METABASE_CARD_ID || '12972';

  // ── Validate env vars are present ────────────────────────────────────────────
  if (!METABASE_URL || !USERNAME || !PASSWORD) {
    return res.status(500).json({
      success: false,
      error: 'Missing environment variables: METABASE_URL, METABASE_USERNAME, METABASE_PASSWORD',
    });
  }

  try {
    // ── Step 1: Authenticate → get session token ──────────────────────────────
    const sessionRes = await fetch(`${METABASE_URL}/api/session`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ username: USERNAME, password: PASSWORD }),
    });

    if (!sessionRes.ok) {
      const txt = await sessionRes.text();
      return res.status(401).json({ success: false, error: `Metabase auth failed: ${txt}` });
    }

    const { id: sessionToken } = await sessionRes.json();

    // ── Step 2: Run the card query ────────────────────────────────────────────
    const queryRes = await fetch(`${METABASE_URL}/api/card/${CARD_ID}/query`, {
      method:  'POST',
      headers: {
        'Content-Type':       'application/json',
        'X-Metabase-Session': sessionToken,
      },
      body: JSON.stringify({}),
    });

    if (!queryRes.ok) {
      const txt = await queryRes.text();
      return res.status(502).json({ success: false, error: `Metabase query failed: ${txt}` });
    }

    const payload = await queryRes.json();

    // ── Step 3: Transform rows → array of named objects ───────────────────────
    const cols = payload.data.cols.map(c => c.name);   // column names in order
    const rows = payload.data.rows.map(row => {
      const obj = {};
      cols.forEach((col, i) => { obj[col] = row[i]; });
      return obj;
    });

    // ── Step 4: Delete the Metabase session (cleanup) ─────────────────────────
    fetch(`${METABASE_URL}/api/session`, {
      method:  'DELETE',
      headers: { 'X-Metabase-Session': sessionToken },
    }).catch(() => {});   // fire-and-forget; don't block the response

    // ── Step 5: Cache at the edge for 5 min, serve stale while revalidating ───
    res.setHeader('Cache-Control', 's-maxage=300, stale-while-revalidate=600');

    return res.json({
      success:   true,
      card_id:   Number(CARD_ID),
      row_count: rows.length,
      columns:   cols,
      data:      rows,
    });

  } catch (err) {
    console.error('[api/data] error:', err);
    return res.status(500).json({ success: false, error: err.message });
  }
}
