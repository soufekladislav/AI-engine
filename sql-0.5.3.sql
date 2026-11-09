-- Fresh AI Engine 0.5.3. Only run in the ENGINE database if automatic
-- Base.metadata.create_all() failed due to missing CREATE TABLE privilege.
-- This does not modify customer balances or existing jobs.
CREATE TABLE IF NOT EXISTS chat_usage_users (
  chat_user_id VARCHAR(40) NOT NULL PRIMARY KEY,
  fresh_user_id VARCHAR(120) NOT NULL DEFAULT '',
  display_name VARCHAR(160) NOT NULL DEFAULT '',
  email VARCHAR(190) NOT NULL DEFAULT '',
  first_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX ix_chat_usage_users_fresh_user_id (fresh_user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
