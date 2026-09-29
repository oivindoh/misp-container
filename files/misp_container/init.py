"""Per-pod preparation: tmp directories, app/Config rendering, GPG key import.

Every entrypoint (configure, web, worker) calls prepare() at start. app/Config
is a per-pod volume rendered from the image defaults, settings.yaml and env.
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
    Path(MISP_BASE, "app/webroot/img/orgs").mkdir(parents=True, exist_ok=True)
    Path(MISP_BASE, "app/webroot/img/custom").mkdir(parents=True, exist_ok=True)


def prepare_config():
    """Render app/Config from the image defaults, settings.yaml and env vars."""
    log.info("rendering app/Config")
    defaults = Path(env("MISP_CONFIG_DEFAULTS", CONFIG_DEFAULTS))
    config_dst = Path(MISP_CONFIG)
    config_dst.mkdir(parents=True, exist_ok=True)

    # Static CakePHP files, once per volume
    for name, source in (("core.php", "core.default.php"), ("routes.php", "routes.php")):
        dst = config_dst / name
        if not dst.exists() or dst.stat().st_size == 0:
            src = defaults / source
            if src.exists():
                log.info("  %s from defaults", name)
                _copy(src, dst)

    bootstrap = config_dst / "bootstrap.php"
    if not bootstrap.exists() or bootstrap.stat().st_size == 0:
        src = defaults / "bootstrap.default.php"
        if src.exists():
            log.info("  bootstrap.php from defaults")
            _copy(src, bootstrap)

    if bootstrap.exists():
        from .log import log_format
        content = bootstrap.read_text()
        rendered = render_log_block(content, log_format())
        if rendered != content:
            log.info("  bootstrap.php logging to stderr (%s)", log_format())
            bootstrap.write_text(rendered)

    # Rendered on every start: env is the source of truth for these
    log.info("  config.php from settings.yaml")
    _generate_config_php(config_dst)
    log.info("  database.php from env")
    _generate_database_config(config_dst)
    log.info("  email.php from env")
    _generate_email_config(config_dst)
    log.info("app/Config ready")


def populate_gnupg():
    """Import the instance GPG key from the mounted Secret, if one is present.

    The key is an armoured secret key export at GNUPG_KEY_FILE. Every pod
    (configure, web, worker) imports the same key, so all replicas sign with it.
    """
    key_file = Path(env("GNUPG_KEY_FILE", GNUPG_KEY_FILE))
    if not key_file.is_file():
        return

    gpg_dir = Path(env("GNUPG_HOMEDIR"))
    if (gpg_dir / "trustdb.gpg").exists():
        log.info("GPG homedir already populated, skipping key import")
        return

    log.info("importing GPG key from %s", key_file)
    gpg_dir.mkdir(parents=True, exist_ok=True)
    try:
        gpg_dir.chmod(0o700)
    except PermissionError:
        pass  # K8s emptyDir: mount point chmod not allowed, permissions are fine

    gpg = [env("GNUPG_BINARY", "gpg"), "--batch", "--homedir", str(gpg_dir)]
    subprocess.run(gpg + ["--import", str(key_file)], check=True)

    # gpg trusts nothing it imports; MISP needs the instance key at ultimate trust
    listing = subprocess.run(
        gpg + ["--list-secret-keys", "--with-colons"],
        capture_output=True, text=True, check=True,
    ).stdout
    fingerprints = [line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")]
    if fingerprints:
        trust = "".join(f"{fpr}:6:\n" for fpr in fingerprints)
        subprocess.run(gpg + ["--import-ownertrust"], input=trust, text=True, check=True)
    log.info("GPG key imported (%d fingerprint(s))", len(fingerprints))


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
    (config_dst / "config.php").write_text(render_config_php(specs))


def _generate_database_config(config_dst):
    """Generate database.php from environment variables, for MySQL/MariaDB or PostgreSQL."""
    from .config import php_literal
    from . import db

    if db.is_postgres():
        content = f"""<?php
class DATABASE_CONFIG {{
    public $default = array(
        'datasource' => 'Database/PostgresObserverExtended',
        'persistent' => false,
        'host' => {php_literal(env("DB_HOST"))},
        'login' => {php_literal(env("DB_USER"))},
        'port' => {_int_env("DB_PORT", 5432)},
        'password' => {php_literal(env("DB_PASSWORD"))},
        'database' => {php_literal(env("DB_NAME"))},
        'schema' => {php_literal(env("DB_SCHEMA", "public"))},
        'prefix' => '',
        'encoding' => 'utf8',
        'flags' => array(PDO::ATTR_STRINGIFY_FETCHES => true),
    );
}}
"""
        (config_dst / "database.php").write_text(content)
        return

    content = f"""<?php
class DATABASE_CONFIG {{
    public $default = array(
        'datasource' => 'Database/MysqlObserverExtended',
        'persistent' => false,
        'host' => {php_literal(env("DB_HOST"))},
        'login' => {php_literal(env("DB_USER"))},
        'port' => {_int_env("DB_PORT", 3306)},
        'password' => {php_literal(env("DB_PASSWORD"))},
        'database' => {php_literal(env("DB_NAME"))},
        'prefix' => '',
        'encoding' => 'utf8mb4 COLLATE utf8mb4_unicode_ci',
        'flags' => array(PDO::ATTR_STRINGIFY_FETCHES => true),
    );
}}
"""
    dst = config_dst / "database.php"
    dst.write_text(content)

    if env("DB_TLS") == "true":
        lines = dst.read_text()
        for key, env_key in [("ssl_ca", "MYSQL_TLS_CA"), ("ssl_cert", "MYSQL_TLS_CERT"), ("ssl_key", "MYSQL_TLS_KEY")]:
            val = env(env_key)
            if val and os.path.isfile(val):
                lines = lines.replace(
                    "public $default = array(",
                    f"public $default = array(\n        '{key}' => {php_literal(val)},",
                    1,
                )
        dst.write_text(lines)


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
    (config_dst / "email.php").write_text(content)


def _int_env(key, default):
    """An integer env var, or the default when unset or not numeric."""
    value = env(key)
    return int(value) if value.isdigit() else default
