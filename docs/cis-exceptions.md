# CIS benchmark exceptions (HRD-01, HRD-03)

Hosts are scanned weekly against the CIS Level 1 server benchmark with OpenSCAP (`roles/cis_audit`)
and container hosts against the CIS Docker Benchmark with docker-bench-security (`roles/docker_bench`).
The same scans run in CI on test containers (`.github/workflows/audits.yml`). This page lists every
deliberate deviation. A rule listed here is reported as **excepted** instead of **fail** in the
metrics and the score; the raw result stays in the evidence (`/var/lib/compliance-evidence/cis/`,
`/var/lib/compliance-evidence/docker-bench/`), so an auditor sees both.

Every exception needs a reason and a compensating control. Add one in the pull request that needs
it: the rule id in the role default (or `cis_audit_exceptions_extra` / `docker_bench_exceptions_extra`
in the inventory for a site-specific one) and a row here. Review the list once a year and when a
benchmark version changes (rule ids move between releases).

## Benchmark content per release

| Release | Content | Profile used | Notes |
| --- | --- | --- | --- |
| Ubuntu 22.04 | `ssg-debderived` (distribution) | `cis_level1_server` | |
| Ubuntu 24.04 | scap-security-guide release (pinned in `roles/versions`) | `cis_level1_server` | The archive's `ssg-debderived` 0.1.71 has no 24.04 data stream. The release is **not pinned yet** (no recorded SHA-256), so the scan refuses to run on 24.04 until it is. |
| Debian 12 | `ssg-debian` (distribution) | `anssi_np_nt28_minimal`, else `standard` | ComplianceAsCode ships no CIS profile for Debian. The Wazuh agent's SCA module runs the CIS Debian 12 policy on every host (`roles/wazuh_manager`, `<sca>`), so the CIS view of Debian hosts is in the SIEM. |

## Host benchmark (OpenSCAP rule ids, `cis_audit_exceptions_default`)

| Rule | Reason | Compensating control |
| --- | --- | --- |
| `partition_for_tmp`, `partition_for_var`, `partition_for_var_tmp`, `partition_for_var_log`, `partition_for_var_log_audit`, `partition_for_home` | Partitioning is decided when the VM is provisioned, not by Ansible; cloud images ship one root filesystem. | Disk-usage alerts (node_exporter); logs leave the host for the SIEM. Sites that provision separate partitions remove these from the list. |
| `grub2_password` | VMs have no console reachable by anyone outside the provider's control plane; a bootloader password breaks unattended reboots (`playbooks/rolling_reboot.yml`). | Console access is limited to the hosting account (IAM controls of the provider); disk encryption (SEC-04). |
| `package_aide_installed`, `aide_build_database`, `aide_periodic_checking_systemd_timer` | File integrity is monitored by the Wazuh agent (syscheck), not AIDE. | Wazuh FIM on system binaries and configuration with real-time alerts in the SIEM. |
| `package_ufw_installed`, `service_ufw_enabled` | The firewall is nftables, managed by `roles/firewall` (default drop). | nftables ruleset from git; the CIS firewall intent (default deny, loopback rules) is met by that ruleset. |
| `sudo_require_authentication` | The deploy account needs passwordless sudo for Ansible; people have no passwords (SSH certificates only). | With `ci_only_deploys`, the deploy account may use sudo only in a session that logged in with the CI's certificate (`roles/admin_login`); people have read-only accounts; sudo I/O is logged (`roles/session_audit`). |
| `package_rsyslog_installed`, `service_rsyslog_enabled` | journald is the local log store; the Wazuh agent ships it to the SIEM. | Central logging in the SIEM; a host that stops shipping alerts (LogShipperNotRunning, LogShipperSilent). |
| `sysctl_net_ipv4_ip_forward`, `sysctl_net_ipv6_conf_all_forwarding` (only where `os_hardening_ip_forward` is true) | Container hosts and VPN gateways forward packets by design. | Applied only on those hosts; nftables filters forwarded traffic. |

## Docker benchmark (docker-bench-security check ids, `docker_bench_exceptions_default`)

| Check | Reason | Compensating control |
| --- | --- | --- |
| 1.1.1 | A separate partition for `/var/lib/docker` is a provisioning decision. | Disk-usage alerts (node_exporter). |
| 2.9 | User namespace remapping breaks host networking and the fixed service uids the roles rely on. | Every container runs as a non-root user with all capabilities dropped and `no-new-privileges` (HRD-02, `tools/container_policy.py`). |
| 2.12 | No authorization plugin. | The Docker socket is root-only and the docker group is empty; root use is audited (`roles/session_audit`). |
| 4.5 | Docker Content Trust is not used. | Images are pinned by tag and digest in `roles/versions`, scanned and listed in SBOMs in CI (CHG-05). |
| 4.6, 5.27 | Upstream images have no HEALTHCHECK; health is not checked by Docker. | Each role checks health after deployment and Prometheus alerts on the service. |
| 5.9 | Services use host networking by design (`docs/role-contract.md`). | The host firewall filters every port; services bind to the addresses they need. |
| 5.11 | CPU shares are not set. | Memory and PID limits are set on every container (HRD-02). |

## Alerts

Regressions and drops raise alerts (`inventories/example/group_vars/monitoring/audits.yml`):
`CISScoreDropped`, `CISRuleRegressed`, `CISScoreBelowTarget`, `CISAuditFailed`, `CISAuditNotRun`,
`DockerBenchWarnings`, `DockerBenchWarningsIncreased`, `DockerBenchNotRun`.
