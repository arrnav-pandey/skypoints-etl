-- =============================================================================
-- 02 - Staging layer DDL
-- =============================================================================
-- Staging is where the data becomes typed, conformed and enriched. It is also
-- where every record carries its own verdict: IS_VALID plus the reasons, so a
-- rejected row is explainable without re-running the pipeline.
--
-- Derived columns required by the brief:
--   AGE            - whole years between DATE_OF_BIRTH and BATCH_DATE
--   STALE_MEMBER   - TRUE when days since LAST_FLIGHT_DATE > 90
--
-- Both are derived from BATCH_DATE rather than CURRENT_DATE() so that replaying
-- a batch from six months ago reproduces the values it originally produced.
-- Using CURRENT_DATE() here would make the table non-deterministic and every
-- downstream reconciliation unreproducible.
-- =============================================================================

CREATE TABLE IF NOT EXISTS SKYPOINTS_STG.STG_MEMBER_PROFILE (
    MEMBER_NAME          VARCHAR(255),
    MEMBER_ID            VARCHAR(18),
    ENROLLMENT_DATE      DATE,
    LAST_FLIGHT_DATE     DATE,
    TIER_CODE            VARCHAR(5),
    AGENT_NAME           VARCHAR(255),
    STATE                VARCHAR(5),
    COUNTRY              VARCHAR(5),      -- as received: 'PHIL', 'AU', ...
    COUNTRY_CODE         VARCHAR(3),      -- conformed to ISO alpha-3
    POST_CODE            NUMBER(5,0),
    DATE_OF_BIRTH        DATE,
    ACTIVE_MEMBER        VARCHAR(1),

    -- Derived
    AGE                  NUMBER(3,0),
    STALE_MEMBER         BOOLEAN,

    -- Verdict
    IS_VALID             BOOLEAN      NOT NULL DEFAULT TRUE,
    REJECT_REASONS       ARRAY,
    WARN_REASONS         ARRAY,

    -- Lineage
    SOURCE_FILE_NAME     VARCHAR(500),
    SOURCE_FILE_ROW      NUMBER,
    BATCH_DATE           DATE         NOT NULL,
    STAGED_AT            TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP()
)
-- Clustered on the batch/country pair the split step filters by, so routing a
-- country reads that country's micro-partitions instead of the whole day.
CLUSTER BY (BATCH_DATE, COUNTRY_CODE)
COMMENT = 'Typed, conformed and enriched member profiles awaiting routing.';

-- -----------------------------------------------------------------------------
-- Reference data: country conformance
-- -----------------------------------------------------------------------------
-- The sample sends 'USA', 'IND', 'PHIL', 'CAN', 'AU' -- a mix of ISO alpha-3
-- and ad-hoc abbreviations. Held as a table rather than a CASE expression so
-- that onboarding a new market is an INSERT by a data steward, not a code
-- deployment by an engineer.
CREATE TABLE IF NOT EXISTS SKYPOINTS_STG.REF_COUNTRY (
    SOURCE_VALUE         VARCHAR(20)  NOT NULL,
    COUNTRY_CODE         VARCHAR(3)   NOT NULL,
    COUNTRY_NAME         VARCHAR(100) NOT NULL,
    TARGET_TABLE         VARCHAR(100) NOT NULL,
    IS_ACTIVE            BOOLEAN      DEFAULT TRUE,
    CONSTRAINT PK_REF_COUNTRY PRIMARY KEY (SOURCE_VALUE)
)
COMMENT = 'Maps raw source country values onto ISO alpha-3 and a target table.';

INSERT INTO SKYPOINTS_STG.REF_COUNTRY (SOURCE_VALUE, COUNTRY_CODE, COUNTRY_NAME, TARGET_TABLE)
SELECT column1, column2, column3, column4
FROM VALUES
    ('USA',  'USA', 'United States',  'TABLE_USA'),
    ('US',   'USA', 'United States',  'TABLE_USA'),
    ('IND',  'IND', 'India',          'TABLE_INDIA'),
    ('IN',   'IND', 'India',          'TABLE_INDIA'),
    ('PHIL', 'PHL', 'Philippines',    'TABLE_PHILIPPINES'),
    ('PHL',  'PHL', 'Philippines',    'TABLE_PHILIPPINES'),
    ('CAN',  'CAN', 'Canada',         'TABLE_CANADA'),
    ('AU',   'AUS', 'Australia',      'TABLE_AUSTRALIA'),
    ('AUS',  'AUS', 'Australia',      'TABLE_AUSTRALIA'),
    ('UK',   'GBR', 'United Kingdom', 'TABLE_UNITED_KINGDOM'),
    ('GBR',  'GBR', 'United Kingdom', 'TABLE_UNITED_KINGDOM')
WHERE column1 NOT IN (SELECT SOURCE_VALUE FROM SKYPOINTS_STG.REF_COUNTRY);

-- -----------------------------------------------------------------------------
-- Quarantine
-- -----------------------------------------------------------------------------
-- Rejected rows are kept, not deleted. A row that fails today may be the
-- evidence that fixes the source tomorrow, and a quarantine table makes the
-- cost of bad data visible instead of hiding it in a row-count discrepancy.
CREATE TABLE IF NOT EXISTS SKYPOINTS_STG.QUARANTINE_MEMBER_PROFILE
    LIKE SKYPOINTS_STG.STG_MEMBER_PROFILE;
