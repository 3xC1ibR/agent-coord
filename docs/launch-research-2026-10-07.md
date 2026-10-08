# Agent Coord launch research

Research date: October 7, 2026. Status: Decision support; recommendations have not been adopted or implemented.

This memo evaluates the [standalone product proposal](standalone-product.md), including the market, provider permissions, hosting, economics, licensing, and launch sequence. The working business assumption is a small, bootstrapped company seeking sustainable revenue. A venture-funded platform would require substantially stronger evidence of market size and durable differentiation.

**Recommendation: launch a narrow Community preview and customer discovery first, while resolving the supported hosted authentication path. Offer a small paid managed-workspace pilot only after both conditions are satisfied: users repeatedly prefer Agent Coord to existing alternatives, and the provider integration is supported.** Keep the managed service an option to validate, rather than the organizing assumption for all development.

The promising hypothesis is reliable coordination across long-lived coding work: retaining decisions, identifying the actual next actor, making handoffs explicit, and preserving evidence of what was implemented and validated. Remote execution, multiple agents, mobile access, and an attention inbox are already available elsewhere. The product must demonstrate a better outcome on real work.

## Findings that change the proposal

| Finding | Decision implication |
| --- | --- |
| The repository already contains a tracked [MIT license](../LICENSE). | Licensing is an existing baseline to review, not a blank slate. Keep MIT for the preview unless there is a concrete reason to change future releases. |
| Conductor sells cloud access for $50/month, with usage pricing planned later. | A $99 workspace must explain its persistent capacity and operational value; generic remote-agent access is insufficient. [Conductor pricing](https://www.conductor.build/pricing) |
| Happier offers a free MIT-licensed client and relay, including cross-provider sessions and a needs-attention group. | Neither provider choice nor an attention inbox alone establishes differentiation. [Happier](https://happier.dev/) |
| Bloop shut down the company behind Vibe Kanban on April 10, 2026, saying that most users were free and it had not found an attractive business model. | Adoption and technical enthusiasm must be measured separately from willingness to pay. [Founder announcement](https://www.vibekanban.com/blog/shutdown) |
| OpenAI directs paid or remotely hosted ChatGPT-plan integrations toward a partner path. | Do not commit a commercial Codex subscription launch to an interpretation of remote CLI login. [Integration scope](https://developers.openai.com/siwc/token-sharing-open-source) |
| Anthropic explicitly describes conditions for hosting its unmodified Claude Code client. | Evaluate a Claude Code hosting path independently; do not assume the Codex answer applies. [Claude Code commercial integration rules](https://code.claude.com/docs/en/legal-and-compliance#can-customers-offer-claude-code-in-their-products) |
| At the proposed $99 price, 30 minutes of monthly support valued at $60/hour leaves about $3/customer under the draft's ten-customer cost assumptions. | Validate support demand and price together. The current model has little room for acquisition or engineering. Calculation below. |
| Ordinary Lightsail instances remain billable while stopped. | A sleeping-workspace discount requires a different cost mechanism, not just a stop button. [AWS billing rules](https://docs.aws.amazon.com/en_en/lightsail/latest/userguide/amazon-lightsail-frequently-asked-questions-faq-billing-and-account-management.html) |

## Market and competitive position

The relevant market includes three overlapping purchases: a better interface for existing agents, infrastructure for work that continues unattended, and coordination of several ongoing tasks. Customers can satisfy these with different combinations of free software, provider-native applications, and rented machines.

The following are published capabilities and prices observed on the research date, not independently benchmarked performance or endorsements of their authentication designs. Model charges, included compute, execution location, and persistence differ; the prices are not equivalent bundles.

| Alternative | Published offer | Implication for Agent Coord |
| --- | --- | --- |
| Conductor | Free local edition; Pro $50/month; Teams $60/user/month. Cloud workspaces use Vercel sandboxes advertised as 8 CPU cores and 16 GB RAM. Its FAQ permits customers to bring subscriptions or keys and says additional compute billing is planned. | Closest commercial comparison for a managed agent workspace. Benchmark the workflow directly; do not assume $99 buys a capability unavailable at $50. [Pricing and FAQ](https://www.conductor.build/pricing) |
| Superset | Free local desktop/CLI; Pro $20/user/month monthly, or $15 on annual billing, adds remote access, automations, mobile, and integrations. Customers supply agent accounts or keys. | Establishes a relatively low software price reference when the customer supplies compute. [Pricing](https://superset.sh/pricing) |
| Happier | Free, MIT-licensed, self-hostable applications and relay. Advertises mobile/web/desktop, approvals, cross-agent delegation, and attention grouping. | Strong direct substitute for the Community proposition. Its documented feature breadth means Agent Coord needs evidence of better coordination reliability or substantially simpler operation. [Product and FAQ](https://happier.dev/) |
| T3 Code | MIT-licensed control interface with desktop, web, and mobile clients for agents on the user's machine. Its README says it is not selling a product. | Another substantial free alternative. The existing Agent Coord remote-access design already references its work. [Repository](https://github.com/pingdotgg/t3code), [local design reference](remote-access.md) |
| Omnara | $0 platform fee for connecting customer-owned machines and model keys; Apache-2.0 self-hosting. Managed machines charge by memory and active runtime, plus retention. | Tests whether customers want a fixed persistent machine or inexpensive intermittent execution. Detailed arithmetic below. [Pricing](https://www.omnara.com/pricing) |
| Vibe Kanban | Company sunset; project continues as community-maintained open source. Its announcement describes thousands of daily users and monetization difficulty. | A particularly relevant negative business case. Do not infer that this category cannot work, but do not treat usage as commercial validation. [Announcement](https://www.vibekanban.com/blog/shutdown) |
| Claude Code Remote Control | Web/mobile access to a session that continues on the user's machine; that machine and process must stay running. | A strong default for Claude-only users who already own an always-on computer. [Remote Control](https://code.claude.com/docs/en/remote-control) |
| Cursor Cloud Agents | Agents execute in isolated cloud VMs, work without the user's computer online, and are billed at selected-model API pricing. | Competes on completed remote work, even though the billing relationship differs. [Cloud Agents](https://cursor.com/docs/cloud-agent) |
| OpenAI Agents API | Managed Codex harness, durable sessions, orchestration, recovery, and hosted or self-hosted execution. Model usage is API-billed. | Both an infrastructure alternative and a source of competition. Using it would change the subscription proposition. [Overview](https://developers.openai.com/api/docs/guides/agents-api/overview) |
| Coder | Free self-hosted Community workspaces; paid enterprise tiers add administration and governance. Its native agent allowance is capped at five on Community/Premium and uncapped on AI Premium. | Enterprise administration is an established market with demanding incumbents; it is an expensive initial direction for a small team. [Plans](https://coder.com/pricing) |

**Positioning to test:** “Keep every coding task accountable: know what needs your decision, what was verified, and who acts next.” Pair it with a real example of an interrupted task, an explicit handoff, and a completed result. Avoid promising that Agent Coord is the first or only product offering these concepts.

The most useful distinction to investigate is the meaning of state. “A process stopped” and “a task is finished” are different; so are “the agent sent a message” and “the user needs to act.” Agent Coord's checkpoint model already represents phase, summary, next action, and next actor. Its scope declarations and atomic handoffs provide another possible advantage. These are product hypotheses supported by the local implementation, not evidence that customers prefer them. [Repository overview](../README.md)

There is a credible alternative to building a broad agent application: publish the coordination core as a small plugin and integrate with existing interfaces. Before investing in another editor, terminal, mobile client, or account system, test whether customers primarily value the coordination semantics. Compare a standalone interface with a companion workflow during discovery. Interoperability may produce a smaller but more defensible business than feature parity with every free client.

## Demand evidence and first customers

Stack Overflow's 2025 survey reported that 84% of respondents used or planned to use AI tools, while 46% distrusted their accuracy and 33% trusted it. Among agent users, only 17% agreed that agents improved team collaboration. These are broad, self-selected survey findings, not an estimate of buyers for Agent Coord. They support investigating verification and coordination friction. [2025 survey](https://survey.stackoverflow.co/2025/ai)

DORA's March 2026 synthesis describes time shifting from code creation to auditing and verification, and associates higher AI adoption with both greater throughput and greater instability. The qualitative analysis draws on 1,110 open-ended responses from Google engineers in Q3 2025. This supports measuring accepted work and review effort rather than output volume. [DORA analysis](https://dora.dev/insights/balancing-ai-tensions/)

Do not use the widely circulated “AI makes developers 19% slower” result as a current market conclusion. METR's February 2026 update explains why its later experiment was affected by selection and measurement problems, including concurrent agent use, and says the size of the current productivity effect remains uncertain. [METR update](https://metr.org/blog/2026-02-24-uplift-update/)

The Omnara launch discussion contains useful discovery leads: developers using SSH and phones, people satisfied with always-on personal machines, and objections to paying for a remote interface without hosted compute. These are anecdotes from a competitor's launch, not representative demand measurements or its current prices. [Launch discussion](https://news.ycombinator.com/item?id=46991591)

| Candidate segment | Why it may buy | Main obstacle | Initial priority |
| --- | --- | --- | --- |
| Independent developer or technical founder with 3–10 ongoing agent tasks across repositories | Personally feels interruption cost; can install tools and make a purchase without procurement | Free alternatives and existing machines | First |
| Consultant or small agency developer juggling projects | Context continuity and clear ownership can matter across engagements | Client code permissions and separation of secrets | Interview early; host only suitably authorized workloads |
| Developer who wants only phone access | Clear, easy-to-explain need | Strong free and provider-native alternatives | Useful Community user; weak initial paid target |
| Engineering manager seeking enterprise governance | Potentially larger budget | SSO, audit, contractual, deployment, and support expectations | Later, only if demand pulls the product there |
| Beginner seeking an autonomous app builder | Values a finished application | Needs broad product support, deployment help, and model quality guarantees | Exclude from first pilot |

Recruit based on demonstrated behavior: ask to see a recent lost thread, an agent waiting unnoticed, a repeated decision, or a collision between sessions. Preference for a demo is weak evidence; voluntarily switching a real workflow and paying to continue are stronger.

For the first 12–15 interviews, ask:

1. Show the last time you lost track of agent work. What happened, and what did it cost in time or rework?
2. How many tasks and repositories are active on a typical day? Which providers and machines do you actually use?
3. What do you use today to know which tasks require you? What have you already tried and abandoned?
4. Is the main problem access from another device, computer availability, task coordination, or reviewing results?
5. Which information must survive a restart or handoff? What would make you distrust the dashboard?
6. Who can approve moving this code to a hosted machine? Which projects could you use immediately?
7. Would you move a real task into the preview this week? After using it, would you purchase the defined pilot at its stated price?

Avoid estimating total addressable market by multiplying developer counts by $99. A useful first market calculation is the number of qualified users reachable through founder-led channels, their actual activation rate, and how many renew. At $129, 100 customers produce $12,900 monthly revenue; the economics below explain why revenue alone is a poor sustainability target.

## Provider authentication and launch paths

Commercial integration permission, technical authentication, and billing entitlement are separate questions. Competitors' marketing does not establish permission for Agent Coord's architecture.

| Deployment | Evidence and remaining boundary | Recommended treatment |
| --- | --- | --- |
| Community Codex on the user's machine | OpenAI's app-server documentation allows continued local/open-source usage and recommends migration to Sign in with ChatGPT. | Suitable first preview route within the documented conditions. [App-server authentication](https://learn.chatgpt.com/docs/app-server#auth-endpoints) |
| User-controlled remote Codex machine | Remote CLI device login is documented. A separate SIWC guide covers open-source apps on self-hosted VMs. | Useful technical validation route; distinguish customer self-hosting from selling a managed integration. [CLI authentication](https://learn.chatgpt.com/docs/auth#login-on-headless-devices), [self-hosted SIWC](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms) |
| Agent Coord-managed Codex machine using existing CLI authentication | The app-server page says: “App-server authentication has never been permitted for commercial or hosted services.” The exact independently authenticated dedicated-VM arrangement is not expressly resolved there. | Treat written clarification or an approved partner integration as a commercial launch dependency. Moving login into a terminal does not itself settle it. [App-server policy](https://learn.chatgpt.com/docs/app-server#auth-endpoints) |
| Paid or remotely hosted SIWC integration | Public plan-usage documentation directs these applications to an interest form. | Apply with the concrete architecture and request supported terms, eligibility, limits, and timeline. Do not assume general availability. [SIWC scope](https://developers.openai.com/siwc/token-sharing-open-source) |
| Hosted unmodified Claude Code | Anthropic describes hosting conditions: accept its Commercial Terms, preserve the binary and authentication methods, let each user authenticate, and do not resell/intermediate their usage. | A separately documented candidate, subject to the exact UI and credential design. [Hosting conditions](https://code.claude.com/docs/en/legal-and-compliance#can-customers-offer-claude-code-in-their-products) |
| A custom Claude application or Agent SDK integration | Anthropic directs developers to API authentication and prohibits collecting or intermediating Claude.ai session credentials. | Do not treat the CLI hosting conditions as permission for a custom subscription-backed harness. [Credential rules](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use), [SDK integration guidance](https://code.claude.com/docs/en/agent-sdk/overview) |

The Anthropic distinction is material: operating the official client and building a custom agent integration are different product paths. On October 7, 2026, Anthropic also updated its SDK guidance: the earlier SDK monthly credit is discontinued; Max and Team monthly API credits are claimed in a Console organization and used with its API key. Those credits do not establish unlimited subscription-backed SDK use. [Dated SDK update](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)

For OpenAI, the supported SIWC app-server recipe configures a Responses provider and passes an authorized access token to the process. The application must renew that token, restart app-server, and resume saved threads. That creates a recovery requirement for a persistent service. This recipe is technically relevant but does not grant commercial partner approval. [SIWC app-server integration](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server)

The SIWC preview also has feature limits: some hosted tools are unsupported, while local shell/MCP and local thread history can still work. Validate the actual tool inventory rather than promising equivalence with the stock client. The self-hosted VM guide notes that transferred credential sessions do not yet provide host-specific usage attribution and revocation. [Preview limits](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations), [VM credential lifecycle](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms)

Keep Codex first for Community because it already has browser support. If partner timing prevents a hosted Codex launch, choose deliberately among a customer-controlled preview, a Claude Code hosting implementation that meets its documented conditions, or an API-key pilot. Do not silently substitute API billing for the promised subscription experience. Supporting Claude in terminal coordination does not mean its browser conversation, approval, and recovery implementation is ready.

**Draft request for OpenAI, not sent:**

> We are building Agent Coord, an MIT-licensed coding-agent coordination application. We want to sell one persistent Linux VM per customer, with separate billing for software and infrastructure. The VM runs the official, unmodified Codex client; our browser UI communicates with `codex app-server`. Each customer uses their own provider account, with no shared credentials or resale of model usage. Is independent CLI sign-in followed by app-server use supported in this commercial arrangement, or must we use an approved Sign in with ChatGPT partner integration? Please confirm the supported authentication flow, account/plan eligibility, unattended execution rules, token storage and refresh requirements, relevant usage limits, and whether customer-owned VM installations have different conditions.

## Product scope and reliability promise

The existing [README](../README.md) and [remote-access guide](remote-access.md) document substantial foundations: browser Codex conversations, queued messages, approvals, checkpoints, thread organization, coordination hooks, and private access through Tailscale. The remote-access guide requires the Mac to remain awake and the backend to remain running. It also distinguishes closed Codex terminal conversations from active terminal sessions and Claude threads. These boundaries should appear in onboarding.

The initial service promise should be narrow: **work continues after the browser disconnects while the assigned runner remains healthy; after a runner interruption, the user sees an honest status and can recover saved work.** Do not imply live migration of shell processes, guaranteed unattended completion, or transparent recovery from every restart.

| Needed to validate the promise | Why it matters |
| --- | --- |
| Clear running, waiting, failed, interrupted, and completed states, with time and evidence | A quiet or disconnected process must not look successful. |
| Durable next action and next actor, with user correction | Distinguishes a useful attention queue from another unread-message list. |
| Reviewable handoff containing decisions, changed artifacts, validation, and unresolved work | Preserves working context beyond a transcript summary. |
| Approval and queued-message behavior that survives reconnects without duplicate execution | Reconnection must not repeat an external side effect or discard user intent. |
| Visible provider authentication and usage-limit failures | A provisioned VM is not evidence that the agent can run. |
| Restore and export of coordination state, provider thread history, and working files | The current app-server adapter says Codex owns conversation history; backing up Agent Coord SQLite alone is insufficient. [Adapter](../plugins/agent-coord/scripts/agent_coord/codex_app_server.py) |
| Reversible updates with a tested recovery procedure | Client versions, protocols, hooks, and stored state will evolve independently. |

The existing scope hook is a coordination mechanism. The README explicitly says it does not parse arbitrary shell commands and is not a security boundary for those commands. Marketing should describe conflict detection accurately; it cannot promise universal protection from concurrent file changes. Separate worktrees may prevent direct working-tree interference, but they still leave integration and shared-resource conflicts. [Hook limitations](../README.md)

Measure this with a repeatable comparison against the user's current workflow and at least Happier or T3 Code. Give each participant the same sequence: three tasks, one question, one review handoff, one disconnected browser, and one interrupted runner. Record missed decisions, time to recover context, incorrect “done” states, duplicate actions, and accepted results. A feature checklist cannot substitute for this comparison.

## Hosting choices and operational design

One Linux VM per customer remains a reasonable pilot architecture. It limits the initial isolation and scheduling problem and makes costs legible. It does not eliminate the need for separate control-plane credentials, authenticated routing, secure administration, and recovery. Multiple repositories in the same user account share a trust boundary; consulting projects with different access requirements may need separate workspaces.

Use one provider, one region close to the first cohort, one default machine size, and documented supported stacks. Delay a general runner marketplace, Kubernetes fleet, arbitrary regions, Windows execution, and native macOS builds. A Linux VM will not reproduce a customer's Mac-only toolchain; qualify repositories before promising compatibility.

| Candidate | Verified price or characteristic | Pilot assessment |
| --- | --- | --- |
| AWS Lightsail | Public-IPv4 Linux: 2 vCPU/8 GB/160 GB at $44/month; 4 vCPU/16 GB/320 GB at $84. Transfer allowances vary by region. [Bundle table](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-bundles.html) | Predictable baseline if the team already operates AWS. Benchmark sustained builds because CPU is burstable. |
| DigitalOcean Basic | Published shared-CPU table: 4 vCPU/8 GiB/160 GiB at $48/month; 8 vCPU/16 GiB/320 GiB at $96. [Pricing](https://www.digitalocean.com/pricing/droplets) | Worth a direct benchmark. More advertised vCPUs do not establish better sustained throughput. |
| Hetzner | June 2026 adjustment lists EU CX33 at $9.99/month and CPX32 at $41.99, excluding IPv4 and VAT. Server classes, regions, and availability differ. [Price adjustment](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/), [server types](https://docs.hetzner.com/cloud/servers/overview/) | Potential cost experiment; do not build the business model on an unverified cheap SKU or old US price. Confirm orderable capacity, architecture, exact resources, and latency. |
| EC2 or another sustained-CPU VM | AWS recommends EC2 for consistently high CPU needs. [Performance guidance](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-faq-instances.html) | Use when actual builds justify higher cost; select a sustained-performance family and cost storage/egress separately. |
| Managed ephemeral sandbox | Buys lifecycle/isolation capability, usually with a runtime-oriented bill. | Attractive if tasks are intermittent; compare actual all-in cost and recovery semantics before changing the persistent-workspace promise. |
| Customer-owned machine | The customer bears compute cost and uptime responsibility. | Best low-capital learning route, but commercial authentication rules still apply to any paid integration. |

A meaningful benchmark uses representative Node/Python services, a browser test workload, and a heavier compiled repository if that is in the cohort. Run warm and cold builds, one/two/four simultaneous sessions, and repeated builds long enough to expose CPU bursting behavior. Measure elapsed time, peak resident memory, disk growth, OOMs, CPU capacity depletion, and responsiveness of approvals under load. No machines were provisioned or workloads benchmarked for this research; sizing remains unvalidated.

For the first externally used service, specify these operational boundaries:

- Customer code cannot obtain provisioning or cross-customer administration credentials. VM-to-VM and runner-to-control-plane access should be explicitly constrained.
- Every browser request, event stream, preview URL, and terminal connection is authorized to the correct workspace. Separate untrusted application previews from the account/control interface's origin.
- Provider sign-in completes through its supported flow. Credentials are not copied into support tickets, analytics, shared images, or a central convenience store.
- Operator access is limited, attributable, and disclosed. Disk encryption and HTTPS do not mean the service operator cannot access plaintext during execution.
- Backups cover the complete recoverable state, including uncommitted files and provider history. Use a consistent database backup procedure. Define how secrets are protected and whether restoring an older credential requires fresh sign-in.
- Export excludes credentials by default and includes enough information to continue on customer infrastructure. State cancellation, failed-payment, retention, and deletion behavior before charging.
- Provisioning and billing handlers tolerate retries; repeated payment events cannot create extra billable machines. Destructive cleanup has an explicit state transition and retention period.

These are proposed acceptance criteria, not claims about the current implementation. A VM boundary also does not prevent a malicious repository from using credentials made available inside that VM. Preserve the agent's permission controls and provide clear access boundaries.

The current private-access design is useful for Community testing. Tailscale's Personal plan is explicitly for non-commercial use, so it should not be assumed to provide a free networking layer for a commercial hosted service. Confirm a suitable plan or build authenticated public routing for the managed offer. [Tailscale pricing FAQ](https://tailscale.com/pricing?plan=business)

For sleeping workspaces, distinguish four states: browser disconnected, agent idle, pending user approval, and machine stopped. Only the latter changes execution availability, and ordinary Lightsail still charges for it. Saving compute by deleting/recreating a VM introduces restore latency and process loss. Do not add sleep pricing until those semantics and the provider bill are both proven. [Lightsail billing](https://docs.aws.amazon.com/en_en/lightsail/latest/userguide/amazon-lightsail-frequently-asked-questions-faq-billing-and-account-management.html)

## Pricing and economic sensitivity

Use the draft's US domestic-card assumptions: Payments at 2.9% plus $0.30, and pay-as-you-go Billing at 0.7% of billing volume. International cards, currency conversion, tax handling, refunds, disputes, and other services can add cost. [Stripe Payments](https://stripe.com/pricing), [Stripe Billing](https://stripe.com/en-us/billing/pricing)

The following is a planning model, not observed customer economics:

```text
P = monthly customer price
V = VM cost
B = backup allowance
F = total shared-service cost per month
N = paying customers sharing F
m = support minutes per customer per month
R = fully loaded support cost per hour

payment and billing fees = 0.036 × P + 0.30
contribution before support = 0.964 × P − 0.30 − V − B − F/N
contribution after support = contribution before support − m × R/60
```

Base assumptions are V=$44, B=$8, F=$100, N=10, and R=$60/hour. The 16 GB row assumes V=$84 and B=$16. Backup allowances are budgets, not measured retention costs. AWS snapshot pricing is $0.05/GB-month and successive snapshots account for changed data; repository churn affects the bill. [Snapshot billing](https://docs.aws.amazon.com/en_en/lightsail/latest/userguide/amazon-lightsail-frequently-asked-questions-faq-billing-and-account-management.html)

| Workspace and price | Fees | Before support | After 15 min support | After 30 min | After 60 min |
| --- | ---: | ---: | ---: | ---: | ---: |
| 8 GB at $99 | $3.86 | $33.14 | $18.14 | $3.14 | −$26.86 |
| 8 GB at $129 | $4.94 | $62.06 | $47.06 | $32.06 | $2.06 |
| 8 GB at $149 | $5.66 | $81.34 | $66.34 | $51.34 | $21.34 |
| 16 GB at $179 | $6.74 | $62.26 | $47.26 | $32.26 | $2.26 |

All contributions exclude product engineering, marketing, tax, unmodeled incident costs, and model inference. The optional Jev classifier would be an additional vendor cost and data flow; disable it by default in a first hosted pilot or explicitly budget and disclose it.

The $99 plan's modeled pre-support contribution is 33.5%; the $179 large plan is 34.8%. Doubling machine size at the proposed prices does not fix the margin structure. A base plan would need about $134.27 to leave 50% before support under these assumptions, or $198.92 to leave 50% after 30 minutes of support. These are algebraic targets, not evidence that buyers will pay those prices.

Scale helps less than it first appears:

| Customers at $99 | Shared cost per customer | Contribution/customer after 15 min support | Total monthly contribution after support |
| --- | ---: | ---: | ---: |
| 1 | $100.00 | −$71.86 | −$71.86 |
| 5 | $20.00 | $8.14 | $40.68 |
| 10 | $10.00 | $18.14 | $181.36 |
| 25 | $4.00 | $24.14 | $603.40 |
| 100 | $1.00 | $27.14 | $2,713.60 |

This deliberately holds shared infrastructure at $100 to isolate the effect of allocation; it is not a capacity forecast. At 100 customers, even that favorable assumption leaves roughly $2,714 before engineering and acquisition. One hour of first-month onboarding consumes another $60/customer unless separately charged or amortized over retention that actually occurs.

A useful comparison is Omnara's published managed-machine rate: $0.0414 per GiB-memory-hour online plus $0.20016 per GiB of memory per 30 days retained. For 8 GiB retained over a 30-day period, arithmetic gives $34.72 at 100 online hours, $100.96 at 300 hours, and $240.07 at 720 hours. These exclude inference and are not hardware-performance equivalents. A fixed persistent plan may appeal to heavy use while being unattractive to intermittent users. [Omnara rates](https://www.omnara.com/pricing)

**Pricing experiment:** offer a precisely bounded $99 founding pilot for one billing cycle, explicitly subject to repricing, and test a $129 ongoing offer with subsequent qualified customers. If customers want extensive setup, test a separate setup charge. Keep larger machines by request until measured. Do not offer lifetime infrastructure pricing or unlimited support. With 5–10 customers this is qualitative price discovery, not a statistically meaningful A/B test.

Test a software-only $19–29/month offer only if users explicitly prefer their own machines and value ongoing managed services such as relay operation or support. That range is a hypothesis informed by competing offers, not validated willingness to pay. A useful free Community edition should still contain the coordination behavior needed to evaluate the product.

For acquisition, choose a payback ceiling from measured contribution. At the ten-customer assumptions, $99 with 15 minutes of support leaves about $18/month: a three-month acquisition allowance is only about $54. At $129 it is about $141. Founder recruiting time counts as acquisition effort even when no advertising invoice exists. Do not extrapolate lifetime value until renewal and churn are observed.

## License and edition boundaries

The tracked [LICENSE](../LICENSE) is MIT, with the copyright line “Copyright (c) 2026 r.” Review the ownership/attribution record before a public release. The proposal's assertion that no license has been selected is inconsistent with the current repository. That does not establish what has previously been distributed or who owns every contribution; those facts require a provenance check.

| Option | Practical effect | Assessment |
| --- | --- | --- |
| Retain MIT | Permissive use, modification, distribution, and sale subject to its notice requirements; matches the current file | Recommended for the initial Community release |
| Apache-2.0 for future work | Permissive license with express patent provisions and notice obligations | Reasonable if those provisions are a deliberate requirement; changing now adds little to product validation. [License text](https://www.apache.org/licenses/LICENSE-2.0) |
| AGPL-3.0 for appropriately owned future work | Modified network-served versions must offer corresponding source under its conditions | Consider only if reciprocal source availability is a core goal. It does not prohibit competing commercial hosting. [Section 13](https://opensource.org/license/agpl-3-0) |
| License restricting competing hosting or commercial use | May protect a commercial boundary, but changes the freedom offered to users | Do not call this open source if it fails the Open Source Definition. [OSI definition](https://opensource.org/osd) |

A later license change should not be planned as a way to withdraw permissions already granted for prior distributed versions. Decide contribution terms and ownership before relying on commercial dual licensing; the repository's current permissive baseline does not create exclusive hosting rights.

For the first edition split, keep the application, local runner, checkpointing, conflict coordination, export, and self-hosting documentation in Community. Charge for provisioned machines, managed updates, tested backups, routing, and defined operational support. These are concrete services. Avoid withholding basic recovery or export to create lock-in.

Team administration can be evaluated later when customers ask to buy shared access, policy controls, audit history, or deployment help. The mere availability of enterprise tiers at competitors does not establish that Agent Coord should build them now.

## Launch sequence and decision gates

The sequence below is a proposed six-to-eight-week learning cycle starting when someone is assigned to run it. Dates are relative, not delivery commitments; renewal assessment requires a full paid billing cycle. No outreach, vendor application, deployment, subscription purchase, or customer billing was performed as part of this research.

| Phase | Proposed work | Evidence required to advance |
| --- | --- | --- |
| Week 1: discovery and provider path | Conduct 12–15 behavior-based interviews; identify at least five plausible design partners; send the concrete integration question; compare workflows with free alternatives | At least five prospects demonstrate recurring coordination pain and agree to put real work into a preview |
| Weeks 1–2: Community preview | Make installation and removal straightforward; document supported client versions; add a setup diagnostic; package the existing workflow and an example repository | At least 4 of 5 observed users complete a first meaningful task and recover it from another device without founder intervention after initial setup |
| Weeks 2–3: differentiated workflow | Evaluate checkpoint accuracy, decisions, handoffs, interrupted work, and review evidence against existing tools | Participants can name a repeated outcome they prefer, and measurements show lower missed-action or context-recovery cost |
| Weeks 3–4: managed pilot, conditional | Benchmark machines; complete authentication, isolation, restore/export, and billing lifecycle checks; offer a defined plan to 5–10 qualified users | Supported provider path; all critical failure exercises pass; at least five users willingly pay the stated price |
| Weeks 4–8: retention and economics | Track repeat use, renewals, support, resource use, and incidents; test ongoing pricing | Retention and contribution thresholds below justify further investment |

The numerical thresholds are proposed operating rules for a small cohort, not industry benchmarks:

- **Activation:** at least 4 of 5 observed users get a meaningful result with a usable checkpoint, rather than merely starting a chat. Track setup time separately; target a median below 15 minutes once prerequisites are installed.
- **Habit:** at least 3 of the first 5 activated users still use the coordination workflow in week four, with at least two recovered/handoff tasks per week. Record why each remaining user stops.
- **Payment:** at least 5 purchases from qualified prospects; at least 3 of the first 5 eligible paid customers renew at the stated ongoing price. Count actual purchases and renewals, not intent surveys.
- **Value:** compare time to identify the next action and recover context with the participant's existing setup. Seek at least a 30% reduction as a directional target, while monitoring review/rework time so faster triage does not conceal worse outcomes.
- **Support:** target no more than 15 minutes of recurring support/customer/month after onboarding, reported as both mean and worst case. At a $99 price, continued 30-minute demand is a reason to reprice or narrow service scope.
- **Economics:** seek at least 30% contribution after routine support and measured operating costs at the proposed ongoing price. Track onboarding separately and define its payback window.
- **Reliability:** scripted reconnect, interruption, token expiry, full disk, update rollback, backup restore, and account deletion exercises must not expose another customer's data, silently lose accepted instructions, or duplicate approved side effects. Publish no uptime percentage based on this small sample.

Use explicit events such as `workspace_ready`, `first_task_completed`, `decision_requested`, `decision_resolved`, `handoff_accepted`, `runner_interrupted`, `restore_verified`, and `subscription_renewed`. Collect metadata needed for the experiment with consent; prompts, source code, secrets, and transcript contents should not enter analytics by default. Define “accepted result” through user confirmation or the project's normal review process, not an agent's self-reported completion.

If Community usage is strong but payment is weak, keep the project sustainable as open source and test paid deployment/support separately. If customers only value generic remote access, integrate with existing tools or narrow the project. If demand is strong but hosted authentication is unresolved, continue a supported customer-controlled route without advertising unavailable managed subscription access. If managed demand is strong but support dominates margin, raise price, constrain supported stacks, or change the operating model before recruiting more users.

## Distribution and launch materials

Begin with direct recruiting among developers already running multiple agent sessions. The first objective is five retained users with observable pain. A large launch can obscure that signal with installs from people who have no recurring need.

Build the launch around a short, reproducible demonstration: several tasks across repositories; one real question; a validation handoff; a browser closed and reopened; and a summary showing exactly what is complete and who acts next. State the execution location and what happens if the machine stops. Let viewers try a sample workflow without connecting private repositories.

The repository needs installation, prerequisites, a client compatibility matrix, update/rollback instructions, uninstall steps, and a plain explanation of what leaves the machine. Existing plugin install commands are a useful channel for users already inside Codex or Claude Code. Keep optional issue tracking out of the first-run path unless the demonstrated workflow requires it. For a downloadable Mac app, signing and notarization belong in release preparation. [Apple distribution guidance](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)

A Show HN is appropriate once the product can be tried. Its guidelines explicitly discourage landing pages and signup barriers and require something the maker can discuss. Use a working Community release and explain the specific coordination problem, implementation limits, and why the project exists. [Show HN guidelines](https://news.ycombinator.com/showhn.html)

Use technical walkthroughs and case studies as the next distribution test: handling an interrupted task, distinguishing unread output from required action, and restoring work after a failed runner. Publish comparison claims only after performing the relevant comparison. Paid advertising is premature until contribution, activation, and retention support a realistic acquisition budget.

A hosted pricing page should plainly show what the customer pays Agent Coord, what they pay the model provider, machine resources, provider usage limits, support scope, backup/export behavior, and cancellation terms. The landing message should lead with the outcome; edition and infrastructure details can explain the purchase below it.

## Decisions to make next

| Decision | Recommended starting position | Evidence that should change it |
| --- | --- | --- |
| Business objective | Bootstrapped learning cycle with bounded investment | An explicit founder decision to optimize for venture-scale growth or open-source impact |
| Initial customer | Experienced individual with several concurrent tasks across repositories | Interviews show stronger paid demand from another reachable group |
| Initial differentiation | Accurate task state, explicit decisions, and evidence-preserving handoffs | Users get equivalent outcomes more easily from free alternatives |
| Public release | Community preview before broad hosted launch | A supported hosted path and strong managed demand emerge sooner |
| Provider order | Codex Community first; hosted provider chosen by supportability and demand | Partner availability or Claude demand materially changes the tradeoff |
| License | Retain existing MIT baseline after attribution/provenance review | A deliberate reciprocity or patent-policy requirement |
| Paid pilot | Defined $99 founding month; test $129 ongoing; larger machines by request | Actual payment, renewal, resource, and support evidence |
| Infrastructure | One customer VM, one region, one provider for the first managed cohort | Benchmarks show poor performance or a different operating model is materially cheaper |
| Team and hybrid features | Defer broad implementation | Customers offer credible purchase commitments for specific capabilities |
| Broad marketing | Wait for activation and retained use | A repeatable, self-serve onboarding experience and defensible message |

The largest unresolved questions are customer preference, willingness to pay, provider approval for the precise commercial integration, Linux workload performance, and support burden. Public research can identify these dependencies and improve the experiment; it cannot replace the experiment.

## Research scope

Primary product pages, provider documentation, original research, license texts, and the local repository were reviewed on October 7, 2026. Community discussion was used only for discovery leads. Competitor claims were not validated by installing their products; no customer interviews, load tests, legal review of a signed agreement, or infrastructure trials were conducted. Prices are published observations with the qualifications stated above. Calculations were checked programmatically. The original proposal and implementation files were not changed.
