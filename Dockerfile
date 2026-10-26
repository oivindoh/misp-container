#
# Non-root MISP 3.0 image
#
# Build targets:
#   final    PHP-FPM, caddy, workers, configure, org sync, metrics (one image, a module per role)
#   modules  misp-modules (distroless)
# Build both: mise run build (scripts/build.py)
#

# MISP 3.0 Early Access: the 3.0_wip branch, pinned to a commit (DEVELOPING.md, MISP 3.0)
ARG CORE_TAG=3.0_wip
ARG CORE_COMMIT=8888b98919f3d4788be0b7dd2c652f1e85281029
ARG PHP_VER=20240924
# The runtime packages of the final image. php-base, which the build stages
# start from, installs the same set, so composer and the extension builds see
# the extensions MISP runs with. systemd-standalone-tmpfiles satisfies
# php8.4-fpm's dependency, which would otherwise pull in systemd.
ARG RUNTIME_PACKAGES="ca-certificates tini procps openssl gpg gpg-agent \
    php8.4 php8.4-apcu php8.4-curl php8.4-xml php8.4-intl php8.4-bcmath php8.4-mbstring \
    php8.4-mysql php8.4-pgsql php8.4-redis php8.4-gd php8.4-fpm php8.4-zip php8.4-ldap php8.4-zstd \
    systemd-standalone-tmpfiles libmagic1 libldap-common librdkafka1 libbrotli1 libfuzzy2 \
    python3-minimal libpython3.13-stdlib"
# The MISP submodules the image does not ship: PyMISP and the STIX helpers, which
# requirements-misp.txt installs as packages
ARG EXCLUDED_SUBMODULES="PyMISP app/files/scripts/cti-python-stix2 app/files/scripts/misp-stix \
    app/files/scripts/mixbox app/files/scripts/python-cybox app/files/scripts/python-maec \
    app/files/scripts/python-stix"

# =============================================================================
# Stage 1: php-base - Common runtime packages
# =============================================================================
FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad AS php-base
ENV DEBIAN_FRONTEND=noninteractive
ARG RUNTIME_PACKAGES

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && apt-get update && \
    apt-get install -y --no-install-recommends $RUNTIME_PACKAGES \
    && apt-get autoremove -y

# =============================================================================
# Stage 2: misp-source - Clone MISP, set permissions
# =============================================================================
FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad AS misp-source
ARG CORE_TAG
ARG CORE_COMMIT
ARG EXCLUDED_SUBMODULES
ARG MISP_UID=1000
ARG MISP_GID=1000

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && apt-get update && apt-get install -y --no-install-recommends git ca-certificates python3

# No submodule is recursive: only misp-iconify nests any, the sources of its icon
# exports, over SSH URLs.
RUN if [ -n "${CORE_COMMIT}" ]; then \
        mkdir -p /var/www/MISP && cd /var/www/MISP && git init -q && \
        git fetch -q --depth 1 https://github.com/MISP/MISP.git "${CORE_COMMIT}" && git checkout -q FETCH_HEAD; \
    else \
        git clone --branch "${CORE_TAG}" --depth 1 https://github.com/MISP/MISP.git /var/www/MISP; \
    fi && \
    cd /var/www/MISP && \
    git config --file .gitmodules --get-regexp path | awk '{print $2}' \
        | grep -v -x -F "$(printf '%s\n' ${EXCLUDED_SUBMODULES})" \
        | xargs git submodule update --init --depth 1 --

# Our changes to MISP's code: one patch each in patches/, with its reason. The build
# fails, naming the patch, when one no longer applies or MISP already carries it.
COPY patches/ /tmp/patches/
COPY scripts/apply_patches.py /tmp/apply_patches.py
RUN python3 /tmp/apply_patches.py /var/www/MISP /tmp/patches

# Clean and set permissions - all in one layer. MISP reads none of the removed
# files at run time: its docs, slides, packaging, tests and tools.
RUN find /var/www/MISP/INSTALL/* ! -name 'POSTGRESQL.sql' -type f -exec rm {} + && \
    find /var/www/MISP/INSTALL/* ! -name 'POSTGRESQL.sql' -type l -exec rm {} + && \
    find /var/www/MISP/.git/* ! -name HEAD -exec rm -rf {} + 2>/dev/null || true && \
    cd /var/www/MISP && rm -rf ${EXCLUDED_SUBMODULES} \
        docs slides format tests tools debian build db_schema.json app/Test app/composer.phar && \
    # The configure step records this marker and the pods wait for it
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
# Stage 3: composer - PHP dependencies, pinned by files/composer.lock
# =============================================================================
# composer-prep takes MISP's composer.json and adds our extra packages, and keeps
# only the S3 and Sts clients of the AWS SDK (the SDK's own removeUnusedServices).
# composer-lock resolves it (scripts/update-composer-lock.sh writes the result to
# files/composer.lock). composer-build installs exactly the lock; an out-of-date
# lock fails the build.
FROM php-base AS composer-prep
ENV COMPOSER_ALLOW_SUPERUSER=1

# composer unpacks with unzip; the final image does not carry it
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && apt-get update && \
    apt-get install -y --no-install-recommends unzip

WORKDIR /tmp
COPY --from=misp-source --chown=0:0 --chmod=0644 /var/www/MISP/app/composer.json /tmp/composer.json
COPY --from=composer:2@sha256:1364b5b9132ab4c42ea3be53e894572c32fe75a512cb3b1c3903fcc9bce53dcc /usr/bin/composer /usr/bin/composer
RUN composer require --no-update --no-interaction \
        elasticsearch/elasticsearch:^8.19 \
        certmichelin/openid-connect-php:^1.3 \
        aws/aws-sdk-php:^3.398 && \
    php -r '$j = json_decode(file_get_contents("composer.json"), true); \
        $j["extra"]["aws/aws-sdk-php"] = ["S3", "Sts"]; \
        $j["scripts"]["pre-autoload-dump"] = "Aws\\Script\\Composer\\Composer::removeUnusedServices"; \
        file_put_contents("composer.json", json_encode($j, JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) . "\n");'

FROM composer-prep AS composer-lock
RUN --mount=type=cache,target=/root/.composer/cache \
    composer update --no-install --no-interaction --with-all-dependencies

FROM composer-prep AS composer-build
COPY files/composer.lock /tmp/composer.lock
# The composer download cache survives across builds (buildah and BuildKit)
RUN --mount=type=cache,target=/root/.composer/cache \
    composer validate --no-check-all --no-check-publish --no-interaction && \
    composer install --no-interaction --no-dev

# =============================================================================
# Stage 4: php-build - Native PHP extensions, built from pinned sources
# =============================================================================
FROM php-base AS php-build
ARG PHP_VER
ENV TZ=Etc/UTC

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ git make php8.4-dev \
        libbrotli-dev libfuzzy-dev librdkafka-dev \
    && apt-get autoremove -y

RUN update-alternatives --set php /usr/bin/php8.4 && \
    update-alternatives --set php-config /usr/bin/php-config8.4 && \
    update-alternatives --set phpize /usr/bin/phpize8.4

# Debian packages none of these four. Each builds from its GitHub repository at a
# pinned commit, so the build reaches no host but GitHub:
#   rdkafka   php-rdkafka/php-rdkafka       6.0.5
#   brotli    kjdev/php-ext-brotli          0.21.0, linked to Debian's libbrotli, which
#                                           the final stage upgrades and Trivy sees
#   simdjson  JakubOnderka/simdjson_php     the fork with the encoder MISP's JsonTool calls
#   ssdeep    JakubOnderka/pecl-text-ssdeep
RUN cp "/usr/lib/$(gcc -dumpmachine)"/libfuzzy.* /usr/lib && \
    build() ( \
        name="$1" repo="$2" commit="$3" && shift 3 && \
        mkdir "/tmp/$name" && cd "/tmp/$name" && git init -q && \
        git fetch -q --depth 1 "https://github.com/$repo.git" "$commit" && git checkout -q FETCH_HEAD && \
        phpize && ./configure "$@" && make -j"$(nproc)" && make install \
    ) && \
    build rdkafka php-rdkafka/php-rdkafka 2df0c74170d95d027e6773d3a9afb1fab5917558 && \
    build brotli kjdev/php-ext-brotli ced23f5b6f52ef58a3a96d4731db4bee40a82736 --with-libbrotli && \
    build simdjson JakubOnderka/simdjson_php ab23d7e5f0edd23b49cf1385f52dc6e300a25fd4 && \
    build ssdeep JakubOnderka/pecl-text-ssdeep aa7ea7045a294548aedc3ccdfbb3936e1716bebd

# =============================================================================
# Stage 5: uv - Python package installer (pinned)
# =============================================================================
FROM ghcr.io/astral-sh/uv:latest@sha256:b46b03ddfcfbf8f547af7e9eaefdf8a39c8cebcba7c98858d3162bd28cf536f6 AS uv

# =============================================================================
# Stage 6: python - Python packages of misp_container and of MISP's scripts
# =============================================================================
# files/requirements-final.txt holds the packages of this repository's code,
# files/requirements-misp.txt those MISP's own scripts import (STIX, TAXII,
# ZeroMQ, sample extraction). pydeep2 and weakrefmethod ship no wheels: they
# build here, with a compiler the final image does not carry. One install
# resolves both files together. The bind mount keeps the uv binary out of
# every layer.
FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad AS python
ENV DEBIAN_FRONTEND=noninteractive

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && apt-get update && apt-get install -y --no-install-recommends \
        python3-minimal libpython3.13-stdlib python3-dev gcc libfuzzy-dev

COPY files/requirements-final.txt files/requirements-misp.txt /tmp/
RUN --mount=type=bind,from=uv,source=/uv,target=/tmp/uv \
    /tmp/uv pip install --system --break-system-packages --no-cache \
        -r /tmp/requirements-final.txt -r /tmp/requirements-misp.txt

# =============================================================================
# Stage 7: caddy-bin - The caddy binary for the web pod's caddy container
# =============================================================================
FROM caddy:2@sha256:cb9d71ad83182011b79355cd57692686374bd78d6fe327efe0ff8507da03ab13 AS caddy-bin
# Kubernetes' allowPrivilegeEscalation: false (no_new_privs) blocks execve of a
# binary with file capabilities, and caddy listens on 8080, so it needs none
RUN setcap -r /usr/bin/caddy

# =============================================================================
# Stage 8: final - Runtime image (non-root)
# =============================================================================
# A fresh FROM, not php-base: the strip below runs in the same layer as the
# install, so what it removes (perl from adduser and debconf, gconv, the Python
# test suite, docs and man pages) never enters a layer. Deleted in a later layer
# on top of php-base, those files would still be pulled.
FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad AS final
ENV DEBIAN_FRONTEND=noninteractive

ARG RUNTIME_PACKAGES
ARG PHP_VER
ARG MISP_UID=1000
ARG MISP_GID=1000
# scripts/build.py sets this to the day of the build, so a local build takes the
# day's security fixes as CI's uncached final stage does
ARG APT_REFRESH=

# The upgrade takes the security pocket's fixes for the base image's packages
# (perl-base and the like, which no stage removes): the scan job fails a release
# on a fixable CRITICAL finding
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    echo "apt refresh ${APT_REFRESH:-none}" && \
    rm -f /etc/apt/apt.conf.d/docker-clean && apt-get update && \
    apt-get upgrade -y --no-install-recommends && \
    apt-get install -y --no-install-recommends $RUNTIME_PACKAGES \
    && apt-get autoremove -y \
    && rm -rf /root/.cache \
              /usr/lib/*/gconv \
              /usr/lib/*/perl \
              /usr/lib/*/perl-base \
              /usr/lib/*/libperl* \
              /usr/lib/python3.*/test \
              /usr/lib/python3.*/unittest \
              /usr/share/doc \
              /usr/share/man

# The pinned Python packages and their commands: uv
# installs both under /usr/local (no pip in the final image)
COPY --from=python /usr/local /usr/local

# Create non-root user
RUN groupadd -g ${MISP_GID} misp && \
    useradd -u ${MISP_UID} -g ${MISP_GID} -m -s /bin/bash misp && \
    update-alternatives --set php /usr/bin/php8.4

# Install the native PHP extensions
# By name: final takes the security pocket's PHP packages, so the build stage's
# other modules may be older than its own
COPY --from=php-build /usr/lib/php/${PHP_VER}/ssdeep.so /usr/lib/php/${PHP_VER}/rdkafka.so \
     /usr/lib/php/${PHP_VER}/brotli.so /usr/lib/php/${PHP_VER}/simdjson.so /usr/lib/php/${PHP_VER}/
RUN for mod in ssdeep rdkafka brotli simdjson; do \
        for dir in /etc/php/*/; do \
            echo "extension=${mod}.so" > "${dir}mods-available/${mod}.ini"; \
        done; \
        phpenmod "${mod}"; \
    done && phpenmod redis && \
    # The console (workers, the configure Job, console tasks) starts a session in
    # some commands; the root is read-only, /tmp is a volume in every such pod.
    # PHP-FPM keeps its sessions in Redis (files/php.ini).
    for dir in /etc/php/*/cli/conf.d; do \
        printf 'session.save_path = "/tmp"\n' > "${dir}/99-misp-cli-sessions.ini"; \
    done

# The web pod's caddy container runs this binary from this image
COPY --from=caddy-bin /usr/bin/caddy /usr/bin/caddy

# Copy MISP source (permissions already set in misp-source stage)
COPY --from=misp-source --chown=${MISP_UID}:${MISP_GID} /var/www/MISP /var/www/MISP
COPY --from=composer-build --chown=${MISP_UID}:${MISP_GID} /tmp/composer.lock /var/www/MISP/app/composer.lock
COPY --from=composer-build --chown=${MISP_UID}:${MISP_GID} /tmp/Vendor /var/www/MISP/app/Vendor

# app/Config defaults (rendered into the per-pod Config volume at start) and the version marker
COPY --from=misp-source --chown=${MISP_UID}:${MISP_GID} /srv/misp-config /srv/misp-config
COPY --from=misp-source --chown=${MISP_UID}:${MISP_GID} /tmp/misp-dist-version /srv/misp-dist-version

# Prepare writable directories (overlaid by the pods' emptyDir volumes and claims).
# app/files ships in the image; MISP writes only to these five subdirectories of it.
RUN for dir in app/files/scripts/tmp app/files/certs app/files/terms app/files/img/orgs \
               app/files/img/custom app/attachments app/tmp app/tmp/cache app/tmp/cache/models \
               app/tmp/cache/persistent app/tmp/cache/views app/tmp/logs \
               app/Config .gnupg; do \
        mkdir -p /var/www/MISP/$dir && chown ${MISP_UID}:${MISP_GID} /var/www/MISP/$dir && chmod 0770 /var/www/MISP/$dir; \
    done

# The Python package: each role starts as python3 -m misp_container.<role>
COPY --chown=${MISP_UID}:${MISP_GID} files/misp_container/ /opt/misp_container/

# Config files and settings. PHP-FPM loads Debian's php.ini, then the files of
# php.d through PHP_INI_SCAN_DIR (web.py)
COPY --chown=${MISP_UID}:${MISP_GID} files/php-fpm-pool.conf /etc/misp-docker/php-fpm-pool.conf
COPY --chown=${MISP_UID}:${MISP_GID} files/php.ini /etc/misp-docker/php.d/99-misp.ini
COPY --chown=${MISP_UID}:${MISP_GID} files/Caddyfile /etc/caddy/Caddyfile
COPY --chown=${MISP_UID}:${MISP_GID} files/misp-config/ /etc/misp-docker/
# gnupg/ is the mount point of the instance GPG key (the misp-gnupg Secret)
RUN find /etc/misp-docker /etc/caddy -type f -exec chmod 0440 {} + && \
    find /etc/misp-docker /etc/caddy -type d -exec chmod 0550 {} + && \
    install -d -o ${MISP_UID} -g ${MISP_GID} -m 0700 /etc/misp-docker/gnupg

ENV PYTHONPATH=/opt PYTHONUNBUFFERED=1

WORKDIR /var/www/MISP
USER ${MISP_UID}

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "-m", "misp_container.web"]

# =============================================================================
# Stage 9: modules - MISP enrichment/import/export/action modules
# =============================================================================
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
