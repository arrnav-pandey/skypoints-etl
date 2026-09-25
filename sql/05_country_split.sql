-- =============================================================================
-- 05 - Route members into per-country tables (deliverable 3)
-- =============================================================================
-- Two problems are solved here and they are easy to conflate:
--
--   1. Latest record wins.   A member can appear several times in one batch.
--      Exactly one version must survive.
--   2. Member has moved.     The surviving version may route to a different
--      country than the row already sitting in a target table. Inserting the
--      new row is not enough -- the old one must be deleted, or the member
--      exists in two countries at once and every country-level count is wrong.
--
-- MERGE alone handles (1) but silently fails (2), because a MERGE against
-- TABLE_INDIA cannot see the stale row in TABLE_USA. The DELETE step below is
-- the part that is easy to forget and expensive to discover later.
-- =============================================================================

SET BATCH_DATE = '2024-01-15';

-- -----------------------------------------------------------------------------
-- Step 1: one surviving row per member
-- -----------------------------------------------------------------------------
-- The source provides NO record-version timestamp. Nothing in the feed says
-- when a row was last changed, so "latest" cannot be read directly and has to
-- be approximated. The ordering below is that approximation, not a fact:
--
--   BATCH_DATE        authoritative - when we received the row.
--   LAST_FLIGHT_DATE  an assumption, and the debatable one. It is a business
--                     date, not a version marker, so it can disagree with
--                     arrival order: a row delivered today reporting an old
--                     flight loses to one delivered last week reporting a
--                     recent flight. Deliberate for this feed, where recent
--                     activity is the best available proxy for the current
--                     profile - but an inference, and the first thing to drop
--                     if the source ever supplies a change timestamp.
--   ENROLLMENT_DATE   re-enrolment after a move.
--   SOURCE_FILE_ROW   determinism only. Without it two runs over the same file
--                     can disagree.
--
-- If the source can add LAST_UPDATED, this collapses to ORDER BY LAST_UPDATED
-- DESC and every assumption above disappears.
CREATE OR REPLACE TEMPORARY TABLE SKYPOINTS_STG.TMP_LATEST_MEMBER AS
SELECT * EXCLUDE (RN)
FROM (
    SELECT
        s.*,
        ROW_NUMBER() OVER (
            PARTITION BY s.MEMBER_ID
            ORDER BY s.BATCH_DATE          DESC NULLS LAST,
                     s.LAST_FLIGHT_DATE    DESC NULLS LAST,
                     s.ENROLLMENT_DATE     DESC NULLS LAST,
                     s.SOURCE_FILE_ROW     DESC
        ) AS RN
    FROM SKYPOINTS_STG.STG_MEMBER_PROFILE s
    WHERE s.BATCH_DATE = TO_DATE($BATCH_DATE)
      AND s.IS_VALID
) ranked
WHERE RN = 1;

-- -----------------------------------------------------------------------------
-- Step 2: detect relocations before writing anything
-- -----------------------------------------------------------------------------
-- Computed against the global view so a move is found wherever the member
-- currently lives, without knowing their previous country in advance.
CREATE OR REPLACE TEMPORARY TABLE SKYPOINTS_STG.TMP_COUNTRY_MOVES AS
SELECT
    g.MEMBER_ID,
    g.COUNTRY_CODE AS PREVIOUS_COUNTRY_CODE,
    l.COUNTRY_CODE AS NEW_COUNTRY_CODE
FROM SKYPOINTS_TGT.V_MEMBER_GLOBAL g
JOIN SKYPOINTS_STG.TMP_LATEST_MEMBER l
  ON l.MEMBER_ID = g.MEMBER_ID
WHERE g.COUNTRY_CODE <> l.COUNTRY_CODE;

-- -----------------------------------------------------------------------------
-- Step 3: upsert into the new country table
-- -----------------------------------------------------------------------------
-- Repeat per country table; in production this statement is generated from
-- REF_COUNTRY so onboarding a market needs no new hand-written SQL.
MERGE INTO SKYPOINTS_TGT.TABLE_INDIA AS tgt
USING (
    SELECT * FROM SKYPOINTS_STG.TMP_LATEST_MEMBER WHERE COUNTRY_CODE = 'IND'
) AS src
ON tgt.MEMBER_ID = src.MEMBER_ID
WHEN MATCHED THEN UPDATE SET
    tgt.MEMBER_NAME       = src.MEMBER_NAME,
    tgt.ENROLLMENT_DATE   = src.ENROLLMENT_DATE,
    tgt.LAST_FLIGHT_DATE  = src.LAST_FLIGHT_DATE,
    tgt.TIER_CODE         = src.TIER_CODE,
    tgt.AGENT_NAME        = src.AGENT_NAME,
    tgt.STATE             = src.STATE,
    tgt.COUNTRY           = src.COUNTRY,
    tgt.COUNTRY_CODE      = src.COUNTRY_CODE,
    tgt.POST_CODE         = src.POST_CODE,
    tgt.DATE_OF_BIRTH     = src.DATE_OF_BIRTH,
    tgt.ACTIVE_MEMBER     = src.ACTIVE_MEMBER,
    tgt.AGE               = src.AGE,
    tgt.STALE_MEMBER      = src.STALE_MEMBER,
    tgt.SOURCE_FILE_NAME  = src.SOURCE_FILE_NAME,
    tgt.SOURCE_FILE_ROW   = src.SOURCE_FILE_ROW,
    tgt.BATCH_DATE        = src.BATCH_DATE,
    tgt.EFFECTIVE_FROM    = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT (
    MEMBER_NAME, MEMBER_ID, ENROLLMENT_DATE, LAST_FLIGHT_DATE, TIER_CODE,
    AGENT_NAME, STATE, COUNTRY, COUNTRY_CODE, POST_CODE, DATE_OF_BIRTH,
    ACTIVE_MEMBER, AGE, STALE_MEMBER, SOURCE_FILE_NAME, SOURCE_FILE_ROW,
    BATCH_DATE
) VALUES (
    src.MEMBER_NAME, src.MEMBER_ID, src.ENROLLMENT_DATE, src.LAST_FLIGHT_DATE,
    src.TIER_CODE, src.AGENT_NAME, src.STATE, src.COUNTRY, src.COUNTRY_CODE,
    src.POST_CODE, src.DATE_OF_BIRTH, src.ACTIVE_MEMBER, src.AGE,
    src.STALE_MEMBER, src.SOURCE_FILE_NAME, src.SOURCE_FILE_ROW, src.BATCH_DATE
);

-- ... the same MERGE for TABLE_USA ('USA'), TABLE_PHILIPPINES ('PHL'),
-- TABLE_CANADA ('CAN'), TABLE_AUSTRALIA ('AUS'), TABLE_UNKNOWN ('UNK').
-- See generate_country_merge() below.

-- -----------------------------------------------------------------------------
-- Step 4: remove relocated members from their previous country table
-- -----------------------------------------------------------------------------
-- Run AFTER the insert, never before: if the job dies between the two, a
-- duplicate member is a recoverable inconsistency, whereas a deleted-then-
-- never-inserted member is data loss.
DELETE FROM SKYPOINTS_TGT.TABLE_USA
WHERE MEMBER_ID IN (
    SELECT MEMBER_ID FROM SKYPOINTS_STG.TMP_COUNTRY_MOVES
    WHERE PREVIOUS_COUNTRY_CODE = 'USA'
);
-- ... repeat per country table.

-- -----------------------------------------------------------------------------
-- Generating the per-country statements
-- -----------------------------------------------------------------------------
-- Hand-writing one MERGE per country guarantees drift the moment a market is
-- added. Generating them from REF_COUNTRY keeps every country identical and
-- makes onboarding a data change rather than a code change.
CREATE OR REPLACE PROCEDURE SKYPOINTS_TGT.SP_ROUTE_MEMBERS(BATCH_DATE STRING)
RETURNS STRING
LANGUAGE SQL
AS
$$
DECLARE
    c CURSOR FOR
        SELECT DISTINCT COUNTRY_CODE, TARGET_TABLE
        FROM SKYPOINTS_STG.REF_COUNTRY
        WHERE IS_ACTIVE;
    routed INTEGER DEFAULT 0;
BEGIN
    FOR rec IN c DO
        EXECUTE IMMEDIATE
            'MERGE INTO SKYPOINTS_TGT.' || rec.TARGET_TABLE || ' AS tgt ' ||
            'USING (SELECT * FROM SKYPOINTS_STG.TMP_LATEST_MEMBER ' ||
            '       WHERE COUNTRY_CODE = ''' || rec.COUNTRY_CODE || ''') AS src ' ||
            'ON tgt.MEMBER_ID = src.MEMBER_ID ' ||
            'WHEN MATCHED THEN UPDATE SET tgt.MEMBER_NAME = src.MEMBER_NAME, ' ||
            '  tgt.LAST_FLIGHT_DATE = src.LAST_FLIGHT_DATE, tgt.TIER_CODE = src.TIER_CODE, ' ||
            '  tgt.COUNTRY = src.COUNTRY, tgt.COUNTRY_CODE = src.COUNTRY_CODE, ' ||
            '  tgt.AGE = src.AGE, tgt.STALE_MEMBER = src.STALE_MEMBER, ' ||
            '  tgt.BATCH_DATE = src.BATCH_DATE, tgt.EFFECTIVE_FROM = CURRENT_TIMESTAMP() ' ||
            'WHEN NOT MATCHED THEN INSERT (MEMBER_NAME, MEMBER_ID, ENROLLMENT_DATE, ' ||
            '  LAST_FLIGHT_DATE, TIER_CODE, AGENT_NAME, STATE, COUNTRY, COUNTRY_CODE, ' ||
            '  POST_CODE, DATE_OF_BIRTH, ACTIVE_MEMBER, AGE, STALE_MEMBER, ' ||
            '  SOURCE_FILE_NAME, SOURCE_FILE_ROW, BATCH_DATE) ' ||
            'VALUES (src.MEMBER_NAME, src.MEMBER_ID, src.ENROLLMENT_DATE, ' ||
            '  src.LAST_FLIGHT_DATE, src.TIER_CODE, src.AGENT_NAME, src.STATE, ' ||
            '  src.COUNTRY, src.COUNTRY_CODE, src.POST_CODE, src.DATE_OF_BIRTH, ' ||
            '  src.ACTIVE_MEMBER, src.AGE, src.STALE_MEMBER, src.SOURCE_FILE_NAME, ' ||
            '  src.SOURCE_FILE_ROW, src.BATCH_DATE)';

        EXECUTE IMMEDIATE
            'DELETE FROM SKYPOINTS_TGT.' || rec.TARGET_TABLE ||
            ' WHERE MEMBER_ID IN (SELECT MEMBER_ID FROM SKYPOINTS_STG.TMP_COUNTRY_MOVES ' ||
            '                     WHERE PREVIOUS_COUNTRY_CODE = ''' || rec.COUNTRY_CODE || ''')';

        routed := routed + 1;
    END FOR;
    RETURN 'routed ' || routed || ' country tables for batch ' || :BATCH_DATE;
END;
$$;
