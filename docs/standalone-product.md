# Ribbon Field standalone product proposal

Status: Discussion draft. Updated October 7, 2026.

Ribbon Field could become a standalone product with an open-source edition that developers run on their own machines and a paid service that supplies the application and remote compute. The intended business model is that developers keep their existing agent subscriptions and pay us for the workspace, coordination experience, and hosting.

The product promise is to start coding work, close a laptop, and return from another device to results, questions, and approvals with the context intact. The immediate opportunity is to test that experience with a small Codex pilot. Hosting architecture, prices, licensing, and launch commitments remain proposals; the subscription integration needs clarification before commercial launch.

## Direction and current foundation

The direction expressed in the discussion is:

- Offer an open-source, self-hosted edition for a laptop, personal server, or customer-managed cloud machine.
- Offer a paid hosted edition that runs the application and agents remotely.
- Start with Codex, with Claude Code support later.
- Charge for software and compute while customers maintain their own model-provider relationships.

Open source and self-hosting are separate properties. We intend to offer both, but have not selected a license or agreed which capabilities belong in each edition.

Ribbon Field already provides browser conversations through Codex app-server, streamed responses, approvals, queued instructions, checkpoints, thread organization, conflict detection, and durable messaging. Its terminal coordination also supports Claude Code; that does not yet establish equivalent hosted browser support for Claude Code. See the [project README](../README.md).

The application launches the official `codex app-server` process and sends local RPC messages such as `turn/start`. Codex handles coding-model requests and its configured authentication. Ribbon Field does not directly call OpenAI's model API for those conversations. The optional Jev attention classifier separately calls TypeSafe and would need its own cost and data-handling decision for hosting.

The repository also contains a [private remote access workflow](remote-access.md). Accessing a user's existing machine remotely is distinct from provisioning and operating customer machines as a paid service.

## Product and editions

The initial customer hypothesis is an individual developer managing several agent conversations across repositories. A useful differentiator is showing where the developer needs to intervene, preserving decisions and handoffs, and allowing a choice of agent and execution location.

Remote execution alone is unlikely to be a durable advantage. OpenAI already offers a managed Codex harness with hosted and self-hosted execution options. Ribbon Field would need to earn its place through coordination, continuity, and the experience across projects and providers. [OpenAI Agents API](https://developers.openai.com/api/docs/guides/agents-api/overview)

| Edition | Proposed scope | Status |
| --- | --- | --- |
| Community | A useful, complete core application and runner on infrastructure the developer controls | Intended direction; license and exact scope open |
| Hosted | Managed persistent workspaces, remote browser access, updates, bounded backups, and operational support | Intended direction; packaging and service commitments open |
| Hybrid | Our hosted dashboard connected to customer-owned runners | Possible later offering |
| Team features | Shared administration, access controls, and team operations | Possible later offering |

The working preference is to make reliable operation the main reason to pay. Feature boundaries, support promises, and whether team features become a separate commercial tier remain undecided.

## Existing subscriptions and authentication

The proposed user experience is to receive an isolated development machine, sign into the official Codex client using a personal account, and use Ribbon Field to manage work on that machine. Credentials stay in that environment and Codex communicates with OpenAI. We bill for the application and compute.

Three cases need to stay distinct:

| Case | What the reviewed documentation establishes |
| --- | --- |
| A developer runs Codex CLI on a remote machine and signs in | Remote and headless login is documented, including `codex login --device-auth`. Remote execution does not inherently require API billing. |
| A commercial hosted application uses app-server authentication | The app-server documentation explicitly restricts this use and directs developers toward the Sign in with ChatGPT partner path. |
| A customer independently signs into the CLI on a rented VM, then Ribbon Field uses the existing authentication through app-server | The precise applicability of that restriction is not explicitly resolved by the reviewed documentation. |

Sources: [Codex remote authentication](https://learn.chatgpt.com/docs/auth#login-on-headless-devices), [app-server authentication](https://learn.chatgpt.com/docs/app-server#auth-endpoints), and [Sign in with ChatGPT integration scope](https://developers.openai.com/siwc/token-sharing-open-source).

Technical feasibility is not sufficient to establish commercial permission. Equally, the sources do not establish a blanket prohibition on renting a developer a computer. Changing where login occurs does not conclusively settle the hosted integration question.

The specific clarification needed from OpenAI is:

> Can we sell dedicated development VMs with an open-source Ribbon Field UI that communicates with the official Codex app-server, where each customer independently authenticates through Codex CLI and credentials stay in their VM?

Bring-your-own-subscription remains the intended model. If it is unsupported for the proposed integration, the choices would include a supported partner integration, a different deployment model, or optional API billing. None has been selected. Claude Code requires a separate review before its hosted launch; OpenAI findings do not establish Anthropic's rules.

## Proposed hosting approach

For a pilot, use one persistent Linux VM per customer and a separate shared service for accounts, billing, provisioning, and authenticated routing. AWS is a candidate, not a requirement.

Each customer VM would contain Ribbon Field, the official agent client, repositories, development tools, and persistent state. Multiple repositories and conversations could share that machine's resources. The shared service would manage the workspace lifecycle without giving customer code access to infrastructure provisioning credentials.

The browser would connect through authenticated HTTPS to the assigned workspace. Terminal access would support setup and CLI login. Closing a browser or laptop would leave work running. Process supervision, backups, and explicit recovery behavior would be part of the service. Resuming saved conversations after a restart is not the same as preserving every running shell process or an in-progress turn.

No GPU is needed to host the agent client while model inference remains with the provider. Machine sizing instead depends on builds, tests, browsers, local databases, and concurrent agent activity.

AWS Lightsail offers a simple starting cost model. The reviewed general-purpose Linux bundles with public IPv4 list:

| Machine | Included storage | Included transfer allowance | Monthly VM price |
| --- | --- | --- | --- |
| 2 vCPU and 8 GB RAM | 160 GB | 5 TB | $44 |
| 4 vCPU and 16 GB RAM | 320 GB | 6 TB | $84 |

These are published USD bundle prices reviewed October 7, 2026; transfer allowances vary by region. Backups and the shared application service are additional. [AWS bundle specifications](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-bundles.html)

Lightsail uses burstable CPU. Sustained builds can exhaust burst capacity, so the pilot needs representative workload benchmarks before performance promises. EC2 with sustained CPU capacity is an alternative for heavier workloads and would require different cost estimates. The proposed VMs provide separate customer environments, not dedicated physical processors. [AWS performance guidance](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-faq-instances.html)

A later architecture could separate the dashboard from a common runner interface supporting local, customer-hosted, and managed machines. That abstraction is a proposal, not a prerequisite to proving the first hosted workspace.

## Proposed pricing and billing

Start with a monthly charge per provisioned workspace. Multiple repositories and agent sessions would share its bounded resources; extra machines would cost extra. Customers would pay their model provider separately and retain its usage limits.

| Offering | Proposed customer price | Proposed allocation |
| --- | --- | --- |
| Community | Free application | Customer supplies infrastructure and agent access |
| Hosted | $99 per month | One persistent 8 GB workspace, application, updates, and bounded backups |
| Hosted Large | $179 per month | One persistent 16 GB workspace with more CPU and storage |

The initial recommendation is to test the $99 plan, with larger machines available on request. These are pricing hypotheses, not validated willingness to pay. Always-on availability within the machine's capacity is the proposal; unlimited CPU throughput, storage, transfer, agent concurrency, or support is not.

Stripe Checkout, Billing, and the customer portal could handle subscriptions and account changes. A successful payment would trigger provisioning. Cancellation would follow an explicit export, retention, and deletion policy. Usage billing or a sleeping workspace plan could be considered later, but would need a clear billable unit and reliable treatment of running agents, approvals, and queued work.

## Illustrative economics

The following models one $99 customer for one month. Model usage is excluded because the intended arrangement is customer-provided subscriptions.

| Item | Amount | Basis |
| --- | --- | --- |
| Revenue | $99.00 | Proposed price |
| Customer VM | $44.00 | Published Lightsail bundle |
| Backups | $8.00 | Planning allowance; actual retention and changed data affect cost |
| Shared service allocation | $10.00 | Assumes a $100 monthly shared service spread across ten customers |
| Payment processing and Billing | $3.86 | US domestic card assumption: 2.9% plus $0.30, plus 0.7% for Billing |
| Total modeled cost | $65.86 | Sum of the above costs |
| Remaining before labor and other overhead | $33.14 | Approximately 33.5% of revenue |

Stripe rates: [Payments](https://stripe.com/pricing) and [Billing](https://stripe.com/billing/pricing). AWS lists snapshot storage at $0.05 per GB-month; the $8 allowance is a budget assumption, not a promise of a particular backup schedule. [AWS snapshot pricing](https://aws.amazon.com/lightsail/pricing/)

At ten such customers, the illustration produces $990 in monthly revenue and about $331 remaining before support labor, engineering, marketing, taxes, refunds, international payment fees, and other overhead. Optional classifier calls, transfer overages, unusual backup churn, and recovery costs also need budgeting. This is not net profit.

The margin is modest for a service requiring personal support. The pilot should measure support time and actual resource consumption as well as demand. Larger customer counts reduce shared infrastructure allocation only while that infrastructure remains sufficient.

## Decisions still required

All decisions below are open. The current preferences are proposals, and no owner or deadline has been assigned.

| Decision | Current preference or options | What would resolve it | Needed before |
| --- | --- | --- | --- |
| Initial customer and promise | Individual developers with several active coding threads; remote continuity and attention management | Pilot interviews and a precise definition of the first useful workflow | Pilot scope is fixed |
| Open-source license and commercial boundary | Useful Community core; charge for operation | Select the license and define which features and support belong in each edition | Public release |
| Hosted subscription integration | Customer signs into official Codex inside their VM | Clarify the specific hosted app-server scenario with OpenAI and select a supported path | Commercial launch |
| First agent and client experience | Codex first, Claude Code later; browser UI plus terminal access | Define required launch features and the route to later provider support | Implementation scope is fixed |
| Hosting provider and region | AWS candidate; Lightsail pilot or EC2 for sustained workloads | Benchmark representative builds and review cost, latency, and data location needs | Provisioning implementation |
| Customer isolation and credentials | Separate VM per customer; credentials stay there | Specify access boundaries, operator access, encryption, and recovery handling | External customers connect repositories |
| Workspace lifecycle | Persistent and always on initially | Decide restart behavior, update windows, failed-payment handling, export, and deletion | Paid pilot |
| Plan sizes and concurrency | 8 GB base; 16 GB upgrade | Measure memory, CPU, browser/test workloads, and simultaneous sessions | Publishing resource promises |
| Price and billing unit | $99 per workspace monthly; $179 larger option | Test willingness to pay and costs; decide trials, upgrades, refunds, and overages | Taking payments |
| Backups and service commitments | Bounded backups and managed recovery | Choose retention, restore expectations, support hours, and availability commitments | Paid pilot |
| Optional Jev classification | Opt-in today; hosted default undecided | Decide data disclosure, consent, supplier costs, and default behavior | Enabling it for hosted customers |
| Pilot success and expansion | Small initial cohort; later hybrid and team offerings | Set thresholds for repeat use, paid retention, completed work, support time, and margin | Recruiting the cohort |

## Suggested validation sequence

First, resolve the subscription integration boundary and choose a narrowly defined customer workflow. In parallel, benchmark the existing application and representative repositories on candidate Linux machines.

Next, package an installable Community experience and a hosted pilot that can provision, authenticate, run work, recover, and export data. Prove the core scenario: a developer starts a task, closes their laptop, returns from another device, and finds the result and context intact.

Then recruit roughly 5–10 paying developers, measure costs and support effort, and test the proposed $99 price. Consider Claude Code browser support, hybrid runners, team administration, and more elaborate orchestration after evidence from that pilot. This sequence is a proposal; creating this document does not approve implementation or launch.
