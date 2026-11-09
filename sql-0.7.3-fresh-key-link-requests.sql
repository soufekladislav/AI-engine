-- Fresh AI Engine 0.7.3 / potvrzované propojení aplikací
-- Additive DDL. Neodstraňuje ani nemění existující Fresh Key data.

CREATE TABLE IF NOT EXISTS fresh_key_link_requests (
    id VARCHAR(36) NOT NULL,
    project_id VARCHAR(36) NOT NULL,
    requested_by_identity_id VARCHAR(36) NOT NULL,
    target_identity_id VARCHAR(36) NOT NULL,
    target_app_code VARCHAR(80) NOT NULL,
    request_type VARCHAR(30) NOT NULL DEFAULT 'attach_existing',
    target_local_project_id VARCHAR(36) NULL,
    requested_project_name VARCHAR(190) NOT NULL DEFAULT '',
    requested_project_url VARCHAR(500) NOT NULL DEFAULT '',
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    message TEXT NOT NULL,
    result_external_project_id VARCHAR(120) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL,
    expires_at DATETIME NULL,
    responded_at DATETIME NULL,
    responder_identity_id VARCHAR(36) NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(project_id) REFERENCES fresh_key_projects(id) ON DELETE CASCADE,
    FOREIGN KEY(requested_by_identity_id) REFERENCES fresh_key_identities(id) ON DELETE CASCADE,
    FOREIGN KEY(target_identity_id) REFERENCES fresh_key_identities(id) ON DELETE CASCADE,
    FOREIGN KEY(target_local_project_id) REFERENCES fresh_key_local_projects(id) ON DELETE SET NULL,
    FOREIGN KEY(responder_identity_id) REFERENCES fresh_key_identities(id) ON DELETE SET NULL,
    INDEX ix_fk_link_requests_project (project_id),
    INDEX ix_fk_link_requests_requester (requested_by_identity_id),
    INDEX ix_fk_link_requests_target_identity (target_identity_id),
    INDEX ix_fk_link_requests_target_app (target_app_code),
    INDEX ix_fk_link_requests_status (status),
    INDEX ix_fk_link_requests_target_local (target_local_project_id),
    INDEX ix_fk_link_requests_responder (responder_identity_id)
);

-- Per-app mirror of local project membership. Local apps stay authoritative;
-- Fresh Key uses this only to show which person collaborates in which app.
CREATE TABLE IF NOT EXISTS fresh_key_project_app_members (
    id INT NOT NULL AUTO_INCREMENT,
    project_id VARCHAR(36) NOT NULL,
    identity_id VARCHAR(36) NOT NULL,
    app_code VARCHAR(80) NOT NULL,
    local_role VARCHAR(40) NOT NULL DEFAULT 'member',
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_fk_project_app_member (project_id, identity_id, app_code),
    KEY ix_fk_project_app_member_project (project_id),
    KEY ix_fk_project_app_member_identity (identity_id),
    KEY ix_fk_project_app_member_app (app_code),
    KEY ix_fk_project_app_member_status (status),
    CONSTRAINT fk_fk_project_app_member_project FOREIGN KEY (project_id) REFERENCES fresh_key_projects(id) ON DELETE CASCADE,
    CONSTRAINT fk_fk_project_app_member_identity FOREIGN KEY (identity_id) REFERENCES fresh_key_identities(id) ON DELETE CASCADE
);
