"""Init container: populate volumes from distribution tarball."""

import os
import re
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

from . import MISP_BASE, DIST_TARBALL, DIST_VERSION_FILE
from .env import env, apply_defaults
from .log import get as getlog

log = getlog("init")

MISP_FILES = f"{MISP_BASE}/app/files"
MISP_CONFIG = f"{MISP_BASE}/app/Config"
MISP_TMP = f"{MISP_BASE}/app/tmp"
GNUPG_KEY_FILE = "/etc/misp-docker/gnupg/private.asc"

# Directories where user customizations should not be overwritten
NO_CLOBBER_DIRS = {"certs", "img", "terms"}

# shutil.copy2 and shutil.copytree copy extended attributes, including the
# SELinux label of the init container's private staging directory, which other
# containers cannot read. shutil.copy copies mode bits only, and _copy_tree
# never calls copystat on directories.
_copy = shutil.copy


def _copy_tree(src, dst):
    """Copy a directory tree, overwriting files, without extended attributes."""
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            _copy_tree(item, target)
        else:
            _copy(item, target)

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
                _copy_tree(child, dest)

        # Copy top-level files
        for f in files_src.iterdir():
            if f.is_file():
                _copy(f, Path(MISP_FILES) / f.name)

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
                _copy(src, dst)

    # bootstrap.php with auth plugin patch
    bootstrap = config_dst / "bootstrap.php"
    if not bootstrap.exists() or bootstrap.stat().st_size == 0:
        log.info("  bootstrap.php from defaults (with auth plugin patch)")
        src = config_src / "bootstrap.default.php"
        if not src.exists():
            src = config_src / "bootstrap.php"
        if src.exists():
            _copy(src, bootstrap)

    _make_writable(MISP_CONFIG)

    if bootstrap.exists() and "Detect what auth modules" not in bootstrap.read_text():
        log.info("  patching bootstrap.php with auth plugin detection")
        content = bootstrap.read_text()
        for plugin in ("CakeResque", "AadAuth", "CertAuth", "LdapAuth", "LinOTPAuth", "OidcAuth", "ShibbAuth"):
            content = content.replace(f"CakePlugin::load('{plugin}');", "")
        content = re.sub(r"CakePlugin::loadAll\(array\(.*?CakeResque.*?\)\);", "", content, flags=re.DOTALL)
        content += AUTH_PLUGIN_PATCH
        bootstrap.write_text(content)

    # config.php from settings.yaml + env, every start: env is the source of
    # truth for bootstrap settings, and each pod has its own Config volume.
    log.info("  config.php from settings.yaml")
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


def populate_gnupg():
    """Import the instance GPG key from the mounted Secret, if one is present.

    The key is an armoured secret key export at GNUPG_KEY_FILE. Every pod
    (web, worker, scheduler) imports the same key, so all replicas sign with it.
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


def _generate_database_config(config_dst):
    """Generate database.php from environment variables."""
    from .config import php_literal

    content = f"""<?php
class DATABASE_CONFIG {{
    public $default = array(
        'datasource' => 'Database/Mysql',
        'persistent' => false,
        'host' => {php_literal(env("MYSQL_HOST"))},
        'login' => {php_literal(env("MYSQL_USER"))},
        'port' => {_int_env("MYSQL_PORT", 3306)},
        'password' => {php_literal(env("MYSQL_PASSWORD"))},
        'database' => {php_literal(env("MYSQL_DATABASE"))},
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


def _generate_config_php(config_dst):
    """Render config.php from settings.yaml (bootstrap groups) and env vars.

    Regenerated on every start in every pod, so all replicas and workers share
    the same bootstrap and BLOCKED settings (redis, supervisor, salt, paths).
    """
    from .config import load_settings_yaml, config_php_specs, render_config_php

    specs = config_php_specs(load_settings_yaml())
    (config_dst / "config.php").write_text(render_config_php(specs))


def _int_env(key, default):
    """An integer env var, or the default when unset or not numeric."""
    value = env(key)
    return int(value) if value.isdigit() else default


def _copy_no_clobber(src, dst):
    """Copy files from src to dst without overwriting existing files."""
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _copy_no_clobber(item, target)
        elif not target.exists():
            _copy(item, target)


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
