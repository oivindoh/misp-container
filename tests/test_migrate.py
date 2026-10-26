"""Unit tests for the migration Job (misp_container.migrate)."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container import db, migrate


class TestConversion:
    def test_boolean_from_tinyint_and_strings(self):
        fn = migrate.coercer("boolean")
        assert fn(1) is True and fn(0) is False and fn(None) is None
        assert fn("1") is True and fn("0") is False and fn(b"1") is True

    def test_zero_date_becomes_null(self):
        fn = migrate.coercer("timestamp without time zone")
        assert fn("0000-00-00 00:00:00") is None
        assert fn("2026-01-01 00:00:00") == "2026-01-01 00:00:00"

    def test_bytea_from_bytearray(self):
        assert migrate.coercer("bytea")(bytearray(b"x")) == b"x"

    def test_other_types_pass_through(self):
        assert migrate.coercer("integer") is None
        assert migrate.coerce_row((1, "a"), None) == (1, "a")
        assert migrate.coerce_row((1, "a"), [migrate.to_bool, None]) == (True, "a")


class TestIdentity:
    def test_differing_salt_is_a_problem(self):
        problems = migrate.check_identity({"Security.salt": "a" * 32, "MISP.uuid": "u"},
                                          {"SECURITY_SALT": "b" * 32, "MISP_UUID": "u"})
        assert problems == ["Security.salt on the source differs from SECURITY_SALT in this deployment"]

    def test_missing_on_source_only_warns(self):
        assert migrate.check_identity({}, {"SECURITY_SALT": "x"}) == []

    def test_equal_values_pass(self):
        assert migrate.check_identity({"MISP.uuid": "u"}, {"MISP_UUID": "u"}) == []


class TestTargetPlan:
    COPIED = "2026-09-29 12:00:00"

    def test_an_empty_target(self):
        assert migrate.target_plan(0, "", "", "") == ("copy", "")

    def test_a_fresh_install_needs_force(self):
        plan, why = migrate.target_plan(80, "", "", "")
        assert plan == "refuse" and "set MIGRATE_FORCE=true" in why
        assert migrate.target_plan(80, "", "true", "") == ("copy", "")

    def test_a_copy_the_job_made_stays(self):
        # A rerun, a configure retry or a MIGRATE_FORCE=true left behind keeps the copy
        for force in ("", "true"):
            plan, why = migrate.target_plan(80, self.COPIED, force, "")
            assert plan == "keep" and f"copy this Job made at {self.COPIED}" in why and "MIGRATE_REPLACE_COPY" in why

    def test_a_copy_the_job_made_needs_both_to_go(self):
        assert migrate.target_plan(80, self.COPIED, "true", "true") == ("copy", "")
        assert migrate.target_plan(80, self.COPIED, "", "true")[0] == "keep"

    def test_the_copy_date_comes_from_the_run_record(self):
        with patch.object(migrate.db, "run_records", return_value={"migrate": {"success": 1, "last_success": 0}}):
            assert migrate.last_copy(["events", "system_settings"]) == ""
        with patch.object(migrate.db, "run_records", return_value={"migrate": {"last_success": 1759147200}}):
            assert migrate.last_copy(["events", "system_settings"]) == "2025-09-29 12:00:00 UTC"
        assert migrate.last_copy(["events"]) == ""


class FakeBucket(migrate.Bucket):
    """A bucket held in a dict: key -> bytes."""

    def __init__(self, name, objects=None):
        super().__init__(name=name, access_key="a", secret_key="s")
        self.objects = dict(objects or {})

    def keys(self, prefix=""):
        return iter(sorted(k for k in self.objects if k.startswith(prefix)))

    def download(self, key, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.objects[key])
        return len(self.objects[key])

    def upload(self, key, path):
        self.objects[key] = path.read_bytes()


def tree(root, files):
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return root


class TestAttachmentKey:
    @pytest.mark.parametrize("rel,key", [
        ("12/34", "12/34"),
        ("12/34_thumb", "12/34_thumb"),
        ("bucket_1000/1234/5", "1234/5"),
        ("shadow/12/34", "shadow/12/34"),
        ("shadow/bucket_0/12/34", "shadow/12/34"),
        ("taxonomies/tlp/machinetag.json", None),
        ("12", None),
        ("bucket_0/notanevent/1", None),
    ])
    def test_key(self, rel, key):
        assert migrate.attachment_key(rel) == key


class TestCopyAttachments:
    SOURCE = {"12/34": b"flat", "bucket_0/56/7": b"bucketed", "shadow/12/35": b"proposal",
              "taxonomies/x": b"ships in the image"}
    KEYS = {"12/34": b"flat", "56/7": b"bucketed", "shadow/12/35": b"proposal"}

    def test_directory_to_volume(self, tmp_path):
        target = tmp_path / "att"
        assert migrate.copy_attachments(tree(tmp_path / "src", self.SOURCE), target) == 3
        found = {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()}
        assert found == self.KEYS

    def test_directory_to_bucket(self, tmp_path):
        bucket = FakeBucket("target")
        assert migrate.copy_attachments(tree(tmp_path / "src", self.SOURCE), bucket) == 3
        assert bucket.objects == self.KEYS

    def test_bucket_to_volume(self, tmp_path):
        target = tmp_path / "att"
        assert migrate.copy_attachments(FakeBucket("source", self.KEYS), target) == 3
        assert (target / "shadow/12/35").read_bytes() == b"proposal"

    def test_bucket_to_bucket(self, tmp_path, monkeypatch):
        monkeypatch.setattr(migrate.tempfile, "gettempdir", lambda: str(tmp_path))
        target = FakeBucket("target")
        assert migrate.copy_attachments(FakeBucket("source", {**self.KEYS, "notes.txt": b"x"}), target) == 3
        assert target.objects == self.KEYS
        assert not (tmp_path / "migrate-attachment").exists()

    def test_org_logos_and_custom_images_go_to_the_volume(self, tmp_path):
        source = tree(tmp_path / "src", {"img/orgs/7.png": b"logo", "img/custom/top.png": b"banner",
                                         "img/other.png": b"ships in the image", **self.SOURCE})
        target = tmp_path / "att"
        assert migrate.copy_images(source, target) == 2
        assert (target / "img/orgs/7.png").read_bytes() == b"logo"
        assert (target / "img/custom/top.png").read_bytes() == b"banner"


class TestAttachmentConfig:
    S3 = {"PLUGIN_S3_BUCKET_NAME": "misp", "PLUGIN_S3_AWS_ENDPOINT": "http://garage:3900/",
          "PLUGIN_S3_REGION": "garage", "PLUGIN_S3_AWS_ACCESS_KEY": "GK1", "PLUGIN_S3_AWS_SECRET_KEY": "s"}

    def env(self, monkeypatch, values):
        for key in [*self.S3, "MIGRATE_SOURCE_FILES", "MIGRATE_SOURCE_S3_BUCKET", "MIGRATE_SOURCE_S3_ACCESS_KEY",
                    "MIGRATE_SOURCE_S3_SECRET_KEY", "PLUGIN_S3_AWS_COMPATIBLE", "PLUGIN_S3_AWS_VALIDATE_CA"]:
            monkeypatch.delenv(key, raising=False)
        for key, value in values.items():
            monkeypatch.setenv(key, value)

    def test_target_is_the_volume_without_a_bucket(self, monkeypatch, tmp_path):
        self.env(monkeypatch, {"MISP_ATTACHMENTS_DIR": str(tmp_path)})
        assert migrate.attachment_target() == tmp_path

    def test_target_is_the_deployments_bucket(self, monkeypatch):
        self.env(monkeypatch, self.S3)
        bucket = migrate.attachment_target()
        assert (bucket.name, bucket.endpoint, bucket.region, bucket.verify) == ("misp", "http://garage:3900", "garage", True)

    def test_aws_target_ignores_the_endpoint(self, monkeypatch):
        self.env(monkeypatch, {**self.S3, "PLUGIN_S3_AWS_COMPATIBLE": "false", "PLUGIN_S3_REGION": ""})
        bucket = migrate.attachment_target()
        assert (bucket.endpoint, bucket.region) == ("", "eu-west-1")

    def test_a_bucket_needs_keys(self, monkeypatch):
        self.env(monkeypatch, {"PLUGIN_S3_BUCKET_NAME": "misp"})
        with pytest.raises(migrate.ConfigError, match="PLUGIN_S3_AWS_ACCESS_KEY and PLUGIN_S3_AWS_SECRET_KEY"):
            migrate.attachment_target()

    def test_one_source_only(self, monkeypatch, tmp_path):
        self.env(monkeypatch, {"MIGRATE_SOURCE_FILES": str(tmp_path), "MIGRATE_SOURCE_S3_BUCKET": "old",
                               "MIGRATE_SOURCE_S3_ACCESS_KEY": "a", "MIGRATE_SOURCE_S3_SECRET_KEY": "s"})
        with pytest.raises(migrate.ConfigError, match="not both"):
            migrate.attachment_source()

    def test_a_missing_source_directory(self, monkeypatch, tmp_path):
        self.env(monkeypatch, {"MIGRATE_SOURCE_FILES": str(tmp_path / "nope")})
        with pytest.raises(migrate.ConfigError, match="no directory"):
            migrate.attachment_source()

    def test_no_source(self, monkeypatch):
        self.env(monkeypatch, {})
        assert migrate.attachment_source() is None

    def test_validate_ca_off(self, monkeypatch):
        self.env(monkeypatch, {**self.S3, "PLUGIN_S3_AWS_VALIDATE_CA": "false"})
        assert migrate.attachment_target().verify is False


class _ReadCursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def execute(self, sql):
        self.sql = sql

    def fetchmany(self, n):
        out, self.rows = self.rows[:n], self.rows[n:]
        return out

    def close(self):
        pass


class _WriteCursor:
    def __init__(self):
        self.calls = []
        self.statements = []

    def executemany(self, sql, rows):
        self.calls.append((sql, list(rows)))

    def execute(self, sql, params):
        self.statements.append((sql, list(params)))

    def close(self):
        pass


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, cls=None):
        return self._cursor


class TestCopyTable:
    def test_batches_and_coerces(self, monkeypatch):
        monkeypatch.setattr(migrate, "BATCH", 2)
        read = _ReadCursor([(1, 1, "x"), (2, 0, "y"), (3, None, "z")])
        write = _WriteCursor()
        n = migrate.copy_table(_Conn(read), _Conn(write), "t", ["id", "flag", "name"],
                               {"id": "integer", "flag": "boolean", "name": "text"})
        assert n == 3
        assert read.sql == "SELECT `id`, `flag`, `name` FROM `t`"
        assert write.statements == [
            ('INSERT INTO "t" ("id", "flag", "name") VALUES (%s, %s, %s), (%s, %s, %s)', [1, True, "x", 2, False, "y"]),
            ('INSERT INTO "t" ("id", "flag", "name") VALUES (%s, %s, %s)', [3, None, "z"]),
        ]

    def test_postgres_statements_stay_under_the_parameter_limit(self, monkeypatch):
        monkeypatch.setattr(migrate, "PG_MAX_PARAMS", 5)
        write = _WriteCursor()
        migrate.insert_rows(write, "t", ["a", "b"], [(1, 2), (3, 4), (5, 6)])
        assert [len(params) for _, params in write.statements] == [4, 2]

    def test_the_run_records_of_a_source_stay_behind(self):
        read = _ReadCursor([])
        migrate.copy_table(_Conn(read), _Conn(_WriteCursor()), "system_settings", ["setting", "value"],
                           {"setting": "character varying", "value": "bytea"})
        assert read.sql.endswith("FROM `system_settings` WHERE setting NOT LIKE 'misp_docker.runs.%'")


class TestIndexes:
    def test_drops_the_secondary_indexes_copies_and_rebuilds_them(self, monkeypatch):
        steps = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, sql, params=None):
                steps.append(sql.split("(")[0].strip() if sql.startswith("CREATE") else sql[:40])

            def fetchall(self):
                return [("attributes_value1", "CREATE INDEX attributes_value1 ON public.attributes USING btree (value1)")]

        monkeypatch.setattr(migrate.db, "_cursor", lambda conn: Cursor())
        monkeypatch.setattr(migrate, "copy_table", lambda *args, **kwargs: steps.append("copy") or 7)
        assert migrate.copy_without_indexes(None, None, "attributes", ["id"], {}) == 7
        assert steps[1:] == ['DROP INDEX "attributes_value1"', "copy",
                             "CREATE INDEX attributes_value1 ON public.attributes USING btree"]


SOURCE = {"host": "old", "port": 3306, "user": "u", "password": "p", "database": "misp", "tls": False}


class TestSchemaGate:
    @pytest.fixture
    def steps(self, monkeypatch):
        steps = []
        monkeypatch.setattr(db, "init_schema", lambda: steps.append("baseline"))
        monkeypatch.setattr(migrate, "setup_tmp", lambda: None)
        monkeypatch.setattr(migrate, "purge_model_cache", lambda: None)
        monkeypatch.setattr(migrate, "prepare_config", lambda: steps.append("app/Config"))
        monkeypatch.setattr(migrate.cake, "run_updates", lambda: steps.append("updates"))
        monkeypatch.setattr(migrate, "write_database_php", lambda fields: steps.append(fields["datasource"]))
        monkeypatch.setattr(migrate.cake, "run_checked", lambda *args: steps.append(" ".join(args)))
        monkeypatch.setattr(migrate, "target_tables", lambda c: [])
        monkeypatch.setattr(migrate, "source_tables", lambda c: [])
        monkeypatch.setattr(migrate, "reset_sequences", lambda c, t: None)
        return steps

    def test_schema_mismatch_exits_5(self, monkeypatch, steps):
        monkeypatch.setattr(migrate, "source_schema_state", lambda c: ("120", set()))
        monkeypatch.setattr(migrate, "target_schema_state", lambda c: ("159", {"m1"}))
        with patch.dict(os.environ, {"MIGRATE_SOURCE_UPGRADE": ""}), pytest.raises(SystemExit) as exc:
            migrate.migrate_into_postgres(object(), object(), SOURCE)
        assert exc.value.code == migrate.EXIT_SCHEMA

    def test_the_target_takes_misps_updates_before_the_check(self, monkeypatch, steps):
        # A release can ship ledger migrations its PostgreSQL baseline does not record
        monkeypatch.setattr(migrate, "source_schema_state", lambda c: steps.append("check") or ("159", {"m1", "m2"}))
        monkeypatch.setattr(migrate, "target_schema_state", lambda c: ("159", {"m1", "m2"}))
        with patch.dict(os.environ, {"MIGRATE_SOURCE_UPGRADE": ""}):
            assert migrate.migrate_into_postgres(object(), object(), SOURCE) == 0
        assert steps == ["baseline", "app/Config", "updates", "check"]

    def test_the_source_upgrade_runs_cake_on_the_source_then_points_back(self, monkeypatch, steps):
        monkeypatch.setattr(migrate, "source_schema_state", lambda c: steps.append("check") or ("159", set()))
        monkeypatch.setattr(migrate, "target_schema_state", lambda c: ("159", set()))
        with patch.dict(os.environ, {"MIGRATE_SOURCE_UPGRADE": "true", "DB_HOST": "postgres"}):
            migrate.migrate_into_postgres(object(), object(), SOURCE)
        assert steps == ["baseline", "app/Config", "updates", "Database/MysqlObserverExtended", "Admin runUpgrade",
                         "Database/PostgresObserverExtended", "check"]

    def test_a_failed_source_upgrade_still_points_cake_back(self, monkeypatch, steps):
        def fail(*args):
            raise RuntimeError("cake Admin runUpgrade exited 1")
        monkeypatch.setattr(migrate.cake, "run_checked", fail)
        with patch.dict(os.environ, {"MIGRATE_SOURCE_UPGRADE": "true"}), pytest.raises(RuntimeError):
            migrate.migrate_into_postgres(object(), object(), SOURCE)
        assert steps[-1] == "Database/PostgresObserverExtended"


class TestConfig:
    def test_missing_source_host_exits_2(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MIGRATE_SOURCE_HOST", None)
            with pytest.raises(SystemExit) as exc:
                migrate.source_settings()
        assert exc.value.code == migrate.EXIT_CONFIG

    def test_source_defaults(self):
        with patch.dict(os.environ, {"MIGRATE_SOURCE_HOST": "old", "MIGRATE_SOURCE_USER": "u",
                                     "MIGRATE_SOURCE_PASSWORD": "p"}):
            os.environ.pop("MIGRATE_SOURCE_PORT", None); os.environ.pop("MIGRATE_SOURCE_NAME", None)
            s = migrate.source_settings()
        assert s["port"] == 3306 and s["database"] == "misp" and s["tls"] is False
