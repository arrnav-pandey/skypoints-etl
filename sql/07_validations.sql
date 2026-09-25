-- =============================================================================
-- 07 - Data validations (deliverable 5)
-- =============================================================================
-- Every check returns a uniform shape (CHECK_NAME, SEVERITY, FAILED_ROWS,
-- SAMPLE) so the suite can be run as one statement and gated on mechanically.
-- A validation that a human has to eyeball is a validation that stops being run.
--
-- Severity is the design decision that matters here. Blocking on every warning
-- means the pipeline halts nightly and people start ignoring it; blocking on
-- nothing means bad data reaches the business. So:
--   ERROR   - the record cannot be loaded correctly. Quarantine it.
--   WARNING - the record is loadable but something is degrading. Report it.
-- =============================================================================

SET BATCH_DATE = '2024-01-15';

CREATE OR REPLACE VIEW SKYPOINTS_STG.V_VALIDATION_RESULTS AS

-- --------------------------------------------------------------------------
-- 1. Mandatory field checks (contract: Member Name, Member ID, Enrollment Date)
-- --------------------------------------------------------------------------
SELECT 'mandatory_member_name'   AS CHECK_NAME, 'ERROR' AS SEVERITY,
       COUNT(*) AS FAILED_ROWS,
       ANY_VALUE(SOURCE_FILE_NAME || ':' || SOURCE_FILE_ROW) AS SAMPLE
FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
WHERE BATCH_DATE = TO_DATE($BATCH_DATE) AND MEMBER_NAME IS NULL

UNION ALL
SELECT 'mandatory_member_id', 'ERROR', COUNT(*),
       ANY_VALUE(SOURCE_FILE_NAME || ':' || SOURCE_FILE_ROW)
FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
WHERE BATCH_DATE = TO_DATE($BATCH_DATE) AND MEMBER_ID IS NULL

UNION ALL
SELECT 'mandatory_enrollment_date', 'ERROR', COUNT(*),
       ANY_VALUE(SOURCE_FILE_NAME || ':' || SOURCE_FILE_ROW)
FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
WHERE BATCH_DATE = TO_DATE($BATCH_DATE) AND ENROLLMENT_DATE IS NULL

-- --------------------------------------------------------------------------
-- 2. Key-column uniqueness
-- --------------------------------------------------------------------------
-- Snowflake declares but does not enforce PRIMARY KEY, so uniqueness must be
-- tested, not assumed. Checked at two levels because they fail for different
-- reasons: duplicates within a batch are a source problem, duplicates across
-- country tables are a routing problem (a relocation whose DELETE was missed).
UNION ALL
SELECT 'unique_member_id_in_batch', 'ERROR', COUNT(*), ANY_VALUE(MEMBER_ID)
FROM (
    SELECT MEMBER_ID
    FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
    WHERE BATCH_DATE = TO_DATE($BATCH_DATE)
    GROUP BY MEMBER_ID
    HAVING COUNT(*) > 1
)

UNION ALL
SELECT 'member_in_exactly_one_country', 'ERROR', COUNT(*), ANY_VALUE(MEMBER_ID)
FROM (
    SELECT MEMBER_ID
    FROM SKYPOINTS_TGT.V_MEMBER_GLOBAL
    GROUP BY MEMBER_ID
    HAVING COUNT(*) > 1
)

UNION ALL
SELECT 'unique_txn_id', 'ERROR', COUNT(*), ANY_VALUE(TXN_ID)
FROM (
    SELECT TXN_ID FROM SKYPOINTS_TGT.REDEMPTION_TXN
    GROUP BY TXN_ID HAVING COUNT(*) > 1
)

-- --------------------------------------------------------------------------
-- 3. Checks aimed at the issues visible in the sample data
-- --------------------------------------------------------------------------
-- 3a. '03051985' arriving as '3051985'. A date handled as a number upstream
--     loses its leading zero. Called out on its own because the fix is in the
--     source system's cast, not in this data.
UNION ALL
SELECT 'date_leading_zero_lost', 'ERROR', COUNT(*),
       ANY_VALUE(SOURCE_FILE_NAME || ':' || SOURCE_FILE_ROW)
FROM SKYPOINTS_RAW.RAW_MEMBER_PROFILE
WHERE BATCH_DATE = TO_DATE($BATCH_DATE)
  AND RECORD_TAG = 'D'
  AND (LENGTH(TRIM(DATE_OF_BIRTH)) = 7 OR LENGTH(TRIM(ENROLLMENT_DATE)) = 7
       OR LENGTH(TRIM(LAST_FLIGHT_DATE)) = 7)

-- 3b. Country codes are inconsistent in the sample ('PHIL', 'AU' vs ISO
--     alpha-3). Anything unmapped cannot be routed, so it is an ERROR: the
--     member would otherwise reach no country table at all.
UNION ALL
SELECT 'country_unmappable', 'ERROR', COUNT(*), ANY_VALUE(COUNTRY)
FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
WHERE BATCH_DATE = TO_DATE($BATCH_DATE) AND COUNTRY_CODE = 'UNK'

-- 3c. Referential integrity between the two feeds. Redemptions for members who
--     do not exist usually mean the feeds arrived out of step, which is worth
--     knowing before someone reconciles mileage liability.
UNION ALL
SELECT 'orphan_redemption_member', 'WARNING', COUNT(*), ANY_VALUE(t.MEMBER_ID)
FROM SKYPOINTS_TGT.REDEMPTION_TXN t
LEFT JOIN SKYPOINTS_TGT.V_MEMBER_GLOBAL m ON m.MEMBER_ID = t.MEMBER_ID
WHERE m.MEMBER_ID IS NULL

-- 3d. Agent_Name is populated for one member and blank for the rest. It is
--     optional per the contract, so this is a completeness signal, not a
--     rejection -- but a sudden jump in the rate means the source changed.
UNION ALL
SELECT 'agent_name_completeness', 'WARNING', COUNT(*), NULL
FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
WHERE BATCH_DATE = TO_DATE($BATCH_DATE) AND AGENT_NAME IS NULL

-- 3e. Every member in the sample shares the same DOB, enrolment and flight
--     date. In real data that is a synthetic-data smell or a stuck upstream
--     default. Cheap to check, and it catches a class of bug that row-level
--     rules never will.
UNION ALL
SELECT 'suspicious_low_date_cardinality', 'WARNING',
       IFF(COUNT(DISTINCT DATE_OF_BIRTH) = 1 AND COUNT(*) > 10, COUNT(*), 0),
       'all members share one date_of_birth'
FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
WHERE BATCH_DATE = TO_DATE($BATCH_DATE)

-- --------------------------------------------------------------------------
-- 4. Domain and cross-field checks
-- --------------------------------------------------------------------------
UNION ALL
SELECT 'tier_code_domain', 'WARNING', COUNT(*), ANY_VALUE(TIER_CODE)
FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
WHERE BATCH_DATE = TO_DATE($BATCH_DATE)
  AND TIER_CODE IS NOT NULL
  AND TIER_CODE NOT IN ('BAS','SLV','GLD','PLT','DIA')

UNION ALL
SELECT 'active_flag_domain', 'ERROR', COUNT(*), ANY_VALUE(ACTIVE_MEMBER)
FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
WHERE BATCH_DATE = TO_DATE($BATCH_DATE)
  AND ACTIVE_MEMBER IS NOT NULL
  AND ACTIVE_MEMBER NOT IN ('A','I')

UNION ALL
SELECT 'flight_before_enrollment', 'ERROR', COUNT(*),
       ANY_VALUE(SOURCE_FILE_NAME || ':' || SOURCE_FILE_ROW)
FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
WHERE BATCH_DATE = TO_DATE($BATCH_DATE)
  AND LAST_FLIGHT_DATE < ENROLLMENT_DATE

UNION ALL
SELECT 'dob_implausible', 'ERROR', COUNT(*), ANY_VALUE(TO_VARCHAR(DATE_OF_BIRTH))
FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
WHERE BATCH_DATE = TO_DATE($BATCH_DATE)
  AND (DATE_OF_BIRTH > BATCH_DATE OR YEAR(DATE_OF_BIRTH) < 1900)

UNION ALL
SELECT 'negative_miles', 'ERROR', COUNT(*), ANY_VALUE(TXN_ID)
FROM SKYPOINTS_TGT.REDEMPTION_TXN
WHERE MILES_REDEEMED < 0

-- --------------------------------------------------------------------------
-- 5. Reconciliation: rows in must equal rows out
-- --------------------------------------------------------------------------
-- The check that catches the failures no row-level rule can see -- a silently
-- dropped micro-batch, a MERGE that matched nothing, a country table missed
-- during routing. Without it, "the job succeeded" and "the data is complete"
-- are different statements that look identical.
UNION ALL
SELECT 'row_count_reconciliation', 'ERROR',
       ABS(
           (SELECT COUNT(*) FROM SKYPOINTS_RAW.RAW_MEMBER_PROFILE
            WHERE BATCH_DATE = TO_DATE($BATCH_DATE) AND RECORD_TAG = 'D')
         - (SELECT COUNT(*) FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
            WHERE BATCH_DATE = TO_DATE($BATCH_DATE))
         - (SELECT COUNT(*) FROM SKYPOINTS_STG.QUARANTINE_MEMBER_PROFILE
            WHERE BATCH_DATE = TO_DATE($BATCH_DATE))
       ),
       'raw detail rows must equal staged + quarantined'

-- --------------------------------------------------------------------------
-- 6. Volume anomaly
-- --------------------------------------------------------------------------
-- At billions of rows a day, a batch that is 10% of yesterday is far more
-- likely to be a truncated delivery than a genuine drop in enrolment. Catching
-- it before the load is cheaper than explaining the dashboard afterwards.
UNION ALL
SELECT 'volume_anomaly', 'WARNING',
       IFF(TODAY_ROWS < PRIOR_AVG * 0.5 OR TODAY_ROWS > PRIOR_AVG * 2, 1, 0),
       'today=' || TODAY_ROWS || ' prior_avg=' || ROUND(PRIOR_AVG)
FROM (
    SELECT
        COUNT_IF(BATCH_DATE = TO_DATE($BATCH_DATE))                       AS TODAY_ROWS,
        COUNT_IF(BATCH_DATE <  TO_DATE($BATCH_DATE)
                 AND BATCH_DATE >= DATEADD('day', -7, TO_DATE($BATCH_DATE))) / 7.0 AS PRIOR_AVG
    FROM SKYPOINTS_RAW.RAW_MEMBER_PROFILE
);

-- --------------------------------------------------------------------------
-- Gate: fail the run on any ERROR-severity breach
-- --------------------------------------------------------------------------
SELECT * FROM SKYPOINTS_STG.V_VALIDATION_RESULTS
WHERE FAILED_ROWS > 0
ORDER BY DECODE(SEVERITY, 'ERROR', 1, 'WARNING', 2), FAILED_ROWS DESC;
