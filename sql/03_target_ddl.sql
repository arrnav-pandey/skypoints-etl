-- =============================================================================
-- 03 - Per-country target tables
-- =============================================================================
-- The brief asks for one table per country (TABLE_INDIA, TABLE_USA, ...).
--
-- Design position, stated plainly because it is the most consequential choice
-- in this assessment:
--
--   Physically separate tables are the *requested* design, not the design I
--   would choose unprompted. On Snowflake, one CURRENT_MEMBER table clustered
--   by COUNTRY_CODE gives the same partition pruning as separate tables, with
--   none of the costs: no DDL per new market, no N-way UNION for global
--   reporting, no schema drift between countries, and relocations become an
--   UPDATE instead of a cross-table DELETE + INSERT.
--
--   The genuine arguments *for* physical separation are data residency and
--   access control -- if India's member data must live in an India-region
--   account, or country teams must be structurally unable to read each other's
--   members, then separate objects are the right answer and the cost is worth
--   paying. Those are the reasons to keep this design; storage layout is not.
--
--   Both are implemented: the per-country tables below satisfy the brief, and
--   V_MEMBER_GLOBAL re-unifies them so reporting is not punished for the split.
--
-- The layout is generated from one template, so every country table is
-- guaranteed identical -- hand-maintained per-country DDL drifts within months.
-- =============================================================================

CREATE TABLE IF NOT EXISTS SKYPOINTS_TGT.TABLE_TEMPLATE (
    MEMBER_NAME          VARCHAR(255) NOT NULL,
    MEMBER_ID            VARCHAR(18)  NOT NULL,
    ENROLLMENT_DATE      DATE         NOT NULL,
    LAST_FLIGHT_DATE     DATE,
    TIER_CODE            VARCHAR(5),
    AGENT_NAME           VARCHAR(255),
    STATE                VARCHAR(5),
    COUNTRY              VARCHAR(5),
    COUNTRY_CODE         VARCHAR(3)   NOT NULL,
    POST_CODE            NUMBER(5,0),
    DATE_OF_BIRTH        DATE,
    ACTIVE_MEMBER        VARCHAR(1),
    AGE                  NUMBER(3,0),
    STALE_MEMBER         BOOLEAN,
    SOURCE_FILE_NAME     VARCHAR(500),
    SOURCE_FILE_ROW      NUMBER,
    BATCH_DATE           DATE         NOT NULL,
    EFFECTIVE_FROM       TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),

    -- Declared for the optimiser and for documentation. Snowflake does not
    -- enforce these, which is exactly why 07_validations.sql tests them
    -- explicitly rather than trusting the constraint to hold.
    CONSTRAINT PK_MEMBER PRIMARY KEY (MEMBER_ID)
)
COMMENT = 'Template for per-country member tables. Not loaded directly.';

CREATE TABLE IF NOT EXISTS SKYPOINTS_TGT.TABLE_USA          LIKE SKYPOINTS_TGT.TABLE_TEMPLATE;
CREATE TABLE IF NOT EXISTS SKYPOINTS_TGT.TABLE_INDIA        LIKE SKYPOINTS_TGT.TABLE_TEMPLATE;
CREATE TABLE IF NOT EXISTS SKYPOINTS_TGT.TABLE_PHILIPPINES  LIKE SKYPOINTS_TGT.TABLE_TEMPLATE;
CREATE TABLE IF NOT EXISTS SKYPOINTS_TGT.TABLE_CANADA       LIKE SKYPOINTS_TGT.TABLE_TEMPLATE;
CREATE TABLE IF NOT EXISTS SKYPOINTS_TGT.TABLE_AUSTRALIA    LIKE SKYPOINTS_TGT.TABLE_TEMPLATE;

-- Unmappable countries land here rather than nowhere. A member whose country
-- is 'ATLANTIS' is still a member; losing them silently is worse than holding
-- them somewhere visible until the reference data is corrected.
CREATE TABLE IF NOT EXISTS SKYPOINTS_TGT.TABLE_UNKNOWN      LIKE SKYPOINTS_TGT.TABLE_TEMPLATE;

-- -----------------------------------------------------------------------------
-- Global view over the per-country split
-- -----------------------------------------------------------------------------
-- Without this, every cross-border question ("how many Gold members worldwide?")
-- requires an N-way UNION that must be edited each time a market opens.
CREATE OR REPLACE VIEW SKYPOINTS_TGT.V_MEMBER_GLOBAL AS
SELECT * FROM SKYPOINTS_TGT.TABLE_USA
UNION ALL SELECT * FROM SKYPOINTS_TGT.TABLE_INDIA
UNION ALL SELECT * FROM SKYPOINTS_TGT.TABLE_PHILIPPINES
UNION ALL SELECT * FROM SKYPOINTS_TGT.TABLE_CANADA
UNION ALL SELECT * FROM SKYPOINTS_TGT.TABLE_AUSTRALIA
UNION ALL SELECT * FROM SKYPOINTS_TGT.TABLE_UNKNOWN;

-- -----------------------------------------------------------------------------
-- Flattened redemption transactions (deliverable 4)
-- -----------------------------------------------------------------------------
-- Deliberately NOT split by country. Transactions are global by nature -- a
-- member in India redeems on a partner in the US -- and the member's country
-- can change, which would force transactions to migrate between tables and
-- silently rewrite history. Country is reached by joining to the member.
CREATE TABLE IF NOT EXISTS SKYPOINTS_TGT.REDEMPTION_TXN (
    TXN_ID               VARCHAR(50)  NOT NULL,
    MEMBER_ID            VARCHAR(18)  NOT NULL,
    TXN_DATE             DATE,
    PARTNER              VARCHAR(100),
    MILES_REDEEMED       NUMBER(12,0),
    STATUS               VARCHAR(20),
    FEED_DATE            DATE,
    SOURCE_FILE_NAME     VARCHAR(500),
    BATCH_DATE           DATE         NOT NULL,
    LOADED_AT            TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    CONSTRAINT PK_REDEMPTION PRIMARY KEY (TXN_ID)
)
-- Clustered on the two columns every query filters or joins on.
CLUSTER BY (TXN_DATE, MEMBER_ID)
COMMENT = 'One row per redemption transaction, current state.';
