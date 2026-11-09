-- Fresh AI Engine 0.7.0 / Fresh Key
-- Additive DDL only. Existing Engine tables/data are untouched.

CREATE TABLE IF NOT EXISTS fresh_key_app_bindings (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	connector_id VARCHAR(36) NOT NULL, 
	app_code VARCHAR(80) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(connector_id) REFERENCES connectors (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_fresh_key_app_bindings_app_code ON fresh_key_app_bindings (app_code);

CREATE UNIQUE INDEX IF NOT EXISTS ix_fresh_key_app_bindings_connector_id ON fresh_key_app_bindings (connector_id);

CREATE TABLE IF NOT EXISTS fresh_key_identities (
	id VARCHAR(36) NOT NULL, 
	email VARCHAR(190) NOT NULL, 
	display_name VARCHAR(190) NOT NULL, 
	email_verified BOOL NOT NULL, 
	engine_account_id VARCHAR(36), 
	active BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(engine_account_id) REFERENCES customer_accounts (id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_fresh_key_identities_email ON fresh_key_identities (email);

CREATE INDEX IF NOT EXISTS ix_fresh_key_identities_email_verified ON fresh_key_identities (email_verified);

CREATE UNIQUE INDEX IF NOT EXISTS ix_fresh_key_identities_engine_account_id ON fresh_key_identities (engine_account_id);

CREATE TABLE IF NOT EXISTS fresh_key_identity_bindings (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	identity_id VARCHAR(36) NOT NULL, 
	app_code VARCHAR(80) NOT NULL, 
	external_user_id VARCHAR(120) NOT NULL, 
	created_at DATETIME NOT NULL, 
	last_seen_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_fk_identity_binding_app_user UNIQUE (app_code, external_user_id), 
	FOREIGN KEY(identity_id) REFERENCES fresh_key_identities (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_fresh_key_identity_bindings_app_code ON fresh_key_identity_bindings (app_code);

CREATE INDEX IF NOT EXISTS ix_fresh_key_identity_bindings_external_user_id ON fresh_key_identity_bindings (external_user_id);

CREATE INDEX IF NOT EXISTS ix_fresh_key_identity_bindings_identity_id ON fresh_key_identity_bindings (identity_id);

CREATE TABLE IF NOT EXISTS fresh_key_projects (
	id VARCHAR(36) NOT NULL, 
	name VARCHAR(190) NOT NULL, 
	organization_name VARCHAR(190) NOT NULL, 
	website_url VARCHAR(500) NOT NULL, 
	owner_identity_id VARCHAR(36) NOT NULL, 
	active BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(owner_identity_id) REFERENCES fresh_key_identities (id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_fresh_key_projects_name ON fresh_key_projects (name);

CREATE INDEX IF NOT EXISTS ix_fresh_key_projects_owner_identity_id ON fresh_key_projects (owner_identity_id);

CREATE TABLE IF NOT EXISTS fresh_key_project_members (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	project_id VARCHAR(36) NOT NULL, 
	identity_id VARCHAR(36) NOT NULL, 
	`role` VARCHAR(40) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	can_manage_connections BOOL NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_fk_project_member UNIQUE (project_id, identity_id), 
	FOREIGN KEY(project_id) REFERENCES fresh_key_projects (id) ON DELETE CASCADE, 
	FOREIGN KEY(identity_id) REFERENCES fresh_key_identities (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_fresh_key_project_members_identity_id ON fresh_key_project_members (identity_id);

CREATE INDEX IF NOT EXISTS ix_fresh_key_project_members_project_id ON fresh_key_project_members (project_id);

CREATE INDEX IF NOT EXISTS ix_fresh_key_project_members_status ON fresh_key_project_members (status);

CREATE TABLE IF NOT EXISTS fresh_key_project_bindings (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	project_id VARCHAR(36) NOT NULL, 
	app_code VARCHAR(80) NOT NULL, 
	external_project_id VARCHAR(120) NOT NULL, 
	external_project_name VARCHAR(190) NOT NULL, 
	created_at DATETIME NOT NULL, 
	last_seen_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_fk_project_binding_app_project UNIQUE (app_code, external_project_id), 
	FOREIGN KEY(project_id) REFERENCES fresh_key_projects (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_fresh_key_project_bindings_app_code ON fresh_key_project_bindings (app_code);

CREATE INDEX IF NOT EXISTS ix_fresh_key_project_bindings_external_project_id ON fresh_key_project_bindings (external_project_id);

CREATE INDEX IF NOT EXISTS ix_fresh_key_project_bindings_project_id ON fresh_key_project_bindings (project_id);

CREATE TABLE IF NOT EXISTS fresh_key_oauth_credentials (
	id VARCHAR(36) NOT NULL, 
	provider VARCHAR(40) NOT NULL, 
	authenticated_by_identity_id VARCHAR(36) NOT NULL, 
	provider_subject VARCHAR(255) NOT NULL, 
	provider_name VARCHAR(190) NOT NULL, 
	access_token_encrypted LONGTEXT NOT NULL, 
	refresh_token_encrypted LONGTEXT NOT NULL, 
	expires_at DATETIME, 
	scopes TEXT NOT NULL, 
	metadata_json LONGTEXT NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(authenticated_by_identity_id) REFERENCES fresh_key_identities (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_fresh_key_oauth_credentials_authenticated_by_identity_id ON fresh_key_oauth_credentials (authenticated_by_identity_id);

CREATE INDEX IF NOT EXISTS ix_fresh_key_oauth_credentials_provider ON fresh_key_oauth_credentials (provider);

CREATE INDEX IF NOT EXISTS ix_fresh_key_oauth_credentials_provider_subject ON fresh_key_oauth_credentials (provider_subject);

CREATE INDEX IF NOT EXISTS ix_fresh_key_oauth_credentials_status ON fresh_key_oauth_credentials (status);

CREATE TABLE IF NOT EXISTS fresh_key_assets (
	id VARCHAR(36) NOT NULL, 
	provider VARCHAR(40) NOT NULL, 
	asset_type VARCHAR(60) NOT NULL, 
	external_id VARCHAR(255) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	metadata_json LONGTEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_fk_asset_provider_type_external UNIQUE (provider, asset_type, external_id)
);

CREATE INDEX IF NOT EXISTS ix_fresh_key_assets_asset_type ON fresh_key_assets (asset_type);

CREATE INDEX IF NOT EXISTS ix_fresh_key_assets_external_id ON fresh_key_assets (external_id);

CREATE INDEX IF NOT EXISTS ix_fresh_key_assets_provider ON fresh_key_assets (provider);

CREATE TABLE IF NOT EXISTS fresh_key_asset_authorizations (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	credential_id VARCHAR(36) NOT NULL, 
	asset_id VARCHAR(36) NOT NULL, 
	access_token_encrypted LONGTEXT NOT NULL, 
	scopes TEXT NOT NULL, 
	metadata_json LONGTEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_fk_asset_auth_credential_asset UNIQUE (credential_id, asset_id), 
	FOREIGN KEY(credential_id) REFERENCES fresh_key_oauth_credentials (id) ON DELETE CASCADE, 
	FOREIGN KEY(asset_id) REFERENCES fresh_key_assets (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_fresh_key_asset_authorizations_asset_id ON fresh_key_asset_authorizations (asset_id);

CREATE INDEX IF NOT EXISTS ix_fresh_key_asset_authorizations_credential_id ON fresh_key_asset_authorizations (credential_id);

CREATE TABLE IF NOT EXISTS fresh_key_project_connections (
	id VARCHAR(36) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	asset_id VARCHAR(36) NOT NULL, 
	credential_id VARCHAR(36) NOT NULL, 
	asset_authorization_id INTEGER, 
	connected_by_identity_id VARCHAR(36) NOT NULL, 
	created_via VARCHAR(80) NOT NULL, 
	capabilities_json LONGTEXT NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	last_error TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_fk_project_connection_asset UNIQUE (project_id, asset_id), 
	FOREIGN KEY(project_id) REFERENCES fresh_key_projects (id) ON DELETE CASCADE, 
	FOREIGN KEY(asset_id) REFERENCES fresh_key_assets (id) ON DELETE CASCADE, 
	FOREIGN KEY(credential_id) REFERENCES fresh_key_oauth_credentials (id) ON DELETE RESTRICT, 
	FOREIGN KEY(asset_authorization_id) REFERENCES fresh_key_asset_authorizations (id) ON DELETE SET NULL, 
	FOREIGN KEY(connected_by_identity_id) REFERENCES fresh_key_identities (id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_fresh_key_project_connections_asset_id ON fresh_key_project_connections (asset_id);

CREATE INDEX IF NOT EXISTS ix_fresh_key_project_connections_connected_by_identity_id ON fresh_key_project_connections (connected_by_identity_id);

CREATE INDEX IF NOT EXISTS ix_fresh_key_project_connections_credential_id ON fresh_key_project_connections (credential_id);

CREATE INDEX IF NOT EXISTS ix_fresh_key_project_connections_project_id ON fresh_key_project_connections (project_id);

CREATE INDEX IF NOT EXISTS ix_fresh_key_project_connections_status ON fresh_key_project_connections (status);

CREATE TABLE IF NOT EXISTS fresh_key_oauth_transactions (
	id VARCHAR(36) NOT NULL, 
	state_hash VARCHAR(64) NOT NULL, 
	identity_id VARCHAR(36) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	provider VARCHAR(40) NOT NULL, 
	app_code VARCHAR(80) NOT NULL, 
	return_url VARCHAR(1200) NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	credential_id VARCHAR(36), 
	discovered_asset_ids_json LONGTEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	expires_at DATETIME NOT NULL, 
	completed_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(identity_id) REFERENCES fresh_key_identities (id) ON DELETE CASCADE, 
	FOREIGN KEY(project_id) REFERENCES fresh_key_projects (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_fresh_key_oauth_transactions_identity_id ON fresh_key_oauth_transactions (identity_id);

CREATE INDEX IF NOT EXISTS ix_fresh_key_oauth_transactions_project_id ON fresh_key_oauth_transactions (project_id);

CREATE INDEX IF NOT EXISTS ix_fresh_key_oauth_transactions_provider ON fresh_key_oauth_transactions (provider);

CREATE UNIQUE INDEX IF NOT EXISTS ix_fresh_key_oauth_transactions_state_hash ON fresh_key_oauth_transactions (state_hash);

CREATE INDEX IF NOT EXISTS ix_fresh_key_oauth_transactions_status ON fresh_key_oauth_transactions (status);

CREATE TABLE IF NOT EXISTS fresh_key_sso_codes (
	id VARCHAR(36) NOT NULL, 
	code_hash VARCHAR(64) NOT NULL, 
	identity_id VARCHAR(36) NOT NULL, 
	app_code VARCHAR(80) NOT NULL, 
	expires_at DATETIME NOT NULL, 
	used_at DATETIME, 
	binding_token_hash VARCHAR(64), 
	binding_expires_at DATETIME, 
	binding_used_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(identity_id) REFERENCES fresh_key_identities (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_fresh_key_sso_codes_app_code ON fresh_key_sso_codes (app_code);

CREATE UNIQUE INDEX IF NOT EXISTS ix_fresh_key_sso_codes_binding_token_hash ON fresh_key_sso_codes (binding_token_hash);

CREATE UNIQUE INDEX IF NOT EXISTS ix_fresh_key_sso_codes_code_hash ON fresh_key_sso_codes (code_hash);

CREATE INDEX IF NOT EXISTS ix_fresh_key_sso_codes_identity_id ON fresh_key_sso_codes (identity_id);
