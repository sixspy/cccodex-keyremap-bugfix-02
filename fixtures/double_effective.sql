-- 双生效并存复现数据：m-2026.11 与 m-2026.12 同时 status='生效'
-- 同旧键 OLD-A1 在两版各落一套新键（NEW-100 / NEW-101）
-- 幂等：无论库当前状态如何，强制摆出双生效现场
DELETE FROM mappings WHERE version='m-2026.12';
DELETE FROM map_versions WHERE version='m-2026.12';
UPDATE map_versions SET status='生效' WHERE version='m-2026.11';
INSERT INTO map_versions(version,status,effective_from) VALUES ('m-2026.12','生效','2026-11-15');
INSERT INTO mappings(version,old_key,new_key,kind) VALUES
  ('m-2026.12','OLD-A1','NEW-101','一对一'),
  ('m-2026.12','OLD-B1','NEW-201','多对一');
