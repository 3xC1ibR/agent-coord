# Hosted machine plan

Status: proposal awaiting the owner's approval. Written 2026-10-28. Nothing in
this plan is built, and no AWS resources exist.

This plan moves the owner's own Ribbon Field agents from a Mac to one always-on
AWS machine that the owner controls. It tests the promise in
[standalone-product.md](standalone-product.md): start coding work, close a
laptop, and return from another device. It covers the owner only. It defines
the interfaces that later multi-user hosting would need, but does not build
accounts or provisioning for anyone else.

Evidence tags: **[measured]** means a command was run, and the command is given.
**[read in code]** gives a `file:line` reference. **[from docs]** is a primary
document, accessed 2026-10-28 unless another date is stated. **[assumed]** is a
judgement or an untested design choice. Where a probe could not run, the plan
says so.

## Decisions

| # | Decision | Recommendation |
| --- | --- | --- |
| 1 | Substrate | EC2 t4g.xlarge (arm64, 4 vCPU, 16 GiB, Unlimited mode) in us-east-1, once phase 0 fixes the backend CPU defect. Not Lightsail: it has no instance IAM role for Secrets Manager. Not Forge: extending it costs 30–60 engineer-days. |
| 2 | Machine as code | Terraform for AWS resources, plus one idempotent provisioning script and systemd. `create_machine.sh` builds everything from nothing. |
| 3 | Connectivity | Tailscale only. The security group has no inbound rules, and the server keeps its loopback-only binding. Root owns the Serve route, which the phone uses with the existing pairing. The desktop uses an SSH port forward with a pinned host key. A tested tailnet policy stops the machine reaching the owner's other devices. |
| 4 | Desktop | One backend at a time, chosen from a Connection menu. The label comes from a `machine` identity that the server reports in `/api/browser/config`. When the server is hosted, local-file features are turned off. |
| 5 | Credentials | Provider logins use each CLI's headless flow and are stored on the root volume, so they survive a reboot but not a rebuild. API keys come from Secrets Manager, fetched by a root-only unit, and are held in tmpfs for agents. Only root can reach instance metadata, so the machine's own identities stay away from agents. |
| 6 | Browser | One persistent Chromium driven over CDP by Playwright MCP. The owner logs in once through a DevTools screencast forwarded over SSH. No hosted browser service and no pixel-based desktop. |
| 7 | Later users | Interfaces only: a machine record, a provisioning interface, Google as an identity provider on the existing Cognito pool, and an allowlist. Resolving the provider-terms question is the gate before anyone else uses a machine. |
| 8 | Cost | $118.76/month on demand, or $91.39 with a 1-year Compute Savings Plan. Each additional user costs about $124 on demand. Before phase 0, today's defects would add $142–309/month. |

## Scope

The plan builds:

- One EC2 machine, rebuilt by one command, running Ribbon Field and Codex,
  Claude Code, Antigravity, Kimi and Muse, with Node, Python, git, gh and a
  browser runtime.
- A server-reported machine identity, a desktop connection switcher, and
  hosted-mode handling of local-only features.
- Phone access to the machine through the existing pairing flow.
- A credential path for API keys that keeps them out of the image, logs,
  Ribbon Field state and git.

The plan does not need, and does not build:

- Accounts, billing or provisioning for other people. Section 7 defines their
  interfaces only.
- Public HTTPS, Tailscale Funnel, a browser terminal, a hosted browser service,
  or a full Linux desktop.
- Gemini CLI sign-in. Consumer Gemini access moved to Antigravity on
  2026-06-18 [from docs:
  [Gemini CLI README](https://github.com/google-gemini/gemini-cli/blob/main/README.md)],
  and Gemini CLI is not a Ribbon Field runtime [read in code:
  `provider_limits.py:26`]. It is installed because the tool list asks for it,
  and stays signed out.
- APNs keys. Phone alerts use Web Push, which needs no developer account, and
  the native iOS app has no background push yet [from docs:
  [remote-access.md](remote-access.md)].
- Jev classification. It is opt-in today and stays off on the hosted machine.
- Changes to Forge, ribbonfield.com or api.ribbonfield.com.

Already available elsewhere [from docs; see the
[comparator evidence](#comparators)]: exe.dev rents persistent VMs from
$15/month. The Codex app runs app-server on an SSH host, but its phone pairs
with an awake desktop, not with the Linux machine. Claude Remote Control relays
one provider's sessions through Anthropic. A remote box running one agent is
commodity. What this plan tests is several providers' native sessions,
coordinated on one machine, reachable from both the desktop and the phone,
with the phone paired directly to that machine.

## Architecture

```text
OWNER'S DEVICES (tailnet members)          AWS ACCOUNT, us-east-1
+--------------------------------+         +---------------------------------------------------------------+
| Mac: Ribbon Field desktop      |         | VPC, public subnet. Security group: no inbound rules.         |
|                                |         |                                                               |
| Connection "This Mac"          |         | EC2 "ribbon-1": t4g.xlarge, Ubuntu 24.04 arm64, IMDSv2 only   |
|   starts backend.py locally    |         | +-----------------------------------------------------------+ |
|                                |         | | sshd (key only) <------------------------------------+    | |
| Connection "Hosted - ribbon-1" | tailnet | |                                                      |    | |
|   ssh -L 8765:127.0.0.1:8765 --+---------+-+------------------------------------------------------+    | |
|                                |         | |                                                           | |
| iPhone: Ribbon Field app or    | tailnet | | tailscaled  Serve https://ribbon-1.<tailnet>.ts.net       | |
|   Home Screen web app ---------+---------+-+--> (proxies to loopback)                                  | |
+--------------------------------+         | |            |                                              | |
                                           | |            v                                              | |
                                           | | ribbon-field.service: agent-coord ui on 127.0.0.1:8765    | |
                                           | |   user ribbonfield, Restart=always                        | |
                                           | |   +- codex app-server     +- claude (stream-json)         | |
                                           | |   +- agy_acp_server       +- kimi acp    +- muse serve    | |
                                           | |   +- Playwright MCP --CDP 127.0.0.1:9222--> Chromium      | |
                                           | |                         (ribbon-field-browser.service)    | |
                                           | | ribbon-field-secrets.service (oneshot, root):             | |
                                           | |   Secrets Manager -> /run/ribbon-field/agent.env (tmpfs)  | |
                                           | |                                                           | |
                                           | | root EBS 40 GB: OS, tools, provider logins, browser data  | |
                                           | | data EBS 120 GB at /srv/ribbon-field:                     | |
                                           | |   state, repositories, transcripts ----> daily snapshots  | |
                                           | +-----------------------------------------------------------+ |
                                           | IAM instance role (root only): read its three secrets         |
                                           | SSM Session Manager: break-glass shell, no inbound port       |
                                           +---------------------------------------------------------------+
Outbound only: OpenAI, Anthropic, Google, Moonshot, Meta, GitHub, package registries, Web Push services.
```

## 1. Substrate

**Recommendation: EC2 t4g.xlarge (Graviton2 arm64, 4 vCPU, 16 GiB) in Unlimited mode, in us-east-1, in the owner's AWS account. This requires phase 0's backend CPU fix. If the build starts before that fix, use m8g.xlarge (fixed performance) instead.**

### EC2, not Lightsail

- **Lightsail has no unlimited mode.** When burst capacity runs out it
  throttles to baseline [from docs:
  [Lightsail baseline CPU](https://docs.aws.amazon.com/lightsail/latest/userguide/baseline-cpu-performance.html)].
- **Lightsail cannot use an IAM instance role.** Its IAM page says "Lightsail
  does not support service roles" and documents no instance profile [from docs:
  [Lightsail IAM](https://docs.aws.amazon.com/lightsail/latest/userguide/security_iam_service-with-iam.html)].
  A community answer confirms that Secrets Manager then needs stored access
  keys [from docs: re:Post, secondary]. That conflicts with the
  instance-scoped credential requirement.
- **In Lightsail's favour:** the $84 plan (4 vCPU, 16 GB) is cheaper and
  includes 6 TB of transfer [from docs: [standalone-product.md](standalone-product.md),
  reviewed 2026-10-07]. Its 40% baseline matches t4g.xlarge.

### Size

- **Memory.** Capacity planning estimates that 4 vCPU and 16 GB hold 20 loaded
  Codex and 6 loaded Claude conversations, with about 10 GB left for builds,
  tests and the browser. This holds only after idle conversations are unloaded
  [from docs: [capacity-planning.md](capacity-planning.md)].
- **CPU, after phase 0.**
  - The modelled steady demand is 0.65–0.85 vCPU: provider CLIs at the measured
    4.8 mean concurrent turns, plus a repaired backend [assumed; inputs from
    capacity-planning.md].
  - t4g.xlarge earns a 1.6 vCPU baseline, which leaves 18–23 vCPU-hours a day
    for builds before any surplus charge.
  - Above that, Unlimited mode costs $0.04 per vCPU-hour [measured: EC2 price
    list published 2026-10-28; burst rules from docs:
    [burstable credits](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/burstable-credits-baseline-concepts.html)].
  - A partial run of the agent-coord suite used 258 CPU-seconds and was never
    throttled at 2 CPUs [measured: cgroup `cpu.stat` in an arm64 container;
    the run was stopped early by host disk pressure].
  - Node and Python project builds were **not run: host disk full**. Phase 1
    measures them on the real machine, using `CPUSurplusCreditBalance`.
- **CPU, today.** The backend spends 2.2–3.0 cores on avoidable SQLite and
  polling overhead [from docs: [capacity-planning.md](capacity-planning.md)].
  That adds $33.58–56.94 a month of surplus on t4g.xlarge. It would leave a
  fixed 4-vCPU machine with 1–1.8 cores for everything else.
- **Architecture: arm64.** Every Ribbon Field runtime has a linux-arm64 build:
  - Codex 0.165.0 and Claude Code 2.1.312 ran in an Ubuntu 24.04 arm64
    container [measured].
  - The Antigravity ACP server's linux-arm64 archive downloaded and passed its
    pinned SHA-256 check [measured].
  - Kimi and Muse installed from their arm64 installers [measured; their
    version checks were cut off by the disk stall].
  - Playwright 1.64 fetches Chrome for Testing 155 for linux-arm64 [measured:
    `playwright install --dry-run` and HTTP HEAD on the archive].
  - Gemini CLI lacks a prebuilt arm64 `node-pty` [from docs: npm metadata].
    It is not a Ribbon Field runtime.
  - The x86 fallback is t3.xlarge at $121.47 or m8i.xlarge at $154.53 a month
    on demand [measured: price list].

### Forge, honestly

Forge (Borrowed Machines) is closer than a new platform. It already starts
Firecracker guests on EC2 hosts with nested virtualization, keeps separate root
and workspace drives, includes OpenSSH and systemd in the guest image, and
has an authenticated Computer API [read in code: `forge/worker/src/vm_manager.py:284-340`,
`forge/infra/tf/modules/worker/main.tf:248-250`, `forge/infra/build-guest-image-v3.sh:92-111`].

Its durability contract is different from a personal machine. Lifetime and
idle expiry are capped at 86,400 seconds
[read in code: `forge/control/src/computer_models.py:18-19`]. Worker startup
kills Firecracker processes and deletes guest backing files
[read in code: `forge/worker/src/host_manager.py:152-159`,
`forge/worker/src/backing_manager.py:61-67`]. Guests are fixed at 1 vCPU and
512 MiB [read in code: `forge/worker/src/vm_manager.py:329-332`]. Sign-in admits
one bootstrap owner [read in code: `forge/control/src/auth_routes.py:154-160`].

| Work to make Forge host this machine | Engineer-days [assumed] |
| --- | ---: |
| Persistent lifetime and recovery after a host reboot | 5–10 |
| Owner SSH (reusing the guest's sshd) | 2–4 |
| Tailscale node inside the guest | 1–3 |
| A 4 vCPU / 16 GiB / 100 GiB machine class | 2–4 |
| Home directory that survives a rebuild | 3–7 |
| systemd services, Docker and a browser in the guest | 4–9 |
| Backups, host upgrades, monitoring, integrated acceptance | 11–22 |
| **Operational personal hosting** | **30–60** |
| More Google identities, public ingress, browser terminal, multi-tenant hardening | +10–25 |

Who benefits: more Google identities and a larger machine class serve Borrowed
Machines' own roadmap. Permanent machines do not: Forge's vision document
explicitly rejects one persistent VM per user [from docs:
`forge/control/docs/governed_execution_context-vision.md:92-108`], and its stable
Computer design excludes machine restore [from docs:
`forge/docs/designs/DESIGN-018-stable-worker-computer.md:39-40`]. A 16 GiB guest
also needs a 32 GiB host once the worker's own overhead is reserved [assumed:
2 vCPU and 4 GiB host reserve].

Plain EC2 gives the required persistence on its own: EBS volumes survive stop
and start, and termination retention is a per-volume setting [from docs:
[EC2 root volumes](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/RootDeviceStorage.html)].
Use EC2 now. Reconsider Forge as an optional execution backend once there are
repeat users and measured packing costs. The full estimate is in the Forge
evidence file listed under [Evidence files](#evidence-files).

## 2. Machine as code

**Recommendation: one Terraform root module for AWS resources, one idempotent
provisioning script on the instance, and systemd units. One command creates
or rebuilds everything.**

Terraform matches the neighbouring infrastructure: agent-coord-web uses
Terraform with encrypted S3 state, account restriction, and secrets whose
containers Terraform creates but whose values it never sees [from docs:
`agent-coord-web/docs/billing-service.md:78-83`].

Proposed layout (new files, names are proposals):

```text
deploy/hosted-machine/
  machine_spec.example.json     name, region, instance type, arch, disk sizes, tool list, owner SSH public key
  tool_manifest.json            pinned version and per-arch source for every tool
  terraform/                    VPC, subnet, security group, instance, data volume, IAM role,
                                secret containers, snapshot policy, recovery alarm
  provision_machine.sh          runs on the instance at first boot and on demand; idempotent
  systemd/ribbon-field.service
  systemd/ribbon-field-secrets.service
  systemd/ribbon-field-browser.service
  create_machine.sh             terraform apply + wait for health + print connection details
  rebuild_machine.sh            replace the instance, keep the data volume and secrets
  destroy_machine.sh            remove the instance; keeps data unless --delete-data is given twice
  store_agent_environment.sh    copy named API keys from the Mac's environment to Secrets Manager
  verify_machine.sh             the acceptance checks in this plan
```

`machine_spec.json` makes size, region, architecture and the tool list
configurable. `create_machine.sh --spec machine_spec.json` runs these steps in
order. Secret values never pass through Terraform:

1. **Containers.** `terraform apply -target` creates the IAM role, the
   security group and three empty Secrets Manager containers:
   `agent-environment`, `tailscale-enrollment` and `ssh-host-keys`.
2. **Seed from the Mac.** The owner's Mac is the trusted workstation.
   - It generates the machine's SSH host keys in a private temporary
     directory, stores the private keys with `put-secret-value` through stdin,
     and deletes the temporary copies.
   - It writes the host public key into `~/.ssh/known_hosts.d/ribbon-1` and
     writes an alias, `~/.ssh/config.d/ribbon-1`:
     `HostName ribbon-1.<tailnet>.ts.net`, `User ribbonfield`, the owner's
     `IdentityFile`, `StrictHostKeyChecking yes`, `ForwardAgent no`,
     `ForwardX11 no`, and `UseKeychain`/`AddKeysToAgent` so that `BatchMode`
     can unlock the key. A second alias, `ribbon-1-admin`, uses the image's
     `ubuntu` account, which keeps sudo. The owner's key is authorized for
     both accounts. Agents run as `ribbonfield` and hold no key.
   - It stores a **one-off, pre-approved** Tailscale auth key tagged
     `tag:ribbon-machine`. The owner creates the key in the admin console for
     each create or rebuild.
   - `store_agent_environment.sh` is optional at this point. An empty
     environment is a supported state.
3. **Launch.** A second `terraform apply` creates the instance from
   Canonical's Ubuntu 24.04 arm64 image (looked up through its public SSM
   parameter), the data volume, the snapshot policy and the recovery alarm.
   - IMDSv2 is required, and both volumes are encrypted.
   - Hop limit 1 only stops bridged containers from reaching instance
     metadata. It does not isolate processes on the host itself.
4. **Provision.** cloud-init installs a pinned Ribbon Field revision and runs
   `provision_machine.sh` as root. The script, its tools and their directories
   are owned by root, and it uses a fixed `PATH`. It never runs or sources
   anything from a path `ribbonfield` can write to. It then:
   - Blocks instance metadata for every user except root: an `iptables` owner
     rule that rejects `169.254.169.254` unless the UID is 0, made persistent
     [from docs: [restricting IMDS by OS user](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-metadata-limiting-access.html)].
     Agents therefore cannot use the instance role at all. They receive only
     the API environment.
   - Creates `ribbonfield` with no sudo rights and outside the `docker` group.
     It formats the data volume on first use and mounts it at
     `/srv/ribbon-field`.
   - Bind-mounts the persistent paths in the next table.
   - Installs apt packages, Node LTS 22.12 or later (with `libatomic1`), gh,
     Tailscale, the AWS CLI, the provider CLIs from `tool_manifest.json`,
     Playwright MCP, Chromium and its libraries
     (`playwright install-deps chromium`).
   - Installs the Ribbon Field CLI with its Codex and Claude plugins, and the
     Web Push dependencies in a virtual environment.
   - Writes `/etc/ribbon-field/machine.json` (`kind`, `name`, `id`), owned by
     root and read-only to the service.
   - Restores the SSH host keys and runs `tailscale up` with the one-off key.
     The key is single-use, so it is worthless after the join, and a rebuild
     needs a fresh one.
   - Configures the Serve route as root: `tailscale serve --bg --https=443
     http://127.0.0.1:8765`. `ribbonfield` is **not** a Tailscale operator
     (see [connectivity](#3-connectivity)).
   - Enables the systemd units, with `LimitCORE=0`. No swap is configured.
5. **Run.** `ribbon-field.service` runs `agent-coord ui --no-browser --port 8765`
   as `ribbonfield`.
   - It uses `Restart=always`, and `KillSignal=SIGINT` until the CLI handles
     SIGTERM (finding L4).
   - It starts after the network, tailscaled, the secrets unit and the data
     mount. systemd restarts it after a crash and starts it at boot.
   - Ribbon Field adopts the root-owned Serve route as its remote origin
     without trying to manage it. That needs a small change to
     `remote_access.py` (phase 0).
6. **Check.** `create_machine.sh` waits until `/api/browser/config` over the
   new SSH alias reports `"machine": {"kind": "hosted", …}`. The server
   change that reports this is in phase 1.
7. Unattended security updates are on, with automatic reboot off, because a
   reboot interrupts running turns. The owner chooses when to reboot.
8. A CloudWatch status-check alarm with the EC2 recover action restarts the
   instance on failed hardware. T4g and M8g support it [from docs:
   [EC2 recovery](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/cloudwatch-recovery.html)].

### What survives

| Path | Volume | Reboot | Rebuild |
| --- | --- | --- | --- |
| `~/.local/state/agent-coord/` (conversations, messages, checkpoints, pairing hashes, push keys) | data | yes | yes |
| `~/work/` (repositories) | data | yes | yes |
| `~/.codex/sessions/`, `~/.codex/archived_sessions/`, `~/.claude/projects/` (Codex and Claude native sessions) | data | yes | yes |
| `~/.kimi-code/sessions/` [read in code: `kimi_setup.py:22-23`, `kimi_usage.py:152-178`] and `~/.local/share/muse/sessions/` [read in code: `muse_usage.py:200-208`] (Kimi and Muse native sessions) | data | yes | yes |
| Antigravity profile and native history, under `state/providers/antigravity/profile/`, except the token | data | yes | yes |
| `~/.codex/auth.json`, `~/.claude/.credentials.json`, `~/.kimi-code/credentials/`, `~/.config/muse/auth.json`, `~/.config/gh/` (provider logins) | root | yes | **no: sign in again** |
| Antigravity token `acp_token.json`: today inside that profile; it must move to a credentials directory on root (finding L11) | root after the move | yes | **no: sign in again** |
| Browser profile `~/.local/share/ribbon-field-browser/` | root | yes | **no: log in once again** |
| Tools, OS, plugins, `/etc/ribbon-field/machine.json` | root | yes | recreated from the manifest |
| API keys | Secrets Manager; tmpfs while running | refetched at boot | refetched |
| Tailscale node state, SSH host keys | root; host keys also in Secrets Manager | yes | same host keys; new node with the same name |
| Running processes, in-flight turns, open event streams | memory | **no** | **no** |

After a restart, saved conversations return. In the Linux probe, a
registered thread and a saved app conversation survived an abrupt SIGTERM and
a restart [measured: synthetic data]. Two things were not observed: resuming
an authenticated provider conversation, and what happens to a turn that was
running. The expected result is that a running turn is shown as interrupted
[assumed]. Acceptance step 8 settles it. A rebuild deliberately drops provider
logins and the browser profile, so snapshots of the data volume never contain
them.

The persistent-path list is an [assumed] design. Acceptance step 9 must prove
that every enabled provider resumes the **same native session** from these
directories alone. If one does not, do not move its whole configuration
directory onto the data volume. Its credential file would then be captured in
every daily snapshot, and deleting it at rebuild would not remove the old
copies. Instead, find that CLI's supported way to store credentials somewhere
else, or ask the owner to accept credential-bearing snapshots with a defined
retention period.

## 3. Connectivity

**Recommendation: Tailscale for every path, with no public listener. Keep the
loopback-only binding.**

The server only binds to loopback [read in code: `ui.py:256-266`]. Its
privilege model depends on that binding. A request whose `Host` is the loopback
address and the bound port, and which carries no forwarding headers, gets
local administrative rights [read in code: `ui.py:510-519`]. A request for the
configured Tailscale origin needs a paired-device cookie [read in code:
`ui.py:500-508`]. Binding to any other address would hand local privileges to
the network. Nothing in this plan needs that.

| Client | Path | Privilege | Code change |
| --- | --- | --- | --- |
| iPhone app or Home Screen web app | Tailscale Serve HTTPS at `https://ribbon-1.<tailnet>.ts.net`, proxied to loopback | Paired device (30-day cookie) | None. The iOS app accepts any HTTPS origin [read in code: `mobile/ios/RibbonField/ConnectionPolicy.swift:9`]. |
| Mac desktop | `ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 ribbon-1` over the tailnet | Local (owner) | Desktop only (section 4). The local port must equal the server port, because the server compares the `Host` port [read in code: `ui.py:513-519`]. |
| Owner's terminal | `ssh ribbon-1` (OpenSSH, key only, reachable only over the tailnet) | Shell as `ribbonfield` | None |
| Administration | `ssh ribbon-1-admin` (the `ubuntu` account, sudo), or `aws ssm start-session --target <instance>` as break-glass | Root through sudo | None |

The phone pairs with the machine directly. The owner creates a pairing link in
the desktop's Remote access panel while the desktop is connected to the hosted
machine, then opens it on the phone. This is the existing one-time link flow,
with five-minute links and 30-day device grants [from docs:
[remote-access.md](remote-access.md)]. On cellular, the Tailscale iOS app's
VPN On Demand rule (Cellular, Always) reconnects automatically [from docs:
[Tailscale iOS On Demand](https://tailscale.com/docs/features/client/ios-vpn-on-demand)].

OpenSSH is preferred over Tailscale SSH for the desktop's port forward. The
standard client and keys behave predictably for an unattended forward.
Tailscale SSH's check mode periodically asks for browser re-authentication
[assumed; confirm if Tailscale SSH is chosen later]. `create_machine.sh` writes
the SSH alias and pins the host key before the first connection (section 2,
step 2), so the first connection needs no interactive trust prompt.

**Serve route ownership.** Root creates the route, and `ribbonfield` is not a
Tailscale operator. An operator can rewrite Serve. An agent could then replace
the HTTPS proxy on port 443 with a raw TCP forward to `127.0.0.1:8765`. A
tailnet peer would send `Host: 127.0.0.1:8765` without forwarding headers and
receive local privileges without pairing, because the server decides "local"
from the `Host` header and the absence of forwarding headers, not from the
connecting process [read in code: `ui.py:497-516`; from docs:
[Serve TCP forwarding](https://tailscale.com/docs/reference/tailscale-cli/serve#use-a-tcp-forwarder),
[Linux operator](https://tailscale.com/docs/reference/troubleshooting/linux/linux-operator-permission)].
Ribbon Field today enables and disables its own route
[read in code: `remote_access.py:93-97`, `:196-230`]. Phase 0 adds a mode in
which it verifies and adopts a route created outside Ribbon Field, and never
changes it. Funnel stays disabled for the node in the tailnet policy.

**Trust boundary, stated plainly.** On this machine, each of these has the
full authority of the `ribbonfield` account and its credentials:

- every paired phone,
- every agent,
- every process that can reach `127.0.0.1:8765`, which includes any local Mac
  process while the desktop's SSH forward is open.

A paired device cannot administer pairing directly. It can, however, start a
full-access session or change a session's permissions
[read in code: `ui.py:711-722`, `:1065-1072`; `codex_app_server.py:1264-1269`].
A local process can create pairing invitations and answer approvals
[read in code: `ui.py:522`, `:1090-1096`]. Provider approval prompts are
therefore a safeguard for the owner, not a boundary between people. That is
acceptable for one owner. Reducing a phone's or an agent's authority would
need permission limits enforced by the server, and this plan does not build
them.

**Tailnet policy is a security requirement.** Tailscale's default policy lets
every device reach every other device. An agent on this machine, possibly
following injected instructions, could then reach the Mac's SSH, its Ribbon
Field route and other tailnet services.

Grants are additive: a narrower rule does not override a broader one, so the
default allow-all rule must be replaced rather than supplemented [from docs:
[grant syntax](https://tailscale.com/docs/reference/syntax/grants)]. The owner
reviews the whole effective policy, because this changes what every device on
the tailnet can reach. The policy needs at least the following, plus whatever
device-to-device access the owner chooses to keep:

```json
{
  "tagOwners": {"tag:ribbon-machine": ["autogroup:admin"]},
  "grants": [
    {"src": ["autogroup:admin"], "dst": ["tag:ribbon-machine"], "ip": ["tcp:22", "tcp:443"]}
  ],
  "tests": [
    {"src": "<owner login>", "accept": ["tag:ribbon-machine:22", "tag:ribbon-machine:443"],
     "deny": ["tag:ribbon-machine:8765", "tag:ribbon-machine:9222"]},
    {"src": "tag:ribbon-machine", "deny": ["<owner Mac>:22", "<owner Mac>:443", "<owner phone>:443"]}
  ]
}
```

The `tests` block makes Tailscale reject a policy edit that would break these
assertions [from docs:
[policy tests](https://tailscale.com/docs/reference/syntax/policy-file)].
Subnet routers and exit nodes need the same denial. Acceptance step 1 then
checks the policy live, against a known listening test service.

Public HTTPS is rejected for this phase. It would put an internet-facing
listener on a machine that holds API keys and a shell. It would also need
public-client authentication that the server does not have. It returns as a
gateway design in [section 7](#7-later-use-by-other-people). Tailscale's
Personal plan is free for non-commercial use, up to six users [from docs:
[Tailscale pricing](https://tailscale.com/pricing)].

## 4. Desktop

**Recommendation: the server reports who it is, the desktop shows that
identity, and hosted mode turns off features that assume the client and
server share a file system.**

Today one flag, `remote`, means two things: the client is a paired device, and
the client does not share the server's file system [read in code:
`ui.py:522`, `web/app.js:3538-3549`]. A hosted desktop has owner privileges but
does not share the file system. The plan separates those two meanings.

### Server

`/api/browser/config` gains this object:

```json
"machine": {"kind": "hosted", "name": "ribbon-1", "id": "7a1c…"}
```

The server reads it from `/etc/ribbon-field/machine.json`, which provisioning
writes and root owns. When the file is absent, the server reports
`{"kind": "local", "name": <host name>}`. No API can change it, so a client
setting can never relabel a server.

When `kind` is `hosted`, no client shares its file system. The web UI then
uses the server-side folder picker that remote clients already use
[read in code: `web/folders.js:253`]. It hides local editor, Finder and
file-URL actions. Messages that say "on this Mac" become machine-neutral, for
example `provider_limits.py:230` and `ui.py:555`.

### Desktop app

- A **Connection** menu lists "This Mac", each configured hosted machine, and
  "Add hosted machine…" (an SSH host alias and a port). Only one backend runs
  at a time. Switching to a hosted machine stops the local `backend.py`; if it
  has active turns, the desktop asks first.
- In hosted mode the app launches `/usr/bin/ssh` with `-N`, `ExitOnForwardFailure`,
  `ServerAliveInterval` and `BatchMode`. It restarts the forward after an exit
  or a wake from sleep. `serverURL` stays `http://127.0.0.1:<port>/`, so the
  existing navigation guard `isLocal()` is unchanged [read in code:
  `desktop/macos/App.swift:700-703`]. Readiness comes from polling
  `/api/browser/config` instead of `backend.py`'s ready line
  [read in code: `desktop/macos/App.swift:113-120`].
- The desktop refuses a hosted connection whose server reports
  `kind: local`. This catches a forward that lands on a different local server,
  so the label cannot be wrong.
- **Native file capabilities depend on the connection mode, not on the web
  UI.** Today the bridge admits any message from the server's origin
  [read in code: `desktop/macos/App.swift:561-564`, `:719-724`]. `openFile`
  accepts absolute Mac paths outside the workspace [read in code:
  `desktop/macos/WorkspaceFiles.swift:12-20`]. File providers are always
  constructed with `local: true` [read in code: `App.swift:153`, `:358`].
  Hiding buttons in a hosted page would not stop hosted content from calling
  these actions. In hosted mode:
  - the native side rejects `openFile`, `fileEditor`, `chooseFiles` and
    `dropFiles` before dispatch;
  - it constructs file providers with `local: false`;
  - it audits menu, drag, open-panel and file-URL paths the same way.
  Server `machine` data is identity for the label, never authorization to
  access the Mac.
- **Client state is namespaced by machine.** Both modes use the same
  `127.0.0.1` origin, and today's folder and project keys contain no machine
  identity [read in code: `web/folders.js:19-35`, `:70-79`;
  `web/app.js:3543-3548`; `App.swift:904-913`]. Local projects, drafts and
  window state could therefore be restored into the hosted backend. Phase 2:
  - keys folder, navigation, draft and window state by `machine.id`;
  - keeps appearance preferences global;
  - closes event streams and drops cached CSRF tokens on every switch.
- **Label.** Every window's subtitle shows "This Mac" or "Hosted · ribbon-1".
  The name comes from the connected server's `machine.name`. The web sidebar
  shows the same text at its foot in muted ink, for the phone as well. This is
  a new attribute of an existing object (the workspace), placed at the lowest
  emphasis that stays visible, in sentence case, with no eyebrow label
  [from docs: `DESIGN.md` "Do's and Don'ts", "Typography"]. On the desktop,
  clicking it opens the Connection menu.

### Local-only features in hosted mode

[read in code: `desktop/macos/FILE_BROWSER.md:39-47`; bridge actions in
`desktop/macos/App.swift:858-1056`]

| Feature | Hosted behaviour |
| --- | --- |
| Open project (`chooseFiles`, NSOpenPanel) | Uses the server folder picker |
| Insert local paths, drop files (`dropFiles`) | Turned off, with an explanation. Uploading is not in scope. |
| Open file in local editor (`openFile`, `fileEditor`) | Turned off. The file browser's text preview still works over the tunnel. |
| Finder reveal, file-URL drags from the file browser | Turned off |
| File browser listing and preview | Keeps working over the tunnel, if `BackendWorkspaceFiles` uses `serverURL` [assumed; verify in phase 2] |
| Open log (`openLog`) | Turned off. The log is on the machine (`journalctl -u ribbon-field`). |
| Desktop notifications | Keep working over the tunnel while the Mac is awake |
| Window, tab and detach features | Unchanged |

The desktop bridge and the hosted server's web assets can run different
versions. The config response should carry a bridge protocol version, and the
desktop should show a mismatch instead of failing quietly [assumed].

### Provider sign-in status

The Usage view already reports `sign_in_required` for Codex, Claude and
Antigravity. Kimi and Muse rows report `unsupported` whatever their login
state, because only three quota collectors exist [read in code:
`provider_limits.py:250-253`]. Phase 2 adds a sign-in check for Kimi and Muse
that never reads credential contents: the official CLI's status command where
one exists, otherwise a credential file that exists and is not empty. That
check stays separate from quota support. In hosted mode the rows show the
machine name and the exact SSH command to sign in. No new surface is needed.
Acceptance relies on a real turn for each provider, not on these
indicators.

## 5. Credentials

**Recommendation: provider logins stay with each official CLI on the root
volume. API keys live in Secrets Manager and reach processes through the
instance role and tmpfs. Nothing secret is placed in the image, in logs, in
Ribbon Field state, in data-volume snapshots, or in git.**

Every agent runs as `ribbonfield`, so every agent can read every provider
login and API key on this list. That matches the Mac today, where agents run
as the owner. The machine's own identities are different: the instance role,
the Tailscale enrollment key and the SSH host keys are reachable only by root.
The controls are least privilege per credential, rotation, root-only metadata,
and keeping credentials out of persistent copies. They do not isolate agents
from each other.

| Credential | Source of truth | How it reaches the machine | Where it rests | Readers | Rotation and revocation | On rebuild |
| --- | --- | --- | --- | --- | --- | --- |
| ChatGPT login for Codex | OpenAI account | `codex login --device-auth` over SSH. Device-code login must first be enabled in ChatGPT security settings [from docs: [Codex auth](https://learn.chatgpt.com/docs/auth)]. Set `cli_auth_credentials_store = "file"`. | `~/.codex/auth.json`, 0600, root volume | `ribbonfield` processes, root | `codex logout`. Codex refreshes tokens while in use [from docs]. A fixed refresh-token lifetime is not documented. | Lost; sign in again |
| Claude Code login | Anthropic account | `claude auth login` over SSH; open the URL elsewhere and paste the code [from docs: [Claude Code authentication](https://code.claude.com/docs/en/authentication)] | `~/.claude/.credentials.json`, 0600, root volume | same | `claude auth logout`. Fallback: `claude setup-token` makes a one-year token for `CLAUDE_CODE_OAUTH_TOKEN`, which Ribbon Field already reads [read in code: `provider_limits.py:212-214`]. Use it only if refresh proves unreliable. | Lost; sign in again |
| Antigravity login | Google account | `agent-coord antigravity login` (ACP `oauth-personal`; the URL is printed for another device) [read in code: `antigravity_setup.py:118`] | Today `<state>/providers/antigravity/profile/antigravity-acp/acp_token.json` [read in code: `antigravity_setup.py:35`, `:118`]. That is inside Ribbon Field state, on the data volume, and would land in snapshots. **Move it to a credentials directory on the root volume** (linux-readiness item). | same | Revoke at the Google account's third-party access page [assumed] | Lost after the move; sign in again |
| Kimi login | Moonshot account | `kimi login` device flow [from docs: [Kimi command](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/kimi-command.html)] | `~/.kimi-code/credentials/*.json`, 0600, directory 0700 | same | Re-login; refresh uses cross-process locking [from docs] | Lost; sign in again |
| Muse login | Meta account | `muse login` (page and code) [from docs: [Muse auth](https://dev.meta.ai/docs/muse-code/auth)] | `~/.config/muse/auth.json`, root volume. Ribbon Field's per-thread `XDG_CONFIG_HOME` links to it rather than copying it [read in code: `muse_setup.py:61-90`]. | same | Re-login | Lost; sign in again |
| API keys: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GCP_API_KEY`, `GAI_API_KEY`, `AGENT_MAIL_API_KEY`, `ELASTIC_*` | Each provider's console. Today they are plaintext exports in the Mac's shell profiles. | `store_agent_environment.sh` on the Mac reads the named variables and writes one JSON secret, `ribbon-field/ribbon-1/agent-environment`, through stdin. It never echoes or writes a file. At boot, `ribbon-field-secrets.service` (root) fetches it, checks each name against an allowlist, and atomically writes two files into the root-owned directory `/run/ribbon-field/`, without following links. One is for systemd `EnvironmentFile=`. The other is a generated, quoted `export` file that login shells source. The root unit never sources either file. | Secrets Manager (KMS-encrypted). On the machine: tmpfs, mode 0640, `root:ribbonfield`. Not on disk, no swap, no core dumps. | `ribbonfield` processes and root. Agents cannot read Secrets Manager directly, because instance metadata is blocked for them. Claude threads do not inherit `ANTHROPIC_API_KEY`, because Ribbon Field removes it [read in code: `claude_code.py:59`]. | Rotate at the provider, rerun `store_agent_environment.sh`, then restart both units. Revoke at the provider. | Returns automatically |
| GitHub | GitHub account | Recommended: a fine-grained personal access token limited to the repositories the agents work on, stored in the same secret as `GH_TOKEN`, with `gh auth setup-git`. Alternative: `gh auth login` device flow. | Secrets Manager and tmpfs (token). With the alternative: `~/.config/gh/hosts.yml`, plaintext, root volume | same | Token expiry of one year or less. Revoke in GitHub settings. | Token returns; the device-flow login is lost |
| AWS | Owner's AWS account | Do not copy the Mac's AWS credentials. The instance role is the only AWS identity on the machine. | Instance metadata (IMDSv2) | **root only.** An `iptables` owner rule blocks metadata for every other UID; it is tested as `ribbonfield` after a reboot. | Role policy: `GetSecretValue` on the three exact secret ARNs, plus SSM Session Manager core. Nothing else. | New instance, same role |
| Tailscale node | Owner's tailnet | A **one-off**, pre-approved, tagged auth key created by the owner for each create or rebuild, stored in `ribbon-field/ribbon-1/tailscale-enrollment` [from docs: [auth keys](https://tailscale.com/docs/features/access-control/auth-keys)] | Node key in `/var/lib/tailscale`, root only, root volume. The used auth key is worthless. | root | Remove the node to revoke. Tag ownership is limited to admins. | New node. Delete the old node first, so the machine keeps its name and the phone's saved address and pairing stay valid. |
| SSH host keys | Generated on the owner's Mac before launch (section 2, step 2) | Stored in `ribbon-field/ribbon-1/ssh-host-keys`; the public key is pinned in the Mac's `known_hosts` | `/etc/ssh`, root only | root (metadata is blocked for agents) | Generate new keys on the Mac, update the secret and the pin, then rebuild | Restored, so the pin stays valid |
| Owner SSH key | Mac | Public key in `machine_spec.json` | `~ribbonfield/.ssh/authorized_keys` (not secret) | — | Edit the spec and reprovision | Restored |
| Phone pairing grants | Ribbon Field | Pairing link from the desktop's Remote access panel | Hashes only, in `state.sqlite3.remote.json` on the data volume [from docs: [remote-access.md](remote-access.md)] | `ribbonfield` processes | Revoke in Remote access. Revoking only deletes the record [read in code: `remote_access.py:302-307`], so restoring an **older snapshot** revives revoked grants. A restore therefore clears all grants and push subscriptions, and devices must pair again. | Kept when the current data volume is reattached |
| Web Push signing key and subscriptions | Ribbon Field | Generated on first use | `state.sqlite3.push.sqlite3` on the data volume [from docs: [remote-access.md](remote-access.md)] | same | Disable and re-enable phone notifications | Kept. This key can only send generic alerts to subscribed phones. |
| Browser session cookies | Each website | The owner logs in once through the viewer ([section 6](#6-browser-use)) | Browser profile, 0700, root volume | `ribbonfield` processes, and anything that can reach the loopback CDP port | Log out at the site or delete the profile | Lost; log in once again (an open question for the owner) |

**Keychain on Linux.** No new store is needed:

- Jev already writes a 0600 key file on non-macOS systems [read in code:
  `jev_settings.py:184-189`].
- Claude limits read `~/.claude/.credentials.json` before trying `security`,
  which runs only on macOS [read in code: `provider_limits.py:215-227`].

Two items remain. Jev's key file sits in the state directory
(`jev.json.key`) [read in code: `jev_settings.py:20`, `:129`], and Jev stays
off on the hosted machine. The Antigravity token must move out of state, as
the table says.

**Secret leak smoke check.** `verify_machine.sh --secrets` loads the current
values from tmpfs. It searches the data volume, the journal and `/var/log`,
and prints only a match count and file paths, never a value. Zero matches is
required, but it is not proof of non-persistence. The search misses encoded
copies, rotated old values, provider tokens that are not in the environment
file, and existing snapshots. Agents' tools inherit the environment, so a tool
can still write a key somewhere.

Phase 3 adds two stronger checks:

- synthetic canary secrets, put through serialization, logging and rotation;
- a check that a restored snapshot contains no canary.

Asking an agent not to print a key is not a storage control.

## 6. Browser use

**Recommendation: (a) one long-lived Chromium with a persistent profile,
driven over CDP by Playwright MCP. The owner completes a login or 2FA through
a DevTools screencast forwarded over SSH.**

The mechanism was measured on macOS arm64, not Linux. Two Docker attempts
were stopped because this Mac's disk kept filling, so a Linux run is
**not run: host disk full**, and phase 1 repeats the check on the machine.
The macOS run used the installed Chrome 156 headless, a new temporary profile,
and `--use-mock-keychain`. It never touched the owner's profile [measured,
two runs]:

- Playwright MCP 0.0.83 (`--cdp-endpoint`) and chrome-devtools-mcp 1.12.0
  (`--browserUrl`) attached to the same Chrome. A cookie and a localStorage
  value written through Playwright were visible through chrome-devtools-mcp.
- After Chrome was stopped with SIGTERM and restarted on the same profile,
  both values were still visible through both servers.
- A second Chrome on the same profile exited with code 21:
  `Failed to create a ProcessSingleton for your profile directory`.
- `Page.startScreencast` delivered frames, and `Input.dispatchMouseEvent` plus
  `Input.dispatchKeyEvent` typed "2fa" into a focused field. These are the
  primitives `chrome://inspect` uses.
- Cold start was 1.9–2.4 s; a restart on an existing profile took 0.6–0.8 s.
  MCP servers reached `tools/list` in 0.3–1.2 s.
- Memory: the Chrome process tree summed 0.8–1.1 GiB RSS (an overstatement,
  because macOS counts shared pages in each process). Playwright MCP used
  127–129 MiB and chrome-devtools-mcp 161–170 MiB.
- chrome-devtools-mcp needs Node 22.12 or later, and sends usage statistics
  unless it is started with `--no-usage-statistics --no-performance-crux`
  [measured].

| Option | Control | Persistent login | Cost | Data handling | Verdict |
| --- | --- | --- | --- | --- | --- |
| (a) Chromium on the machine over CDP, with Playwright MCP or chrome-devtools-mcp | DOM and accessibility tree | Profile directory on the root volume | None beyond the machine (about 1 GiB of memory while pages are open) | Cookies never leave the machine | **Chosen** |
| (b) Hosted browser service | CDP or service API | Saved contexts or profiles | $0.02–0.12 per browser-hour, plus proxies [from docs: [Browserbase](https://www.browserbase.com/pricing), [Steel](https://docs.steel.dev/overview/pricinglimits), [Browser Use](https://browser-use.com/cloud-browser), [AgentCore](https://aws.amazon.com/bedrock/agentcore/pricing/)] | A third party holds the authenticated profile. Recording is often on by default. Some services' zero-data-retention modes turn profiles off. | Not needed. Revisit if sites block the AWS address or concurrency grows. |
| (c) Full Linux desktop with pixel-based computer use | Screenshots and clicks | Profile directory | Desktop stack memory, plus far more model tokens per step | Same as (a) | Rejected |

Pixel control is still the weak spot. The best full OSWorld 2.1 result is
44.4% binary task success (updated 2026-10-15) [from docs:
[OSWorld leaderboard data](https://osworld-v2.xlang.ai/static/data/leaderboard/official-results.json)].
On WebArena, DOM-based web agents report 74.3% (verified) to 86.1% (unverified)
[from docs: WebArena leaderboard sheet]. These benchmarks differ, so the
comparison is directional only.

Design:

- `ribbon-field-browser.service` runs one Chromium as `ribbonfield`:
  - with `--headless=new`, `--remote-debugging-address=127.0.0.1`,
    `--remote-debugging-port=9222` and the absolute profile path
    `--user-data-dir=/home/ribbonfield/.local/share/ribbon-field-browser`;
  - with its sandbox enabled, never `--no-sandbox`. Ubuntu 24.04 restricts
    unprivileged user namespaces, so Chromium may need an AppArmor profile to
    start sandboxed [assumed; phase 1 verifies];
  - from a pinned executable. Its libraries are installed as root with
    `playwright install-deps chromium` [from docs:
    [Playwright browsers](https://playwright.dev/docs/browsers)].
- Agents attach through Playwright MCP's `--cdp-endpoint http://127.0.0.1:9222`,
  or chrome-devtools-mcp's `--browserUrl` (with `--no-usage-statistics
  --no-performance-crux`). They share the one profile. A second Chromium
  cannot open the same profile directory, so attaching avoids the profile-lock
  refusal measured above.
- Each provider that is expected to browse gets the MCP server in its own
  configuration: Codex, Claude, Kimi, Muse and Antigravity, if enabled. Each
  is shown to work in acceptance step 4.
- **Viewer.** The owner runs `ssh -L 9222:127.0.0.1:9222 ribbon-1`, opens
  `chrome://inspect` on the Mac, and uses the target's screencast to type a
  password or a 2FA code once. This needs no Xvfb or VNC. Chrome's advertised
  DevTools frontend loads from `chrome-devtools-frontend.appspot.com`, so the
  Mac needs internet access while viewing [measured].
- **Stale lock.** Chrome can leave a stale `SingletonLock` named after the
  host name [measured]. The unit's start step removes `Singleton*` files only
  when no Chromium process has the profile open [assumed design].
- Codex's `computer-use` and `node_repl` servers are macOS app bundles and are
  left out of the machine's Codex configuration. On the Mac they accounted for
  58 `cua_node` processes and 30 nested app-servers [from docs:
  [capacity-planning.md](capacity-planning.md)].
- **CDP exposure.** The CDP port gives full control of every logged-in site.
  It stays on loopback and is never served on the tailnet. Because root owns
  the Serve route, an agent cannot republish it. Acceptance step 1 checks
  this through the actual Serve route, not just by scanning ports.

Shared-browser caveat: several agents in one profile can disturb each other's
tabs. That is acceptable for one owner. Per-agent browser contexts are a later
improvement if it bites [assumed].

## 7. Later use by other people

Design these interfaces now. Build none of them in this pass.

**Gate.** The subscription-terms question in
[standalone-product.md](standalone-product.md) (lines 59–61) blocks other
users. It does not block the owner's personal use.

| Provider | Owner's own machine | Provisioning machines for customers |
| --- | --- | --- |
| OpenAI | Headless login is documented for the official CLI | App-server auth docs exclude commercial hosted services and point to the Sign in with ChatGPT partner path [from docs: [app-server auth](https://learn.chatgpt.com/docs/app-server#auth-endpoints)] |
| Anthropic | Supported login flows | Allowed: preinstalling the **unmodified** binary, with each user authenticating and paying under their own agreement, and without intermediating credentials [from docs: [legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)] |
| Google Antigravity | Official headless login | A Google forum answer (2026-10-08, explicitly not a legal interpretation) says orchestrating Antigravity under a consumer login through other agents is unsupported and can trigger enforcement [from docs: [forum](https://discuss.ai.google.dev/t/is-google-ai-pro-antigravity-cli-automation-with-codex-permitted/187241)] |

The Antigravity answer applies to the owner too: see the open questions.

| Interface | Shape |
| --- | --- |
| Machine record | `machine_id`, `owner_subject` (Cognito `sub`), `name`, `state` (requested, provisioning, running, stopped, rebuilding, deleting, deleted, failed), `region`, `instance_type`, `arch`, `aws_account_id`, `instance_id`, `data_volume_id`, `connection` (`{"transport": "tailscale" or "gateway", "hostname": …}`), `software_revision`, `tool_manifest_revision`, `created_at`, `updated_at`, `last_health_at`. The server's `machine` identity is a projection of this record (`id`, `name`, `kind`). |
| Machine specification | The `machine_spec.json` used now, unchanged |
| Provisioning interface | `create(spec) → machine_id`, `describe(id)`, `rebuild(id)`, `stop(id)`, `start(id)`, `destroy(id, retain_data)`, `rotate_credentials(id)`. Today: the owner's local scripts. Later: a worker in a separate AWS account, so customer code never holds provisioning credentials [from docs: [standalone-product.md](standalone-product.md)]. |
| Identity | Google as a federated identity provider on the existing Cognito user pool. Identity stays the immutable Cognito `sub` [from docs: `agent-coord-web/docs/billing-service.md:63`]. Decide how to link Google and email/password accounts that share an email. |
| Allowlist | A Cognito pre-sign-up and pre-authentication trigger checks an allowlist table keyed by normalized email, for invited users only |
| Entitlement | `GET /v1/access` [from docs: `agent-coord-web/docs/billing-service.md:95`] gains a `hosted_machine` capability |
| Connectivity | A per-user tailnet does not suit most users. Later the machine opens an outbound connection to a Ribbon Field gateway, which enforces Cognito sign-in plus device pairing. The server keeps its loopback binding and treats gateway requests as remote. |
| Desktop connection kinds | `local` and `ssh` now; `gateway` later, with a native Cognito OAuth client |

## 8. Cost

**Recommendation: t4g.xlarge, $118.76 a month on demand.** Prices are for
us-east-1, Linux, 730 hours a month, from the AWS price list published
2026-10-28 [measured: public price-list files; us-west-2 instance prices are
identical]. They assume phase 0 is done.

| Line item | t4g.large (smaller) | **t4g.xlarge (recommended)** | m8g.xlarge (larger, fixed) |
| --- | ---: | ---: | ---: |
| Instance, on demand | 49.06 | 98.11 | 131.05 |
| Unlimited surplus at 0.65–0.85 vCPU steady demand | 1.46–7.30, plus every build | 0.00, with 18–23 vCPU-hours a day of headroom | — |
| EBS gp3, 40 GB root + 120 GB data, $0.08/GB-month | 12.80 | 12.80 | 12.80 |
| Data-volume snapshots, about 60 GB at $0.05 | 3.00 | 3.00 | 3.00 |
| Public IPv4, $0.005/hour | 3.65 | 3.65 | 3.65 |
| Secrets Manager, 3 secrets at $0.40 | 1.20 | 1.20 | 1.20 |
| Internet egress, ≤100 GB free | 0.00 | 0.00 | 0.00 |
| CloudWatch recovery alarm (free tier) | 0.00 | 0.00 | 0.00 |
| Tailscale Personal (free for non-commercial use) [from docs] | 0.00 | 0.00 | 0.00 |
| **Total, on demand** | **71.17–77.01** | **118.76** | **151.70** |
| Total, 1-year Compute Savings Plan | 57.52–63.36 | 91.39 | 116.93 |

The smaller option has 8 GiB, which leaves about 4 GB for builds and the
browser [from docs: [capacity-planning.md](capacity-planning.md)], and every
build is billed as surplus. The larger option is the choice if phase 0's CPU
fix slips.

**Before phase 0, the defects cost more than the machine.**

- **Egress.** The thread list is refetched after streamed events, at
  0.5–1.1 MB/s with three windows open [from docs:
  [capacity-planning.md](capacity-planning.md)]. That is 1.3–2.9 TB a month,
  costing **$108–252** at $0.09/GB [measured: price list].
- **Traffic through Tailscale is still AWS internet egress.** This is an
  inference, because no AWS or Tailscale page states it outright [assumed].
- **CPU.** The backend CPU defect adds $33.58–56.94 a month.
- **Total.** t4g.xlarge would cost about $260–428 a month.
- **After the fix,** about 25–60 GB a month stays within the free 100 GB
  [assumed].

**Each additional user** (one machine each, t4g.xlarge) adds about **$123.99
on demand or $96.62 with a Savings Plan**. That covers:

- the machine rows above;
- about 50 GB of egress ($4.50), since the account's free 100 GB is already
  used;
- CloudWatch beyond the free tier ($0.73).

Not included:

- Tailscale: commercial use falls outside the free Personal plan. Standard is
  $8 per user a month [from docs], or use the gateway in section 7.
- The shared account, provisioning and gateway services.
- Support time.

At the $99 price proposed in [standalone-product.md](standalone-product.md),
an always-on 16 GiB machine loses money on infrastructure alone. The
8 GiB machine ($71–77) fits under that price, but leaves a thin margin. This
supports the open pricing question; it does not settle it.

## Linux readiness

**Ribbon Field's server runs headless on Ubuntu 24.04 arm64 without the Mac
app or any provider CLI.** The work list is small. The larger preconditions
are the capacity fixes, which apply on macOS too.

What was run: the committed tree at `74a89de`, exported with `git archive`
into a clean `ubuntu:24.04` arm64 container. It used Python 3.12.3 and git
2.43.0, with no host mounts and no host environment [measured]. The full suite
was interrupted twice by host disk pressure, after 160 and then 114 cases. The
complete Linux suite result therefore comes from CI. The latest Ubuntu 24.04
job on main (x86_64, Python 3.10, Node 22) ran 1,561 tests: OK, 11 skipped, at
commit `d341dae` on 2026-10-26. The last five runs on main all passed
[measured: GitHub Actions API]. CI does not cover arm64, real provider turns,
the systemd service, Tailscale on Linux, authenticated resume, or in-flight
turn recovery.

| # | Finding | Evidence | Fix | Required for hosted | Estimate [assumed] |
| --- | --- | --- | --- | --- | ---: |
| L1 | The server starts, binds `127.0.0.1:8765`, and serves the root page and all 82 referenced assets. Config, threads, sessions, workspaces, provider usage and the SSE stream (JSON frames) work. | [measured] HTTP probe in the container | — | — | 0 |
| L2 | With no provider installed, model lists and session creation return clear 400 errors and the server stays up. Muse creates lazily and fails on first send with the same error. | [measured] | — | — | 0 |
| L3 | State survives an abrupt stop and a restart (synthetic thread and app conversation). | [measured] | Backup and restore drill (phase 4) | Yes | in phase 4 |
| L4 | `agent-coord ui` exits on SIGTERM without cleanup (exit −15), because only `KeyboardInterrupt` is handled. SIGINT exits 0. | [measured]; [read in code] `ui.py:1223` | Handle SIGTERM like `desktop/macos/backend.py:54-67`, or set `KillSignal=SIGINT` in the unit | Yes; the unit setting is enough at first | 1–2 h |
| L5 | One test fails with Ubuntu's Node 18 because `crypto.randomUUID` is missing. It passes with Web Crypto enabled; CI uses Node 22. | [measured] `test_activity_integration…test_browser_activity` | Provision Node 22.12 or later, which chrome-devtools-mcp also requires | Provisioning only | 0.5 h |
| L6 | `install-cli` writes `~/.local/bin/agent-coord`, which is not on a service's PATH. Plugin installation needs `codex` and `claude` to be installed first. | [measured] | Set PATH in the unit, and order the provisioning steps | Provisioning only | 0.5 h |
| L7 | Binding `0.0.0.0` is refused. Requests with a foreign `Host` or `X-Forwarded-Host` get 403. | [measured]; [read in code] `ui.py:256`, `:500-516` | None. This confirms the connectivity design. | — | 0 |
| L8 | `--tailscale` without Tailscale fails cleanly, but its message says "on this Mac". Tailscale Serve on Linux was **not run**. | [measured] | Make the wording machine-neutral; test Serve in phase 1 | Yes | 0.5 h + phase 1 |
| L9 | The macOS-only call sites are all guarded or are fallbacks: `ui open`, `security` for Claude limits, Jev Keychain, the Tailscale app path, Homebrew paths, launch metrics, and `kqueue` in jobs. None blocks Linux. | [read in code] `navigation.py:199-212`, `provider_limits.py:215-227`, `jev_settings.py:32-111`, `remote_access.py:41-49`, `jobs.py:91-106` | Wording only | No | 0.5 h |
| L10 | Every provider binary is found through PATH or a user-local path. None needs an app bundle. | [read in code] `codex_app_server.py:58`, `claude_code.py:31`, `antigravity_setup.py:43-52`, `kimi_setup.py:27-36`, `muse_setup.py:22-31` | — | — | 0 |
| L11 | The Antigravity login is stored inside the Ribbon Field state directory. | [read in code] `antigravity_setup.py:35`, `:118` | Store provider credentials under a separate directory on the root volume | Yes | 2–4 h |
| L12 | Web Push needs `pywebpush`. Startup works without it. | [read in code] `web_push.py`, `native_push.py:30-64` | Install `desktop/macos/push-requirements.txt` into a virtual environment | For phone alerts | 1–3 h |
| L13 | Memory grows without bound, the backend spends 2.2–3.0 cores on overhead, and thread-list refetches cost $108–252 a month in egress. | [from docs] [capacity-planning.md](capacity-planning.md) preconditions 1, 3 and 4 | Unload idle conversations; refetch the list only for list-relevant events and slim it; reuse database connections and add the missing index | Yes | 3–6 days |
| L14 | **Not run:** a real signed-in provider turn, authenticated resume, the fate of an in-flight turn, SSE reconnect, a full arm64 suite on the current commit, and Tailscale Serve on Linux | — | Phase 1's first run on the machine | Yes | in phase 1 |

Reproduce the Linux probe on a host with at least 3 GB free for Docker. This
Mac did not reliably have that.

```bash
git archive HEAD -o /tmp/ribbon-field.tar
docker run -d --name rf-linux-probe ubuntu:24.04 sleep infinity
docker cp /tmp/ribbon-field.tar rf-linux-probe:/tmp/ribbon-field.tar
docker exec rf-linux-probe sh -c 'mkdir -p /opt/rf && tar -xf /tmp/ribbon-field.tar -C /opt/rf &&
  apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  python3 git ca-certificates ripgrep curl xz-utils libatomic1'
docker exec rf-linux-probe sh -c 'N=$(curl -fsSL https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt |
  awk "/linux-arm64.tar.xz/{print \$2}") && curl -fsSL https://nodejs.org/dist/latest-v22.x/$N |
  tar -xJ -C /usr/local --strip-components=1'
docker exec -w /opt/rf rf-linux-probe python3 -W error::ResourceWarning -m unittest discover -s tests -v
docker exec -w /opt/rf -d rf-linux-probe plugins/agent-coord/scripts/agent-coord ui --no-browser --port 8765
docker exec rf-linux-probe sh -c 'sleep 3; curl -s http://127.0.0.1:8765/api/browser/config | head -c 200; echo'
docker exec rf-linux-probe sh -c 'kill -TERM $(pgrep -f "agent-coord ui")'   # L4: exits without cleanup
docker rm -f rf-linux-probe
gh run list --repo 3xC1ibR/agent-coord --workflow CI --branch main --limit 5   # CI evidence
```

The provider probe follows the same pattern: install Node, run each CLI's
official installer, then run `<cli> --version`. The browser check is a
168-line Node script that takes a Chrome binary path, a directory of MCP
binaries and a profile directory. It is kept with the uncommitted evidence
under [Evidence files](#evidence-files). Phase 1 runs it on the machine
against Playwright's Chromium.

## Build phases

Estimates are engineer-days of implementation and review [assumed]. Each phase
ends with its acceptance steps passing on the real machine.

| Phase | Work | Acceptance | Estimate |
| --- | --- | --- | ---: |
| 0. Preconditions in this repository | <ul><li>The Linux-readiness items marked required ([Linux readiness](#linux-readiness)).</li><li>The capacity preconditions: unload idle conversations, refetch the thread list only for list-relevant events and slim it, and reuse database connections [from docs: [capacity-planning.md](capacity-planning.md)].</li><li>Move the Antigravity token out of the state directory.</li><li>Add a remote-access mode that adopts a Serve route it does not own and never changes it.</li></ul> | CI is green. A Mac soak keeps memory bounded for 24 hours, backend CPU under 0.3 core, and thread-list traffic down by an order of magnitude. | 5–8 |
| 1. Machine as code | <ul><li>Terraform module and the ordered bootstrap (section 2).</li><li>`provision_machine.sh`, `tool_manifest.json` and the three units.</li><li>Root-only metadata, a `ribbonfield` account without sudo, and the root-owned Serve route.</li><li>The secrets unit with its allowlist and atomic writes.</li><li>Tailscale tag, policy and policy tests; SSH alias and host-key pin.</li><li>The `machine` identity in `/api/browser/config`, which the health gate needs.</li><li>`verify_machine.sh --base` and `--secrets`.</li></ul> The first run re-measures everything this plan marks "not run" on the real machine: a full arm64 suite, builds against `CPUSurplusCreditBalance`, headless Chromium, and Tailscale Serve. | Steps 1–2 | 4–6 |
| 2. Desktop | <ul><li>Split "remote" into privilege and co-location in the web UI, and add the sidebar label.</li><li>App.swift: Connection menu, SSH forward manager, window subtitle, refusal of mismatched servers.</li><li>Native bridge gating and `local: false` file providers in hosted mode.</li><li>Client state keyed by machine.</li><li>Kimi and Muse sign-in checks.</li><li>Follow DESIGN.md and the interface-design skill: before and after screenshots in light and dark, at desktop and phone widths.</li></ul> | Steps 3–4 | 5–8 |
| 3. Phone, browser and credentials in use | <ul><li>Pair the phone and install the Web Push dependencies.</li><li>Add the browser unit, and the MCP server to each browsing provider; remove the macOS-only MCP servers.</li><li>Run the viewer procedure.</li><li>Canary-secret persistence tests.</li></ul> | Steps 5–7 | 2–4 |
| 4. Resilience | <ul><li>Reboot drill.</li><li>Rebuild with Tailscale node replacement.</li><li>Restore drill from an older snapshot, which clears pairing grants.</li><li>Budget alarm.</li></ul> | Steps 8–10 | 2–3 |

Phases 1 and 2 can run in parallel once phase 0's memory fix lands. The total
is about 18–29 engineer-days [assumed]. Nothing in section 7 is built.

## Acceptance script

`verify_machine.sh` automates the checks marked *auto*. The owner performs the
others. Each step lists its pass condition.

1. **One command creates the machine.** In an AWS account with no existing
   `ribbon-field/ribbon-1/*` secrets, run
   `deploy/hosted-machine/create_machine.sh --spec machine_spec.json`.
   Pass: the command exits 0 and `ssh ribbon-1` works with no trust prompt.
   `verify_machine.sh ribbon-1 --base` (*auto*) then confirms the following:
   - **Service and identity.** `ribbon-field.service` is active, and
     `/api/browser/config` reports `"machine": {"kind": "hosted", "name": "ribbon-1", …}`.
   - **AWS.** The security group has no inbound rules, and IMDSv2 is required.
   - **Root-only metadata, also after a reboot.** As `ribbonfield`:
     - `curl http://169.254.169.254/` fails;
     - `aws secretsmanager get-secret-value` fails;
     - `sudo -n true` fails;
     - `ribbonfield` is not in the `docker` group.
     As root, the role can read its three secrets and cannot run `aws s3 ls`.
   - **Serve route.** `tailscale serve status --json` shows exactly HTTPS 443
     proxying to `http://127.0.0.1:8765`. As `ribbonfield`, `tailscale serve`
     changes are refused.
   - **No local privileges through the tailnet.** A request through the real
     Serve route with `Host: 127.0.0.1:8765` and no cookie gets no local
     privileges: config without a token, and invitation creation refused.
   - **Tailnet policy.** Its `tests` pass. From the owner's Mac, a listening
     test service on the machine is reachable only on 22 and 443. From the
     machine, a listening test service on the Mac is unreachable.
2. **Provider logins over SSH.** Run `ssh ribbon-1`, then `codex login --device-auth`,
   `claude auth login`, `kimi login`, `muse login`, and
   `agent-coord antigravity login` if Antigravity is enabled. Complete each on
   the Mac or the phone.
   Pass:
   - Each CLI's own status command reports signed in, or a one-line
     prompt through the CLI succeeds.
   - Credential files are 0600 and on the root volume.
     `find /srv/ribbon-field \( -name 'auth.json' -o -name '.credentials.json' -o -name 'acp_token.json' -o -path '*credentials*' \)`
     returns nothing.
3. **Desktop switches to Hosted.** Choose Connection → Hosted · ribbon-1.
   Pass:
   - Every window's subtitle and the sidebar read "Hosted · ribbon-1".
     Choose Connection → This Mac: they read "This Mac", and the local
     backend starts again.
   - **Mismatched server.** Point the hosted connection at a local
     `agent-coord ui --port 8765` on the Mac. The desktop refuses it, because
     that server reports `kind: local`.
   - **Native bridge.** In hosted mode, Open Project shows the server folder
     picker. A hosted page that calls `openFile` with a Mac path, or
     `chooseFiles` or `dropFiles`, through the bridge directly is rejected by
     the native side.
   - **State per machine.** Switch local → hosted → local with different open
     projects and an unsent draft. Each backend gets back only its own
     projects and draft, and no window sends to the backend that was left.
4. **One thread per provider.** In a repository cloned under `~/work` on the
   machine, start one Codex and one Claude conversation. Ask each to run
   `uname -sm && hostname` and to start a ten-minute task, such as running the
   agent-coord test suite twice. Start the Codex thread without full access,
   so that it asks permission for a command. Then start one short Kimi, Muse
   and (if enabled) Antigravity conversation. Each runs the same command and
   opens a page in the shared browser.
   Pass: every conversation reports `Linux` and `ribbon-1`, and every
   browsing provider reaches the browser.
5. **Close the laptop.** Close the Mac's lid for at least 15 minutes.
   Pass: when the phone opens (step 6), the long turns have kept running.
6. **Phone on cellular.** With Wi-Fi off, open the Ribbon Field app, which was
   paired to `https://ribbon-1.<tailnet>.ts.net` during phase 3.
   Pass:
   - Progress updates arrive.
   - Approving the Codex permission request lets the command run.
   - A follow-up sent to each provider's conversation is answered.
   - With the Home Screen web app, a turn-finished notification also arrives.
7. **Stored key and browser session.** Ask an agent: "Using `OPENROUTER_API_KEY`,
   list the available models and report how many there are. Do not print the
   key. Then open <site the owner logged into through the viewer> in the
   browser and report the signed-in account name."
   Pass: both answers are correct. `verify_machine.sh ribbon-1 --secrets`
   (*auto*) finds no current secret value outside `/run/ribbon-field/`. The
   canary tests from phase 3 also pass.
8. **Reboot.** Run `ssh ribbon-1-admin sudo reboot`.
   Pass within five minutes:
   - The service is active, and the desktop's forward reconnects by itself.
   - The phone reloads without pairing again.
   - Every thread is listed with its history. A turn that was running shows
     as interrupted, not silently lost.
   - Provider logins and the browser login still work, and metadata is still
     blocked for `ribbonfield`.
   - A follow-up continues each conversation.
9. **Destroy and rebuild.** Before rebuilding, tell each conversation a fact,
   such as "the code word is lantern". Run `destroy_machine.sh ribbon-1`, which
   keeps the data volume and secrets. Create a fresh one-off Tailscale key,
   then run `create_machine.sh --spec machine_spec.json`.
   Pass:
   - The machine has the same tailnet name, and the phone opens without
     pairing again.
   - The desktop connects without a host-key warning.
   - Threads, history, repositories, checkpoints and API keys are all present.
   - Every provider needs signing in again, and the browser needs one login,
     both by design.
   - After step 2 is repeated, each conversation answers the code word from
     the **same native session**.
10. **Restore from an older snapshot.** Pair a test browser, take a snapshot,
    revoke the test browser, then restore the data volume from that snapshot.
    Pass:
    - The revoked browser's old cookie is refused, because the restore
      cleared all grants, and the owner's phone must pair again.
    - No phase 3 canary secret is found in the snapshot.
    - The restored threads resume.

## Independent review

A fresh Codex agent (GPT-6.1 Sol, a different provider from the author)
received only this document and the repository. Its job was to find gaps and
security holes. It checked claims against the code, and ran a synthetic
authorization probe, the 33 remote-access tests and the provider-limits tests.
It found 14 confirmed issues and 3 suspicions, all accepted. Its full report is
under [Evidence files](#evidence-files).

| # | Severity | Finding | Change in this plan |
| --- | --- | --- | --- |
| F1 | High | Through the instance role, any process could read the Tailscale enrollment key and the SSH host keys. Hop limit 1 does not isolate host processes. | Instance metadata is root-only, `ribbonfield` has no sudo, the enrollment key is one-off, and the secrets are scoped to exact ARNs (sections 2 and 5, step 1) |
| F2 | High | Making `ribbonfield` a Tailscale operator would let an agent rewrite Serve into a TCP forward that receives local privileges without pairing | Root owns the Serve route; Ribbon Field adopts it read-only (sections 2 and 3, phase 0, step 1) |
| F3 | High | Hiding file actions in the web UI does not stop hosted content from calling native Mac file actions | Native bridge gating and `local: false` providers (section 4, step 3) |
| F4 | High | The persistence list omitted Kimi and Muse native sessions, and Antigravity history | Added to the data volume; only the Antigravity token moves to root (section 2) |
| F5 | High | First boot depended on secrets that had not been seeded, and the read-only role could not save host keys | Ordered bootstrap from the Mac, with host keys generated there; identity moved to phase 1 (section 2) |
| F6 | Medium | Paired phones and local agents carry full account authority | Trust boundary stated plainly (section 3) |
| F7 | Medium | Kimi and Muse report `unsupported`, not sign-in state | A sign-in check, and acceptance based on real turns (section 4, step 2) |
| F8 | Medium | The fallback would have put tokens into snapshots | Fallback removed (section 2) |
| F9 | Medium | The leak scan cannot prove non-persistence | Claim narrowed; canary tests, no core dumps, no swap (section 5) |
| F10 | Medium | Additive grants need a complete policy and negative tests | Policy with `tests` and live checks (section 3, step 1) |
| F11 | Medium | Unattended SSH lacked first-use trust and an explicit user | Generated alias with the host key pinned and forwarding off (section 2) |
| F12 | Medium | Client state was not separated by machine | Keyed by `machine.id` (section 4, step 3) |
| F13 | Medium | Restoring an older snapshot revives revoked phone grants | Restores clear grants (section 5, step 10) |
| F14 | Medium | Acceptance covered only Codex and Claude | Every enabled provider runs, browses, resumes and takes a phone follow-up (steps 4, 6, 9) |
| S1 | Suspicion | Linux browser launch details were missing | Headless, sandboxed, pinned, with dependencies and safe lock removal (section 6) |
| S2 | Suspicion | Root provisioning could trust paths agents can write | Root-owned paths, allowlist, atomic writes, never sourced by root (sections 2 and 5) |
| S3 | Suspicion | No phone-only recovery when grants or logins expire | Open question 11 |

The reviewer also confirmed that several parts were already correct:

- the loopback restriction and the handling of forwarding headers;
- the SSH forward's `Host` semantics;
- the pairing implementation;
- the iOS origin policy;
- the Linux Claude credential lookup;
- recovery-alarm support for T4g and M8g.

It corrected one of the author's suspicions: Muse's per-thread configuration
links the auth file rather than copying it, and the overlay is cleaned up
[read in code: `muse_setup.py:61-90`, `muse_code.py:46-55`].

## Open questions for the owner

Each question has a default the build will use unless the owner decides
otherwise.

1. **Approve this plan?** Phase 0 changes this repository. Phase 1 creates
   billable AWS resources and changes the tailnet policy.
2. **Region.** Default: us-east-1. It matches the existing account-service
   infrastructure and is close to the Central US. Prices are the same in
   us-west-2.
3. **Tailnet policy.** Replacing Tailscale's default allow-all rule changes
   what every device on the tailnet can reach. Which device-to-device access
   should stay? Default: owner devices reach each other as today; the machine
   reaches nothing.
4. **Antigravity.** A Google forum answer says orchestrating Antigravity under
   a consumer login through other agents is unsupported (section 7). Default:
   install it but leave it signed out. Until the owner decides, it is recorded
   as an explicit exception to the five-provider outcome.
5. **Agents without sudo.** Agents cannot install system packages. They use
   user-level toolchains (npm prefixes, `uv`, `pip --user`) or ask the owner.
   Granting sudo would expose every machine identity in section 5. Default:
   no sudo.
6. **Browser profile across rebuilds.** Default: it is lost on rebuild and
   kept out of snapshots. The alternative keeps it on the data volume, inside
   the daily snapshots.
7. **GitHub access.** Default: a fine-grained token for named repositories.
   The alternative is `gh auth login` with the owner's broad OAuth grant.
8. **AWS access for agents.** Default: none. The instance role is root-only.
   The alternative is a separate, narrowly scoped role, which would need its
   own path to agents.
9. **Security-update reboots.** Default: updates install automatically, and
   the owner reboots by hand. The alternative is a weekly reboot window that
   interrupts running turns.
10. **Switching the desktop to Hosted stops local agents.** Default: yes,
    after a confirmation when turns are running. "One backend at a time"
    requires this.
11. **How long away from the laptop?** Phone grants last 30 days, and the
    browser viewer and pairing administration need the Mac. Default: accept
    that limit for this pass. Recovery from the phone alone, over an SSH app
    on the tailnet, is documented but not built.
12. **The Mac's plaintext API key exports.** They stay as they are; this plan
    does not change the Mac. Should a follow-up move them into the Keychain?
13. **Monthly budget alarm.** Default: an AWS Budgets alert at $150, which is
    the recommended machine plus about 25%.

## Comparators

[from docs; accessed 2026-10-28]

| Offering | What it gives | Price |
| --- | --- | --- |
| exe.dev | Persistent root-access Linux VMs with SSH, HTTPS and systemd; a 2 vCPU / 4 GiB pool | $15/month personal |
| Fly.io Sprites | Persistent microVMs with sleep and resume; integrates Claude Code, Codex and Antigravity | Usage: CPU $0.0385/hour, RAM $0.021875/GB-hour (from 2026-10-01) |
| Codex SSH hosts | Desktop runs app-server on an SSH host. The phone pairs with an awake desktop, not with the host. | ChatGPT plan; host separate |
| Claude Remote Control | Phone or browser control of a Claude session on any machine, through Anthropic's relay | Claude plan |
| Cursor cloud agents and My Machines | Managed VMs, or the user's machine as a worker; agent loop in Cursor's cloud | $30–200/month plans |
| GitHub Codespaces | Managed dev VMs, deleted after up to 30 days stopped | $0.18/hour for 2 cores |

## Evidence files

The probes wrote these files under `build/hosted-machine-probe/` on the
owner's Mac. That directory is ignored by git, and the files are not
committed. The commands that matter are copied into this plan.

- `research/provider-auth-and-market.md`: provider authentication, terms,
  comparators, hosted browsers, benchmarks and Tailscale.
- `forge/forge-extension-estimate.md`: the Forge estimate.
- `ribbon-field/findings.md`: Linux readiness.
- `providers-browser/findings.md`: provider CLIs and browser runtime.
- `aws/aws-substrate-and-cost.md`: substrate and cost.
