-- =============================================================================
-- 07 - Data validations (deliverable 5)
-- =============================================================================
-- Every check returns a uniform shape (CHECK_NAME, SEVERITY, BATCH_DATE,
-- FAILED_ROWS, SAMPLE) so the suite can be run as one statement and gated on
-- mechanically. A validation that a human has to eyeball is a validation that
-- stops being run.
--
-- Severity is the design decision that matters here. Blocking on every warning
-- means the pipeline halts nightly and people start ignoring it; blocking on
-- nothing means bad data reaches the business. So:
--   ERROR   - the record cannot be loaded correctly. Quarantine it.
--   WARNING - the record is loadable but something is degrading. Report it.
--
-- The view deliberately contains NO session variable and NO batch filter. A
-- view whose result depends on session state is not reproducible: two people
-- querying it get different answers, and it cannot be safely scheduled or
-- shared. So the checks aggregate BY batch and the caller filters to the batch
-- it cares about. This also makes the suite retrospective for free -- the same
-- view shows whether last Tuesday's load was clean.
--
-- Checks that are global rather than per-batch (cross-country uniqueness,
-- orphan transactions) report a NULL batch date and are always evaluated.
-- =============================================================================

CREATE OR REPLACE VIEW SKYPOINTS_STG.V_VALIDATION_RESULTS AS

-- --------------------------------------------------------------------------
-- 1. Mandatory field checks (contract: Member Name, Member ID, Enrollment Date)
-- --------------------------------------------------------------------------
SELECT 'mandatory_member_name' AS CHECK_NAME,
       'ERROR'                 AS SEVERITY,
       BATCH_DATE              AS BATCH_DATE,
       COUNT(*)                AS FAILED_ROWS,
       ANY_VALUE(SOURCE_FILE_NAME || ':' || SOURCE_FILE_ROW)::VARCHAR AS SAMPLE
FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
WHERE MEMBER_NAME IS NULL
GROUP BY BATCH_DATE

UNION ALL
SELECT 'mandatory_member_id', 'ERROR', BATCH_DATE, COUNT(*),
       ANY_VALUE(SOURCE_FILE_NAME || ':' || SOURCE_FILE_ROW)::VARCHAR
FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
WHERE MEMBER_ID IS NULL
GROUP BY BATCH_DATE

UNION ALL
SELECT 'mandatory_enrollment_date', 'ERROR', BATCH_DATE, COUNT(*),
       ANY_VALUE(SOURCE_FILE_NAME || ':' || SOURCE_FILE_ROW)::VARCHAR
FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
WHERE ENROLLMENT_DATE IS NULL
GROUP BY BATCH_DATE

-- --------------------------------------------------------------------------
-- 2. Key-column uniqueness
-- --------------------------------------------------------------------------
-- Snowflake declares but does not enforce PRIMARY KEY, so uniqueness must be
-- tested, not assumed. Checked at two levels because they fail for different
-- reasons: duplicates within a batch are a source problem, duplicates across
-- country tables are a routing problem (a relocation whose DELETE was missed).
UNION ALL
SELECT 'unique_member_id_in_batch', 'ERROR', BATCH_DATE, COUNT(*),
       ANY_VALUE(MEMBER_ID)::VARCHAR
FROM (
    SELECT BATCH_DATE, MEMBER_ID
    FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
    GROUP BY BATCH_DATE, MEMBER_ID
    HAVING COUNT(*) > 1
)
GROUP BY BATCH_DATE

UNION ALL
SELECT 'member_in_exactly_one_country', 'ERROR', NULL::DATE, COUNT(*),
       ANY_VALUE(MEMBER_ID)::VARCHAR
FROM (
    SELECT MEMBER_ID
    FROM SKYPOINTS_TGT.V_MEMBER_GLOBAL
    GROUP BY MEMBER_ID
    HAVING COUNT(*) > 1
)

UNION ALL
SELECT 'unique_txn_id', 'ERROR', NULL::DATE, COUNT(*), ANY_VALUE(TXN_ID)::VARCHAR
FROM (
    SELECT TXN_ID
    FROM SKYPOINTS_TGT.REDEMPTION_TXN
    GROUP BY TXN_ID
    HAVING COUNT(*) > 1
)

-- --------------------------------------------------------------------------
-- 3. Checks aimed at the issues visible in the sample data
-- --------------------------------------------------------------------------
-- 3a. '03051985' arriving as '3051985'. A date handled as a number upstream
--     loses its leading zero. Called out on its own because the fix is in the
--     source system's cast, not in this data.
UNION ALL
SELECT 'date_leading_zero_lost', 'ERROR', BATCH_DATE, COUNT(*),
       ANY_VALUE(SOURCE_FILE_NAME || ':' || SOURCE_FILE_ROW)::VARCHAR
FROM SKYPOINTS_RAW.RAW_MEMBER_PROFILE
WHERE RECORD_TAG = 'D'
  AND (LENGTH(TRIM(DATE_OF_BIRTH)) = 7
       OR LENGTH(TRIM(ENROLLMENT_DATE)) = 7
       OR LENGTH(TRIM(LAST_FLIGHT_DATE)) = 7)
GROUP BY BATCH_DATE

-- 3b. Country codes are inconsistent in the sample ('PHIL', 'AU' vs ISO
--     alpha-3). Anything unmapped cannot be routed, so it is an ERROR: the
--     member would otherwise reach no country table at all.
UNION ALL
SELECT 'country_unmappable', 'ERROR', BATCH_DATE, COUNT(*), ANY_VALUE(COUNTRY)::VARCHAR
FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
WHERE COUNTRY_CODE = 'UNK'
GROUP BY BATCH_DATE

-- 3c. Referential integrity between the two feeds. Redemptions for members who
--     do not exist usually mean the feeds arrived out of step, which is worth
--     knowing before someone reconciles mileage liability.
UNION ALL
SELECT 'orphan_redemption_member', 'WARNING', NULL::DATE, COUNT(*),
       ANY_VALUE(t.MEMBER_ID)::VARCHAR
FROM SKYPOINTS_TGT.REDEMPTION_TXN t
LEFT JOIN SKYPOINTS_TGT.V_MEMBER_GLOBAL m ON m.MEMBER_ID = t.MEMBER_ID
WHERE m.MEMBER_ID IS NULL

-- 3d. Agent_Name is populated for one member and blank for the rest. It is
--     optional per the contract, so this is a completeness signal, not a
--     rejection -- but a sudden jump in the rate means the source changed.
UNION ALL
SELECT 'agent_name_completeness', 'WARNING', BATCH_DATE, COUNT(*), NULL::VARCHAR
FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
WHERE AGENT_NAME IS NULL
GROUP BY BATCH_DATE

-- 3e. Every member in the sample shares the same DOB, enrolment and flight
--     date. In real data that is a synthetic-data smell or a stuck upstream
--     default. Cheap to check, and it catches a class of bug that row-level
--     rules never will.
UNION ALL
SELECT 'suspicious_low_date_cardinality', 'WARNING', BATCH_DATE,
       IFF(COUNT(DISTINCT DATE_OF_BIRTH) = 1 AND COUNT(*) > 10, COUNT(*), 0),
       'all members in the batch share one date_of_birth'::VARCHAR
FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
GROUP BY BATCH_DATE

-- --------------------------------------------------------------------------
-- 4. Domain and cross-field checks
-- --------------------------------------------------------------------------
UNION ALL
SELECT 'tier_code_domain', 'WARNING', BATCH_DATE, COUNT(*), ANY_VALUE(TIER_CODE)::VARCHAR
FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
WHERE TIER_CODE IS NOT NULL
  AND TIER_CODE NOT IN ('BAS','SLV','GLD','PLT','DIA')
GROUP BY BATCH_DATE

UNION ALL
SELECT 'active_flag_domain', 'ERROR', BATCH_DATE, COUNT(*), ANY_VALUE(ACTIVE_MEMBER)::VARCHAR
FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
WHERE ACTIVE_MEMBER IS NOT NULL
  AND ACTIVE_MEMBER NOT IN ('A','I')
GROUP BY BATCH_DATE

UNION ALL
SELECT 'flight_before_enrollment', 'ERROR', BATCH_DATE, COUNT(*),
       ANY_VALUE(SOURCE_FILE_NAME || ':' || SOURCE_FILE_ROW)::VARCHAR
FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
WHERE LAST_FLIGHT_DATE < ENROLLMENT_DATE
GROUP BY BATCH_DATE

UNION ALL
SELECT 'dob_implausible', 'ERROR', BATCH_DATE, COUNT(*),
       ANY_VALUE(TO_VARCHAR(DATE_OF_BIRTH))::VARCHAR
FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
WHERE DATE_OF_BIRTH > BATCH_DATE OR YEAR(DATE_OF_BIRTH) < 1900
GROUP BY BATCH_DATE

UNION ALL
SELECT 'negative_miles', 'ERROR', BATCH_DATE, COUNT(*), ANY_VALUE(TXN_ID)::VARCHAR
FROM SKYPOINTS_TGT.REDEMPTION_TXN
WHERE MILES_REDEEMED < 0
GROUP BY BATCH_DATE

-- --------------------------------------------------------------------------
-- 5. Reconciliation: rows in must equal rows out
-- --------------------------------------------------------------------------
-- The check that catches the failures no row-level rule can see -- a silently
-- dropped micro-batch, a MERGE that matched nothing, a country table missed
-- during routing. Without it, "the job succeeded" and "the data is complete"
-- are different statements that look identical.
UNION ALL
SELECT 'row_count_reconciliation', 'ERROR', BATCH_DATE,
       ABS(SUM(RAW_ROWS) - SUM(STG_ROWS) - SUM(QTN_ROWS)),
       ('raw=' || SUM(RAW_ROWS) || ' staged=' || SUM(STG_ROWS)
        || ' quarantined=' || SUM(QTN_ROWS))::VARCHAR
FROM (
    SELECT BATCH_DATE, COUNT(*) AS RAW_ROWS, 0 AS STG_ROWS, 0 AS QTN_ROWS
    FROM SKYPOINTS_RAW.RAW_MEMBER_PROFILE
    WHERE RECORD_TAG = 'D'
    GROUP BY BATCH_DATE
    UNION ALL
    SELECT BATCH_DATE, 0, COUNT(*), 0
    FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
    GROUP BY BATCH_DATE
    UNION ALL
    SELECT BATCH_DATE, 0, 0, COUNT(*)
    FROM SKYPOINTS_STG.QUARANTINE_MEMBER_PROFILE
    GROUP BY BATCH_DATE
)
GROUP BY BATCH_DATE

-- --------------------------------------------------------------------------
-- 6. Volume anomaly
-- --------------------------------------------------------------------------
-- At billions of rows a day, a batch that is 10% of yesterday is far more
-- likely to be a truncated delivery than a genuine drop in enrolment. Catching
-- it before the load is cheaper than explaining the dashboard afterwards.
UNION ALL
SELECT 'volume_anomaly', 'WARNING', BATCH_DATE,
       IFF(ROWS_TODAY < PRIOR_AVG * 0.5 OR ROWS_TODAY > PRIOR_AVG * 2, 1, 0),
       ('today=' || ROWS_TODAY || ' prior_avg=' || ROUND(PRIOR_AVG, 1))::VARCHAR
FROM (
    SELECT BATCH_DATE,
           COUNT(*) AS ROWS_TODAY,
           AVG(COUNT(*)) OVER (
               ORDER BY BATCH_DATE
               ROWS BETWEEN 7 PRECEDING AND 1 PRECEDING
           ) AS PRIOR_AVG
    FROM SKYPOINTS_RAW.RAW_MEMBER_PROFILE
    WHERE RECORD_TAG = 'D'
    GROUP BY BATCH_DATE
)
WHERE PRIOR_AVG IS NOT NULL;

-- =============================================================================
-- Gate: fail the run on any ERROR-severity breach for the batch being loaded
-- =============================================================================
-- The batch filter lives here, in the caller, not in the view. Global checks
-- carry a NULL batch date and are always included.
SET BATCH_DATE = '2024-01-15';

SELECT CHECK_NAME, SEVERITY, BATCH_DATE, FAILED_ROWS, SAMPLE
FROM SKYPOINTS_STG.V_VALIDATION_RESULTS
WHERE FAILED_ROWS > 0
  AND (BATCH_DATE = TO_DATE($BATCH_DATE) OR BATCH_DATE IS NULL)
ORDER BY DECODE(SEVERITY, 'ERROR', 1, 'WARNING', 2), FAILED_ROWS DESC;
