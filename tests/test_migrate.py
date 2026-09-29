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


class TestSql:
    def test_insert_quotes_per_engine(self):
        assert migrate.insert_sql("t", ["a", "b"], db.MYSQL) == "INSERT INTO `t` (`a`, `b`) VALUES (%s, %s)"
        assert migrate.insert_sql("t", ["a"], db.POSTGRES) == 'INSERT INTO "t" ("a") VALUES (%s)'

    def test_mysql8_collations_rewritten_for_mariadb(self):
        ddl = "CREATE TABLE `t` (`a` varchar(1) COLLATE utf8mb4_0900_ai_ci) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"
        out = migrate.rewrite_ddl(ddl)
        assert "0900" not in out and "utf8mb4_unicode_ci" in out


class TestIdentity:
    def test_differing_salt_is_a_problem(self):
        problems = migrate.check_identity({"Security.salt": "a" * 32, "MISP.uuid": "u"},
                                          {"SECURITY_SALT": "b" * 32, "MISP_UUID": "u"})
        assert problems == ["Security.salt on the source differs from SECURITY_SALT in this deployment"]

    def test_missing_on_source_only_warns(self):
        assert migrate.check_identity({}, {"SECURITY_SALT": "x"}) == []

    def test_equal_values_pass(self):
        assert migrate.check_identity({"MISP.uuid": "u"}, {"MISP_UUID": "u"}) == []


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

    def executemany(self, sql, rows):
        self.calls.append((sql, list(rows)))

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
        n = migrate.copy_table(_Conn(read), _Conn(write), "t", ["id", "flag", "name"], db.POSTGRES,
                               {"id": "integer", "flag": "boolean", "name": "text"})
        assert n == 3
        assert read.sql == "SELECT `id`, `flag`, `name` FROM `t`"
        assert [len(rows) for _, rows in write.calls] == [2, 1]
        assert write.calls[0][1] == [(1, True, "x"), (2, False, "y")]
        assert write.calls[1][1] == [(3, None, "z")]

    def test_same_engine_rows_untouched(self):
        read = _ReadCursor([(1, 1)])
        write = _WriteCursor()
        migrate.copy_table(_Conn(read), _Conn(write), "t", ["id", "flag"], db.MYSQL)
        assert write.calls[0][1] == [(1, 1)]


class TestCrossEngineGate:
    def test_schema_mismatch_exits_5(self, monkeypatch):
        monkeypatch.setattr(db, "init_schema", lambda: None)
        monkeypatch.setattr(migrate, "source_schema_state", lambda c: ("120", set()))
        monkeypatch.setattr(migrate, "target_schema_state", lambda c, e: ("159", {"m1"}))
        with pytest.raises(SystemExit) as exc:
            migrate.migrate_cross_engine(object(), object())
        assert exc.value.code == migrate.EXIT_SCHEMA


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
