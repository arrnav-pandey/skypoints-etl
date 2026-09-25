-- =============================================================================
-- 01 - Landing (raw) layer
-- =============================================================================
-- The landing layer is an immutable, faithful copy of what the source sent.
-- Nothing is cast, trimmed or rejected here. That is deliberate: at billions of
-- rows a day the expensive mistake is discovering a parsing bug after the
-- source file has aged out of the landing zone. Keeping the raw line means any
-- batch can be replayed without asking the source system to resend.
--
-- Columns are VARCHAR because a raw layer that enforces types cannot land the
-- very records that violate them -- exactly the records worth investigating.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS SKYPOINTS_RAW;
CREATE SCHEMA IF NOT EXISTS SKYPOINTS_STG;
CREATE SCHEMA IF NOT EXISTS SKYPOINTS_TGT;

-- -----------------------------------------------------------------------------
-- Member profile flat file
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS SKYPOINTS_RAW.RAW_MEMBER_PROFILE (
    RAW_MEMBER_SK        NUMBER      IDENTITY   NOT NULL,

    -- Source columns, untyped and untouched.
    RECORD_TAG           VARCHAR(1),
    MEMBER_NAME          VARCHAR(255),
    MEMBER_ID            VARCHAR(18),
    ENROLLMENT_DATE      VARCHAR(8),
    LAST_FLIGHT_DATE     VARCHAR(8),
    TIER_CODE            VARCHAR(5),
    AGENT_NAME           VARCHAR(255),
    STATE                VARCHAR(5),
    COUNTRY              VARCHAR(5),
    POST_CODE            VARCHAR(5),   -- declared in the layout; absent from the sample file
    DATE_OF_BIRTH        VARCHAR(8),
    ACTIVE_MEMBER        VARCHAR(1),

    -- Lineage. Without these a bad row cannot be traced back to a line in a
    -- file, which makes any conversation with the source system guesswork.
    SOURCE_FILE_NAME     VARCHAR(500) NOT NULL,
    SOURCE_FILE_ROW      NUMBER       NOT NULL,
    RAW_LINE             VARCHAR,
    BATCH_DATE           DATE         NOT NULL,
    LOADED_AT            TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
)
CLUSTER BY (BATCH_DATE)
COMMENT = 'Immutable landing copy of the daily member profile flat file.';

-- -----------------------------------------------------------------------------
-- Partner redemption JSON feed
-- -----------------------------------------------------------------------------
-- Landed as VARIANT, one row per source document. Snowflake stores VARIANT
-- columnar-shredded, so this is not a blob: the flattening in 06 reads only the
-- attributes it touches rather than re-parsing whole documents.
CREATE TABLE IF NOT EXISTS SKYPOINTS_RAW.RAW_REDEMPTION_FEED (
    RAW_FEED_SK          NUMBER      IDENTITY   NOT NULL,
    PAYLOAD              VARIANT      NOT NULL,
    SOURCE_FILE_NAME     VARCHAR(500) NOT NULL,
    SOURCE_FILE_ROW      NUMBER       NOT NULL,
    BATCH_DATE           DATE         NOT NULL,
    LOADED_AT            TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
)
CLUSTER BY (BATCH_DATE)
COMMENT = 'Immutable landing copy of the daily partner redemption JSON feed.';

-- -----------------------------------------------------------------------------
-- Ingestion objects
-- -----------------------------------------------------------------------------
CREATE FILE FORMAT IF NOT EXISTS SKYPOINTS_RAW.FF_MEMBER_PIPE
    TYPE = CSV
    FIELD_DELIMITER = '|'
    SKIP_HEADER = 0            -- the |H| record is filtered in the COPY, not skipped
                               -- blindly, so a missing header is detectable
    TRIM_SPACE = TRUE
    EMPTY_FIELD_AS_NULL = TRUE
    NULL_IF = ('')
    -- Never silently discard a malformed row: it must land so it can be
    -- quarantined with a reason.
    ON_ERROR = 'CONTINUE';

CREATE FILE FORMAT IF NOT EXISTS SKYPOINTS_RAW.FF_REDEMPTION_JSON
    TYPE = JSON
    STRIP_OUTER_ARRAY = TRUE;

CREATE STAGE IF NOT EXISTS SKYPOINTS_RAW.STG_MEMBER_FEED
    FILE_FORMAT = SKYPOINTS_RAW.FF_MEMBER_PIPE;

CREATE STAGE IF NOT EXISTS SKYPOINTS_RAW.STG_REDEMPTION_FEED
    FILE_FORMAT = SKYPOINTS_RAW.FF_REDEMPTION_JSON;

-- Leading '|' produces an empty $1, so source columns start at $2.
COPY INTO SKYPOINTS_RAW.RAW_MEMBER_PROFILE (
    RECORD_TAG, MEMBER_NAME, MEMBER_ID, ENROLLMENT_DATE, LAST_FLIGHT_DATE,
    TIER_CODE, AGENT_NAME, STATE, COUNTRY, DATE_OF_BIRTH, ACTIVE_MEMBER,
    SOURCE_FILE_NAME, SOURCE_FILE_ROW, BATCH_DATE
)
FROM (
    SELECT
        $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
        METADATA$FILENAME,
        METADATA$FILE_ROW_NUMBER,
        TO_DATE(REGEXP_SUBSTR(METADATA$FILENAME, '(\\d{8})_\\d{8}', 1, 1, 'e', 1), 'YYYYMMDD')
    FROM @SKYPOINTS_RAW.STG_MEMBER_FEED
)
FILE_FORMAT = (FORMAT_NAME = SKYPOINTS_RAW.FF_MEMBER_PIPE)
PATTERN = '.*SKYPOINTS_MEMBERS_[0-9]{8}_[0-9]{8}[.]dat'
ON_ERROR = 'CONTINUE';

COPY INTO SKYPOINTS_RAW.RAW_REDEMPTION_FEED (
    PAYLOAD, SOURCE_FILE_NAME, SOURCE_FILE_ROW, BATCH_DATE
)
FROM (
    SELECT
        $1,
        METADATA$FILENAME,
        METADATA$FILE_ROW_NUMBER,
        TO_DATE(REGEXP_SUBSTR(METADATA$FILENAME, '(\\d{8})_\\d{8}', 1, 1, 'e', 1), 'YYYYMMDD')
    FROM @SKYPOINTS_RAW.STG_REDEMPTION_FEED
)
FILE_FORMAT = (FORMAT_NAME = SKYPOINTS_RAW.FF_REDEMPTION_JSON)
PATTERN = '.*SKYPOINTS_REDEMPTIONS_[0-9]{8}_[0-9]{8}[.]json'
ON_ERROR = 'CONTINUE';
