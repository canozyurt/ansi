# Minimal Coraza SPOA image built from the pinned upstream Go module (Apache-2.0).
# Used when edge_waf_build is true, i.e. when the upstream image registry is not reachable.
ARG BUILDER_IMAGE
FROM ${BUILDER_IMAGE} AS build
LABEL com.compliant-infra.build=coraza-spoa
ARG CORAZA_SPOA_VERSION
COPY extra-ca.crt /tmp/extra-ca.crt
RUN cat /etc/ssl/certs/ca-certificates.crt /tmp/extra-ca.crt > /tmp/ca.crt
ENV SSL_CERT_FILE=/tmp/ca.crt CGO_ENABLED=0 GOBIN=/out GOFLAGS=-trimpath GOTOOLCHAIN=local
RUN go install -ldflags="-s -w" "github.com/corazawaf/coraza-spoa@${CORAZA_SPOA_VERSION}"

FROM scratch
LABEL org.opencontainers.image.title="coraza-spoa" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.source="https://github.com/corazawaf/coraza-spoa"
COPY --from=build /out/coraza-spoa /coraza-spoa
USER 65532:65532
ENTRYPOINT ["/coraza-spoa"]
