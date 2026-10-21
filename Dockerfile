# syntax=docker/dockerfile:1
#
# Non-root MISP 2.5 Docker image
#
# Build targets:
#   final    PHP-FPM, workers, configure, org sync, metrics (one image, entrypoint per role)
#   caddy    static files + FastCGI reverse proxy (scratch)
#   modules  misp-modules (distroless)
# Build through compose: podman compose build
#

# MISP 3.0 Early Access: the 3.0_wip branch, pinned to a commit (DEVELOPING.md, MISP 3.0)
ARG CORE_TAG=3.0_wip
ARG CORE_COMMIT=8888b98919f3d4788be0b7dd2c652f1e85281029
ARG PHP_VER=20240924
# The runtime packages of the final image. php-base, which the build stages
# start from, installs the same set, so composer and the extension builds see
# the extensions MISP runs with.
ARG RUNTIME_PACKAGES="ca-certificates tini gettext procps openssl gpg gpg-agent \
    php8.4 php8.4-apcu php8.4-curl php8.4-xml php8.4-intl php8.4-bcmath php8.4-mbstring \
    php8.4-mysql php8.4-pgsql php8.4-redis php8.4-gd php8.4-fpm php8.4-zip php8.4-ldap \
    libmagic1 libldap-common librdkafka1 libbrotli1 libzstd1 ssdeep libfuzzy2 \
    unzip zip curl uuid-runtime jq python3-minimal libpython3.13-stdlib"

# =============================================================================
# Stage 1: php-base - Common runtime packages
# =============================================================================
# debian:trixie-20261020-slim
FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad AS php-base
ENV DEBIAN_FRONTEND=noninteractive
ARG RUNTIME_PACKAGES

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && apt-get update && \
    apt-get install -y --no-install-recommends $RUNTIME_PACKAGES \
    && apt-get autoremove -y

# =============================================================================
# Stage 2: composer - PHP dependencies, pinned by files/composer.lock
# =============================================================================
# composer-prep downloads upstream composer.json and adds our extra packages.
# composer-lock resolves it (scripts/update-composer-lock.sh writes the result
# to files/composer.lock). composer-build installs exactly the lock; an
# out-of-date lock fails the build.
FROM php-base AS composer-prep
ARG CORE_TAG
ARG CORE_COMMIT
ENV COMPOSER_ALLOW_SUPERUSER=1

WORKDIR /tmp
RUN curl -fsSL -o /tmp/composer.json https://raw.githubusercontent.com/MISP/MISP/${CORE_COMMIT:-${CORE_TAG}}/app/composer.json

# composer:2.9.8
COPY --from=composer:2@sha256:1364b5b9132ab4c42ea3be53e894572c32fe75a512cb3b1c3903fcc9bce53dcc /usr/bin/composer /usr/bin/composer
RUN composer config --no-interaction allow-plugins.composer/installers true && \
    composer require --no-update --no-interaction \
        elasticsearch/elasticsearch:8.19.0 \
        jakub-onderka/openid-connect-php:1.5.0 \
        certmichelin/openid-connect-php:1.3.0 \
        aws/aws-sdk-php:3.398.1

FROM composer-prep AS composer-lock
RUN --mount=type=cache,target=/root/.composer/cache \
    composer update --no-install --no-interaction --with-all-dependencies

FROM composer-prep AS composer-build
COPY files/composer.lock /tmp/composer.lock
# The composer download cache survives across builds (buildah and BuildKit)
RUN --mount=type=cache,target=/root/.composer/cache \
    composer validate --no-check-all --no-check-publish --no-interaction && \
    composer install --no-interaction

# =============================================================================
# Stage 3: php-build - Native PHP extensions, built from pinned sources
# =============================================================================
FROM php-base AS php-build
ARG PHP_VER
ENV TZ=Etc/UTC

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ git make php8.4-dev \
        libbrotli-dev libfuzzy-dev librdkafka-dev libzstd-dev \
    && apt-get autoremove -y

RUN update-alternatives --set php /usr/bin/php8.4 && \
    update-alternatives --set php-config /usr/bin/php-config8.4 && \
    update-alternatives --set phpize /usr/bin/phpize8.4

# Each extension builds from its GitHub repository at a pinned commit, so the
# build reaches no host but GitHub:
#   rdkafka   arnaud-lb/php-rdkafka         6.0.5
#   zstd      kjdev/php-ext-zstd            0.18.0, its zstd library a submodule
#   brotli    kjdev/php-ext-brotli          0.21.0, its brotli library a submodule
#   simdjson  JakubOnderka/simdjson_php     the fork with the encoder MISP's JsonTool calls
#   ssdeep    JakubOnderka/pecl-text-ssdeep
RUN cp "/usr/lib/$(gcc -dumpmachine)"/libfuzzy.* /usr/lib && \
    build() ( \
        mkdir "/tmp/$1" && cd "/tmp/$1" && git init -q && \
        git fetch -q --depth 1 "https://github.com/$2.git" "$3" && git checkout -q FETCH_HEAD && \
        git submodule -q update --init --recursive --depth 1 && \
        phpize && ./configure && make -j"$(nproc)" && make install \
    ) && \
    build rdkafka arnaud-lb/php-rdkafka 2df0c74170d95d027e6773d3a9afb1fab5917558 && \
    build zstd kjdev/php-ext-zstd c2593a4ce2457b23e7fa7f81ddf0dd9bbdd89b47 && \
    build brotli kjdev/php-ext-brotli ced23f5b6f52ef58a3a96d4731db4bee40a82736 && \
    build simdjson JakubOnderka/simdjson_php ab23d7e5f0edd23b49cf1385f52dc6e300a25fd4 && \
    build ssdeep JakubOnderka/pecl-text-ssdeep aa7ea7045a294548aedc3ccdfbb3936e1716bebd && \
    tar -czf /php-extensions.tar.gz \
        /usr/lib/php/${PHP_VER}/ssdeep.so \
        /usr/lib/php/${PHP_VER}/rdkafka.so \
        /usr/lib/php/${PHP_VER}/brotli.so \
        /usr/lib/php/${PHP_VER}/simdjson.so \
        /usr/lib/php/${PHP_VER}/zstd.so

# =============================================================================
# Stage 4: misp-source - Clone MISP, set permissions
# =============================================================================
# debian:trixie-20261020-slim
FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad AS misp-source
ARG CORE_TAG
ARG CORE_COMMIT
ARG MISP_UID=1000
ARG MISP_GID=1000

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && apt-get update && apt-get install -y --no-install-recommends git ca-certificates

# Initialise only the submodules the image ships: PyMISP and the STIX helpers
# under app/files/scripts are removed below, so they are never fetched. No
# submodule is recursive: only misp-iconify nests any, the sources of its icon
# exports, over SSH URLs.
RUN if [ -n "${CORE_COMMIT}" ]; then \
        mkdir -p /var/www/MISP && cd /var/www/MISP && git init -q && \
        git fetch -q --depth 1 https://github.com/MISP/MISP.git "${CORE_COMMIT}" && git checkout -q FETCH_HEAD; \
    else \
        git clone --branch "${CORE_TAG}" --depth 1 https://github.com/MISP/MISP.git /var/www/MISP; \
    fi && \
    cd /var/www/MISP && \
    git config --file .gitmodules --get-regexp path | awk '{print $2}' \
        | grep -v -E '^(PyMISP|app/files/scripts/(cti-python-stix2|misp-stix|mixbox|python-cybox|python-maec|python-stix))$' \
        | xargs git submodule update --init --depth 1 --

# CakePHP's Postgres::describe() reuses the $seq match of an earlier column
# for every column with a NULL default, so the sequence map points each
# column of a table at the id sequence and an INSERT into a table keyed on
# a varchar (system_settings) fails in setval(). Reset the match per column.
# The build fails, naming the file, when upstream changes the loop.
# Reported upstream: https://github.com/MISP/MISP/issues/11172
RUN CAKE_PG=/var/www/MISP/app/Lib/cakephp/lib/Cake/Model/Datasource/Database/Postgres.php && \
    T="$(printf '\t')" && \
    if ! grep -q "^${T}${T}${T}foreach (\$cols as \$c) {\$" "$CAKE_PG"; then \
        echo "$CAKE_PG changed upstream: the describe() loop that the \$seq reset patches is gone. Check whether CakePHP fixed the bug, then delete or adapt this RUN" >&2; exit 1; \
    fi && \
    sed -i "s|^\(${T}${T}${T}\)foreach (\$cols as \$c) {\$|\1foreach (\$cols as \$c) {\n\1${T}\$seq = null;|" "$CAKE_PG" && \
    if ! grep -q "^${T}${T}${T}${T}\$seq = null;\$" "$CAKE_PG"; then \
        echo "$CAKE_PG: the \$seq reset did not apply" >&2; exit 1; \
    fi

# Clean and set permissions - all in one layer
RUN find /var/www/MISP/INSTALL/* ! -name 'MYSQL.sql' ! -name 'MARIADB.sql' ! -name 'POSTGRESQL.sql' -type f -exec rm {} + && \
    find /var/www/MISP/INSTALL/* ! -name 'MYSQL.sql' ! -name 'MARIADB.sql' ! -name 'POSTGRESQL.sql' -type l -exec rm {} + && \
    find /var/www/MISP/.git/* ! -name HEAD -exec rm -rf {} + 2>/dev/null || true && \
    rm -rf /var/www/MISP/PyMISP \
           /var/www/MISP/app/files/scripts/cti-python-stix2 \
           /var/www/MISP/app/files/scripts/misp-stix \
           /var/www/MISP/app/files/scripts/mixbox \
           /var/www/MISP/app/files/scripts/python-cybox \
           /var/www/MISP/app/files/scripts/python-maec \
           /var/www/MISP/app/files/scripts/python-stix && \
    # The configure step records this marker and the pods wait for it; the since
    # gates of settings.yaml compare its leading numbers
    echo "${CORE_TAG}${CORE_COMMIT:++${CORE_COMMIT}}" > /tmp/misp-dist-version && \
    # app/Config is a per-pod volume rendered at start from these defaults
    mkdir -p /srv/misp-config && \
    cp /var/www/MISP/app/Config/core.default.php /var/www/MISP/app/Config/bootstrap.default.php \
       /var/www/MISP/app/Config/routes.php /srv/misp-config/ && \
    rm -rf /var/www/MISP/app/Config/* && \
    chown -R ${MISP_UID}:${MISP_GID} /srv/misp-config /tmp/misp-dist-version && \
    # Now set restrictive permissions for the runtime image
    find /var/www/MISP -type f -exec chmod 0440 {} + && \
    find /var/www/MISP -type d -exec chmod 0550 {} + && \
    chmod +x /var/www/MISP/app/Console/cake && \
    touch /var/www/MISP/.git/ORIG_HEAD && chmod 0660 /var/www/MISP/.git/ORIG_HEAD && \
    chown -R ${MISP_UID}:${MISP_GID} /var/www/MISP

# =============================================================================
# Stage 5: uv - Python package installer (pinned)
# =============================================================================
# uv 0.11.14
FROM ghcr.io/astral-sh/uv:latest@sha256:b46b03ddfcfbf8f547af7e9eaefdf8a39c8cebcba7c98858d3162bd28cf536f6 AS uv

# =============================================================================
# Stage 6: final - Runtime image (non-root)
# =============================================================================
# A fresh FROM, not php-base: the strip below runs in the same layer as the
# install, so what it removes (perl from adduser and debconf, gconv, the systemd
# libraries, the Python test suite, docs and man pages) never enters a layer.
# Deleted in a later layer on top of php-base, those files would still be pulled.
# debian:trixie-20261020-slim
FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad AS final
ENV DEBIAN_FRONTEND=noninteractive

ARG RUNTIME_PACKAGES
ARG MISP_UID=1000
ARG MISP_GID=1000

# The upgrade takes the security pocket's fixes for the base image's packages
# (perl-base and the like, which no stage removes): the scan job fails a release
# on a fixable CRITICAL finding
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && apt-get update && \
    apt-get upgrade -y --no-install-recommends && \
    apt-get install -y --no-install-recommends $RUNTIME_PACKAGES \
    && apt-get autoremove -y \
    && rm -rf /root/.cache \
              /usr/lib/*/gconv \
              /usr/lib/*/perl \
              /usr/lib/*/perl-base \
              /usr/lib/*/libperl* \
              /usr/lib/*/systemd \
              /usr/lib/python3.*/test \
              /usr/lib/python3.*/unittest \
              /usr/share/doc \
              /usr/share/man

# Install pinned Python packages via uv (no pip in final image). The bind
# mount keeps the uv binary out of every layer.
COPY files/requirements-final.txt /tmp/requirements.txt
RUN --mount=type=bind,from=uv,source=/uv,target=/tmp/uv \
    /tmp/uv pip install --system --break-system-packages --no-cache -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Create non-root user
RUN groupadd -g ${MISP_GID} misp && \
    useradd -u ${MISP_UID} -g ${MISP_GID} -m -s /bin/bash misp && \
    update-alternatives --set php /usr/bin/php8.4

# Install the native PHP extensions
COPY --from=php-build /php-extensions.tar.gz /
RUN tar -xzf /php-extensions.tar.gz && rm /php-extensions.tar.gz && \
    for mod in ssdeep rdkafka brotli simdjson zstd; do \
        for dir in /etc/php/*/; do \
            echo "extension=${mod}.so" > "${dir}mods-available/${mod}.ini"; \
        done; \
        phpenmod "${mod}"; \
    done && phpenmod redis && \
    # The console (workers, the configure Job, console tasks) starts a session in
    # some commands; the root is read-only, /tmp is a volume in every such pod.
    # PHP-FPM keeps its sessions in Redis (php.ini.template).
    for dir in /etc/php/*/cli/conf.d; do \
        printf 'session.save_path = "/tmp"\n' > "${dir}/99-misp-cli-sessions.ini"; \
    done

# Copy MISP source (permissions already set in misp-source stage)
COPY --from=misp-source --chown=${MISP_UID}:${MISP_GID} /var/www/MISP /var/www/MISP
COPY --from=composer-build --chown=${MISP_UID}:${MISP_GID} /tmp/composer.lock /var/www/MISP/app/composer.lock
COPY --from=composer-build --chown=${MISP_UID}:${MISP_GID} /tmp/Vendor /var/www/MISP/app/Vendor
COPY --from=composer-build --chown=${MISP_UID}:${MISP_GID} /tmp/Plugin /var/www/MISP/app/Plugin

# app/Config defaults (rendered into the per-pod Config volume at start) and the version marker
COPY --from=misp-source --chown=${MISP_UID}:${MISP_GID} /srv/misp-config /srv/misp-config
COPY --from=misp-source --chown=${MISP_UID}:${MISP_GID} /tmp/misp-dist-version /srv/misp-dist-version

# Prepare writable directories (overlaid by emptyDir volumes in K8s / named volumes in Compose).
# app/files ships in the image; MISP writes only to these five subdirectories of it.
RUN for dir in app/files/scripts/tmp app/files/certs app/files/terms app/files/img/orgs \
               app/files/img/custom app/attachments app/tmp app/tmp/cache app/tmp/cache/models \
               app/tmp/cache/persistent app/tmp/cache/views app/tmp/logs \
               app/Config .gnupg; do \
        mkdir -p /var/www/MISP/$dir && chown ${MISP_UID}:${MISP_GID} /var/www/MISP/$dir && chmod 0770 /var/www/MISP/$dir; \
    done

# Copy Python entrypoint package and scripts
COPY --chown=${MISP_UID}:${MISP_GID} files/misp_container/ /opt/misp_container/
COPY --chown=${MISP_UID}:${MISP_GID} --chmod=0550 files/entrypoint-configure.py /entrypoint-configure.py
COPY --chown=${MISP_UID}:${MISP_GID} --chmod=0550 files/entrypoint-web.py /entrypoint-web.py
COPY --chown=${MISP_UID}:${MISP_GID} --chmod=0550 files/entrypoint-worker.py /entrypoint-worker.py
COPY --chown=${MISP_UID}:${MISP_GID} --chmod=0550 files/entrypoint-sync.py /entrypoint-sync.py
COPY --chown=${MISP_UID}:${MISP_GID} --chmod=0550 files/entrypoint-metrics.py /entrypoint-metrics.py

# Config templates and settings
COPY --chown=${MISP_UID}:${MISP_GID} files/php-fpm-pool.conf.template /etc/misp-docker/php-fpm-pool.conf.template
COPY --chown=${MISP_UID}:${MISP_GID} files/php.ini.template /etc/misp-docker/php.ini.template
COPY --chown=${MISP_UID}:${MISP_GID} files/misp-config/ /etc/misp-docker/
RUN find /etc/misp-docker -type f -exec chmod 0440 {} + && \
    find /etc/misp-docker -type d -exec chmod 0550 {} +

ENV PYTHONPATH=/opt PYTHONUNBUFFERED=1

WORKDIR /var/www/MISP
USER ${MISP_UID}

EXPOSE 9002

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/entrypoint-web.py"]

# =============================================================================
# Stage 7: caddy - Static files + FastCGI reverse proxy (scratch image)
# =============================================================================
# Build with: docker build --target caddy -t misp-caddy .
# caddy:2.11.3
FROM caddy:2@sha256:cb9d71ad83182011b79355cd57692686374bd78d6fe327efe0ff8507da03ab13 AS caddy-bin
# Strip cap_net_bind_service from the binary -- we listen on 8080 (unprivileged),
# and Kubernetes securityContext allowPrivilegeEscalation:false (no_new_privs)
# blocks execve on binaries with file capabilities.
RUN setcap -r /usr/bin/caddy

FROM scratch AS caddy

# Caddy binary (capabilities stripped)
COPY --from=caddy-bin /usr/bin/caddy /usr/bin/caddy

# CA certificates for HTTPS upstream connections
COPY --from=caddy-bin /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt

# MISP static files (CSS, JS, images)
COPY --from=misp-source --chown=1000:1000 /var/www/MISP/app/webroot /var/www/MISP/app/webroot

# Caddyfile
COPY --chown=1000:1000 files/Caddyfile /etc/caddy/Caddyfile

# Writable dirs for TLS cert storage and config autosave
COPY --from=caddy-bin --chown=1000:1000 /config /config
COPY --from=caddy-bin --chown=1000:1000 /data /data

ENV XDG_CONFIG_HOME=/config
ENV XDG_DATA_HOME=/data
ENV PHP_FPM_HOST=127.0.0.1

USER 1000

EXPOSE 8080
EXPOSE 443

CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile"]

# =============================================================================
# Stage 8: modules - MISP enrichment/import/export/action modules
# =============================================================================
# Build with: docker build --target modules -t misp-modules .
# Distroless Python on Debian 13 (trixie). No shell, no package manager.
#
# Edit files/requirements-modules.txt to change the version or extras:
#   misp-modules[minimal]  (default) -- common enrichment APIs
#   misp-modules[all]      -- everything including numpy, pandas, opencv
#   misp-modules           -- core only (~89 modules, 106 MB)

FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad AS modules-build
ENV DEBIAN_FRONTEND=noninteractive

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && apt-get update && apt-get install -y --no-install-recommends \
        python3-minimal libpython3.13-stdlib python3-dev gcc g++

COPY --from=uv /uv /tmp/uv
COPY files/requirements-modules.txt /tmp/requirements.txt
RUN /tmp/uv pip install --system --break-system-packages --no-cache -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt /tmp/uv

# gcr.io/distroless/python3-debian13 (Python 3.13, nonroot UID 65532)
FROM gcr.io/distroless/python3-debian13:nonroot@sha256:614040f7f08b3f0dca943ea54eae94ea555ea2b9ca83d1acda1b7e4238ce91fb AS modules
COPY --from=modules-build /usr/local/lib/python3.13/dist-packages /usr/local/lib/python3.13/dist-packages

EXPOSE 6666

ENTRYPOINT ["python3", "-m", "misp_modules"]
# An empty address listens on IPv4 and IPv6; "::" alone is IPv6 only (Tornado
# sets IPV6_V6ONLY), "0.0.0.0" IPv4 only
CMD ["-l", ""]
