# SIEM agent on laptops and other endpoints

Servers get the Wazuh agent from `playbooks/siem.yml` (`roles/wazuh_agent`). Laptops (Phase 2,
endpoint visibility) use the same agent version, the same managers and the same enrollment password,
installed by the device management tool or by hand. They then receive the same central policy
(file integrity monitoring, rootkit checks, CIS assessment, inventory) from the managers.

## What a laptop needs

| Item | Value |
| --- | --- |
| Agent version | `versions_packages.wazuh_agent` in `roles/versions/defaults/main.yml` (never newer than the managers) |
| Managers | The agents' VIP or load balancer name (ports 1514/tcp and 1515/tcp), reachable over the VPN |
| Enrollment password | `vault_wazuh_enrollment_password` (same as servers); hand it to the MDM as a secret, never in a script in git |
| SIEM CA | `/opt/wazuh-ca/ca.pem` on the first SIEM host; lets the agent verify the manager before sending the password |
| Agent group | `default` (or a `laptops` group created on the master with its own `agent.conf`) |

The firewall already allows 1514/1515 from `firewall_mgmt_cidrs` (the VPN ranges).

## Debian / Ubuntu laptops

```bash
VERSION=4.14.8-1            # versions_packages.wazuh_agent
MANAGER=siem.example.internal
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://packages.wazuh.com/key/GPG-KEY-WAZUH | sudo tee /etc/apt/keyrings/wazuh.asc >/dev/null
gpg --show-keys --with-fingerprint /etc/apt/keyrings/wazuh.asc   # must show 0DCF CA55 47B1 9D2A 6099 5060 96B3 EE5F 2911 1145
echo "deb [signed-by=/etc/apt/keyrings/wazuh.asc] https://packages.wazuh.com/4.x/apt/ stable main" \
  | sudo tee /etc/apt/sources.list.d/wazuh.list
sudo apt-get update && sudo apt-get install -y "wazuh-agent=${VERSION}" && sudo apt-mark hold wazuh-agent
# Enrollment password and CA: copy them from the secrets manager, not from shell history.
sudo install -m 0640 -o root -g wazuh /path/from/mdm/authd.pass /var/ossec/etc/authd.pass
sudo install -m 0640 -o root -g wazuh /path/from/mdm/siem-ca.pem /var/ossec/etc/siem-ca.pem
```

Then set the manager and enrollment block in `/var/ossec/etc/ossec.conf` (the server role's
`roles/wazuh_agent/templates/ossec.conf.j2` is the reference), with a unique `agent_name` (for example the
asset tag) and `<label key="owner">` set to the person or team that owns the device:

```xml
<client>
  <server><address>siem.example.internal</address><port>1514</port><protocol>tcp</protocol></server>
  <enrollment>
    <enabled>yes</enabled>
    <manager_address>siem.example.internal</manager_address>
    <agent_name>LAPTOP-ASSET-TAG</agent_name>
    <server_ca_path>/var/ossec/etc/siem-ca.pem</server_ca_path>
    <authorization_pass_path>etc/authd.pass</authorization_pass_path>
  </enrollment>
</client>
```

`sudo systemctl enable --now wazuh-agent`; the laptop appears as active in the dashboard within a minute.

## macOS and Windows

Use Wazuh's signed installers of the same version through the MDM, with these installer variables:
`WAZUH_MANAGER`, `WAZUH_REGISTRATION_PASSWORD` (the enrollment password, supplied as an MDM secret),
`WAZUH_REGISTRATION_CA` (path of the SIEM CA deployed by the MDM) and `WAZUH_AGENT_NAME`. Example for Windows:

```powershell
msiexec /i wazuh-agent-4.14.8-1.msi /q WAZUH_MANAGER="siem.example.internal" `
  WAZUH_REGISTRATION_PASSWORD="$env:WAZUH_ENROLL" WAZUH_REGISTRATION_CA="C:\ProgramData\siem-ca.pem" `
  WAZUH_AGENT_NAME="$env:COMPUTERNAME"
```

## Silence and offboarding

- A laptop that stops reporting raises the manager's level-10 "agent disconnected" alert (rule 100110,
  LOG-06) after `wazuh_manager_agents_disconnection_time`. Laptops are often offline; route the
  `laptops` group's disconnections to a lower-urgency receiver in Alertmanager if needed.
- When a device is retired, remove its agent (`DELETE /agents?agents_list=<id>` on the manager API) so
  its key cannot be reused; rotate the enrollment password when it may have leaked (update the Vault
  value and rerun `playbooks/siem.yml`; existing agents keep their keys).
