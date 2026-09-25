-- =============================================================================
-- 08 - Landing and conforming the per-country member feeds
-- =============================================================================
-- The pipe-delimited feed in 01-07 is one source contract. The files actually
-- supplied with the assessment are three more, and no two of them agree:
--
--   USA.csv   ID, Name, TierCode, EnrollmentDate, FlightDate
--             MDYYYY as a number ('6152022'), and no date of birth at all
--   IND.csv   ID, Name, DOB, TierCode, EnrollmentDate,
--             Individual or Corporate, Flight Date
--             M/D/YYYY text, plus a column no specification declares
--   AUS.xlsx  Unique ID, Member Name, Tier Type, Date of Birth,
--             Date of Enrollment, Date of Flight
--             Excel types, a literal 'NULL' string, and '2021-13-13'
--
-- Writing one landing table and one load per country would triple the code and
-- guarantee the three drift apart. Instead the differences are held as
-- reference data and a single generated load reads them, which is the same
-- approach REF_COUNTRY takes for routing.
--
-- Country is carried by the FILENAME, not by a column. That is the whole
-- identity of the market, so a file whose name cannot be resolved is refused
-- rather than defaulted -- guessing would route real members into the wrong
-- country's table.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Landing: one narrow table for every country feed
-- -----------------------------------------------------------------------------
-- Columns are landed as a VARIANT map of source-header -> raw value rather than
-- as fixed columns, because the three feeds do not share a column set. This
-- keeps the landing zone a faithful copy of whatever arrived, which is the one
-- property that makes a batch replayable.
CREATE TABLE IF NOT EXISTS SKYPOINTS_RAW.RAW_COUNTRY_FEED (
    RAW_FEED_SK        NUMBER       IDENTITY NOT NULL,
    COUNTRY_CODE       VARCHAR(3)   NOT NULL,   -- resolved from the filename
    PAYLOAD            VARIANT      NOT NULL,   -- {"ID": "1", "Name": "Sam", ...}
    SOURCE_FILE_NAME   VARCHAR(500) NOT NULL,
    SOURCE_FILE_ROW    NUMBER       NOT NULL,
    SOURCE_FORMAT      VARCHAR(10)  NOT NULL,   -- csv | xlsx
    BATCH_DATE         DATE         NOT NULL,
    LOADED_AT          TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
)
CLUSTER BY (BATCH_DATE, COUNTRY_CODE)
COMMENT = 'Immutable landing copy of the per-country member feeds, schema-agnostic.';

-- -----------------------------------------------------------------------------
-- Reference data: the column and date contract
-- -----------------------------------------------------------------------------
-- One row per (country, source column). This covers the column mapping and the
-- date encoding only -- file format, text encoding and worksheet selection are
-- properties of the ingestion layer that lands the file, not of the conforming
-- SQL, and live with the loader (see skypoints.sources.SourceContract).
--
-- Onboarding a market becomes an INSERT by a data steward instead of a
-- deployment by an engineer, which matters for a loyalty programme that adds
-- partner countries as a matter of course.
CREATE TABLE IF NOT EXISTS SKYPOINTS_STG.REF_SOURCE_CONTRACT (
    COUNTRY_CODE       VARCHAR(3)   NOT NULL,
    SOURCE_COLUMN      VARCHAR(100) NOT NULL,   -- header as the source writes it
    CANONICAL_COLUMN   VARCHAR(100),            -- NULL = keep, but undeclared
    DATE_FORMAT        VARCHAR(20),             -- only for date columns
    IS_ACTIVE          BOOLEAN DEFAULT TRUE,
    CONSTRAINT PK_REF_SOURCE_CONTRACT PRIMARY KEY (COUNTRY_CODE, SOURCE_COLUMN)
)
COMMENT = 'Per-country source schema: column mapping and date encoding.';

INSERT INTO SKYPOINTS_STG.REF_SOURCE_CONTRACT
    (COUNTRY_CODE, SOURCE_COLUMN, CANONICAL_COLUMN, DATE_FORMAT)
SELECT column1, column2, column3, column4
FROM VALUES
    -- USA: MDYYYY as a number. No DOB column exists, so AGE is unknowable.
    ('USA', 'ID',                      'MEMBER_ID',        NULL),
    ('USA', 'Name',                    'MEMBER_NAME',      NULL),
    ('USA', 'TierCode',                'TIER_CODE',        NULL),
    ('USA', 'EnrollmentDate',          'ENROLLMENT_DATE',  'MMDDYYYY'),
    ('USA', 'FlightDate',              'LAST_FLIGHT_DATE', 'MMDDYYYY'),
    -- IND: slash-delimited, and one column that no design document mentions.
    ('IND', 'ID',                      'MEMBER_ID',        NULL),
    ('IND', 'Name',                    'MEMBER_NAME',      NULL),
    ('IND', 'DOB',                     'DATE_OF_BIRTH',    'MM/DD/YYYY'),
    ('IND', 'TierCode',                'TIER_CODE',        NULL),
    ('IND', 'EnrollmentDate',          'ENROLLMENT_DATE',  'MM/DD/YYYY'),
    ('IND', 'Flight Date',             'LAST_FLIGHT_DATE', 'MM/DD/YYYY'),
    ('IND', 'Individual or Corporate',  NULL,              NULL),
    -- AUS: Excel-typed dates, with text fallbacks for the cells typed as text.
    ('AUS', 'Unique ID',               'MEMBER_ID',        NULL),
    ('AUS', 'Member Name',             'MEMBER_NAME',      NULL),
    ('AUS', 'Tier Type',               'TIER_CODE',        NULL),
    ('AUS', 'Date of Birth',           'DATE_OF_BIRTH',    'YYYY-MM-DD'),
    ('AUS', 'Date of Enrollment',      'ENROLLMENT_DATE',  'YYYY-MM-DD'),
    ('AUS', 'Date of Flight',          'LAST_FLIGHT_DATE', 'YYYY-MM-DD')
WHERE (column1, column2) NOT IN (
    SELECT COUNTRY_CODE, SOURCE_COLUMN FROM SKYPOINTS_STG.REF_SOURCE_CONTRACT
);

-- -----------------------------------------------------------------------------
-- Normalising a source value
-- -----------------------------------------------------------------------------
-- Two traps in the supplied data are handled here rather than in every query.
CREATE OR REPLACE FUNCTION SKYPOINTS_STG.FN_NULLIFY(RAW VARCHAR)
RETURNS VARCHAR
LANGUAGE SQL
COMMENT = 'Turns the strings a source uses to MEAN null into an actual NULL.'
AS
$$
    -- AUS sends the four characters N-U-L-L in a date cell. Loaded naively it
    -- becomes the text 'NULL', which is not null and defeats every IS NULL
    -- check downstream.
    IFF(TRIM(RAW) IS NULL OR UPPER(TRIM(RAW)) IN ('NULL','N/A','NA','NONE','NIL','-',''),
        NULL, TRIM(RAW))
$$;

CREATE OR REPLACE FUNCTION SKYPOINTS_STG.FN_PARSE_SOURCE_DATE(RAW VARCHAR, FMT VARCHAR)
RETURNS DATE
LANGUAGE SQL
COMMENT = 'Parses a source date, restoring a leading zero lost to a numeric cast.'
AS
$$
    CASE
        WHEN SKYPOINTS_STG.FN_NULLIFY(RAW) IS NULL THEN NULL
        -- A 7-digit value in an 8-wide numeric date has lost exactly one
        -- leading zero, so its month is necessarily single-digit. Zero-pad
        -- BEFORE parsing: '1052022' is 5 January, not 5 October, because
        -- 5 October would have arrived as the full 8 digits '10052022'.
        WHEN FMT = 'MMDDYYYY' AND REGEXP_LIKE(TRIM(RAW), '^[0-9]{7,8}$')
            THEN TRY_TO_DATE(LPAD(TRIM(RAW), 8, '0'), 'MMDDYYYY')
        ELSE TRY_TO_DATE(TRIM(RAW), FMT)
    END
$$;

-- -----------------------------------------------------------------------------
-- Conform the landed feeds into the shared staging table
-- -----------------------------------------------------------------------------
-- One statement for every country. The PAYLOAD is addressed through
-- REF_SOURCE_CONTRACT, so a new market needs no new SQL.
SET BATCH_DATE = '2022-12-31';

INSERT INTO SKYPOINTS_STG.STG_MEMBER_PROFILE (
    MEMBER_NAME, MEMBER_ID, ENROLLMENT_DATE, LAST_FLIGHT_DATE, TIER_CODE,
    COUNTRY, COUNTRY_CODE, DATE_OF_BIRTH, AGE, STALE_MEMBER,
    IS_VALID, REJECT_REASONS, WARN_REASONS,
    SOURCE_FILE_NAME, SOURCE_FILE_ROW, BATCH_DATE
)
WITH mapped AS (
    SELECT
        r.COUNTRY_CODE,
        r.SOURCE_FILE_NAME,
        r.SOURCE_FILE_ROW,
        r.BATCH_DATE,
        -- OBJECT_AGG rebuilds the payload keyed by canonical column name, so
        -- everything downstream is schema-independent.
        OBJECT_AGG(
            COALESCE(c.CANONICAL_COLUMN, 'EXTRA__' || c.SOURCE_COLUMN),
            TO_VARIANT(GET(r.PAYLOAD, c.SOURCE_COLUMN))
        ) AS COLS,
        OBJECT_AGG(
            COALESCE(c.CANONICAL_COLUMN, c.SOURCE_COLUMN),
            TO_VARIANT(c.DATE_FORMAT)
        ) AS FMTS
    FROM SKYPOINTS_RAW.RAW_COUNTRY_FEED r
    JOIN SKYPOINTS_STG.REF_SOURCE_CONTRACT c
      ON c.COUNTRY_CODE = r.COUNTRY_CODE
     AND c.IS_ACTIVE
    WHERE r.BATCH_DATE = TO_DATE($BATCH_DATE)
    GROUP BY r.COUNTRY_CODE, r.SOURCE_FILE_NAME, r.SOURCE_FILE_ROW, r.BATCH_DATE, r.PAYLOAD
),
typed AS (
    SELECT
        m.COUNTRY_CODE,
        m.SOURCE_FILE_NAME,
        m.SOURCE_FILE_ROW,
        m.BATCH_DATE,
        SKYPOINTS_STG.FN_NULLIFY(m.COLS:MEMBER_NAME::VARCHAR)  AS MEMBER_NAME,
        SKYPOINTS_STG.FN_NULLIFY(m.COLS:MEMBER_ID::VARCHAR)    AS MEMBER_ID,
        UPPER(SKYPOINTS_STG.FN_NULLIFY(m.COLS:TIER_CODE::VARCHAR)) AS TIER_CODE,
        SKYPOINTS_STG.FN_PARSE_SOURCE_DATE(
            m.COLS:ENROLLMENT_DATE::VARCHAR, m.FMTS:ENROLLMENT_DATE::VARCHAR)  AS ENROLLMENT_DATE,
        SKYPOINTS_STG.FN_PARSE_SOURCE_DATE(
            m.COLS:LAST_FLIGHT_DATE::VARCHAR, m.FMTS:LAST_FLIGHT_DATE::VARCHAR) AS LAST_FLIGHT_DATE,
        SKYPOINTS_STG.FN_PARSE_SOURCE_DATE(
            m.COLS:DATE_OF_BIRTH::VARCHAR, m.FMTS:DATE_OF_BIRTH::VARCHAR)       AS DATE_OF_BIRTH,
        m.COLS
    FROM mapped m
)
SELECT
    t.MEMBER_NAME,
    t.MEMBER_ID,
    t.ENROLLMENT_DATE,
    t.LAST_FLIGHT_DATE,
    t.TIER_CODE,
    t.COUNTRY_CODE AS COUNTRY,
    t.COUNTRY_CODE,
    t.DATE_OF_BIRTH,
    -- NULL, never 0, when no DOB was supplied: USA ships none at all and a
    -- fabricated age would silently corrupt every age-based segment.
    --
    -- Calendar arithmetic, not days/365.25. The approximation disagrees with
    -- the true age near a birthday, and the Python implementation is
    -- birthday-aware, so the two paths would return different ages for the
    -- same member. DATE_FROM_PARTS normalises 29 Feb in a non-leap year to
    -- 1 March, which matches how the Python side treats it.
    IFF(t.DATE_OF_BIRTH IS NULL, NULL,
        DATEDIFF('year', t.DATE_OF_BIRTH, t.BATCH_DATE)
        - IFF(
            DATE_FROM_PARTS(YEAR(t.BATCH_DATE), MONTH(t.DATE_OF_BIRTH), DAY(t.DATE_OF_BIRTH))
                > t.BATCH_DATE,
            1, 0
          )
    ) AS AGE,
    IFF(t.LAST_FLIGHT_DATE IS NULL, NULL,
        DATEDIFF('day', t.LAST_FLIGHT_DATE, t.BATCH_DATE) > 90)         AS STALE_MEMBER,
    -- A date that was sent but did not parse is the '2021-13-13' case. Every
    -- declared date column is checked, not just the one the sample happens to
    -- break: an unparseable DOB or flight date would otherwise pass as valid
    -- carrying a silent NULL, which is exactly the failure this layer exists
    -- to prevent.
    ARRAY_SIZE(ARRAY_COMPACT(ARRAY_CONSTRUCT(
        IFF(t.MEMBER_NAME IS NULL,     'mandatory_field:member_name',     NULL),
        IFF(t.MEMBER_ID IS NULL,       'mandatory_field:member_id',       NULL),
        IFF(t.ENROLLMENT_DATE IS NULL, 'mandatory_field:enrollment_date', NULL),
        IFF(SKYPOINTS_STG.FN_NULLIFY(t.COLS:ENROLLMENT_DATE::VARCHAR) IS NOT NULL
            AND t.ENROLLMENT_DATE IS NULL,
            'date_valid_calendar:enrollment_date', NULL),
        IFF(SKYPOINTS_STG.FN_NULLIFY(t.COLS:LAST_FLIGHT_DATE::VARCHAR) IS NOT NULL
            AND t.LAST_FLIGHT_DATE IS NULL,
            'date_valid_calendar:last_flight_date', NULL),
        IFF(SKYPOINTS_STG.FN_NULLIFY(t.COLS:DATE_OF_BIRTH::VARCHAR) IS NOT NULL
            AND t.DATE_OF_BIRTH IS NULL,
            'date_valid_calendar:date_of_birth', NULL),
        IFF(t.LAST_FLIGHT_DATE < t.ENROLLMENT_DATE,
            'flight_after_enrollment', NULL)
    ))) = 0 AS IS_VALID,
    ARRAY_COMPACT(ARRAY_CONSTRUCT(
        IFF(t.MEMBER_NAME IS NULL,     'mandatory_field:member_name',     NULL),
        IFF(t.MEMBER_ID IS NULL,       'mandatory_field:member_id',       NULL),
        IFF(t.ENROLLMENT_DATE IS NULL, 'mandatory_field:enrollment_date', NULL),
        IFF(SKYPOINTS_STG.FN_NULLIFY(t.COLS:ENROLLMENT_DATE::VARCHAR) IS NOT NULL
            AND t.ENROLLMENT_DATE IS NULL,
            'date_valid_calendar:enrollment_date', NULL),
        IFF(SKYPOINTS_STG.FN_NULLIFY(t.COLS:LAST_FLIGHT_DATE::VARCHAR) IS NOT NULL
            AND t.LAST_FLIGHT_DATE IS NULL,
            'date_valid_calendar:last_flight_date', NULL),
        IFF(SKYPOINTS_STG.FN_NULLIFY(t.COLS:DATE_OF_BIRTH::VARCHAR) IS NOT NULL
            AND t.DATE_OF_BIRTH IS NULL,
            'date_valid_calendar:date_of_birth', NULL),
        IFF(t.LAST_FLIGHT_DATE < t.ENROLLMENT_DATE,
            'flight_after_enrollment', NULL)
    )),
    ARRAY_COMPACT(ARRAY_CONSTRUCT(
        -- Not an error: an entire market simply does not collect DOB.
        IFF(t.DATE_OF_BIRTH IS NULL, 'age_underivable', NULL),
        IFF(t.TIER_CODE IS NOT NULL AND t.TIER_CODE NOT IN ('BAS','SLV','GLD','PLT','DIA'),
            'tier_code_domain', NULL)
    )),
    t.SOURCE_FILE_NAME,
    t.SOURCE_FILE_ROW,
    t.BATCH_DATE
FROM typed t;

-- =============================================================================
-- The identifier collision
-- =============================================================================
-- ID 1 is Sam in USA, Vikas in IND and Mike in AUS. Three different people,
-- one identifier. Two readings are possible and the data cannot separate them:
-- either these members relocated, or each country numbers its members
-- independently.
--
-- The consequence is that MEMBER_ID alone cannot be the key, which is why the
-- per-country tables are keyed on (COUNTRY_CODE, MEMBER_ID). This is a genuine
-- contradiction with the brief's "latest record wins when a member has moved
-- countries", which requires an identifier stable across countries. The data
-- supplies none.
--
-- Reported, never auto-resolved: merging two people who share an ID is
-- unrecoverable, whereas leaving them separate and flagged is not.
CREATE OR REPLACE VIEW SKYPOINTS_STG.V_ID_COLLISIONS AS
SELECT
    MEMBER_ID,
    COUNT(DISTINCT COUNTRY_CODE)         AS COUNTRY_COUNT,
    ARRAY_AGG(DISTINCT COUNTRY_CODE)     AS COUNTRIES,
    ARRAY_AGG(MEMBER_NAME)               AS NAMES,
    -- Differing names under one ID is strong evidence that the ID namespaces
    -- are not globally stable, so the rows should not be treated as one member
    -- who relocated. It remains an inference, which is why this is surfaced as
    -- a column for a human to weigh rather than acted on automatically.
    COUNT(DISTINCT MEMBER_NAME) > 1      AS NAMES_DIFFER
FROM SKYPOINTS_STG.STG_MEMBER_PROFILE
WHERE MEMBER_ID IS NOT NULL
GROUP BY MEMBER_ID
HAVING COUNT(DISTINCT COUNTRY_CODE) > 1;
