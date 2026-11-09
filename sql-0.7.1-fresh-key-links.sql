-- Fresh AI Engine 0.7.1 / Fresh Key Propojení
-- Additive DDL only. Existing Fresh Key project/link rows remain untouched.

CREATE TABLE IF NOT EXISTS fresh_key_local_projects (
    id VARCHAR(36) NOT NULL,
    app_code VARCHAR(80) NOT NULL,
    external_project_id VARCHAR(120) NOT NULL,
    external_project_name VARCHAR(190) NOT NULL,
    website_url VARCHAR(500) NOT NULL,
    organization_name VARCHAR(190) NOT NULL,
    active BOOL NOT NULL,
    created_at DATETIME NOT NULL,
    last_seen_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_fk_local_project_app_project UNIQUE (app_code, external_project_id)
);

CREATE INDEX IF NOT EXISTS ix_fresh_key_local_projects_app_code ON fresh_key_local_projects (app_code);
CREATE INDEX IF NOT EXISTS ix_fresh_key_local_projects_external_project_id ON fresh_key_local_projects (external_project_id);
CREATE INDEX IF NOT EXISTS ix_fresh_key_local_projects_active ON fresh_key_local_projects (active);

CREATE TABLE IF NOT EXISTS fresh_key_local_project_access (
    id INTEGER NOT NULL AUTO_INCREMENT,
    local_project_id VARCHAR(36) NOT NULL,
    identity_id VARCHAR(36) NOT NULL,
    `role` VARCHAR(40) NOT NULL,
    created_at DATETIME NOT NULL,
    last_seen_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_fk_local_project_identity UNIQUE (local_project_id, identity_id),
    FOREIGN KEY(local_project_id) REFERENCES fresh_key_local_projects (id) ON DELETE CASCADE,
    FOREIGN KEY(identity_id) REFERENCES fresh_key_identities (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_fresh_key_local_project_access_local_project_id ON fresh_key_local_project_access (local_project_id);
CREATE INDEX IF NOT EXISTS ix_fresh_key_local_project_access_identity_id ON fresh_key_local_project_access (identity_id);
