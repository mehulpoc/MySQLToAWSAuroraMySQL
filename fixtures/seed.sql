-- Seed data for marketo_src
-- Uses a 5-way cross-join of a 10-row digit table to generate large row sets
-- set-based (fast). A regular table (not TEMPORARY) is required because
-- MySQL 5.7 prohibits referencing the same TEMPORARY table more than once
-- in a single query ("Can't reopen table") so using _seed_d table as temporary table.
--
-- Row counts:
--   leads           acc_12345: 52 000   acc_99999: 21 000
--   activities      acc_12345: 52 000   acc_99999: 21 000
--   email_prefs     acc_12345:      4   acc_99999:      1  (composite PK)

USE marketo_src;

-- ── digit helper table ────────────────────────────────────────────────────────
DROP TABLE IF EXISTS _seed_d;
CREATE TABLE _seed_d (n TINYINT UNSIGNED NOT NULL) ENGINE=MEMORY;
INSERT INTO _seed_d VALUES (0),(1),(2),(3),(4),(5),(6),(7),(8),(9);

-- ── leads: 52 000 rows for acc_12345 ─────────────────────────────────────────
INSERT INTO leads (account_id, email, created_at)
SELECT
    'acc_12345',
    CONCAT('lead_', seq + 1, '@acc12345.example'),
    DATE_ADD('2022-01-01 00:00:00', INTERVAL (seq * 10) MINUTE)
FROM (
    SELECT a.n + b.n*10 + c.n*100 + d.n*1000 + e.n*10000 AS seq
    FROM _seed_d a, _seed_d b, _seed_d c, _seed_d d, _seed_d e
    HAVING seq < 52000
) t;

-- ── leads: 21 000 rows for acc_99999 ─────────────────────────────────────────
INSERT INTO leads (account_id, email, created_at)
SELECT
    'acc_99999',
    CONCAT('lead_', seq + 1, '@acc99999.example'),
    DATE_ADD('2022-06-01 00:00:00', INTERVAL (seq * 10) MINUTE)
FROM (
    SELECT a.n + b.n*10 + c.n*100 + d.n*1000 + e.n*10000 AS seq
    FROM _seed_d a, _seed_d b, _seed_d c, _seed_d d, _seed_d e
    HAVING seq < 21000
) t;

-- ── activities: 52 000 rows for acc_12345 ────────────────────────────────────
-- lead_id matches acc_12345 leads (ids 1..52000)
INSERT INTO activities (account_id, lead_id, activity_type, payload, created_at)
SELECT
    'acc_12345',
    seq + 1,
    ELT(1 + (seq MOD 4), 'email_open', 'email_click', 'form_fill', 'web_visit'),
    JSON_OBJECT('campaign', CONCAT('camp_', seq MOD 20), 'score', seq MOD 100),
    DATE_ADD('2022-01-01 00:00:00', INTERVAL (seq * 11) MINUTE)
FROM (
    SELECT a.n + b.n*10 + c.n*100 + d.n*1000 + e.n*10000 AS seq
    FROM _seed_d a, _seed_d b, _seed_d c, _seed_d d, _seed_d e
    HAVING seq < 52000
) t;

-- ── activities: 21 000 rows for acc_99999 ────────────────────────────────────
-- lead_id matches acc_99999 leads (ids 52001..73000)
INSERT INTO activities (account_id, lead_id, activity_type, payload, created_at)
SELECT
    'acc_99999',
    52000 + seq + 1,
    ELT(1 + (seq MOD 4), 'email_open', 'email_click', 'form_fill', 'web_visit'),
    JSON_OBJECT('campaign', CONCAT('camp_', seq MOD 10), 'score', seq MOD 100),
    DATE_ADD('2022-06-01 00:00:00', INTERVAL (seq * 11) MINUTE)
FROM (
    SELECT a.n + b.n*10 + c.n*100 + d.n*1000 + e.n*10000 AS seq
    FROM _seed_d a, _seed_d b, _seed_d c, _seed_d d, _seed_d e
    HAVING seq < 21000
) t;

-- ── email_preferences: composite-PK rows ─────────────────────────────────────
INSERT INTO email_preferences (account_id, email, preference_type, value, updated_at)
VALUES
    ('acc_12345', 'lead_1@acc12345.example',  'marketing',    'opted_in',  NOW()),
    ('acc_12345', 'lead_1@acc12345.example',  'product_news', 'opted_in',  NOW()),
    ('acc_12345', 'lead_2@acc12345.example',  'marketing',    'opted_out', NOW()),
    ('acc_12345', 'lead_2@acc12345.example',  'product_news', 'opted_in',  NOW()),
    ('acc_99999', 'lead_1@acc99999.example',  'marketing',    'opted_in',  NOW());

-- ── cleanup ───────────────────────────────────────────────────────────────────
DROP TABLE _seed_d;
