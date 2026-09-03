"""
Spyne SLA Dashboard API — Vercel serverless entry point
Credentials: set as Environment Variables in your Vercel project settings.
"""

import os
import time
import logging
from datetime import datetime

import clickhouse_connect
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config (from Vercel Environment Variables) ──────────────────────────────
CH_HOST     = os.environ.get("CH_HOST",     "leio048s4j.us-east-1.aws.clickhouse.cloud")
CH_PORT     = int(os.environ.get("CH_PORT", "8443"))
CH_USER     = os.environ.get("CH_USER",     "rahul.raj")
CH_PASSWORD = os.environ.get("CH_PASSWORD", "")   # Set this in Vercel dashboard — never hardcode
CACHE_TTL   = int(os.environ.get("CACHE_TTL", "300"))

# ── App ─────────────────────────────────────────────────────────────────────
app = FastAPI(title="Spyne SLA API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── ClickHouse (new client per cold start — serverless has no persistent conn) ──
def get_ch_client():
    return clickhouse_connect.get_client(
        host=CH_HOST,
        port=CH_PORT,
        username=CH_USER,
        password=CH_PASSWORD,
        secure=True,
        connect_timeout=30,
        send_receive_timeout=600,
    )

# ── Simple in-process cache (survives within one warm lambda instance) ───────
_cache: dict = {}

def get_cached(key):
    e = _cache.get(key)
    if e and (time.time() - e["ts"]) < CACHE_TTL:
        return e["data"]
    return None

def set_cached(key, data):
    _cache[key] = {"ts": time.time(), "data": data}

# ── SQL ──────────────────────────────────────────────────────────────────────
SLA_QUERY = """
WITH base AS (
    SELECT
        sk.sku_id                                     AS sku_id,
        sk.mediaId                                    AS mediaId,
        toDate(sk.created_on)                         AS created_day,
        toStartOfMonth(sk.created_on)                 AS created_month,
        ed.customer_segment                           AS customer_segment,
        CASE
            WHEN ed.version = 'v2' THEN
                CASE
                    WHEN isNotNull(m.processedAt) AND m.processedAt != toDateTime64(0, 3)
                        THEN m.processedAt
                    ELSE sk.created_on
                END
            ELSE sk.created_on
        END AS received_at,
        CASE
            WHEN m.outputProcessingList_catalog = '1'
                 AND ed.version = 'v2'
                 AND sk.is_360 IN (0, '0', 'false', 'FALSE') THEN
                CASE
                    WHEN o.partnerId IS NOT NULL
                        THEN r.firstPushedAt
                    WHEN o.partnerId IS NULL
                         AND ed.quality_check = 1 AND ed.enterprise_qc_priority = 0
                        THEN sal_data.qc_done_time
                    WHEN o.partnerId IS NULL
                         AND NOT (ed.quality_check = 1 AND ed.enterprise_qc_priority = 0)
                        THEN sal_data.done_time
                    ELSE NULL
                END
            WHEN ed.version = 'v1'
                 AND sk.is_360 IN (0, '0', 'false', 'FALSE') THEN
                CASE
                    WHEN ed.quality_check = 1 AND ed.enterprise_qc_priority = 0
                        THEN sal_data.qc_done_time
                    ELSE sal_data.done_time
                END
            ELSE NULL
        END AS sentAt,
        CASE
            WHEN m.outputProcessingList_catalog != '1' AND ed.version = 'v2'
                THEN 'Output Processing Off'
            WHEN sentAt IS NULL AND (received_at + INTERVAL 6 HOUR) <= now()
                THEN 'Yes'
            WHEN sentAt IS NOT NULL AND sentAt >= (received_at + INTERVAL 6 HOUR)
                THEN 'Yes'
            ELSE 'No'
        END AS after_6_hrs,
        CASE
            WHEN m.outputProcessingList_catalog != '1' AND ed.version = 'v2'
                THEN 'Output Processing Off'
            WHEN sk.total_frames_no < 1
                THEN 'No Photos'
            WHEN sk.total_frames_no >= 1 AND sentAt IS NOT NULL
                THEN 'Delivered'
            ELSE 'Not Delivered'
        END AS sku_delivery_status
    FROM eventila.ai_sku AS sk
    LEFT JOIN eventila.enterprise_team_details AS etd ON sk.team_id = etd.team_id
    LEFT JOIN eventila.enterprise_details AS ed ON etd.enterprise_id = ed.enterprise_id
    LEFT JOIN PartnerSystem.outputworkflows o
        ON o.teamId = etd.team_id AND o.enterpriseId = etd.enterprise_id AND o.isActive
    LEFT JOIN PartnerSystem.inputworkflows i
        ON i.teamId = etd.team_id AND i.enterpriseId = etd.enterprise_id
        AND i.isActive AND i.createDraft = 'true'
    LEFT JOIN inventory.`dealerVinMapping` AS dvm
        ON dvm.teamId = etd.team_id AND dvm.enterpriseId = etd.enterprise_id
        AND dvm.dealerVinId = sk.dealerVinId
    LEFT JOIN media_management.medias AS m
        ON m.dealerVinId = sk.dealerVinId AND m.mediaId = sk.mediaId
    LEFT JOIN PartnerSystem.rooftopinventories r ON r.dealerVinId = m.dealerVinId
    LEFT JOIN (
        SELECT sal.sku_id,
            nullIf(minIf(sal.created_on, sal.updated_status = 'qc_done'), toDateTime64(0, 6)) AS qc_done_time,
            nullIf(minIf(sal.created_on, sal.updated_status = 'Done'),    toDateTime64(0, 6)) AS done_time
        FROM eventila.sku_activity_log AS sal
        WHERE sal.created_on >= toStartOfMonth(today() - INTERVAL 4 MONTH)
          AND sal.updated_status IN ('qc_done', 'Done')
        GROUP BY sal.sku_id
    ) AS sal_data ON sal_data.sku_id = sk.sku_id
    LEFT JOIN (
        SELECT sal.sku_id,
            max((sal.old_status = 'qc_inprogress' AND sal.updated_status = 'qc_inprogress')
                OR sal.updated_status = 'qc_onhold') AS is_reprocessed
        FROM eventila.sku_activity_log AS sal
        WHERE sal.created_on >= toStartOfMonth(today() - INTERVAL 4 MONTH)
        GROUP BY sal.sku_id
    ) AS reprocess_check ON reprocess_check.sku_id = sk.sku_id
    WHERE sk.created_on >= toStartOfMonth(today() - INTERVAL 4 MONTH)
      AND sk.is_hidden = 0
      AND sk.status NOT IN ('Draft', 'Failed')
      AND toString(sk.is_360) IN ('0', 'false')
      AND ed.is_test_account = 0 AND etd.is_test_account = 0
      AND ed.is_active AND ed.stage IN ('Live') AND ed.category = 'Automobile'
      AND coalesce(reprocess_check.is_reprocessed, 0) = 0
      AND ed.enterprise_id NOT IN (
          '0b4bc56b1','00d2aafe9','197d146c4','18d200080','28733e36c',
          '2LA80M7WO','8e2f0d75a','293e1a285','TaD1VC1Ko','3471c086e',
          '39b5a5268','af5e033aa','c95e31793','caae51a38','L3X0W7YW6',
          '74e1ee1ab','4J8975Z1G','4bc9d1ce6','7KIAEAQQA'
      )
    ORDER BY sk.created_on DESC, sk.mediaId
    LIMIT 1 BY sk.sku_id
)
SELECT 0 AS sort_group, toDate(created_day) AS sort_date, 'day' AS granularity,
    toString(created_day) AS period,
    round(100.0-(countDistinctIf(sku_id,after_6_hrs='Yes')*100.0/nullIf(countDistinct(sku_id),0)),2) AS sla_pct,
    round(quantileExactIf(0.99)(dateDiff('second',received_at,sentAt)/3600.0,sentAt IS NOT NULL AND sentAt>=received_at),2) AS p99_tat_hrs,
    round(quantileExactIf(0.95)(dateDiff('second',received_at,sentAt)/3600.0,sentAt IS NOT NULL AND sentAt>=received_at),2) AS p95_tat_hrs,
    round(100.0-(countDistinctIf(sku_id,customer_segment='Ent' AND after_6_hrs='Yes')*100.0/nullIf(countDistinctIf(sku_id,customer_segment='Ent'),0)),2) AS ent_sla_pct,
    round(quantileExactIf(0.99)(dateDiff('second',received_at,sentAt)/3600.0,customer_segment='Ent' AND sentAt IS NOT NULL AND sentAt>=received_at),2) AS ent_p99_tat_hrs,
    round(quantileExactIf(0.95)(dateDiff('second',received_at,sentAt)/3600.0,customer_segment='Ent' AND sentAt IS NOT NULL AND sentAt>=received_at),2) AS ent_p95_tat_hrs,
    round(100.0-(countDistinctIf(sku_id,customer_segment='Mid' AND after_6_hrs='Yes')*100.0/nullIf(countDistinctIf(sku_id,customer_segment='Mid'),0)),2) AS mid_sla_pct,
    round(quantileExactIf(0.99)(dateDiff('second',received_at,sentAt)/3600.0,customer_segment='Mid' AND sentAt IS NOT NULL AND sentAt>=received_at),2) AS mid_p99_tat_hrs,
    round(quantileExactIf(0.95)(dateDiff('second',received_at,sentAt)/3600.0,customer_segment='Mid' AND sentAt IS NOT NULL AND sentAt>=received_at),2) AS mid_p95_tat_hrs,
    round(100.0-(countDistinctIf(sku_id,customer_segment='Resellers' AND after_6_hrs='Yes')*100.0/nullIf(countDistinctIf(sku_id,customer_segment='Resellers'),0)),2) AS resellers_sla_pct,
    round(quantileExactIf(0.99)(dateDiff('second',received_at,sentAt)/3600.0,customer_segment='Resellers' AND sentAt IS NOT NULL AND sentAt>=received_at),2) AS resellers_p99_tat_hrs,
    round(quantileExactIf(0.95)(dateDiff('second',received_at,sentAt)/3600.0,customer_segment='Resellers' AND sentAt IS NOT NULL AND sentAt>=received_at),2) AS resellers_p95_tat_hrs,
    round(100.0-(countDistinctIf(sku_id,customer_segment='SMB' AND after_6_hrs='Yes')*100.0/nullIf(countDistinctIf(sku_id,customer_segment='SMB'),0)),2) AS smb_sla_pct,
    round(quantileExactIf(0.99)(dateDiff('second',received_at,sentAt)/3600.0,customer_segment='SMB' AND sentAt IS NOT NULL AND sentAt>=received_at),2) AS smb_p99_tat_hrs,
    round(quantileExactIf(0.95)(dateDiff('second',received_at,sentAt)/3600.0,customer_segment='SMB' AND sentAt IS NOT NULL AND sentAt>=received_at),2) AS smb_p95_tat_hrs
FROM base WHERE sku_delivery_status='Delivered' GROUP BY created_day

UNION ALL

SELECT 1 AS sort_group, toDate(created_month) AS sort_date, 'month' AS granularity,
    concat(monthName(created_month),' ',toString(toYear(created_month))) AS period,
    round(100.0-(countDistinctIf(sku_id,after_6_hrs='Yes')*100.0/nullIf(countDistinct(sku_id),0)),2) AS sla_pct,
    round(quantileExactIf(0.99)(dateDiff('second',received_at,sentAt)/3600.0,sentAt IS NOT NULL AND sentAt>=received_at),2) AS p99_tat_hrs,
    round(quantileExactIf(0.95)(dateDiff('second',received_at,sentAt)/3600.0,sentAt IS NOT NULL AND sentAt>=received_at),2) AS p95_tat_hrs,
    round(100.0-(countDistinctIf(sku_id,customer_segment='Ent' AND after_6_hrs='Yes')*100.0/nullIf(countDistinctIf(sku_id,customer_segment='Ent'),0)),2) AS ent_sla_pct,
    round(quantileExactIf(0.99)(dateDiff('second',received_at,sentAt)/3600.0,customer_segment='Ent' AND sentAt IS NOT NULL AND sentAt>=received_at),2) AS ent_p99_tat_hrs,
    round(quantileExactIf(0.95)(dateDiff('second',received_at,sentAt)/3600.0,customer_segment='Ent' AND sentAt IS NOT NULL AND sentAt>=received_at),2) AS ent_p95_tat_hrs,
    round(100.0-(countDistinctIf(sku_id,customer_segment='Mid' AND after_6_hrs='Yes')*100.0/nullIf(countDistinctIf(sku_id,customer_segment='Mid'),0)),2) AS mid_sla_pct,
    round(quantileExactIf(0.99)(dateDiff('second',received_at,sentAt)/3600.0,customer_segment='Mid' AND sentAt IS NOT NULL AND sentAt>=received_at),2) AS mid_p99_tat_hrs,
    round(quantileExactIf(0.95)(dateDiff('second',received_at,sentAt)/3600.0,customer_segment='Mid' AND sentAt IS NOT NULL AND sentAt>=received_at),2) AS mid_p95_tat_hrs,
    round(100.0-(countDistinctIf(sku_id,customer_segment='Resellers' AND after_6_hrs='Yes')*100.0/nullIf(countDistinctIf(sku_id,customer_segment='Resellers'),0)),2) AS resellers_sla_pct,
    round(quantileExactIf(0.99)(dateDiff('second',received_at,sentAt)/3600.0,customer_segment='Resellers' AND sentAt IS NOT NULL AND sentAt>=received_at),2) AS resellers_p99_tat_hrs,
    round(quantileExactIf(0.95)(dateDiff('second',received_at,sentAt)/3600.0,customer_segment='Resellers' AND sentAt IS NOT NULL AND sentAt>=received_at),2) AS resellers_p95_tat_hrs,
    round(100.0-(countDistinctIf(sku_id,customer_segment='SMB' AND after_6_hrs='Yes')*100.0/nullIf(countDistinctIf(sku_id,customer_segment='SMB'),0)),2) AS smb_sla_pct,
    round(quantileExactIf(0.99)(dateDiff('second',received_at,sentAt)/3600.0,customer_segment='SMB' AND sentAt IS NOT NULL AND sentAt>=received_at),2) AS smb_p99_tat_hrs,
    round(quantileExactIf(0.95)(dateDiff('second',received_at,sentAt)/3600.0,customer_segment='SMB' AND sentAt IS NOT NULL AND sentAt>=received_at),2) AS smb_p95_tat_hrs
FROM base WHERE sku_delivery_status='Delivered' GROUP BY created_month

ORDER BY sort_group ASC, sort_date ASC
SETTINGS join_algorithm='grace_hash', max_bytes_in_join=10737418240,
         max_bytes_before_external_sort=10737418240, max_threads=4
"""

COLUMNS = [
    "sort_group","sort_date","granularity","period",
    "sla_pct","p99_tat_hrs","p95_tat_hrs",
    "ent_sla_pct","ent_p99_tat_hrs","ent_p95_tat_hrs",
    "mid_sla_pct","mid_p99_tat_hrs","mid_p95_tat_hrs",
    "resellers_sla_pct","resellers_p99_tat_hrs","resellers_p95_tat_hrs",
    "smb_sla_pct","smb_p99_tat_hrs","smb_p95_tat_hrs",
]

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.get("/api/sla-data")
async def sla_data(refresh: bool = Query(False)):
    cache_key = "sla_data"
    if not refresh:
        cached = get_cached(cache_key)
        if cached is not None:
            return cached

    t0 = time.time()
    try:
        client = get_ch_client()
        result = client.query(SLA_QUERY)
        rows = result.result_rows
    except Exception as exc:
        logger.error(f"ClickHouse error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))

    elapsed = round(time.time() - t0, 2)
    records = []
    for row in rows:
        rec = {}
        for col, val in zip(COLUMNS, row):
            if hasattr(val, "isoformat"):
                val = val.isoformat()
            rec[col] = val
        records.append(rec)

    payload = {
        "fetched_at": datetime.utcnow().isoformat(),
        "query_time_seconds": elapsed,
        "total_rows": len(records),
        "data": records,
    }
    set_cached(cache_key, payload)
    return payload


@app.get("/api/cache/clear")
async def clear_cache():
    _cache.clear()
    return {"cleared": True}
    
