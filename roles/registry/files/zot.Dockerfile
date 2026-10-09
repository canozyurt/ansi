# Zot from the pinned, checksum-verified release binary (roles/registry, registry_build).
# The binary is the same build the upstream image ships; it needs glibc, so the base is Debian slim.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG UID=10001
COPY zot /usr/bin/zot
RUN groupadd --system --gid "${UID}" zot \
 && useradd --system --uid "${UID}" --gid "${UID}" --no-create-home --shell /usr/sbin/nologin zot
USER ${UID}:${UID}
EXPOSE 5000
ENTRYPOINT ["/usr/bin/zot"]
CMD ["serve", "/etc/zot/config.json"]
