# Disk encryption (data at rest, SEC-04)

Every path that holds service data, container volumes or local backups must be encrypted at rest,
either by **dm-crypt/LUKS on the host** or by the **hosting provider** (encrypted volumes the host
cannot see). Backups are also encrypted client-side by restic, and pgBackRest repositories with
AES-256, whatever the disks do.

This repository **checks** encryption; it never formats, encrypts or opens volumes. Encrypting a
disk is part of building the host (installer, cloud-init or image pipeline), because it has to
happen before any data is written.

## What the baseline checks

`roles/disk_encryption` runs in `playbooks/baseline.yml` on every host:

1. For every path in `encrypted_mounts_required` that exists (default `/var/lib/docker`, `/opt`,
   `/srv`, `/var/backups`), it finds the backing device (`findmnt`) and walks its device stack
   (`lsblk -s`). The path is **dm-crypt protected** when a `crypt` layer appears anywhere below it.
   LVM on LUKS, LUKS on RAID and similar stacks count.
2. A path is **protected** when it is on dm-crypt, or when the host declares
   `disk_encryption_provider` (provider-side encryption, which the host cannot detect).
3. It exports metrics through node_exporter's textfile collector:

   | Metric | Meaning |
   | --- | --- |
   | `disk_encryption_mount_dmcrypt{mountpoint}` | 1 if the path is on dm-crypt |
   | `disk_encryption_mount_protected{mountpoint}` | 1 if on dm-crypt or provider-encrypted |
   | `disk_encryption_provider_declared{provider}` | 1 if the host declares provider encryption |

   The `DataMountNotEncrypted` alert (`inventories/example/group_vars/monitoring/resilience.yml`)
   fires after one hour on any unprotected path.
4. With `disk_encryption_enforce: true` (the example inventory sets it for all hosts) the run
   **fails** on any unprotected path. With `false` it prints a warning, which suits labs.

The datastore roles keep their own checks of their data directories (`postgres_data_dir_encrypted`,
`datastore_data_dir_encrypted`); add any data directory outside the defaults to
`encrypted_mounts_required`.

## Option A: provider encryption

Use this when the provider encrypts block volumes at rest and you cannot or do not want to run LUKS
in the guest (for example AWS EBS with a KMS key, GCP persistent disks, Azure managed disks,
Hetzner/OpenStack encrypted volumes).

```yaml
# host_vars/<host>.yml or a group
disk_encryption_provider: aws-ebs   # one of disk_encryption_providers_known
```

Declaring a provider is an **attestation**. Keep evidence: an export of the volume settings, or
the provider's documentation page with the date, in the GRC tool under SEC-04. Who holds the keys
(provider-managed or customer-managed KMS) is a shared-responsibility point; see
[shared-responsibility.md](shared-responsibility.md).

## Option B: LUKS on the host

Create the encrypted volume when the host is built, then mount the data paths from it.

Debian/Ubuntu installer: choose "Guided - use entire disk and set up encrypted LVM", or preseed
`partman-auto/method string crypto`. On an existing host with a separate data disk:

```sh
cryptsetup luksFormat --type luks2 --pbkdf argon2id /dev/sdb   # passphrase from OpenBao, see below
cryptsetup open /dev/sdb data
mkfs.ext4 /dev/mapper/data
echo "data UUID=$(blkid -s UUID -o value /dev/sdb) none luks,discard" >> /etc/crypttab
echo "/dev/mapper/data /srv ext4 defaults,nofail 0 2" >> /etc/fstab
```

Do this **before** the services are deployed, or move the data with the service stopped. Bind-mount
or symlink `/var/lib/docker` and `/opt` onto the encrypted volume as well, or put the whole root
file system on LUKS.

Keep the LUKS passphrase (the recovery key) in OpenBao (`secret/hosts/<host>/luks`) and in the
offline break-glass envelope. Never store it on the host.

## Unattended unlock: clevis with Tang and/or the TPM

A server with LUKS needs the passphrase at every boot. Clevis unlocks it automatically when a
policy is met, and the passphrase stays valid as the recovery key.

| Policy (`disk_encryption_clevis_policy`) | Unlocks when | Protects against |
| --- | --- | --- |
| `tang` | `disk_encryption_clevis_tang_threshold` Tang servers on the internal network answer | a disk or a whole server taken off-site |
| `tpm2` | the TPM of the same machine unseals (PCR 7: Secure Boot state) | a disk removed from the machine |
| `tang+tpm2` | both: the TPM **and** the Tang threshold (Shamir `sss` pin) | both of the above; the strictest option |

**Tang** (network-bound disk encryption) is a small stateless server. Run two or more on hosts
that are not themselves unlocked by these Tang servers (for example the backup servers, or
hardware in another rack), reachable only from the internal network. A stolen disk or server
cannot reach them. Get each server's signing-key thumbprint with `tang-show-keys` on the server
and pin it; never trust on first use.

**TPM2** binds the key to the machine and its Secure Boot state. Firmware or bootloader updates can
change PCR 7. Re-bind after such updates (`clevis luks regen`), or keep `tang` in the policy so
the volume still unlocks.

Turn it on for hosts whose LUKS volumes already exist:

```yaml
disk_encryption_clevis_devices:
  - { device: /dev/disk/by-uuid/0b1c...-... }
disk_encryption_clevis_policy: tang
disk_encryption_clevis_tang_servers:
  - { url: "http://tang-1.example.internal", thp: "x3Y..." }
  - { url: "http://tang-2.example.internal", thp: "Pq9..." }
disk_encryption_clevis_tang_threshold: 1
disk_encryption_luks_passphrase: "{{ vault_luks_passphrase }}"   # host_vars vault file, copy of the OpenBao entry
```

The role installs clevis (`clevis-luks`, `clevis-initramfs`, `clevis-systemd`, plus `clevis-tpm2`
for TPM policies), checks that each device is already LUKS, adds a clevis key slot unless one is
bound already, and rebuilds the initramfs. It never removes the passphrase slot.

Test a reboot of one host per hardware type before you rely on it. If the Tang servers are down
and the policy needs them, the host waits at boot for the passphrase: unlock it through the
out-of-band console with the recovery key from OpenBao or the break-glass envelope.

## Databases, object storage and backups

| Data | Encryption at rest | Status |
| --- | --- | --- |
| PostgreSQL, Valkey, RabbitMQ, Garage, Forgejo, OpenBao, Wazuh data directories | the volume (LUKS or provider), checked by this role and the datastore roles | check implemented (not tested) |
| OpenBao storage | also encrypted by OpenBao's barrier (AES-256-GCM) | implemented |
| restic backups | client-side AES-256 (restic), repository password from the seed in Vault | tested |
| pgBackRest repositories | `aes-256-cbc` repository encryption | tested |
| Garage objects | volume encryption; applications may add SSE-C per object | documented |
| Data exports (`playbooks/data_export.yml`) | CMS (AES-256) to the recipient's certificate | not tested |
