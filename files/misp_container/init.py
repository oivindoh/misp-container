"""Init container: populate volumes from distribution tarball."""

import os
import re
import shutil
import stat
import tarfile
from pathlib import Path

from . import MISP_BASE, DIST_TARBALL, DIST_VERSION_FILE
from .env import env, apply_defaults
from .log import get as getlog

log = getlog("init")

MISP_FILES = f"{MISP_BASE}/app/files"
MISP_CONFIG = f"{MISP_BASE}/app/Config"
MISP_TMP = f"{MISP_BASE}/app/tmp"

# Directories where user customizations should not be overwritten
NO_CLOBBER_DIRS = {"certs", "img", "terms"}

AUTH_PLUGIN_PATCH = """
/**
 * Detect what auth modules need to be loaded based on the loaded config
 */
if (Configure::read('AadAuth')) { CakePlugin::load('AadAuth'); }
if (Configure::read('CertAuth')) { CakePlugin::load('CertAuth'); }
if (Configure::read('LdapAuth')) { CakePlugin::load('LdapAuth'); }
if (Configure::read('LinOTPAuth')) { CakePlugin::load('LinOTPAuth'); }
if (Configure::read('OidcAuth')) { CakePlugin::load('OidcAuth'); }
if (Configure::read('ShibbAuth')) { CakePlugin::load('ShibbAuth'); }
"""


def populate_files():
    """Extract distribution files from tarball into the files/ volume."""
    image_version = _read_file(DIST_VERSION_FILE, "unknown")
    version_file = Path(MISP_FILES) / "VERSION"
    current_version = _read_file(str(version_file), "")

    if current_version == image_version:
        log.info("app/files/ already at version %s, skipping", image_version)
        return

    log.info("extracting distribution files (%s -> %s)", current_version or "empty", image_version)

    staging = Path("/tmp/misp-dist-staging")
    staging.mkdir(parents=True, exist_ok=True)

    with tarfile.open(DIST_TARBALL, "r:gz") as tar:
        tar.extractall(staging)

    # Ensure target is writable (Docker Compose pre-populates named volumes
    # from the image layer with restrictive permissions)
    _make_writable(MISP_FILES)

    files_src = staging / "files"
    if files_src.is_dir():
        for child in sorted(files_src.iterdir()):
            if not child.is_dir():
                continue
            dest = Path(MISP_FILES) / child.name
            if child.name in NO_CLOBBER_DIRS:
                log.info("  %s (no-clobber)", child.name)
                dest.mkdir(parents=True, exist_ok=True)
                _copy_no_clobber(child, dest)
            else:
                log.info("  %s (full sync)", child.name)
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(child, dest)

        # Copy top-level files
        for f in files_src.iterdir():
            if f.is_file():
                shutil.copy2(f, Path(MISP_FILES) / f.name)

    # Write version marker
    version_file.write_text(image_version)

    shutil.rmtree(staging, ignore_errors=True)
    log.info("app/files/ populated")


def populate_config():
    """Generate CakePHP config files from templates + env vars."""
    log.info("generating app/Config/ files")

    staging = Path("/tmp/misp-config-staging")
    staging.mkdir(parents=True, exist_ok=True)

    with tarfile.open(DIST_TARBALL, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.name.startswith("Config/")]
        tar.extractall(staging, members=members)

    config_src = staging / "Config"
    config_dst = Path(MISP_CONFIG)

    _make_writable(MISP_CONFIG)

    # Static config files
    for name, defaults in [("core.php", "core.default.php"), ("routes.php", "routes.php")]:
        dst = config_dst / name
        if not dst.exists() or dst.stat().st_size == 0:
            log.info("  %s from defaults", name)
            src = config_src / defaults
            if not src.exists():
                src = config_src / name
            if src.exists():
                shutil.copy2(src, dst)

    # bootstrap.php with auth plugin patch
    bootstrap = config_dst / "bootstrap.php"
    if not bootstrap.exists() or bootstrap.stat().st_size == 0:
        log.info("  bootstrap.php from defaults (with auth plugin patch)")
        src = config_src / "bootstrap.default.php"
        if not src.exists():
            src = config_src / "bootstrap.php"
        if src.exists():
            shutil.copy2(src, bootstrap)

    _make_writable(MISP_CONFIG)

    if bootstrap.exists() and "Detect what auth modules" not in bootstrap.read_text():
        log.info("  patching bootstrap.php with auth plugin detection")
        content = bootstrap.read_text()
        for plugin in ("CakeResque", "AadAuth", "CertAuth", "LdapAuth", "LinOTPAuth", "OidcAuth", "ShibbAuth"):
            content = content.replace(f"CakePlugin::load('{plugin}');", "")
        content = re.sub(r"CakePlugin::loadAll\(array\(.*?CakeResque.*?\)\);", "", content, flags=re.DOTALL)
        content += AUTH_PLUGIN_PATCH
        bootstrap.write_text(content)

    # config.php with bootstrap settings from env vars.
    # In K8s each pod has its own Config volume (emptyDir), so the worker
    # never sees the web entrypoint's config.php writes. This ensures all
    # pods start with working Redis/Python/MISP settings from day one.
    config_php = config_dst / "config.php"
    if not config_php.exists() or config_php.stat().st_size == 0:
        log.info("  config.php from env vars")
        _generate_config_php(config_dst)

    # database.php from env vars
    log.info("  database.php from template")
    _generate_database_config(config_dst)

    # email.php from env vars
    log.info("  email.php from template")
    _generate_email_config(config_dst)

    shutil.rmtree(staging, ignore_errors=True)
    log.info("config generation complete")


def setup_tmp():
    """Create required tmp directory structure."""
    log.info("creating tmp directory structure")
    for d in ("cache", "cache/models", "cache/persistent", "cache/views", "logs"):
        Path(MISP_TMP, d).mkdir(parents=True, exist_ok=True)
    Path(MISP_BASE, "app/webroot/img/orgs").mkdir(parents=True, exist_ok=True)
    Path(MISP_BASE, "app/webroot/img/custom").mkdir(parents=True, exist_ok=True)


def _generate_database_config(config_dst):
    """Generate database.php from environment variables."""
    content = f"""<?php
class DATABASE_CONFIG {{
    public $default = array(
        'datasource' => 'Database/Mysql',
        'persistent' => false,
        'host' => '{env("MYSQL_HOST")}',
        'login' => '{env("MYSQL_USER")}',
        'port' => {env("MYSQL_PORT")},
        'password' => '{env("MYSQL_PASSWORD")}',
        'database' => '{env("MYSQL_DATABASE")}',
        'prefix' => '',
        'encoding' => 'utf8',
    );
}}
"""
    dst = config_dst / "database.php"
    dst.write_text(content)

    if env("MYSQL_TLS") == "true":
        lines = dst.read_text()
        for key, env_key in [("ssl_ca", "MYSQL_TLS_CA"), ("ssl_cert", "MYSQL_TLS_CERT"), ("ssl_key", "MYSQL_TLS_KEY")]:
            val = env(env_key)
            if val and os.path.isfile(val):
                lines = lines.replace(
                    "public $default = array(",
                    f"public $default = array(\n        '{key}' => '{val}',",
                    1,
                )
        dst.write_text(lines)


def _generate_email_config(config_dst):
    """Generate email.php from environment variables."""
    email = env("MISP_EMAIL", env("ADMIN_EMAIL"))
    smtp = env("SMTP_FQDN")
    port = env("SMTP_PORT")
    content = f"""<?php
class EmailConfig {{
    public $default = array(
        'transport'     => 'Smtp',
        'from'          => array('{email}' => 'MISP'),
        'host'          => '{smtp}',
        'port'          => {port},
        'timeout'       => 30,
        'client'        => null,
        'log'           => false,
    );
    public $smtp = array(
        'transport'     => 'Smtp',
        'from'          => array('{email}' => 'MISP'),
        'host'          => '{smtp}',
        'port'          => {port},
        'timeout'       => 30,
        'client'        => null,
        'log'           => false,
    );
}}
"""
    (config_dst / "email.php").write_text(content)


def _generate_config_php(config_dst):
    """Generate config.php with bootstrap settings from environment variables.

    In Kubernetes each pod has its own Config emptyDir, so the worker pod
    never sees the web entrypoint's config.php writes. This ensures every
    pod starts with working Redis, Python, and supervisor settings.

    Env var names follow the auto-derived convention from settings.yaml:
      MISP.redis_host -> MISP_REDIS_HOST
    """
    import os

    def s(env_var):
        """Get env var (defaults already applied by apply_defaults)."""
        return os.environ.get(env_var, "")

    redis_host = s("MISP_REDIS_HOST")
    redis_port = s("MISP_REDIS_PORT") or "6379"
    redis_pw = s("MISP_REDIS_PASSWORD")
    sv_host = s("SIMPLEBACKGROUNDJOBS_SUPERVISOR_HOST")
    sv_user = s("SIMPLEBACKGROUNDJOBS_SUPERVISOR_USER")
    sv_pass = s("SIMPLEBACKGROUNDJOBS_SUPERVISOR_PASSWORD")
    salt = s("SECURITY_SALT")

    # Build Security section -- salt is conditional
    security_entries = [
        "'advanced_authkeys' => true",
        "'rest_client_enable_arbitrary_urls' => false",
        "'disable_local_feed_access' => false",
        "'disable_instance_file_uploads' => false",
    ]
    if salt:
        security_entries.append(f"'salt' => '{salt}'")
    security_php = ",\n        ".join(security_entries)

    content = f"""<?php
$config = array(
    'MISP' => array(
        'python_bin' => '{s("MISP_PYTHON_BIN")}',
        'redis_host' => '{redis_host}',
        'redis_port' => {redis_port},
        'redis_password' => '{redis_pw}',
        'redis_database' => 13,
        'tmpdir' => '{s("MISP_TMPDIR")}',
        'attachments_dir' => '{s("MISP_ATTACHMENTS_DIR")}',
        'background_jobs' => true,
        'self_update' => false,
        'online_version_check' => true,
        'ca_path' => '/etc/ssl/certs/ca-certificates.crt',
        'download_gpg_from_homedir' => true,
        'osuser' => 'misp',
    ),
    'GnuPG' => array(
        'binary' => '{s("GNUPG_BINARY")}',
    ),
    'SimpleBackgroundJobs' => array(
        'enabled' => true,
        'supervisor_host' => '{sv_host}',
        'supervisor_port' => 9001,
        'supervisor_user' => '{sv_user}',
        'supervisor_password' => '{sv_pass}',
        'redis_host' => '{redis_host}',
        'redis_port' => {redis_port},
        'redis_password' => '{redis_pw}',
        'redis_database' => 1,
        'redis_namespace' => 'background_jobs',
        'max_job_history_ttl' => 86400,
    ),
    'Plugin' => array(
        'ZeroMQ_redis_host' => '{redis_host}',
        'ZeroMQ_redis_port' => {redis_port},
        'ZeroMQ_redis_password' => '{redis_pw}',
        'ZeroMQ_enable' => false,
        'Enrichment_services_url' => '{s("PLUGIN_ENRICHMENT_SERVICES_URL")}',
        'Enrichment_services_port' => {s("PLUGIN_ENRICHMENT_SERVICES_PORT") or "6666"},
        'Enrichment_services_enable' => true,
        'Import_services_url' => '{s("PLUGIN_IMPORT_SERVICES_URL")}',
        'Import_services_port' => {s("PLUGIN_IMPORT_SERVICES_PORT") or "6666"},
        'Import_services_enable' => true,
        'Export_services_url' => '{s("PLUGIN_EXPORT_SERVICES_URL")}',
        'Export_services_port' => {s("PLUGIN_EXPORT_SERVICES_PORT") or "6666"},
        'Export_services_enable' => true,
        'Action_services_url' => '{s("PLUGIN_ACTION_SERVICES_URL")}',
        'Action_services_port' => {s("PLUGIN_ACTION_SERVICES_PORT") or "6666"},
        'Action_services_enable' => true,
    ),
    'Security' => array(
        {security_php},
    ),
);
"""
    (config_dst / "config.php").write_text(content)


def _copy_no_clobber(src, dst):
    """Copy files from src to dst without overwriting existing files."""
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _copy_no_clobber(item, target)
        elif not target.exists():
            shutil.copy2(item, target)


def _make_writable(path):
    """Ensure a directory tree is writable by the owner."""
    p = Path(path)
    if not p.exists():
        return
    for item in p.rglob("*"):
        try:
            item.chmod(item.stat().st_mode | stat.S_IWUSR)
        except OSError:
            pass
    try:
        p.chmod(p.stat().st_mode | stat.S_IWUSR)
    except OSError:
        pass


def _read_file(path, default=""):
    try:
        return Path(path).read_text().strip()
    except (OSError, IOError):
        return default
