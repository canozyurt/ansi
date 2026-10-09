# PostgreSQL server image for roles/postgres_cluster: PostgreSQL, pgaudit and pgBackRest from the
# Ubuntu LTS archive (signed, exact versions) and Patroni from PyPI (wheel verified by SHA-256).
# Built on each host by the role; every input is pinned in roles/versions.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG PG_MAJOR
ARG PG_PACKAGES
ARG PATRONI_VERSION
ARG PATRONI_SHA256

LABEL org.opencontainers.image.title="compliant-infra PostgreSQL with Patroni" \
      com.compliant-infra.build=postgres

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/usr/lib/postgresql/${PG_MAJOR}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8

COPY extra-ca.crt /usr/local/share/ca-certificates/build-extra-ca.crt
COPY patroni.whl /tmp/patroni-${PATRONI_VERSION}-py3-none-any.whl

# Fixed uid/gid 999 so data, certificates and configuration on the host have stable owners.
# The cluster created by the Debian packaging is removed: Patroni runs initdb itself.
RUN set -eux; \
    groupadd --system --gid 999 postgres; \
    useradd --system --uid 999 --gid 999 --home-dir /var/lib/postgresql --shell /bin/bash postgres; \
    mkdir -p /etc/postgresql-common/createcluster.d; \
    echo 'create_main_cluster = false' > /etc/postgresql-common/createcluster.d/no-main.conf; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates; \
    if [ -s /usr/local/share/ca-certificates/build-extra-ca.crt ]; then update-ca-certificates; \
    else rm -f /usr/local/share/ca-certificates/build-extra-ca.crt; fi; \
    apt-get install -y --no-install-recommends ${PG_PACKAGES} \
        python3 python3-psycopg2 python3-yaml python3-urllib3 python3-click python3-prettytable \
        python3-dateutil python3-psutil python3-ydiff python3-pythonjsonlogger python3-etcd python3-pip; \
    echo "${PATRONI_SHA256}  /tmp/patroni-${PATRONI_VERSION}-py3-none-any.whl" | sha256sum -c -; \
    pip3 install --no-deps --no-index --no-cache-dir --break-system-packages \
        /tmp/patroni-${PATRONI_VERSION}-py3-none-any.whl; \
    apt-get purge -y --auto-remove python3-pip; \
    rm -rf /var/lib/apt/lists/* /tmp/*.whl /root/.cache; \
    rm -f /usr/local/share/ca-certificates/build-extra-ca.crt; update-ca-certificates --fresh >/dev/null; \
    install -d -o postgres -g postgres -m 0700 /var/lib/postgresql/data; \
    install -d -o postgres -g postgres -m 2775 /var/run/postgresql; \
    patroni --version; python3 -c 'import patroni.dcs.etcd3'; postgres --version; pgbackrest version; \
    test -f /usr/lib/postgresql/${PG_MAJOR}/lib/pgaudit.so

USER 999:999
WORKDIR /var/lib/postgresql
ENTRYPOINT ["patroni"]
CMD ["/etc/patroni-config/patroni.yml"]
