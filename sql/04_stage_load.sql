-- =============================================================================
-- 04 - Load staging from raw, with derived columns (deliverable 2)
-- =============================================================================
-- Single-pass, set-based. No cursors, no row-by-row UDF calls: at billions of
-- rows the difference between a vectorised expression and a per-row function
-- call is the difference between minutes and hours.
--
-- Note the date handling. The design document implies one 8-digit format, but
-- the sample DOB '03051985' is not a valid YYYYMMDD value (month 19, day 85) --
-- it is DDMMYYYY. TRY_TO_DATE is used with the expected format first and the
-- alternative second, so a member is not rejected over an upstream formatting
-- inconsistency, while WARN_REASONS records that the fallback fired.
-- =============================================================================

SET BATCH_DATE = '2024-01-15';
SET STALE_AFTER_DAYS = 90;

INSERT INTO SKYPOINTS_STG.STG_MEMBER_PROFILE (
    MEMBER_NAME, MEMBER_ID, ENROLLMENT_DATE, LAST_FLIGHT_DATE, TIER_CODE,
    AGENT_NAME, STATE, COUNTRY, COUNTRY_CODE, POST_CODE, DATE_OF_BIRTH,
    ACTIVE_MEMBER, AGE, STALE_MEMBER, IS_VALID, REJECT_REASONS, WARN_REASONS,
    SOURCE_FILE_NAME, SOURCE_FILE_ROW, BATCH_DATE
)
WITH typed AS (
    SELECT
        NULLIF(TRIM(r.MEMBER_NAME), '')                     AS MEMBER_NAME,
        NULLIF(TRIM(r.MEMBER_ID), '')                       AS MEMBER_ID,
        TRY_TO_DATE(TRIM(r.ENROLLMENT_DATE),  'YYYYMMDD')   AS ENROLLMENT_DATE,
        TRY_TO_DATE(TRIM(r.LAST_FLIGHT_DATE), 'YYYYMMDD')   AS LAST_FLIGHT_DATE,
        UPPER(NULLIF(TRIM(r.TIER_CODE), ''))                AS TIER_CODE,
        NULLIF(TRIM(r.AGENT_NAME), '')                      AS AGENT_NAME,
        UPPER(NULLIF(TRIM(r.STATE), ''))                    AS STATE,
        UPPER(NULLIF(TRIM(r.COUNTRY), ''))                  AS COUNTRY,
        TRY_TO_NUMBER(TRIM(r.POST_CODE))                    AS POST_CODE,
        -- DDMMYYYY as sent, YYYYMMDD as fallback.
        COALESCE(
            TRY_TO_DATE(TRIM(r.DATE_OF_BIRTH), 'DDMMYYYY'),
            TRY_TO_DATE(TRIM(r.DATE_OF_BIRTH), 'YYYYMMDD')
        )                                                   AS DATE_OF_BIRTH,
        TRY_TO_DATE(TRIM(r.DATE_OF_BIRTH), 'DDMMYYYY')      AS DOB_PRIMARY,
        UPPER(NULLIF(TRIM(r.ACTIVE_MEMBER), ''))            AS ACTIVE_MEMBER,
        r.DATE_OF_BIRTH                                     AS DOB_RAW,
        r.SOURCE_FILE_NAME,
        r.SOURCE_FILE_ROW,
        r.BATCH_DATE
    FROM SKYPOINTS_RAW.RAW_MEMBER_PROFILE r
    WHERE r.BATCH_DATE = TO_DATE($BATCH_DATE)
      AND r.RECORD_TAG = 'D'          -- header records are metadata, not members
),
conformed AS (
    SELECT
        t.*,
        COALESCE(c.COUNTRY_CODE, 'UNK') AS COUNTRY_CODE
    FROM typed t
    LEFT JOIN SKYPOINTS_STG.REF_COUNTRY c
           ON c.SOURCE_VALUE = t.COUNTRY
          AND c.IS_ACTIVE
),
derived AS (
    SELECT
        c.*,
        -- Age in whole years, anchored on the batch date so the value is
        -- reproducible on re-run rather than drifting with the wall clock.
        --
        -- Calendar arithmetic rather than days/365.25: the approximation
        -- disagrees with the true age either side of a birthday, and the
        -- Python implementation is birthday-aware, so the two paths would
        -- return different ages for the same member.
        CASE
            WHEN c.DATE_OF_BIRTH IS NULL THEN NULL
            ELSE DATEDIFF('year', c.DATE_OF_BIRTH, c.BATCH_DATE)
                 - IFF(
                     DATE_FROM_PARTS(
                         YEAR(c.BATCH_DATE), MONTH(c.DATE_OF_BIRTH), DAY(c.DATE_OF_BIRTH)
                     ) > c.BATCH_DATE,
                     1, 0
                   )
        END AS AGE,
        -- NULL (not FALSE) when the member has never flown: 'never flown' and
        -- 'has not flown recently' are different states and merging them would
        -- mis-target re-engagement campaigns.
        CASE
            WHEN c.LAST_FLIGHT_DATE IS NULL THEN NULL
            ELSE DATEDIFF('day', c.LAST_FLIGHT_DATE, c.BATCH_DATE) > $STALE_AFTER_DAYS
        END AS STALE_MEMBER
    FROM conformed c
),
judged AS (
    SELECT
        d.*,
        ARRAY_COMPACT(ARRAY_CONSTRUCT(
            IFF(d.MEMBER_NAME IS NULL,      'mandatory_field:member_name',      NULL),
            IFF(d.MEMBER_ID IS NULL,        'mandatory_field:member_id',        NULL),
            IFF(d.ENROLLMENT_DATE IS NULL,  'mandatory_field:enrollment_date',  NULL),
            IFF(d.COUNTRY_CODE = 'UNK',     'country_conformance:unroutable',   NULL),
            IFF(d.DOB_RAW IS NOT NULL AND d.DATE_OF_BIRTH IS NULL,
                'date_parse:date_of_birth', NULL),
            IFF(LENGTH(d.DOB_RAW) = 7,      'date_leading_zero_lost:date_of_birth', NULL),
            IFF(d.ACTIVE_MEMBER IS NOT NULL AND d.ACTIVE_MEMBER NOT IN ('A','I'),
                'active_flag_domain',       NULL),
            IFF(d.LAST_FLIGHT_DATE < d.ENROLLMENT_DATE,
                'flight_after_enrollment',  NULL),
            IFF(d.ENROLLMENT_DATE > d.BATCH_DATE OR d.LAST_FLIGHT_DATE > d.BATCH_DATE,
                'date_not_future',          NULL),
            IFF(d.DATE_OF_BIRTH > d.BATCH_DATE OR YEAR(d.DATE_OF_BIRTH) < 1900,
                'dob_plausible',            NULL)
        )) AS REJECT_REASONS,
        ARRAY_COMPACT(ARRAY_CONSTRUCT(
            IFF(d.AGENT_NAME IS NULL,       'optional_completeness:agent_name', NULL),
            IFF(d.TIER_CODE IS NOT NULL AND d.TIER_CODE NOT IN ('BAS','SLV','GLD','PLT','DIA'),
                'tier_code_domain',         NULL),
            IFF(d.DOB_PRIMARY IS NULL AND d.DATE_OF_BIRTH IS NOT NULL,
                'date_format_drift:date_of_birth', NULL)
        )) AS WARN_REASONS
    FROM derived d
)
SELECT
    MEMBER_NAME, MEMBER_ID, ENROLLMENT_DATE, LAST_FLIGHT_DATE, TIER_CODE,
    AGENT_NAME, STATE, COUNTRY, COUNTRY_CODE, POST_CODE, DATE_OF_BIRTH,
    ACTIVE_MEMBER, AGE, STALE_MEMBER,
    ARRAY_SIZE(REJECT_REASONS) = 0 AS IS_VALID,
    REJECT_REASONS,
    WARN_REASONS,
    SOURCE_FILE_NAME, SOURCE_FILE_ROW, BATCH_DATE
FROM judged;

-- Invalid rows are moved aside, not discarded, so the source system can be
-- given a precise list of what it sent wrong.
INSERT INTO SKYPOINTS_STG.QUARANTINE_MEMBER_PROFILE
SELECT * FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
WHERE BATCH_DATE = TO_DATE($BATCH_DATE) AND NOT IS_VALID;

DELETE FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
WHERE BATCH_DATE = TO_DATE($BATCH_DATE) AND NOT IS_VALID;
