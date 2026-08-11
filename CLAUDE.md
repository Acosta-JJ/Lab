# Lab — Talos Kubernetes homelab

Living notes for this project. Update it as decisions are made and as we learn
things the hard way. Prefer recording *why* over *what*: the what is in the code.

## Goal

A 3-node highly available Kubernetes cluster on Raspberry Pi 5, run as close to a
real production environment as a homelab allows. Learning is the point; breakage
is acceptable and expected.

## Hardware

| Item | Value |
|---|---|
| Nodes | 3x Raspberry Pi 5 |
| Storage | SD cards only — **no NVMe, no USB SSD** |
| Network | Single 1 GbE NIC per node, all on one unmanaged switch, one power strip |

Everything shares one power strip and one switch. Those are the real single
points of failure; node-level HA does not cover them.

## Architecture

- All three nodes are **control plane and worker simultaneously**
  (`cluster.allowSchedulingOnControlPlanes: true`). Talos has no separate worker
  role — a worker is just a node without the control plane components.
- **etcd with 3 members**: quorum is 2, so one node may fail. Two cannot.
- **Layer 2 VIP** for the Kubernetes API endpoint, elected through etcd. No
  external load balancer needed.
- **Longhorn** for replicated persistent volumes, 3 replicas with strict
  anti-affinity (one replica per node).

## Current state

| | |
|---|---|
| Talos version | v1.13.8 (latest stable as of 2026-08-11) |
| Schematic ID | `b00ac8400b2ad823d3d5e972136dd89c0d960d58e0ff2b12d5b8b87e9d53e670` |
| Image | `metal-arm64.raw.xz` from Image Factory, downloaded, not yet flashed |
| Cluster | Not bootstrapped yet |

Next: flash the three SD cards, boot node 1 in maintenance mode, and read the
network interface name and disk name needed to write the machine config.

## Hard-won facts

### The image is built once and is immutable

Talos has no shell, no SSH and no package manager. Anything a node will ever need
must be in the image. Three things are frozen into the schematic and can only be
changed by regenerating the image and reflashing:

1. **The `rpi_5` overlay.** The generic arm64 image does not boot on a Pi: there
   is no UEFI. The overlay supplies U-Boot, `config.txt`, the device tree blobs
   and the RP1 southbridge firmware.
2. **System extensions.** Longhorn needs `iscsi-tools` (iscsiadm + `iscsi_tcp`
   module) and `util-linux-tools` (nsenter, fstrim). Neither ships in the base
   image.
3. **`config.txt`.** Fan control, CMA size, PCIe generation. Not editable on a
   running system.

The **schematic ID is a content hash**, so the YAML in `talos/` fully reproduces
the image. It is not a secret — the Image Factory is public and the ID only lets
someone download the same image. That changes if a secret is ever put in the
schematic: the ID is a bearer reference to the built image. **Never put secrets
in the schematic.**

Upgrades must reference the schematic:
`talosctl upgrade --image factory.talos.dev/installer/<schematic-id>:<version>`.
Using the generic installer leaves the Pi unbootable. Verify the ID still matches
`talos/schematic.yaml` before upgrading — a stale ID in a comment is the likely
failure mode.

### Provisioning decisions that cannot be undone

Volume configuration is **only applied if the volume does not exist yet**.
Applying it later silently does nothing.

- `EPHEMERAL` (`/var`, holds etcd, container images, logs) defaults to
  `grow: true` and fills the disk.
- With one SD card per node there is no second disk, so: **do not split the
  card.** Let `EPHEMERAL` fill it and point Longhorn at `/var/lib/longhorn` via
  `kubelet.extraMounts`. Fewer moving parts. The cost is that `talosctl reset`
  wipes that node's replicas — acceptable, the other two copies cover it.

### Static IPs are a hard requirement, not a preference

etcd members register by IP. After a power outage, if the Pis boot faster than
the router (they do) or the router does not come back, DHCP nodes come up with no
address and **the cluster never forms quorum**.

With static IPs in the machine config, the address lives on the `STATE` partition
and the cluster recovers with the router switched off entirely: the nodes talk to
each other through the switch, etcd forms quorum, the VIP is elected (pure L2),
and pods start from images already cached in containerd.

The first boot is always DHCP — the image carries no config. Static addressing
arrives with the machine config.

### The VIP is for Kubernetes, never for talosctl

VIP election depends on etcd. If etcd is down the VIP is gone — which is exactly
when the Talos API is needed to fix etcd. `talosctl` endpoints are always the
three real node IPs.

### Never run `talosctl bootstrap` twice

It is only for the birth of the cluster. After a reboot or a power outage etcd
restarts from its data directory on its own; there is nothing to bootstrap.
Kubernetes restores every workload from etcd by itself — Argo CD is insurance
against losing etcd, not against losing power.

### SD-only consequences

etcd fsyncs constantly and SD cards have poor write latency and limited
endurance. Expect the cards to be consumables. Raise etcd `heartbeat-interval`
and `election-timeout` in the machine config so a slow fsync does not trigger
spurious leader elections — the highest-value tuning available here.

### Raspberry Pi 5 specifics

- Only the **HDMI port closest to the USB-C connector** works.
- The `rpi_5` overlay sets `talos.dashboard.disabled=1`, so the console shows
  plain logs, not the Talos TUI. `talosctl dashboard` still works remotely.
- The GPIO UART is **disabled** in `config.txt` for the Pi 5 (U-Boot
  incompatibility). HDMI is the only local console.
- Pi 5 support in Talos is **community-tested**, not officially supported.
  Upgrade one node first, verify it rejoins etcd, then do the rest.

### Node failure behaviour

- etcd survives at 2 of 3, but fault tolerance is now **zero** — do not touch
  anything else until the node is back.
- Pods take ~40 s to be marked `NotReady` and another 300 s before eviction.
  RWO volumes then hit `Multi-Attach` errors, and StatefulSet pods hang in
  `Terminating` indefinitely. Fixes: the
  `node.kubernetes.io/out-of-service` taint, and Longhorn's
  `node-down-pod-deletion-policy: delete-both-statefulset-and-deployment-pod`.
- Longhorn volumes go `Degraded` and **cannot rebuild** the third replica: with
  strict anti-affinity there is no third node. Expected, not a bug.
- When the node returns, the replica rebuild saturates the single 1 GbE link,
  which also carries etcd traffic → leader elections → VIP flapping. Set
  `concurrent-replica-rebuild-per-node-limit: 1`.

### Longhorn on Talos

Besides the two extensions:

- `kubelet.extraMounts` for the data path with `rshared` propagation — the
  kubelet runs in a container and cannot see the host path otherwise.
- Label the namespace `pod-security.kubernetes.io/enforce=privileged`; Talos
  enforces the `baseline` PSS profile by default. Without it the manager pods are
  never created and the error surfaces on the ReplicaSet, not the pod.
- Longhorn replicates, it does not back up. A `DROP TABLE` replicates instantly.
  Configure a backup target and schedule `talosctl etcd snapshot`.

## Conventions

- **Everything in this repo is written in English.** Conversation with the owner
  is in Spanish; the artifacts are not.
- Comments are terse. Explain a line only when the reason is not obvious.
- Commit messages follow **Conventional Commits**:
  `feat(talos): add image schematic for the Raspberry Pi 5 nodes`.
- **No secrets in the repo.** `talosconfig`, `secrets.yaml`, `kubeconfig` and the
  generated `controlplane.yaml` / `worker.yaml` are gitignored — the generated
  configs embed certificates and tokens in plaintext. The repository is public.
  When secrets are needed, use SOPS + age; `*.enc.yaml` and `*.sops.yaml` are
  allowed through.
- `secrets.yaml` must be backed up off this machine. Losing it means never being
  able to add a node to the cluster again.
