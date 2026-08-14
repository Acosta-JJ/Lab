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

## Platform

| Component | Version | Notes |
|---|---|---|
| Longhorn | 1.11.3 | `longhorn` is the default StorageClass, 2 replicas |
| Flux | v2.9.4 | Bootstrapped over SSH into `clusters/lab` |
| kube-prometheus-stack | chart 88.3.0 | 24h retention capped at 1500MB, 60s scrape |

**Argo CD was replaced by Flux on 2026-08-13**, for native SOPS support. Flux is
also lighter — four controllers against Argo's six components, with no Redis and
no web server — which matters on this hardware.

**Monitoring.** Retention is deliberately short and scraping runs at 60s: every
sample is written twice by Longhorn onto SD cards, which are the consumable part
of this cluster. Alertmanager is off until there is somewhere to route alerts.

Exposing the control plane required machine config changes, since Talos binds
those components to localhost. `kube-controller-manager` and `kube-scheduler`
took `bind-address: 0.0.0.0` and needed no restart; **etcd's
`listen-metrics-urls` needed a rolling reboot**, and Talos does not allow
restarting etcd through the API. `kube-proxy` was left alone: Talos renders its
manifest at bootstrap and does not reconcile `extraArgs` into it, so its
ServiceMonitor is disabled rather than left failing forever.

etcd is scraped with **static targets**, not the chart's `kubeEtcd` discovery.
That path builds a headless Service backed by a hand-written `v1 Endpoints`
object, which is deprecated in Kubernetes 1.33+ and which Argo CD never applied,
so the job was simply absent with no error anywhere.

**Baseline for `etcd_disk_wal_fsync_duration_seconds` p99, measured 2026-08-12:
7.9 / 13.6 / 7.9 ms.** Healthy is under 10ms and tolerable under 25ms, so the SD
cards are keeping up. Watch this number: sustained growth means the cards are
wearing out and leader elections will follow.

Grafana dashboard `Raspberry Pi cluster` covers SoC temperature, CPU,
undervoltage alarm, etcd fsync, memory and free space on `/var` — the shipped
dashboards know nothing about the first three.

**Longhorn.** Data path `/var/mnt/longhorn`, bind-mounted into the kubelet by the
machine config. Two replicas per volume with strict anti-affinity, rebuilds
limited to one at a time. Usable replicated capacity is bounded by the small
nodes: Longhorn sees ~22.7 GiB free on each 32 GB card, minus its 25 % reserve,
so budget **roughly 16 GiB**. The 111 GiB on `rpi-1` cannot be used for
replicated data — there is no second large node to pair it with.

**Flux and the repository layout.**

```
clusters/lab/flux-system/     generated by `flux bootstrap`, Flux manages itself
clusters/lab/infrastructure.yaml   Kustomization → ./infrastructure
clusters/lab/apps.yaml             Kustomization → ./apps, dependsOn infrastructure
infrastructure/longhorn/      storage, installed before anything claims a volume
apps/kustomization.yaml       lists the applications
apps/<name>/                  one directory per application
```

`apps` declares `dependsOn: infrastructure`, so a rebuild from scratch installs
Longhorn before any PVC asks for its StorageClass. **Nothing in this cluster is
installed by hand any more** — the repository reproduces all of it.

Longhorn was adopted into Flux rather than reinstalled: `releaseName` and values
had to match the running release exactly, which made the first reconciliation a
no-op upgrade — Helm went to revision 2 with zero pod restarts and volumes
staying attached. Any drift in those values would have restarted storage
components underneath live volumes.

**Secrets: SOPS with age.** Native support in Flux is why this cluster left Argo
CD. Files matching `*.sops.yaml` are encrypted per `.sops.yaml`, and both
Kustomizations carry a `decryption` block pointing at the `sops-age` Secret.

Only *values* are encrypted, never keys or structure, so a diff still shows which
field changed and a manifest can be reviewed without decrypting it.

```bash
export SOPS_AGE_KEY_FILE=<path to the age key>   # put this in ~/.zshrc
sops apps/monitoring/grafana-admin.sops.yaml     # decrypts, opens $EDITOR, re-encrypts
```

**There is always exactly one secret applied by hand: the key that decrypts the
rest.** `sops-age` in `flux-system` is it, and it can never live in Git.

`grafana-admin.sops.yaml` is the worked example. Before it, the chart generated a
random admin password on every render, so each reconciliation rotated it
silently; `grafana.admin.existingSecret` pins it.

Note that changing `existingSecret` replaces the Grafana pod, and its RWO
Longhorn volume has to detach from the old pod before the new one can mount it.
Transient `MountVolume.WaitForAttach` errors during that handover are expected,
not a fault.

**`GF_SECURITY_ADMIN_PASSWORD` only applies when Grafana initialises its
database.** Point it at a new secret on an existing install and the container
gets the new value in its environment while the admin user in `grafana.db` keeps
the old one — login fails and everything looks correctly configured. Two ways
out: `grafana-cli admin reset-admin-password`, or delete the Grafana PVC and let
it reinitialise. The second is the GitOps answer, since it makes the running
state match what Git declares, and nothing is lost: dashboards come from
ConfigMaps and datasources from the chart.

Repeated failed logins also trip Grafana's brute-force protection
(`too many consecutive incorrect login attempts`), which masks the real cause —
after that, even the right password is rejected for a few minutes.

**To deploy something new: create a directory under `apps/` and add it to
`apps/kustomization.yaml`.** Flux has no equivalent of Argo's directory
generator, so applications are listed rather than discovered — one extra line
per app.

Each application declares its own `Namespace`: Flux has no `CreateNamespace`
option either.

Helm charts are installed directly by the helm-controller, so the umbrella chart
Argo needed is gone. `apps/monitoring/` is the pattern to copy: a
`HelmRepository`, a `HelmRelease` with values un-nested, and a
`configMapGenerator` that builds the Grafana dashboard straight from a JSON
file — which also sidesteps the Go template brace clash for free.

**Bootstrap used a dedicated SSH deploy key**, generated for this repository
alone and stored at `~/Desktop/Lab-secrets/flux/deploy-key`. Never give the
cluster a personal SSH key: Flux stores it in a Secret, and a personal key would
grant write access to every repository the account can reach.

`apps/busybox-longhorn-test/` is a deliberate keeper: a 1 Gi Longhorn volume with
a pod appending a timestamp every 10s. Deleting the pod and finding the previous
pod's lines still in `/data/heartbeat.log` verifies the whole storage path end to
end.

### Handing resources over between GitOps controllers

Argo Applications carry the `resources-finalizer.argocd.argoproj.io` finalizer,
so deleting one **prunes everything it manages**, PVCs included. Patching the
finalizer off the Applications and then deleting the ApplicationSet is not
enough: its controller re-adds the finalizer while processing the cascade, and
both applications were destroyed along with their Longhorn volumes.

The correct order is to detach the Applications from their generator first —
`kubectl delete applicationset <name> --cascade=orphan` — then remove the
finalizers, verify they are gone, and only then delete the Applications. Setting
`preserveResourcesOnDeletion: true` on the ApplicationSet does the same job
declaratively.

The loss here was inconsequential: a test volume and 24h of metrics, both
rebuilt from Git within minutes. That is the point of the design — but the
procedure matters the day it holds something real.

### Two traps that cost real time on this hardware

Both predate the move to Flux but the lessons hold.

**Argo CD's repo-server liveness probe** (historical, Argo is gone). Its path was
`/healthz?full=true`, which made the server render manifests as part of the
health check. Rendering kube-prometheus-stack on ARM cores took longer than the
default timeout, so Kubernetes killed it mid-render — 11 restarts, every sync
failing with `connection refused`. The symptom looked like memory exhaustion and
was not. The lesson generalises: **check `MemoryPressure` and OOM events before
believing a memory hypothesis**, and suspect probe timeouts on slow hardware.

**Grafana dashboards inside Helm templates.** Grafana legend placeholders use the
same double-brace syntax as Go templates, so Helm evaluates them and the render
dies with `function "instance" not defined`. Keep dashboard JSON outside
`templates/` and pull it in with `.Files.Get`. Helm parses comments too, so even
mentioning such a placeholder in a comment inside a template breaks it.

### Planned: remote access

Two mechanisms for two different jobs, deliberately not one for both.

**Cloudflare Tunnel for Grafana only.** `cloudflared` runs in the cluster and
dials out, so no port is opened and no public IP or working CGNAT traversal is
needed. **Cloudflare Access must sit in front of it** — publishing Grafana's own
login to the internet is not acceptable, it has a history of auth CVEs and
scanners find subdomains within hours. The tunnel token is a secret and this
repository is public, so this is the task that finally requires SOPS with age;
`.gitignore` already lets `*.enc.yaml` and `*.sops.yaml` through.

**WireGuard for administration, hosted outside the cluster.** A VPN that runs as
a pod is unreachable exactly when it is needed: a dead node, lost etcd quorum, a
broken CNI. It belongs on the router — which pairs neatly with the dedicated
router already planned for carrying the subnet — or on a separate always-on
device. It also beats a tunnel for this job, because `kubectl` and especially
`talosctl` speak gRPC to specific IPs, and being inside the subnet is what makes
reaching the real node addresses work when the VIP is gone. Tailscale is the
pragmatic variant if NAT traversal and key handling are not worth the learning.

Note that Talos's **KubeSpan is not this**: it is WireGuard between nodes across
networks, not client access.

**Argo CD and Longhorn stay off any public tunnel**, Access or not: Argo holds
cluster-admin over everything. The Kubernetes and Talos APIs are never exposed.

### Grafana's admin password rotates on its own

The chart generates a random password when none is set, and Argo re-renders on
every sync, so the secret changes underneath you. Set it explicitly from a
SOPS-encrypted secret once SOPS is in place.

Still to do, roughly in order:

1. **A LoadBalancer implementation** (MetalLB or Cilium L2) — the VIP only
   serves the Kubernetes API, not application services.
2. **Backups** — scheduled `talosctl etcd snapshot` and a Longhorn backup target
   outside the cluster. Longhorn replicates; it does not back up.
3. **A dedicated router** so the cluster carries its own subnet (see Addressing).

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

### The fan needs a DaemonSet, because the kernel has no driver for it

Talos runs the mainline kernel, which does not ship `pwm-rp1`. Linux therefore
never registers a cooling device — `/sys/class/thermal` holds a `thermal_zone0`
and no `cooling_device` at all — and the Active Cooler never spins. The boards
were reaching 90 °C. This is upstream issue
[sbc-raspberrypi#90](https://github.com/siderolabs/sbc-raspberrypi/issues/90),
open since April 2026, with the mainline RP1 PWM patches still in review.

It is **not** a `config.txt` problem: `dtparam=cooling_fan=on` would have
nothing to bind to.

`apps/rpi-fan/` drives the PWM directly through the RP1's PCI BAR, following
Raspberry Pi's own fan curve with 5 °C hysteresis. Temperatures went from
72/83/75 °C to 40/52/46 °C.

Three things about it are deliberate:

- **The register offsets are copied verbatim** from a script derived from the
  kernel sources. The RP1 also hosts ethernet and USB, so a wrong offset does
  not mean "the fan stays off", it means losing a node's network. Any change
  deserves a staged rollout — one node, confirm the fan and the node's
  connectivity, then widen.
- **On shutdown the fan is set to 100 %.** The hardware keeps its last duty
  cycle after the process exits, so maximum cooling is the safe thing to leave
  behind. Expect a node's fan to run flat out if its pod is evicted.
- **The ConfigMap keeps its name hash**, unlike the Grafana dashboard, so
  editing the script rolls the pods instead of leaving old code running.

**Remove this once mainline ships `pwm-rp1`** — the kernel driver and this would
fight over the same peripheral. And it disappears entirely on server hardware:
this is a Pi-5-on-mainline problem, not a Talos one.

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
