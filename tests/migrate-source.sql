-- The content of the migration suite's MISP 2.5 source (tests/e2e/test_migration.py),
-- loaded after MISP's own INSTALL/MYSQL.sql of 2.5. The admin org, the admin email and
-- MISP.uuid are those of tests/k3s/values.yaml, so the configure step on the copy
-- finds them and adds no rows. The authkeys are the suite's ADMIN_KEY and INBOUND_KEY,
-- hashed as MISP 2.5 stores them. The suite writes the attachment's file as 1/1.

INSERT INTO organisations (id, name, date_created, date_modified, description, type, sector, created_by, uuid, local) VALUES
(1, 'Test Org', NOW(), NOW(), 'Automatically generated admin organisation', 'ADMIN', '', 0, '2399b00e-b7f4-4fdb-aeb9-03d28e83a210', 1),
(2, 'Migrate Test Org', NOW(), NOW(), 'Created by the migration suite', '', 'government', 1, '7c2f4a9e-3d5b-4e8a-9f10-6b7c8d9e0f11', 1);

INSERT INTO users (id, password, org_id, email, role_id, termsaccepted, date_created, date_modified, last_pw_change) VALUES
(1, '$2y$10$/Ep99qiLMRmUyaT3StHBXuZDHF0xhmgf/LSMWRzTzAGTyBdMwgkF.', 1, 'test-admin@example.com', 1, 1, UNIX_TIMESTAMP(), UNIX_TIMESTAMP(), UNIX_TIMESTAMP()),
(2, '$2y$10$/Ep99qiLMRmUyaT3StHBXuZDHF0xhmgf/LSMWRzTzAGTyBdMwgkF.', 2, 'migrate-user@example.com', 3, 0, UNIX_TIMESTAMP(), UNIX_TIMESTAMP(), UNIX_TIMESTAMP()),
(3, '$2y$10$/Ep99qiLMRmUyaT3StHBXuZDHF0xhmgf/LSMWRzTzAGTyBdMwgkF.', 2, 'migrate-inbound@example.com', 5, 0, UNIX_TIMESTAMP(), UNIX_TIMESTAMP(), UNIX_TIMESTAMP());

INSERT INTO auth_keys (id, uuid, authkey, authkey_start, authkey_end, created, expiration, user_id, comment) VALUES
(1, '41e208d4-e9db-4b69-b015-a92272a4e86d', '$2y$10$JGdHVA0B//EMVyTTp4d5N.JDUS2dogFAee.BpwH9CiqPdnfItALk.', 'migr', '0000', UNIX_TIMESTAMP(), 0, 1, 'admin'),
(2, '7336bd2f-9746-4dcb-b39a-ae3e22d5838a', '$2y$10$Xb1motRGVghJ2zFhNR6pRuyX.0x7hcwO9Bs/TyXklPjwPW9ClfTWe', 'migr', '0000', UNIX_TIMESTAMP(), 0, 3, 'inbound sync');

INSERT INTO tags (id, name, colour, exportable, org_id) VALUES
(1, 'migrate-test:global-tag', '#123456', 1, 0),
(2, 'migrate-test:org-tag', '#654321', 1, 2);

INSERT INTO servers (id, name, url, authkey, org_id, push, pull, remote_org_id, self_signed, pull_rules, push_rules) VALUES
(1, 'Migrate Test Server', 'https://migrate-test.example.com', 'migrateoutbound0000000000000000000000000', 1, 0, 1, 2, 0,
 '{"tags":{"OR":[],"NOT":[]},"orgs":{"OR":[],"NOT":[]},"url_params":""}',
 '{"tags":{"OR":[],"NOT":[]},"orgs":{"OR":[],"NOT":[]}}');

INSERT INTO events (id, org_id, orgc_id, user_id, date, info, uuid, analysis, attribute_count, timestamp, distribution, sharing_group_id, threat_level_id) VALUES
(1, 1, 1, 1, '2026-01-01', 'Migration suite event', '9317aac8-ade4-4825-9355-35da95c55eca', 0, 1, UNIX_TIMESTAMP(), 0, 0, 4);

INSERT INTO attributes (id, event_id, category, type, value1, value2, to_ids, uuid, timestamp, distribution, sharing_group_id, comment) VALUES
(1, 1, 'Payload delivery', 'attachment', 'migrate.txt', '', 0, 'eb444996-b14f-4c6a-b7ca-461b9946a6d4', UNIX_TIMESTAMP(), 5, 0, '');

-- MISP 2.5 keeps a setting it saves in the database as JSON
INSERT INTO system_settings (setting, value) VALUES
('MISP.uuid', '"2399b00e-b7f4-4fdb-aeb9-03d28e83a210"');
