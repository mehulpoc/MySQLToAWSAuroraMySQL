-- Source schema: on-prem multi-tenant MySQL 5.7
CREATE DATABASE IF NOT EXISTS marketo_src
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE marketo_src;

CREATE TABLE IF NOT EXISTS leads (
  id         BIGINT       NOT NULL AUTO_INCREMENT,
  account_id VARCHAR(32)  NOT NULL,
  email      VARCHAR(255),
  created_at DATETIME,
  PRIMARY KEY (id),
  KEY ix_acct (account_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS activities (
  id            BIGINT       NOT NULL AUTO_INCREMENT,
  account_id    VARCHAR(32)  NOT NULL,
  lead_id       BIGINT,
  activity_type VARCHAR(64),
  payload       JSON,
  created_at    DATETIME,
  PRIMARY KEY (id),
  KEY ix_acct (account_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Composite-PK table: exercises the "not chunkable" path in the orchestrator
CREATE TABLE IF NOT EXISTS email_preferences (
  account_id       VARCHAR(32)  NOT NULL,
  email            VARCHAR(255) NOT NULL,
  preference_type  VARCHAR(64)  NOT NULL,
  value            VARCHAR(255),
  updated_at       DATETIME,
  PRIMARY KEY (account_id, email, preference_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
