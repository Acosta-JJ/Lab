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
| Storage | SD cards only — **no NVMe, no USB SSD**. Sizes are uneven: 1x 128 GB, 2x 32 GB |
| Network | Single 1 GbE NIC per node, TP-Link TL-SG1005D unmanaged switch, one power strip |

Everything shares one power strip and one switch. Those are the real single
points of failure; node-level HA does not cover them.

## Addressing

Cluster name is `lab`. Everything lives on `192.168.1.0/24`, behind the main
router — the same network the admin laptop uses over Wi-Fi.

| | |
|---|---|
| `rpi-1` | `192.168.1.11/24` |
| `rpi-2` | `192.168.1.12/24` |
| `rpi-3` | `192.168.1.13/24` |
| VIP (Kubernetes API) | `192.168.1.10` |
| Gateway | `192.168.1.1` |
| Resolvers | `192.168.1.1`, `1.1.1.1` |
| Interface / install disk | `end0` / `/dev/mmcblk0` |

`.10`–`.13` must be excluded from the router's DHCP pool.

**Planned: the cluster carries its own subnet.** A dedicated small router will sit
between the switch and whatever network the cluster is plugged into, always
serving `192.168.1.0/24` on its LAN side and NATing upstream. The cluster then
never notices it moved, and no reconfiguration is needed to relocate it.

This matters because changing the subnet after the fact is genuinely hard: etcd
members register each other by IP (`https://192.168.1.11:2380`) and that list
lives inside etcd itself. Change all three addresses at once and the members look
for each other at the old ones, quorum never forms, and there is no quorum to
issue the command that would fix it. The real options are a rolling
remove-wipe-rejoin per node with both subnets reachable at once, or a rebuild —
which is cheap here, since nodes are disposable by design.

Because the subnet is fixed by the dedicated router, no DNS name or extra
`certSANs` are needed for the API endpoint.

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
**The cluster is up.** Bootstrapped 2026-08-12.

| | |
|---|---|
| Talos / Kubernetes | v1.13.8 / v1.36.2 (kernel 6.18.42-talos arm64, containerd 2.2.6) |
| Schematic ID | `b00ac8400b2ad823d3d5e972136dd89c0d960d58e0ff2b12d5b8b87e9d53e670` |
| Nodes | `rpi-1`, `rpi-2`, `rpi-3` — all `Ready`, all `control-plane`, **no taints** |
| etcd | 3 voting members, no learners. Quorum 2, tolerates one node down. |
| VIP | `192.168.1.10` active; `kubectl` reaches the API through it |
| CNI | Flannel (Talos default) |
| kubeconfig | `~/Desktop/Lab-secrets/kubeconfig` |

`talosctl health` passes every check. Control plane components run on all three
nodes. Restart counts on `kube-scheduler` and `kube-controller-manager` right
after bootstrap are normal — they race for leadership and the losers restart.

Still to do, roughly in order:

1. **Longhorn** — decide the replica count first (see the open decision below),
   then the `kubelet.extraMounts` data path and the privileged namespace label.
2. **A LoadBalancer implementation** (MetalLB or Cilium L2) — the VIP only
   serves the Kubernetes API, not application services.
3. **Argo CD** — insurance against losing etcd, not against losing power.
4. **Backups** — scheduled `talosctl etcd snapshot` and a Longhorn backup target
   outside the cluster.

## Repository layout

```
talos/schematic.yaml     Image Factory input; the ID in its header comes from it
talos/patches/
  common.yaml            scheduling on control plane, install disk, etcd tuning
  network-common.yaml    VIP and resolvers, shared by all nodes
  rpi-{1,2,3}.yaml       hostname and static address, applied per node
```

`common.yaml` and `network-common.yaml` are passed to `talosctl gen config`; the
per-node patches are passed to `talosctl apply-config`.

Generated output lives in **`~/Desktop/Lab-secrets/`**, deliberately outside this
repository: the `.gitignore` would cover it, but this repo is public and secrets
should not sit in the working tree at all.

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

### All three nodes and the laptop must share one flat network

A second router (a Huawei box at `192.168.3.1`) was handing out its own
`192.168.3.0/24` behind the main router. With the switch uplinked to it, the node
was invisible from the laptop's Wi-Fi and the whole addressing plan had to be
redone. The switch's uplink belongs on a **LAN port of the main router**.

What matters is the network, not the cabling: a laptop on Wi-Fi reaches a node on
copper fine, as long as both hang off the same router. If a spare router must be
reused as a switch, disable its DHCP server and uplink into a LAN port, never the
WAN port.

Diagnostic that settles it quickly: compare the default gateway's MAC before and
after unplugging a device, and look up its OUI vendor. A real unmanaged switch
has no IP, no DHCP and no admin page, so anything answering as a gateway is a
router.

### Losing `secrets.yaml` does not stop the cluster, it stops repairs

`secrets.yaml` holds the Talos, Kubernetes, etcd and aggregator CAs plus the
bootstrap tokens. A running cluster does not care if the copy is lost — every
node has its config on the `STATE` partition. What is lost is the ability to
generate a valid config for a **new** node, which is exactly what a dead Pi or a
worn-out SD card requires.

It can be rebuilt from an existing control plane config:

```bash
talosctl gen secrets --from-controlplane-config controlplane.yaml -o secrets.yaml
```

So `secrets.yaml` and `controlplane.yaml` recover each other — but they sit in
the same directory, so one accident takes both. Keep a copy **off this machine**.

### `talosctl` flag placement

`-n/--nodes` and `-e/--endpoints` are subcommand flags, not global ones:
`talosctl version -n <ip>` works, `talosctl -n <ip> version` fails with
`unknown shorthand flag`. Set them once instead:

```bash
export TALOSCONFIG=~/Desktop/Lab-secrets/talosconfig
talosctl config endpoint 192.168.1.11 192.168.1.12 192.168.1.13
talosctl config node 192.168.1.11
```

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

The cards are also **uneven (128 / 32 / 32 GB)**, which constrains capacity more
than the total suggests:

- With one replica per node, **replicated capacity is bounded by the smallest
  node**. The extra ~96 GB on the large card cannot hold replicated data; it only
  buys headroom for container images and logs on that one node.
- On a 32 GB card: ~29 GiB of `EPHEMERAL`, minus 6-8 GB of container images
  (control plane plus Longhorn), minus Longhorn's default 25 %
  `storageMinimalAvailablePercentage` reserve. Budget **~12-15 GiB of usable
  volume space** per small node.
- kubelet starts evicting pods and garbage-collecting images below 10 % free.
  On a slow card that cascades badly, so keep well clear of the threshold.
- The 32 GB cards have fewer spare blocks and will wear out first. They are the
  ones to monitor and the first to replace.

**Open decision — Longhorn replica count.** Default is 3, one per node. With
exactly 3 nodes, **2 replicas is arguably better here**: when a node dies the
missing replica can be rebuilt onto the third node because strict anti-affinity
is still satisfiable, so the volume self-heals instead of sitting `Degraded`
until the node returns. It also halves the space and the rebuild traffic, both
scarce on SD over a single 1 GbE link. Cost: two copies instead of three.
Decide when deploying Longhorn.

### Updating the Pi 5 EEPROM is mandatory, not optional

**This cost hours on the first node.** A Pi 5 with a stock/outdated bootloader
EEPROM boots Raspberry Pi OS perfectly and fails to boot Talos, with no error
code: steady green LED, fan spinning, and **no ethernet link**, because the
kernel never starts.

The reason is that the two boot chains differ:

```
Raspberry Pi OS:  EEPROM -> kernel8.img -> Linux
Talos:            EEPROM -> u-boot.bin -> U-Boot -> kernel -> Linux
```

Talos chainloads U-Boot (`kernel=u-boot.bin` in `config.txt`), a step Pi OS
never exercises. So "Pi OS boots fine" proves nothing about the EEPROM being
new enough for Talos.

Update it with Raspberry Pi Imager: *Misc utility images -> Bootloader (Pi 5
family) -> SD Card Boot*, write to a spare card, boot the Pi with only that
card, wait for the **fast continuous green blink**, power off. No OS or SSH
needed. **Do this on every board before flashing Talos.**

Symptom triage when a node does not come up:

| Observation | Meaning |
|---|---|
| No ethernet link LED | Kernel never booted — suspect EEPROM/U-Boot |
| Ethernet link, no IP | Kernel booted; DHCP or network config problem |
| Counted LED blink pattern | Firmware-level error, see the Talos RPi docs table |

### Flashing: verify, never assume

Two separate failures made a flash appear to succeed while the card was
untouched:

1. **The SD write-protect tab.** A locked card reports `Media Read-Only: Yes`
   in `diskutil info` and silently discards everything. Check it first.
2. **No read-back verification.** `dd` does not verify. Raspberry Pi Imager
   does, so prefer it — and it takes the `.xz` directly (its file picker filters
   by extension and will not show a `.raw`).

Verify after writing with `diskutil list`: a correctly flashed card shows a
**GPT** scheme with EFI ~105 MB, BIOS Boot 1 MB, Linux ~2.1 GB, META 1 MB, and
the **rest of the card unallocated**. `EPHEMERAL` is only created and grown on
first boot, so unallocated space is expected until the node has booted once.

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
