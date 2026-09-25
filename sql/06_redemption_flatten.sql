-- =============================================================================
-- 06 - Flatten the JSON redemption feed and join it to member profiles
--      (deliverable 4)
-- =============================================================================

SET BATCH_DATE = '2024-01-15';

-- -----------------------------------------------------------------------------
-- Flatten: one nested document -> one row per transaction
-- -----------------------------------------------------------------------------
-- LATERAL FLATTEN explodes the redemptions array while keeping the parent's
-- member_id and feed_date on every child row. Denormalising the parent keys
-- down onto the child is the point: it makes each transaction row independently
-- joinable and filterable without re-traversing the nesting.
--
-- OUTER => TRUE so a member who redeemed nothing still produces a row. Without
-- it, an empty array vanishes entirely and a partner sending nothing looks
-- identical to a partner sending nothing wrong.
CREATE OR REPLACE TEMPORARY TABLE SKYPOINTS_STG.TMP_REDEMPTION_FLAT AS
SELECT
    f.value:txn_id::VARCHAR(50)             AS TXN_ID,
    r.PAYLOAD:member_id::VARCHAR(18)        AS MEMBER_ID,
    TRY_TO_DATE(f.value:txn_date::VARCHAR, 'YYYYMMDD')   AS TXN_DATE,
    f.value:partner::VARCHAR(100)           AS PARTNER,
    TRY_TO_NUMBER(f.value:miles_redeemed::VARCHAR)       AS MILES_REDEEMED,
    UPPER(f.value:status::VARCHAR(20))      AS STATUS,
    TRY_TO_DATE(r.PAYLOAD:feed_date::VARCHAR, 'YYYYMMDD') AS FEED_DATE,
    r.SOURCE_FILE_NAME,
    r.BATCH_DATE
FROM SKYPOINTS_RAW.RAW_REDEMPTION_FEED r,
     LATERAL FLATTEN(INPUT => r.PAYLOAD:redemptions, OUTER => TRUE) f
WHERE r.BATCH_DATE = TO_DATE($BATCH_DATE);

-- -----------------------------------------------------------------------------
-- Deduplicate to current state per transaction
-- -----------------------------------------------------------------------------
-- Partners re-send a transaction as its status advances (PENDING ->
-- COMPLETED), so the same TXN_ID legitimately arrives more than once. The
-- table is a current-state view at transaction grain, not an append-only log,
-- so the latest statement wins. Ranking on FEED_DATE then LOADED_AT keeps this
-- deterministic when a partner restates twice in one day.
MERGE INTO SKYPOINTS_TGT.REDEMPTION_TXN AS tgt
USING (
    SELECT * EXCLUDE (RN)
    FROM (
        SELECT t.*,
               ROW_NUMBER() OVER (
                   PARTITION BY t.TXN_ID
                   ORDER BY t.FEED_DATE DESC NULLS LAST,
                            t.BATCH_DATE DESC
               ) AS RN
        FROM SKYPOINTS_STG.TMP_REDEMPTION_FLAT t
        WHERE t.TXN_ID IS NOT NULL         -- empty-array rows carry no transaction
          AND t.MEMBER_ID IS NOT NULL      -- unjoinable to a member
          AND t.MILES_REDEEMED >= 0
    ) ranked
    WHERE RN = 1
) AS src
ON tgt.TXN_ID = src.TXN_ID
WHEN MATCHED AND (tgt.STATUS IS DISTINCT FROM src.STATUS
               OR tgt.MILES_REDEEMED IS DISTINCT FROM src.MILES_REDEEMED)
THEN UPDATE SET
    tgt.STATUS          = src.STATUS,
    tgt.MILES_REDEEMED  = src.MILES_REDEEMED,
    tgt.FEED_DATE       = src.FEED_DATE,
    tgt.BATCH_DATE      = src.BATCH_DATE,
    tgt.LOADED_AT       = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT (
    TXN_ID, MEMBER_ID, TXN_DATE, PARTNER, MILES_REDEEMED, STATUS,
    FEED_DATE, SOURCE_FILE_NAME, BATCH_DATE
) VALUES (
    src.TXN_ID, src.MEMBER_ID, src.TXN_DATE, src.PARTNER, src.MILES_REDEEMED,
    src.STATUS, src.FEED_DATE, src.SOURCE_FILE_NAME, src.BATCH_DATE
);

-- =============================================================================
-- Joining redemptions back to member profile data
-- =============================================================================
-- The join key is MEMBER_ID. Two properties of this model make the join safe:
--
--   * the per-country target tables hold exactly one current row per member
--     (enforced by the latest-record-wins MERGE in 05), so the join is
--     many-to-one and cannot fan out and inflate mileage totals;
--   * transactions are NOT split by country, so the join does not have to
--     guess which country table a member lives in.
--
-- Joining through V_MEMBER_GLOBAL rather than a specific country table is what
-- keeps this correct when a member relocates: their transaction history stays
-- attached to them instead of being stranded in their old country.
--
-- A LEFT join from transactions to members is the diagnostic direction: it
-- exposes orphan transactions for members who are not (yet) in the profile
-- feed, which is a real condition when the two feeds arrive out of step.
CREATE OR REPLACE VIEW SKYPOINTS_TGT.V_MEMBER_REDEMPTION AS
SELECT
    t.TXN_ID,
    t.MEMBER_ID,
    t.TXN_DATE,
    t.PARTNER,
    t.MILES_REDEEMED,
    t.STATUS,
    m.MEMBER_NAME,
    m.COUNTRY_CODE,
    m.TIER_CODE,
    m.STALE_MEMBER,
    m.AGE,
    -- Flags transactions whose member has not arrived in the profile feed.
    -- Surfaced as a column rather than filtered away so the gap is countable.
    m.MEMBER_ID IS NULL AS IS_ORPHAN_TXN
FROM SKYPOINTS_TGT.REDEMPTION_TXN t
LEFT JOIN SKYPOINTS_TGT.V_MEMBER_GLOBAL m
       ON m.MEMBER_ID = t.MEMBER_ID;

-- Example: redeemed miles by country and tier, completed transactions only.
-- Pre-aggregating before the join keeps the member side of the join small,
-- which matters when the transaction table is the billion-row one.
SELECT
    m.COUNTRY_CODE,
    m.TIER_CODE,
    COUNT(DISTINCT t.MEMBER_ID) AS REDEEMING_MEMBERS,
    SUM(t.MILES_REDEEMED)       AS TOTAL_MILES
FROM (
    SELECT MEMBER_ID, MILES_REDEEMED
    FROM SKYPOINTS_TGT.REDEMPTION_TXN
    WHERE STATUS = 'COMPLETED'
      AND TXN_DATE >= DATEADD('day', -30, TO_DATE($BATCH_DATE))
) t
JOIN SKYPOINTS_TGT.V_MEMBER_GLOBAL m ON m.MEMBER_ID = t.MEMBER_ID
GROUP BY 1, 2
ORDER BY TOTAL_MILES DESC;
