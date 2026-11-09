-- Fresh AI Engine 0.3.2 schema (MySQL/MariaDB)

SET NAMES utf8mb4;

SET FOREIGN_KEY_CHECKS=0;


CREATE TABLE capability_definitions (
	code VARCHAR(50) NOT NULL, 
	label VARCHAR(120) NOT NULL, 
	description TEXT NOT NULL, 
	phrases_json TEXT NOT NULL, 
	priority INTEGER NOT NULL, 
	enabled BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (code)
)

;


CREATE TABLE customer_accounts (
	id VARCHAR(36) NOT NULL, 
	email VARCHAR(190) NOT NULL, 
	password_hash VARCHAR(255) NOT NULL, 
	company_name VARCHAR(190) NOT NULL, 
	billing_name VARCHAR(190) NOT NULL, 
	billing_ico VARCHAR(40) NOT NULL, 
	billing_dic VARCHAR(40) NOT NULL, 
	billing_address TEXT NOT NULL, 
	active BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE UNIQUE INDEX ix_customer_accounts_email ON customer_accounts (email);




CREATE TABLE account_email_verifications (
	account_id VARCHAR(36) NOT NULL,
	token_hash VARCHAR(64) NOT NULL,
	expires_at DATETIME NOT NULL,
	sent_at DATETIME NOT NULL,
	verified_at DATETIME,
	PRIMARY KEY (account_id),
	FOREIGN KEY(account_id) REFERENCES customer_accounts (id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX ix_account_email_verifications_token_hash ON account_email_verifications (token_hash);

CREATE TABLE diagnostic_shares (
	id VARCHAR(12) NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	issued_at DATETIME NOT NULL, 
	expires_at DATETIME NOT NULL, 
	refreshed_at DATETIME NOT NULL, 
	manifest_json LONGTEXT NOT NULL, 
	bundle_zip LONGBLOB NOT NULL, 
	PRIMARY KEY (id)
)

;


CREATE TABLE jobs (
	id VARCHAR(36) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	source VARCHAR(80) NOT NULL, 
	project_id VARCHAR(120), 
	user_id VARCHAR(120), 
	task_type VARCHAR(50) NOT NULL, 
	quality VARCHAR(20) NOT NULL, 
	max_cost_usd FLOAT, 
	max_output_tokens INTEGER NOT NULL, 
	prompt_hash VARCHAR(64) NOT NULL, 
	prompt_preview TEXT NOT NULL, 
	prompt_payload LONGTEXT, 
	context_payload LONGTEXT, 
	selected_route_code VARCHAR(80), 
	provider VARCHAR(40), 
	model_name VARCHAR(120), 
	response_preview TEXT, 
	response_payload LONGTEXT, 
	error TEXT, 
	input_tokens INTEGER NOT NULL, 
	output_tokens INTEGER NOT NULL, 
	estimated_cost_usd FLOAT NOT NULL, 
	latency_ms INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	started_at DATETIME, 
	finished_at DATETIME, 
	PRIMARY KEY (id)
)

;

CREATE INDEX ix_jobs_created_at ON jobs (created_at);

CREATE INDEX ix_jobs_model_name ON jobs (model_name);

CREATE INDEX ix_jobs_project_id ON jobs (project_id);

CREATE INDEX ix_jobs_provider ON jobs (provider);

CREATE INDEX ix_jobs_source ON jobs (source);

CREATE INDEX ix_jobs_status ON jobs (status);

CREATE INDEX ix_jobs_task_type ON jobs (task_type);

CREATE INDEX ix_jobs_user_id ON jobs (user_id);


CREATE TABLE model_routes (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	code VARCHAR(80) NOT NULL, 
	provider VARCHAR(40) NOT NULL, 
	model_name VARCHAR(120) NOT NULL, 
	enabled BOOL NOT NULL, 
	quality INTEGER NOT NULL, 
	priority INTEGER NOT NULL, 
	capabilities VARCHAR(250) NOT NULL, 
	input_usd_per_million FLOAT NOT NULL, 
	output_usd_per_million FLOAT NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE UNIQUE INDEX ix_model_routes_code ON model_routes (code);

CREATE INDEX ix_model_routes_provider ON model_routes (provider);


CREATE TABLE runtime_settings (
	`key` VARCHAR(100) NOT NULL, 
	value TEXT NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (`key`)
)

;


CREATE TABLE connectors (
	id VARCHAR(36) NOT NULL, 
	account_id VARCHAR(36) NOT NULL, 
	name VARCHAR(120) NOT NULL, 
	key_hash VARCHAR(64) NOT NULL, 
	key_prefix VARCHAR(24) NOT NULL, 
	active BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	rotated_at DATETIME, 
	expires_at DATETIME, 
	deleted_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES customer_accounts (id) ON DELETE CASCADE
)

;

CREATE INDEX ix_connectors_account_id ON connectors (account_id);

CREATE UNIQUE INDEX ix_connectors_key_hash ON connectors (key_hash);

CREATE INDEX ix_connectors_name ON connectors (name);


CREATE TABLE credit_ledger (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	account_id VARCHAR(36) NOT NULL, 
	delta_credits FLOAT NOT NULL, 
	entry_type VARCHAR(40) NOT NULL, 
	reference VARCHAR(120) NOT NULL, 
	note VARCHAR(255) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES customer_accounts (id) ON DELETE CASCADE
)

;

CREATE INDEX ix_credit_ledger_account_id ON credit_ledger (account_id);

CREATE INDEX ix_credit_ledger_created_at ON credit_ledger (created_at);

CREATE INDEX ix_credit_ledger_entry_type ON credit_ledger (entry_type);

CREATE INDEX ix_credit_ledger_reference ON credit_ledger (reference);


CREATE TABLE credit_orders (
	id VARCHAR(36) NOT NULL, 
	account_id VARCHAR(36) NOT NULL, 
	credits FLOAT NOT NULL, 
	amount_czk FLOAT NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	payment_reference VARCHAR(120) NOT NULL, 
	created_at DATETIME NOT NULL, 
	paid_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES customer_accounts (id) ON DELETE CASCADE
)

;

CREATE INDEX ix_credit_orders_account_id ON credit_orders (account_id);

CREATE INDEX ix_credit_orders_status ON credit_orders (status);


CREATE TABLE job_attempts (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	job_id VARCHAR(36) NOT NULL, 
	route_code VARCHAR(80) NOT NULL, 
	provider VARCHAR(40) NOT NULL, 
	model_name VARCHAR(120) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	latency_ms INTEGER NOT NULL, 
	input_tokens INTEGER NOT NULL, 
	output_tokens INTEGER NOT NULL, 
	estimated_cost_usd FLOAT NOT NULL, 
	error TEXT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE
)

;

CREATE INDEX ix_job_attempts_job_id ON job_attempts (job_id);

CREATE INDEX ix_job_attempts_provider ON job_attempts (provider);


CREATE TABLE routing_decisions (
	job_id VARCHAR(36) NOT NULL, 
	trace_json LONGTEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (job_id), 
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE
)

;


CREATE TABLE job_billing (
	job_id VARCHAR(36) NOT NULL, 
	account_id VARCHAR(36) NOT NULL, 
	connector_id VARCHAR(36) NOT NULL, 
	reserved_credits FLOAT NOT NULL, 
	credits_charged FLOAT NOT NULL, 
	cost_usd FLOAT NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (job_id), 
	FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE, 
	FOREIGN KEY(account_id) REFERENCES customer_accounts (id) ON DELETE CASCADE, 
	FOREIGN KEY(connector_id) REFERENCES connectors (id) ON DELETE CASCADE
)

;

CREATE INDEX ix_job_billing_account_id ON job_billing (account_id);

CREATE INDEX ix_job_billing_connector_id ON job_billing (connector_id);

SET FOREIGN_KEY_CHECKS=1;

-- v0.3.4: jednorázové tokeny pro obnovu hesla
CREATE TABLE IF NOT EXISTS `account_password_resets` (
  `account_id` VARCHAR(36) NOT NULL,
  `token_hash` VARCHAR(64) NOT NULL,
  `expires_at` DATETIME NOT NULL,
  `requested_at` DATETIME NOT NULL,
  `used_at` DATETIME NULL,
  PRIMARY KEY (`account_id`),
  UNIQUE KEY `uq_account_password_resets_token_hash` (`token_hash`),
  CONSTRAINT `fk_account_password_resets_account` FOREIGN KEY (`account_id`) REFERENCES `customer_accounts` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- v0.4.0: vazba lokálního účtu na centrální Fresh Crackers identitu
CREATE TABLE IF NOT EXISTS `external_identities` (
  `account_id` VARCHAR(36) NOT NULL,
  `provider` VARCHAR(40) NOT NULL DEFAULT 'freshcrackers',
  `external_user_id` VARCHAR(120) NOT NULL,
  `linked_at` DATETIME NOT NULL,
  PRIMARY KEY (`account_id`),
  UNIQUE KEY `uq_external_identities_user` (`external_user_id`),
  KEY `ix_external_identities_provider` (`provider`),
  CONSTRAINT `fk_external_identities_account` FOREIGN KEY (`account_id`) REFERENCES `customer_accounts` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- v0.4.0: idempotentní záznamy WooCommerce fulfillmentu
CREATE TABLE IF NOT EXISTS `external_orders` (
  `id` VARCHAR(80) NOT NULL,
  `account_id` VARCHAR(36) NOT NULL,
  `provider` VARCHAR(40) NOT NULL DEFAULT 'woocommerce',
  `external_order_id` VARCHAR(80) NOT NULL,
  `product_code` VARCHAR(100) NOT NULL DEFAULT '',
  `action_type` VARCHAR(40) NOT NULL DEFAULT 'credit',
  `action_value` FLOAT NOT NULL DEFAULT 0,
  `amount` FLOAT NOT NULL DEFAULT 0,
  `currency` VARCHAR(12) NOT NULL DEFAULT 'CZK',
  `status` VARCHAR(40) NOT NULL DEFAULT 'completed',
  `created_at` DATETIME NOT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_external_orders_account_id` (`account_id`),
  KEY `ix_external_orders_external_order_id` (`external_order_id`),
  KEY `ix_external_orders_provider` (`provider`),
  KEY `ix_external_orders_status` (`status`),
  CONSTRAINT `fk_external_orders_account` FOREIGN KEY (`account_id`) REFERENCES `customer_accounts` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- v0.4.4: metadata požadavků technické podpory pro rate limit; bez obsahu zpráv a skutečných IP adres
CREATE TABLE IF NOT EXISTS `support_submissions` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `ip_hash` VARCHAR(64) NOT NULL,
  `email_hash` VARCHAR(64) NOT NULL,
  `account_id` VARCHAR(36) NULL,
  `reference` VARCHAR(24) NOT NULL,
  `created_at` DATETIME NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_support_submissions_reference` (`reference`),
  KEY `ix_support_submissions_ip_hash` (`ip_hash`),
  KEY `ix_support_submissions_email_hash` (`email_hash`),
  KEY `ix_support_submissions_created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- v0.4.5: administrace zákaznických účtů a koš
CREATE TABLE IF NOT EXISTS `account_trash` (
  `account_id` VARCHAR(36) NOT NULL,
  `deleted_at` DATETIME NOT NULL,
  `purged_at` DATETIME NULL,
  PRIMARY KEY (`account_id`),
  CONSTRAINT `fk_account_trash_account` FOREIGN KEY (`account_id`) REFERENCES `customer_accounts` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `admin_account_audit` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `account_id` VARCHAR(36) NOT NULL,
  `action` VARCHAR(80) NOT NULL,
  `details` VARCHAR(255) NOT NULL DEFAULT '',
  `created_at` DATETIME NOT NULL,
  PRIMARY KEY (`id`),
  KEY `ix_admin_account_audit_account_id` (`account_id`),
  KEY `ix_admin_account_audit_action` (`action`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `account_invitations` (
  `account_id` VARCHAR(36) NOT NULL,
  `token_hash` VARCHAR(64) NOT NULL,
  `expires_at` DATETIME NOT NULL,
  `used_at` DATETIME NULL,
  `created_at` DATETIME NOT NULL,
  PRIMARY KEY (`account_id`),
  UNIQUE KEY `uq_account_invitations_token_hash` (`token_hash`),
  CONSTRAINT `fk_account_invitations_account` FOREIGN KEY (`account_id`) REFERENCES `customer_accounts` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `account_admin_state` (
  `account_id` VARCHAR(36) NOT NULL,
  `suspended` BOOL NOT NULL DEFAULT 0,
  `updated_at` DATETIME NOT NULL,
  PRIMARY KEY (`account_id`),
  CONSTRAINT `fk_account_admin_state_account` FOREIGN KEY (`account_id`) REFERENCES `customer_accounts` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

