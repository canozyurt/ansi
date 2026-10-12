# Admin access: VPN, bastion, session recording, DNS

Playbook: `playbooks/access.yml` (roles `session_audit`, `admin_login`, `wireguard_gateway`, `bastion`,
`dns_resolver`, `dns_client`). Run it after `secrets.yml`: the bastion refuses to deploy until
`ssh_hardening_trusted_user_ca_keys` holds the OpenBao SSH CA key. On new hosts, `playbooks/bootstrap.yml`
creates the CA and writes the key to `.openbao-init/` ([first-deploy.md](first-deploy.md)).

## What the VPN does and does not do

The WireGuard VPN only gives network reachability into the mgmt zone. It is not an authentication
layer for anything behind it:

- every admin UI still requires Keycloak SSO with MFA (WebAuthn for privileged roles);
- SSH still requires a short-lived certificate from the OpenBao SSH CA (`ssh-client-signer`, 8 h);
- plain keys and passwords are refused on the bastion, and passwords everywhere.

Losing a laptop with a VPN key therefore exposes network reachability only until the peer is removed
or expires (every peer has an owner and an `expires` date; the gateway drops it at that date even
without an Ansible run).

## Operator workflow

```bash
# 1. VPN up (one-time: generate the device key, send the PUBLIC key to platform-admin for wireguard_peers)
wg genkey | tee ~/.config/wg/mgmt.key | wg pubkey
sudo wg-quick up mgmt-vpn-1          # template from /etc/wireguard/clients/<peer>-vpn-1.conf

# 2. Log in to OpenBao with SSO (Keycloak, MFA) and get an 8-hour certificate for your role
export BAO_ADDR=https://bao.example.internal:8200
bao login -method=oidc
bao write -field=signed_key ssh-client-signer/sign/operator \
    public_key=@$HOME/.ssh/id_ed25519.pub > ~/.ssh/id_ed25519-cert.pub

# 3. Jump to the target (no shell on the bastion; the account on the target is named after the role)
ssh -J jump@bastion-1.example.internal operator@app-1.example.internal
```

`~/.ssh/config` shortcut so that `ssh -J bastion target` works:

```
Host bastion
    HostName bastion-1.example.internal
    User jump
Host *.example.internal
    User operator                 # or platform-admin
    CertificateFile ~/.ssh/id_ed25519-cert.pub
```

Principals: `platform-admin` and `operator` (the default roles allowed on hosts). `platform-admin` has
sudo (every sudo session is logged with input and output); `operator` can read logs (adm,
systemd-journal) but has no sudo. Other roles get no SSH principal.

## Failover

- VPN: by default each gateway has its own key and each client gets two templates (`<peer>-vpn-1.conf`,
  `<peer>-vpn-2.conf`); if the primary is down, bring it down and the standby up. Tunnel traffic is
  masqueraded to the gateway's mgmt address, so no return route or shared address is needed. With a
  floating address (`wireguard_gateway_floating_endpoint`, e.g. a keepalived VIP), clients get one
  template and both gateways share one key pair, copied by Ansible from the first gateway.
- Bastion: two or more equivalent jump hosts; use the other one.
- DNS: two resolvers; systemd-resolved on every host lists both.

## Session recording (IAM-07)

The jump hosts give no shell, so recording happens on the targets:

- sshd (LogLevel VERBOSE) logs the certificate key ID and serial of every login; on the bastion the
  jump account logs at DEBUG1, which adds the forwarding target of every jump;
- auditd logs every `execve` by a logged-in person (`auid>=1000`, key `session_cmd`), including after
  sudo/su because the login uid is kept;
- pam_tty_audit records the keystrokes of sessions escalated to root (sudo, su), passwords excluded;
- sudo keeps input/output logs (roles/os_hardening).

The SIEM agent ships `/var/log/audit/audit.log` and the journal. To follow one session: find the
sshd login (key ID) on the target, take the audit session id (`ses=`), then `ausearch --session <ses>`.

## NET-02 and the firewall

`firewall_mgmt_cidrs` (SSH and other management ports on every host) is the bastion addresses + the VPN
range + the VPN gateways' mgmt addresses (NAT source) + the automation runner. The bastion group
narrows it to the VPN only. The VPN routes into the mgmt zone only, so app and data hosts are reached
over SSH through the bastion. Make sure the Ansible runner is in `access_automation_cidrs` or is a VPN
peer before applying, or Ansible locks itself out.
