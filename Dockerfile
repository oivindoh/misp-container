# syntax=docker/dockerfile:1
#
# Non-root MISP 2.5 Docker image
#
# Build targets:
#   docker build --target final -t misp .          # PHP-FPM + workers
#   docker build --target caddy -t misp-caddy .    # Static files + reverse proxy (~64MB)
#

ARG DOCKER_HUB_PROXY=""
ARG CORE_TAG=v2.5.39
ARG CORE_COMMIT
ARG PHP_VER=20240924

# =============================================================================
# Stage 1: php-base - Common runtime packages
# =============================================================================
# debian:trixie-20260505-slim
FROM "${DOCKER_HUB_PROXY}debian:trixie-slim@sha256:109e2c65005bf160609e4ba6acf7783752f8502ad218e298253428690b9eaa4b" AS php-base
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
        gettext \
        procps \
        openssl \
        gpg \
        gpg-agent \
        mariadb-client \
        php8.4 \
        php8.4-apcu \
        php8.4-curl \
        php8.4-xml \
        php8.4-intl \
        php8.4-bcmath \
        php8.4-mbstring \
        php8.4-mysql \
        php8.4-redis \
        php8.4-gd \
        php8.4-fpm \
        php8.4-zip \
        php8.4-ldap \
        libmagic1 \
        libldap-common \
        librdkafka1 \
        libbrotli1 \
        libsimdjson25 \
        libzstd1 \
        ssdeep \
        libfuzzy2 \
        unzip \
        zip \
        curl \
        uuid-runtime \
        jq \
        python3-minimal \
    && apt-get autoremove -y && apt-get clean -y && rm -rf /var/lib/apt/lists/*

# =============================================================================
# Stage 2: composer-build - PHP dependencies via Composer
# =============================================================================
FROM php-base AS composer-build
ARG CORE_TAG
ARG CORE_COMMIT
ENV COMPOSER_ALLOW_SUPERUSER=1

WORKDIR /tmp
RUN curl -o /tmp/composer.json https://raw.githubusercontent.com/MISP/MISP/${CORE_COMMIT:-${CORE_TAG}}/app/composer.json
RUN sed -i '/cake-resque/d' /tmp/composer.json && \
    sed -i 's/authentication",/authentication"/' /tmp/composer.json

# composer:2.9.8
COPY --from=composer:2@sha256:1364b5b9132ab4c42ea3be53e894572c32fe75a512cb3b1c3903fcc9bce53dcc /usr/bin/composer /usr/bin/composer
RUN composer config --no-interaction allow-plugins.composer/installers true && \
    composer install && \
    composer require --with-all-dependencies --no-interaction \
        elasticsearch/elasticsearch:^8.7.0 \
        jakub-onderka/openid-connect-php:^1.0.0 \
        certmichelin/openid-connect-php:1.3.0 \
        aws/aws-sdk-php

# =============================================================================
# Stage 3: php-build - Native PHP PECL extensions
# =============================================================================
FROM php-base AS php-build
ARG PHP_VER
ENV TZ=Etc/UTC

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ git make php8.4-dev php-pear \
        libbrotli-dev libfuzzy-dev librdkafka-dev libsimdjson-dev libzstd-dev \
    && apt-get autoremove -y && apt-get clean -y && rm -rf /var/lib/apt/lists/*

RUN update-alternatives --set php /usr/bin/php8.4 && \
    update-alternatives --set php-config /usr/bin/php-config8.4 && \
    update-alternatives --set phpize /usr/bin/phpize8.4

RUN pecl channel-update pecl.php.net && \
    cp "/usr/lib/$(gcc -dumpmachine)"/libfuzzy.* /usr/lib && \
    pecl install rdkafka && \
    pecl install simdjson && \
    pecl install zstd && \
    pecl install brotli && \
    git clone --recursive --depth=1 https://github.com/JakubOnderka/pecl-text-ssdeep.git /tmp/pecl-text-ssdeep && \
    cd /tmp/pecl-text-ssdeep && phpize && ./configure && make && make install && \
    tar -czf /pecl_libs.tar.gz \
        /usr/lib/php/${PHP_VER}/ssdeep.so \
        /usr/lib/php/${PHP_VER}/rdkafka.so \
        /usr/lib/php/${PHP_VER}/brotli.so \
        /usr/lib/php/${PHP_VER}/simdjson.so \
        /usr/lib/php/${PHP_VER}/zstd.so

# =============================================================================
# Stage 4: misp-source - Clone MISP, set permissions, create dist tarball
# =============================================================================
# debian:trixie-20260505-slim
FROM debian:trixie-slim@sha256:b6e2a152f22a40ff69d92cb397223c906017e1391a73c952b588e51af8883bf8 AS misp-source
ARG CORE_TAG
ARG CORE_COMMIT
ARG MISP_UID=1000
ARG MISP_GID=1000

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && apt-get clean -y && rm -rf /var/lib/apt/lists/*

RUN if [ -n "${CORE_COMMIT}" ]; then \
        git clone https://github.com/MISP/MISP.git /var/www/MISP && cd /var/www/MISP && git checkout "${CORE_COMMIT}"; \
    else \
        git clone --branch "${CORE_TAG}" --depth 1 https://github.com/MISP/MISP.git /var/www/MISP; \
    fi && \
    cd /var/www/MISP && git submodule update --init --recursive .

# Clean, set permissions, and create dist tarball - all in one layer
RUN find /var/www/MISP/INSTALL/* ! -name 'MYSQL.sql' -type f -exec rm {} + && \
    find /var/www/MISP/INSTALL/* ! -name 'MYSQL.sql' -type l -exec rm {} + && \
    find /var/www/MISP/.git/* ! -name HEAD -exec rm -rf {} + 2>/dev/null || true && \
    rm -rf /var/www/MISP/PyMISP \
           /var/www/MISP/app/files/scripts/cti-python-stix2 \
           /var/www/MISP/app/files/scripts/misp-stix \
           /var/www/MISP/app/files/scripts/mixbox \
           /var/www/MISP/app/files/scripts/python-cybox \
           /var/www/MISP/app/files/scripts/python-maec \
           /var/www/MISP/app/files/scripts/python-stix && \
    # Create dist tarball BEFORE setting restrictive permissions,
    # so extracted files have normal 0644/0755 perms and can be freely managed.
    echo "${CORE_COMMIT:-${CORE_TAG}}" > /tmp/misp-dist-version && \
    tar czf /srv/misp-dist.tar.gz -C /var/www/MISP/app files Config && \
    chown ${MISP_UID}:${MISP_GID} /srv/misp-dist.tar.gz /tmp/misp-dist-version && \
    # Strip app/files/ and app/Config/ from the image - the init container populates
    # these from the tarball at runtime. Saves ~191 MB from the image layer.
    rm -rf /var/www/MISP/app/files/* /var/www/MISP/app/Config/* && \
    # Now set restrictive permissions for the runtime image
    find /var/www/MISP -type f -exec chmod 0440 {} + && \
    find /var/www/MISP -type d -exec chmod 0550 {} + && \
    chmod +x /var/www/MISP/app/Console/cake && \
    touch /var/www/MISP/.git/ORIG_HEAD && chmod 0660 /var/www/MISP/.git/ORIG_HEAD && \
    chown -R ${MISP_UID}:${MISP_GID} /var/www/MISP

# =============================================================================
# Stage 5: uv - Python package installer (pinned, used by final + sync stages)
# =============================================================================
# uv 0.11.14
FROM ghcr.io/astral-sh/uv:latest@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d AS uv

# =============================================================================
# Stage 6: final - Runtime image (non-root)
# =============================================================================
# Separate FROM (not php-base) so the final image carries only runtime deps.
# php-base includes perl, gconv, video codecs etc. pulled in during apt install
# that are only needed by build stages (phpize, adduser). Starting fresh and
# installing only runtime packages saves ~75 MB.
# debian:trixie-20260505-slim
FROM "${DOCKER_HUB_PROXY}debian:trixie-slim@sha256:109e2c65005bf160609e4ba6acf7783752f8502ad218e298253428690b9eaa4b" AS final
ENV DEBIAN_FRONTEND=noninteractive

ARG CORE_TAG
ARG CORE_COMMIT
ARG PHP_VER
ARG MISP_UID=1000
ARG MISP_GID=1000

# Install runtime packages only, then strip transitive deps not needed at runtime:
# - perl: pulled in by adduser/debconf, only needed during apt install
# - gconv: libc6 charset converters, not needed by PHP/MISP
# - systemd libs, python test suite, docs/man pages
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
        gettext \
        procps \
        openssl \
        gpg \
        gpg-agent \
        php8.4 \
        php8.4-apcu \
        php8.4-curl \
        php8.4-xml \
        php8.4-intl \
        php8.4-bcmath \
        php8.4-mbstring \
        php8.4-mysql \
        php8.4-redis \
        php8.4-gd \
        php8.4-fpm \
        php8.4-zip \
        php8.4-ldap \
        libmagic1 \
        libldap-common \
        librdkafka1 \
        libbrotli1 \
        libsimdjson25 \
        libzstd1 \
        ssdeep \
        libfuzzy2 \
        unzip \
        zip \
        curl \
        uuid-runtime \
        jq \
        python3-minimal \
        libpython3.13-stdlib \
    && apt-get autoremove -y && apt-get clean -y \
    && rm -rf /var/lib/apt/lists/* /root/.cache \
              /usr/lib/*/gconv \
              /usr/lib/*/perl \
              /usr/lib/*/perl-base \
              /usr/lib/*/libperl* \
              /usr/lib/*/systemd \
              /usr/lib/python3.*/test \
              /usr/lib/python3.*/unittest \
              /usr/share/doc \
              /usr/share/man

# Install pinned Python packages via uv (no pip in final image)
COPY --from=uv /uv /tmp/uv
COPY files/requirements-final.txt /tmp/requirements.txt
RUN /tmp/uv pip install --system --break-system-packages --no-cache -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt /tmp/uv

# Create non-root user
RUN groupadd -g ${MISP_GID} misp && \
    useradd -u ${MISP_UID} -g ${MISP_GID} -m -s /bin/bash misp && \
    update-alternatives --set php /usr/bin/php8.4 && \
    mkdir -p /run/php && chown ${MISP_UID}:${MISP_GID} /run/php

# Install PHP PECL extensions
COPY --from=php-build /pecl_libs.tar.gz /
RUN tar -xzf /pecl_libs.tar.gz && rm /pecl_libs.tar.gz && \
    for mod in ssdeep rdkafka brotli simdjson zstd; do \
        for dir in /etc/php/*/; do \
            echo "extension=${mod}.so" > "${dir}mods-available/${mod}.ini"; \
        done; \
        phpenmod "${mod}"; \
    done && phpenmod redis

# Copy MISP source (permissions already set in misp-source stage)
COPY --from=misp-source --chown=${MISP_UID}:${MISP_GID} /var/www/MISP /var/www/MISP
COPY --from=composer-build --chown=${MISP_UID}:${MISP_GID} /tmp/composer.lock /var/www/MISP/app/composer.lock
COPY --from=composer-build --chown=${MISP_UID}:${MISP_GID} /tmp/Vendor /var/www/MISP/app/Vendor
COPY --from=composer-build --chown=${MISP_UID}:${MISP_GID} /tmp/Plugin /var/www/MISP/app/Plugin

# Copy the compressed dist tarball (used by init container to populate volumes)
# This replaces the old .src directory copy (~63MB tarball vs ~191MB directory duplication)
COPY --from=misp-source --chown=${MISP_UID}:${MISP_GID} /srv/misp-dist.tar.gz /srv/misp-dist.tar.gz
COPY --from=misp-source --chown=${MISP_UID}:${MISP_GID} /tmp/misp-dist-version /srv/misp-dist-version

# Prepare writable directories (overlaid by emptyDir volumes in K8s / named volumes in Compose)
RUN for dir in app/files app/attachments app/tmp app/tmp/cache app/tmp/cache/models \
               app/tmp/cache/persistent app/tmp/cache/views app/tmp/logs \
               app/Config app/webroot/img/orgs app/webroot/img/custom .gnupg; do \
        mkdir -p /var/www/MISP/$dir && chown ${MISP_UID}:${MISP_GID} /var/www/MISP/$dir && chmod 0770 /var/www/MISP/$dir; \
    done

# Copy Python entrypoint package and scripts
COPY --chown=${MISP_UID}:${MISP_GID} files/misp_container/ /opt/misp_container/
COPY --chown=${MISP_UID}:${MISP_GID} --chmod=0550 files/entrypoint-init.py /entrypoint-init.py
COPY --chown=${MISP_UID}:${MISP_GID} --chmod=0550 files/entrypoint-web.py /entrypoint-web.py
COPY --chown=${MISP_UID}:${MISP_GID} --chmod=0550 files/entrypoint-worker.py /entrypoint-worker.py

# Config templates and settings
COPY --chown=${MISP_UID}:${MISP_GID} files/php-fpm-pool.conf.template /etc/misp-docker/php-fpm-pool.conf.template
COPY --chown=${MISP_UID}:${MISP_GID} files/php.ini.template /etc/misp-docker/php.ini.template
COPY --chown=${MISP_UID}:${MISP_GID} files/misp-config/settings.yaml /etc/misp-docker/settings.yaml
RUN find /etc/misp-docker -type f -exec chmod 0440 {} + && \
    find /etc/misp-docker -type d -exec chmod 0550 {} +

ENV PYTHONPATH=/opt PYTHONUNBUFFERED=1

WORKDIR /var/www/MISP
USER ${MISP_UID}

EXPOSE 9002

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/entrypoint-web.py"]

# =============================================================================
# Stage 6: caddy - Static files + FastCGI reverse proxy (scratch image)
# =============================================================================
# Build with: docker build --target caddy -t misp-caddy .
# caddy:2.11.3
FROM caddy:2@sha256:ec18ee54aab3315c22e25f3b2babda73ff8007d39b13b3bd1bfffa2f0444c7d9 AS caddy-bin
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
# Stage 7: sync - Lightweight org/team sync tool
# =============================================================================
# Build with: docker build --target sync -t misp-sync .
# Only Python + pyyaml + our sync code. No PHP, no MISP source.
# debian:trixie-20260505-slim
FROM "${DOCKER_HUB_PROXY}debian:trixie-slim@sha256:109e2c65005bf160609e4ba6acf7783752f8502ad218e298253428690b9eaa4b" AS sync
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-minimal libpython3.13-stdlib ca-certificates tini \
    && apt-get autoremove -y && apt-get clean -y && rm -rf /var/lib/apt/lists/* \
              /usr/share/doc /usr/share/man

COPY --from=uv /uv /tmp/uv
COPY files/requirements-sync.txt /tmp/requirements.txt
RUN /tmp/uv pip install --system --break-system-packages --no-cache -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt /tmp/uv

ARG MISP_UID=1000
ARG MISP_GID=1000

RUN groupadd -g ${MISP_GID} misp && \
    useradd -u ${MISP_UID} -g ${MISP_GID} -s /bin/false misp

COPY --chown=${MISP_UID}:${MISP_GID} files/misp_container/ /opt/misp_container/
COPY --chown=${MISP_UID}:${MISP_GID} --chmod=0550 files/entrypoint-sync.py /entrypoint-sync.py

ENV PYTHONPATH=/opt PYTHONUNBUFFERED=1

USER ${MISP_UID}

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/entrypoint-sync.py"]

# =============================================================================
# Stage 8: metrics - Prometheus metrics exporter
# =============================================================================
# Build with: docker build --target metrics -t misp-metrics .
# Lightweight HTTP server exposing /metrics on port 9191.
# debian:trixie-20260505-slim
FROM "${DOCKER_HUB_PROXY}debian:trixie-slim@sha256:109e2c65005bf160609e4ba6acf7783752f8502ad218e298253428690b9eaa4b" AS metrics
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-minimal libpython3.13-stdlib ca-certificates tini \
    && apt-get autoremove -y && apt-get clean -y && rm -rf /var/lib/apt/lists/* \
              /usr/share/doc /usr/share/man

COPY --from=uv /uv /tmp/uv
COPY files/requirements-metrics.txt /tmp/requirements.txt
RUN /tmp/uv pip install --system --break-system-packages --no-cache -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt /tmp/uv

ARG MISP_UID=1000
ARG MISP_GID=1000

RUN groupadd -g ${MISP_GID} misp && \
    useradd -u ${MISP_UID} -g ${MISP_GID} -s /bin/false misp

COPY --chown=${MISP_UID}:${MISP_GID} files/misp_container/ /opt/misp_container/
COPY --chown=${MISP_UID}:${MISP_GID} --chmod=0550 files/entrypoint-metrics.py /entrypoint-metrics.py

ENV PYTHONPATH=/opt PYTHONUNBUFFERED=1

EXPOSE 9191

USER ${MISP_UID}

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/entrypoint-metrics.py"]

# =============================================================================
# Stage 9: modules - MISP enrichment/import/export/action modules
# =============================================================================
# Build with: docker build --target modules -t misp-modules .
# Distroless Python on Debian 13 (trixie). No shell, no package manager.
#
# Edit files/requirements-modules.txt to change the version or extras:
#   misp-modules[minimal]==3.0.7  (default) -- common enrichment APIs
#   misp-modules[all]==3.0.7      -- everything including numpy, pandas, opencv
#   misp-modules==3.0.7           -- core only (~89 modules, 106 MB)

FROM "${DOCKER_HUB_PROXY}debian:trixie-slim@sha256:109e2c65005bf160609e4ba6acf7783752f8502ad218e298253428690b9eaa4b" AS modules-build
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-minimal libpython3.13-stdlib python3-dev gcc g++ \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /tmp/uv
COPY files/requirements-modules.txt /tmp/requirements.txt
RUN /tmp/uv pip install --system --break-system-packages --no-cache -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt /tmp/uv

# gcr.io/distroless/python3-debian13 (Python 3.13, nonroot UID 65532)
FROM gcr.io/distroless/python3-debian13:nonroot@sha256:614040f7f08b3f0dca943ea54eae94ea555ea2b9ca83d1acda1b7e4238ce91fb AS modules
COPY --from=modules-build /usr/local/lib/python3.13/dist-packages /usr/local/lib/python3.13/dist-packages

EXPOSE 6666

ENTRYPOINT ["python3", "-m", "misp_modules"]
CMD ["-l", "0.0.0.0"]
