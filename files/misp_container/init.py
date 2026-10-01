"""Per-pod preparation: tmp directories, app/Config rendering, GPG key import.

Every entrypoint (configure, web, worker) calls prepare() at start. It renders
every file of app/Config from the image defaults, settings.yaml and env: an
emptyDir per pod in Kubernetes, a volume the containers share in Compose.
app/files ships in the image; only app/files/{scripts/tmp,certs,terms,img/orgs}
are volumes.
"""

import os
import shutil
import subprocess
from pathlib import Path

from . import MISP_BASE, CONFIG_DEFAULTS
from .env import env
from .log import get as getlog

log = getlog("prepare")

MISP_CONFIG = f"{MISP_BASE}/app/Config"
MISP_TMP = f"{MISP_BASE}/app/tmp"
GNUPG_KEY_FILE = "/etc/misp-docker/gnupg/private.asc"
CERTS_DIR = "/etc/misp-docker/certs"
MISP_CERTS = f"{MISP_BASE}/app/files/certs"

# shutil.copy2 copies extended attributes, including the SELinux label of the
# source, which other containers cannot read. shutil.copy copies mode bits only.
_copy = shutil.copy

# MISP's CakeLog writes app/tmp/logs/debug.log and error.log by default. In a
# pod those files reach no log collector and grow until the volume fills.
# This engine writes each entry to stderr instead, as JSON or text after
# LOG_FORMAT. PHP cannot open /dev/stderr when stderr is a pipe; php://stderr
# writes to the descriptor directly.
LOG_BLOCK_START = "// -- misp-container logging: rendered on every start by misp_container/init.py --"
LOG_BLOCK_END = "// -- end misp-container logging --"
LOG_WRITERS = {
    "json": """$t = microtime(true);
            $line = json_encode(array(
                'time' => gmdate('Y-m-d\\TH:i:s', (int)$t) . sprintf('.%03dZ', ($t - floor($t)) * 1000),
                'level' => (string)$type,
                'context' => 'misp',
                'message' => (string)$message,
            ), JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_INVALID_UTF8_SUBSTITUTE);""",
    "text": """$line = date('Y-m-d H:i:s') . ' ' . str_pad(strtoupper((string)$type), 5) . ' [misp] ' . $message;""",
}
LOG_BLOCK = """
{start}
App::uses('BaseLog', 'Log/Engine');
if (!class_exists('ContainerLog', false)) {{
    class ContainerLog extends BaseLog
    {{
        public function write($type, $message)
        {{
            {writer}
            return file_put_contents('php://stderr', $line . "\\n") !== false;
        }}
    }}
}}
CakeLog::drop('debug');
CakeLog::drop('error');
CakeLog::config('container', array('engine' => 'ContainerLog'));
// A console shell adds its own stdout and stderr streams unless streams with
// those names exist, and each entry would be written twice. These take a type
// that never occurs.
CakeLog::config('stdout', array('engine' => 'ContainerLog', 'types' => array('none')));
CakeLog::config('stderr', array('engine' => 'ContainerLog', 'types' => array('none')));
{end}
"""


def render_log_block(content: str, fmt: str) -> str:
    """bootstrap.php with the logging block for fmt (json or text), replacing an earlier one."""
    start = content.find(LOG_BLOCK_START)
    if start != -1:
        end = content.find(LOG_BLOCK_END, start)
        if end != -1:
            content = content[:start].rstrip("\n") + "\n" + content[end + len(LOG_BLOCK_END):].lstrip("\n")
    block = LOG_BLOCK.format(start=LOG_BLOCK_START, end=LOG_BLOCK_END,
                             writer=LOG_WRITERS.get(fmt, LOG_WRITERS["text"]))
    return content.rstrip("\n") + "\n" + block


def prepare():
    """Everything a pod needs on disk before MISP runs."""
    setup_tmp()
    prepare_config()
    populate_gnupg()
    populate_certs()


def setup_tmp():
    """Create required tmp directory structure."""
    log.info("creating tmp directory structure")
    for d in ("cache", "cache/models", "cache/persistent", "cache/views", "logs"):
        Path(MISP_TMP, d).mkdir(parents=True, exist_ok=True)


def prepare_config():
    """Render app/Config from the image defaults, settings.yaml and env vars."""
    log.info("rendering app/Config")
    defaults = Path(env("MISP_CONFIG_DEFAULTS", CONFIG_DEFAULTS))
    config_dst = Path(MISP_CONFIG)
    config_dst.mkdir(parents=True, exist_ok=True)

    # From this image's MISP on every start: a Compose config volume outlives an
    # upgrade. Pods that share the volume start at once, so each file is replaced whole.
    for name, source in (("core.php", "core.default.php"), ("routes.php", "routes.php")):
        src = defaults / source
        if src.exists():
            _replace(config_dst / name, src.read_text())
    src = defaults / "bootstrap.default.php"
    if src.exists():
        from .log import log_format
        _replace(config_dst / "bootstrap.php", render_log_block(src.read_text(), log_format()))
        log.info("  core.php, routes.php and bootstrap.php from this image, logging to stderr (%s)", log_format())

    # Rendered on every start: env is the source of truth for these
    log.info("  config.php from settings.yaml")
    _generate_config_php(config_dst)
    log.info("  database.php from env")
    _generate_database_config(config_dst)
    log.info("  email.php from env")
    _generate_email_config(config_dst)
    log.info("app/Config ready")


def _replace(path: Path, text: str) -> None:
    """Write a file whole: a reader sees the old content or the new, never part of it."""
    partial = path.with_name(f".{path.name}.{os.getpid()}")
    partial.write_text(text)
    os.replace(partial, path)


def populate_gnupg():
    """Import the instance GPG key from the mounted Secret, if one is present.

    The key is an armoured secret key export at GNUPG_KEY_FILE. Every pod
    (configure, web, worker) imports the same key, so all replicas sign with it.
    """
    key_file = Path(env("GNUPG_KEY_FILE", GNUPG_KEY_FILE))
    if not key_file.is_file():
        return

    gpg_dir = Path(env("GNUPG_HOMEDIR"))
    gpg_dir.mkdir(parents=True, exist_ok=True)
    try:
        gpg_dir.chmod(0o700)
    except PermissionError:
        pass  # K8s emptyDir: mount point chmod not allowed, permissions are fine
    gpg = [env("GNUPG_BINARY", "gpg"), "--batch", "--homedir", str(gpg_dir)]

    # A homedir that outlives the pod (a Compose volume) holds the key of an
    # earlier start; a rotated Secret is imported next to it
    wanted = _fingerprints(gpg + ["--show-keys", "--with-colons", str(key_file)])
    present = _fingerprints(gpg + ["--list-secret-keys", "--with-colons"]) if (gpg_dir / "trustdb.gpg").exists() else set()
    if wanted and wanted <= present:
        log.info("GPG homedir holds the key of the Secret, skipping key import")
        return

    log.info("importing GPG key from %s", key_file)
    subprocess.run(gpg + ["--import", str(key_file)], check=True)

    # gpg trusts nothing it imports; MISP needs the instance key at ultimate trust
    fingerprints = _fingerprints(gpg + ["--list-secret-keys", "--with-colons"])
    if fingerprints:
        trust = "".join(f"{fpr}:6:\n" for fpr in sorted(fingerprints))
        subprocess.run(gpg + ["--import-ownertrust"], input=trust, text=True, check=True)
    log.info("GPG key imported (%d fingerprint(s))", len(fingerprints))


def _fingerprints(command: list[str]) -> set[str]:
    """The primary key fingerprints a gpg listing (--with-colons) prints."""
    listing = subprocess.run(command, capture_output=True, text=True, check=True).stdout
    found = set()
    primary = False
    for line in listing.splitlines():
        kind = line.split(":")[0]
        if kind in ("pub", "sec"):
            primary = True
        elif kind in ("sub", "ssb"):
            primary = False
        elif kind == "fpr" and primary:
            found.add(line.split(":")[9])
    return found


def populate_certs():
    """Copy sync server certificates from the mounted Secret into app/files/certs.

    MISP stores a server's certificate as app/files/certs/<server id>.pem. The
    directory is a per-pod volume, so the Secret (misp-certs) carries them to
    every pod. Files uploaded through the UI on one replica stay on that replica.
    """
    src = Path(env("MISP_CERTS_SOURCE", CERTS_DIR))
    if not src.is_dir():
        return
    dst = Path(env("MISP_CERTS_DIR", MISP_CERTS))
    dst.mkdir(parents=True, exist_ok=True)
    count = 0
    for item in sorted(src.iterdir()):
        if item.is_file():
            _copy(item, dst / item.name)
            count += 1
    log.info("copied %d server certificate(s) from %s", count, src)


def check_writable(path, purpose):
    """Exit when a directory that MISP must write to is not writable."""
    probe = Path(path) / ".write-check"
    try:
        probe.write_text("")
        probe.unlink()
    except OSError as e:
        log.error("%s directory %s is not writable: %s", purpose, path, e)
        log.error("mount a volume there, or set PLUGIN_S3_BUCKET_NAME to use S3 for attachments")
        raise SystemExit(1)


def _generate_config_php(config_dst):
    """Render config.php from settings.yaml (bootstrap groups) and env vars.

    Regenerated on every start in every pod, so all replicas and workers share
    the same bootstrap and BLOCKED settings (redis, supervisor, salt, paths).
    """
    from .config import load_settings_yaml, config_php_specs, render_config_php

    specs = config_php_specs(load_settings_yaml())
    _replace(config_dst / "config.php", render_config_php(specs))


def _generate_database_config(config_dst):
    """Generate database.php from environment variables, for MySQL/MariaDB or PostgreSQL."""
    from .config import php_literal
    from . import db

    postgres = db.is_postgres()
    fields = {
        "datasource": "Database/PostgresObserverExtended" if postgres else "Database/MysqlObserverExtended",
        "persistent": False,
        "host": env("DB_HOST"),
        "login": env("DB_USER"),
        "port": _int_env("DB_PORT", 5432 if postgres else 3306),
        "password": env("DB_PASSWORD"),
        "database": env("DB_NAME"),
    }
    if postgres:
        fields["schema"] = env("DB_SCHEMA", "public")
    fields["prefix"] = ""
    fields["encoding"] = "utf8" if postgres else "utf8mb4 COLLATE utf8mb4_unicode_ci"
    if not postgres and env("DB_TLS") == "true":
        for key, env_key in (("ssl_ca", "MYSQL_TLS_CA"), ("ssl_cert", "MYSQL_TLS_CERT"), ("ssl_key", "MYSQL_TLS_KEY")):
            if env(env_key) and os.path.isfile(env(env_key)):
                fields[key] = env(env_key)

    lines = [f"        '{key}' => {php_literal(value)}," for key, value in fields.items()]
    lines.append("        'flags' => array(PDO::ATTR_STRINGIFY_FETCHES => true),")
    content = "<?php\nclass DATABASE_CONFIG {\n    public $default = array(\n" + "\n".join(lines) + "\n    );\n}\n"
    _replace(config_dst / "database.php", content)


def _generate_email_config(config_dst):
    """Generate email.php from environment variables."""
    from .config import php_literal

    email = php_literal(env("MISP_EMAIL", env("ADMIN_EMAIL")))
    smtp = php_literal(env("SMTP_FQDN"))
    port = _int_env("SMTP_PORT", 25)
    content = f"""<?php
class EmailConfig {{
    public $default = array(
        'transport'     => 'Smtp',
        'from'          => array({email} => 'MISP'),
        'host'          => {smtp},
        'port'          => {port},
        'timeout'       => 30,
        'client'        => null,
        'log'           => false,
    );
    public $smtp = array(
        'transport'     => 'Smtp',
        'from'          => array({email} => 'MISP'),
        'host'          => {smtp},
        'port'          => {port},
        'timeout'       => 30,
        'client'        => null,
        'log'           => false,
    );
}}
"""
    _replace(config_dst / "email.php", content)


def _int_env(key, default):
    """An integer env var, or the default when unset or not numeric."""
    value = env(key)
    return int(value) if value.isdigit() else default
