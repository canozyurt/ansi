#!/bin/bash
# Test-only: builds a wazuh-agent .deb from the official wazuh/wazuh-agent image, for sandboxes that
# cannot reach packages.wazuh.com. The binaries are Wazuh's own (the image installs the official
# package); this script only repackages /var/ossec with a minimal postinst that recreates the
# package's user, ownership and systemd unit. Production uses the apt repository (or Wazuh's .deb).
#
#   molecule/siem/build-agent-deb.sh <version> <output.deb>
#   export WAZUH_AGENT_DEB=<output.deb>   # molecule/siem then installs it with wazuh_agent_install_method=deb
set -euo pipefail
version="${1:?version, e.g. 4.14.8}"
out="${2:?output .deb path}"
image="docker.io/wazuh/wazuh-agent:${version}"
work="$(mktemp -d)"
trap 'rm -rf "${work}"; docker rm -f wazuh-agent-deb-src >/dev/null 2>&1 || true' EXIT

docker pull -q "${image}" >/dev/null
docker create --name wazuh-agent-deb-src "${image}" >/dev/null
mkdir -p "${work}/root/var" "${work}/root/usr/lib/systemd/system" "${work}/root/DEBIAN"
# docker export keeps the image's numeric ownership (docker cp would not); run as root.
docker export wazuh-agent-deb-src \
  | tar -x -C "${work}/root" --same-owner --numeric-owner var/ossec usr/lib/systemd/system/wazuh-agent.service
# Ownership and modes as installed by the official package (the image's wazuh user is uid/gid 999).
(cd "${work}/root" && find var/ossec -printf '%U:%G %m /%p\n') \
  | sed -e 's/^999:/wazuh:/' -e 's/:999 /:wazuh /' -e 's/^0:/root:/' -e 's/:0 /:root /' > "${work}/perms"
docker rm -f wazuh-agent-deb-src >/dev/null

# The image's package selected the Amazon Linux SCA policy; add the CIS policies for our distributions.
rm -f "${work}"/root/var/ossec/ruleset/sca/*.yml
for policy in debian/cis_debian12.yml ubuntu/cis_ubuntu22-04.yml ubuntu/cis_ubuntu24-04.yml; do
  curl -fsSL "https://raw.githubusercontent.com/wazuh/wazuh/v${version}/ruleset/sca/${policy}" \
    -o "${work}/root/var/ossec/ruleset/sca/$(basename "${policy}").disabled"
done

cat > "${work}/root/DEBIAN/control" <<CONTROL
Package: wazuh-agent
Version: ${version}-1
Architecture: amd64
Maintainer: compliant-infra test build <noreply@example.invalid>
Depends: libc6, adduser
Description: Wazuh agent (test repackaging of the official ${image} contents)
CONTROL

{
  echo '#!/bin/sh'
  echo 'set -e'
  echo 'getent group wazuh >/dev/null || groupadd -r wazuh'
  echo 'getent passwd wazuh >/dev/null || useradd -g wazuh -G wazuh -d /var/ossec -r -s /sbin/nologin wazuh'
  # Enable only the CIS policy of this distribution, like the official package does.
  echo '. /etc/os-release'
  echo 'case "${ID}${VERSION_ID}" in'
  echo '  debian12) p=cis_debian12.yml ;; ubuntu22.04) p=cis_ubuntu22-04.yml ;; ubuntu24.04) p=cis_ubuntu24-04.yml ;; *) p= ;;'
  echo 'esac'
  echo '[ -n "$p" ] && cp /var/ossec/ruleset/sca/$p.disabled /var/ossec/ruleset/sca/$p'
  echo 'rm -f /var/ossec/ruleset/sca/*.disabled'
  echo 'chown root:wazuh /var/ossec/ruleset/sca/*.yml 2>/dev/null || true'
  echo 'chmod 0640 /var/ossec/ruleset/sca/*.yml 2>/dev/null || true'
  while read -r owner mode path; do
    echo "chown ${owner} '${path}' 2>/dev/null || true; chmod ${mode} '${path}' 2>/dev/null || true"
  done < "${work}/perms"
  echo 'touch /var/ossec/logs/active-responses.log; chown wazuh:wazuh /var/ossec/logs/active-responses.log; chmod 0660 /var/ossec/logs/active-responses.log'
  echo 'echo "VERSION=\"v'"${version}"'\"" > /etc/ossec-init.conf'
  echo 'systemctl daemon-reload >/dev/null 2>&1 || true'
} > "${work}/root/DEBIAN/postinst"
chmod 0755 "${work}/root/DEBIAN/postinst"
printf '/var/ossec/etc/ossec.conf\n/var/ossec/etc/local_internal_options.conf\n/var/ossec/etc/client.keys\n' > "${work}/root/DEBIAN/conffiles"
dpkg-deb --root-owner-group -Zxz --build "${work}/root" "${out}" >/dev/null
sha256sum "${out}"
