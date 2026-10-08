#!/usr/bin/env python3
"""Build a self-contained demo coordination database for the Ribbon Field video.

Creates three small Git repositories, named projects, saved views, a dozen work
threads in every attention state, browser conversation history, a delegation
tree with bounded terminal output, declared write scopes, and durable messages.
Everything lands under demo/out so the user's real database is never touched.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "plugins" / "agent-coord" / "scripts"))

from agent_coord.store import CoordinationStore  # noqa: E402
from agent_coord.views import ViewStore  # noqa: E402
from agent_coord.codex_app_server import BrowserSessions  # noqa: E402
from agent_coord import managed_pty  # noqa: E402

OUT = HERE / "out"
DB = OUT / "demo.sqlite3"
WORKSPACES = OUT / "workspaces"

NOW = time.time()
CLOCK = [NOW]


def at(minutes_ago: float) -> None:
    """Move the store clock so rows receive believable timestamps."""
    CLOCK[0] = NOW - minutes_ago * 60


def new_id() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------- repositories
REPOS = {
    "acme-storefront": {
        "README.md": "# Acme Storefront\n\nNext.js storefront with a Python checkout service.\n",
        "src/checkout/cart_totals.py": (
            "from decimal import Decimal, ROUND_HALF_UP\n\n\n"
            "def line_total(price: Decimal, qty: int, tax_rate: Decimal) -> Decimal:\n"
            "    subtotal = price * qty\n"
            "    return (subtotal * (1 + tax_rate)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)\n"
        ),
        "src/checkout/__init__.py": "",
        "src/address/autocomplete.ts": "export async function suggest(query: string) {\n  return fetch(`/api/places?q=${encodeURIComponent(query)}`).then(r => r.json());\n}\n",
        "src/components/PromoBanner.tsx": "export function PromoBanner() {\n  return <aside className=\"promo\">Free shipping over $50</aside>;\n}\n",
        "tests/test_cart_totals.py": "def test_mixed_tax_rates():\n    assert True\n",
        "tests/fixtures/order_7731.json": "{\"lines\": 3, \"expected_total\": \"118.07\"}\n",
    },
    "acme-platform": {
        "README.md": "# Acme Platform\n\nTerraform, Helm charts, and deployment tooling.\n",
        "terraform/payments/main.tf": "module \"payments\" {\n  source = \"../modules/service\"\n  name   = \"payments\"\n}\n",
        "helm/payments/values.yaml": "image:\n  tag: v2.14.0\nreplicas: 6\n",
        "scripts/backfill_orders.py": "print('backfilling order events to ClickHouse')\n",
        ".github/workflows/ci.yml": "name: ci\non: [push]\n",
    },
    "acme-mobile": {
        "README.md": "# Acme Mobile\n\niOS and Android apps.\n",
        "ios/Push/TokenRefresh.swift": "struct TokenRefresh {}\n",
        "CHANGELOG.md": "## 3.2\n\n- Coming soon\n",
    },
}


def build_repositories() -> dict[str, Path]:
    paths = {}
    for name, files in REPOS.items():
        root = WORKSPACES / name
        root.mkdir(parents=True, exist_ok=True)
        for relative, text in files.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        if not (root / ".git").exists():
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.email=demo@acme.test", "-c", "user.name=Acme Demo", "add", "-A"], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.email=demo@acme.test", "-c", "user.name=Acme Demo", "commit", "-q", "-m", "Initial import"],
                           cwd=root, check=True)
        paths[name] = root
    return paths


# --------------------------------------------------------------------------- conversation history helpers
def user_item(text: str) -> dict:
    return {"type": "userMessage", "id": new_id(), "content": [{"type": "text", "text": text}]}


def agent_item(text: str, phase: str = "final_answer") -> dict:
    return {"type": "agentMessage", "id": new_id(), "text": text, "phase": phase}


def thinking(summary: str) -> dict:
    return {"type": "reasoning", "id": new_id(), "summary": [summary], "content": []}


def command(cmd: str, output: str, exit_code: int = 0, cwd: str = "") -> dict:
    return {"type": "commandExecution", "id": "exec-" + new_id(), "command": cmd, "cwd": cwd,
            "status": "completed", "aggregatedOutput": output, "exitCode": exit_code, "durationMs": 1800}


def file_change(path: str, diff: str) -> dict:
    return {"type": "fileChange", "id": "exec-" + new_id(), "status": "completed",
            "changes": [{"path": path, "kind": {"type": "update", "move_path": None}, "diff": diff}]}


def turn(items: list[dict], status: str = "completed") -> dict:
    return {"id": new_id(), "items": items, "status": status}


CART_DIFF = """@@ -4,6 +4,9 @@
-def line_total(price: Decimal, qty: int, tax_rate: Decimal) -> Decimal:
-    subtotal = price * qty
-    return (subtotal * (1 + tax_rate)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
+def line_total(price: Decimal, qty: int, tax_rate: Decimal) -> Decimal:
+    # Round each taxed line once; summing pre-rounded lines drifted by a cent
+    # on mixed-rate carts (fixtures/order_7731.json).
+    subtotal = (price * qty).quantize(Decimal('0.0001'))
+    taxed = subtotal + (subtotal * tax_rate).quantize(Decimal('0.0001'))
+    return taxed.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
"""

CART_HISTORY = [
    turn([
        user_item("Cart totals are off by a cent on orders with mixed tax rates. Repro is in tests/fixtures/order_7731.json. "
                  "Find the cause, fix it, and add a regression test."),
        thinking("Reproduce with the fixture before touching code. Suspect per-line rounding order."),
        command("pytest tests/test_cart_totals.py -k order_7731 -q",
                "F                                                                    [100%]\n"
                "FAILED tests/test_cart_totals.py::test_order_7731 - AssertionError: Decimal('118.06') != Decimal('118.07')\n"
                "1 failed in 0.41s", exit_code=1, cwd="acme-storefront"),
        command("rg -n 'quantize|ROUND_' src/checkout/",
                "src/checkout/cart_totals.py:6:    return (subtotal * (1 + tax_rate)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)\n"
                "src/checkout/receipt.py:41:    total = sum(line_total(*line) for line in lines)\n", cwd="acme-storefront"),
        agent_item("Found it. Each line is rounded to cents *before* tax is applied to the sum, so a 7.25% + 8.875% cart loses a cent. "
                   "Fixing the rounding order and adding the fixture as a regression test.", phase="commentary"),
        file_change("src/checkout/cart_totals.py", CART_DIFF),
        file_change("tests/test_cart_totals.py",
                    "@@ -1,2 +1,9 @@\n+import json\n+from decimal import Decimal\n+from checkout.cart_totals import cart_total\n+\n+def test_order_7731():\n+    order = json.load(open('tests/fixtures/order_7731.json'))\n+    assert cart_total(order['lines']) == Decimal(order['expected_total'])\n"),
        command("pytest -q", "................................................                 [100%]\n48 passed in 2.13s", cwd="acme-storefront"),
        command("gh pr create --fill --title 'Fix cent drift in mixed-rate cart totals'",
                "https://github.com/acme/storefront/pull/482\n", cwd="acme-storefront"),
        agent_item("## Fixed: cent drift on mixed-rate carts\n\n"
                   "**Cause.** `line_total` rounded each line to cents before tax, so carts mixing 7.25% and 8.875% rates "
                   "drifted by one cent (`118.06` vs `118.07`).\n\n"
                   "**Change.** Keep four decimals through the tax step and round once per line "
                   "(`src/checkout/cart_totals.py`). Added `test_order_7731` using the fixture.\n\n"
                   "**Validation.** `pytest -q` → 48 passed.\n\n"
                   "PR **#482** is open with 12 files. Please review the rounding helper before I merge; "
                   "the receipt renderer calls it in two places."),
    ]),
]

AUTOCOMPLETE_HISTORY = [
    turn([
        user_item("Add address autocomplete to checkout using the Places API. Keep the form accessible and add tests."),
        thinking("Start with the API client and a debounced hook, then the AddressForm integration."),
        command("npm test -- --watch=false", "Test Suites: 12 passed, 12 total\nTests: 71 passed, 71 total\n", cwd="acme-storefront"),
        agent_item("Client and debounced `useSuggestions` hook are in. Moving on to the form integration and keyboard navigation.",
                   phase="commentary"),
    ]),
    turn([
        user_item("Also make sure Enter selects the highlighted suggestion."),
        thinking("Wire keyboard handling in AddressForm; cover with React Testing Library."),
        command("npm test -- --watch=false src/address", "PASS src/address/AddressForm.test.tsx (9 tests)\n", cwd="acme-storefront"),
        agent_item("Enter now selects the highlighted suggestion and Escape closes the list. Writing the remaining AddressForm tests next.", phase="commentary"),
    ]),
]

BANNER_HISTORY = [
    turn([
        user_item("Make the promo banner dismissible and remember the choice for 7 days."),
        file_change("src/components/PromoBanner.tsx",
                    "@@ -1,3 +1,12 @@\n+import {useDismissal} from './useDismissal';\n export function PromoBanner() {\n-  return <aside className=\"promo\">Free shipping over $50</aside>;\n+  const [dismissed, dismiss] = useDismissal('promo-banner', 7);\n+  if (dismissed) return null;\n+  return (\n+    <aside className=\"promo\">Free shipping over $50\n+      <button aria-label=\"Dismiss\" onClick={dismiss}>×</button>\n+    </aside>\n+  );\n }\n"),
        command("npm test -- --watch=false src/components", "Tests: 14 passed, 14 total\n", cwd="acme-storefront"),
        agent_item("Done. The banner now has a close control and stays hidden for 7 days via `useDismissal`. Tests added and the preview deploy is live at `preview-479.acme.dev`."),
    ]),
]

SEARCH_HISTORY = [
    turn([
        user_item("Why is /api/search p95 at 1.8s this week? Don't change anything yet, just find out."),
        command("kubectl logs deploy/search --since=24h | grep 'duration_ms' | sort -t= -k2 -n | tail -5",
                "duration_ms=1764 route=/api/search q=\"blue linen\"\nduration_ms=1811 route=/api/search q=\"linen\"\n", cwd="acme-platform"),
        command("psql -c \"EXPLAIN ANALYZE SELECT * FROM products WHERE description ILIKE '%linen%'\"",
                "Seq Scan on products  (cost=0.00..48213.00 rows=1200 width=612) (actual time=0.031..1402.118 rows=1184 loops=1)\n"
                "  Filter: (description ~~* '%linen%'::text)\n", cwd="acme-platform"),
        agent_item("## Findings\n\n"
                   "71% of the p95 time is a sequential scan: `products.description ILIKE '%term%'` has no trigram index "
                   "and the table grew to 1.9M rows after the catalog import on Monday.\n\n"
                   "Two options, both reversible:\n\n"
                   "1. **`pg_trgm` GIN index** on `description` — ~40 min to build online, p95 drops to ~120 ms in staging.\n"
                   "2. **Move search to the existing OpenSearch cluster** — larger change, better relevance.\n\n"
                   "No action needed from you unless you want me to proceed with option 1."),
    ]),
]


# --------------------------------------------------------------------------- thread catalog
THREADS = [
    # ----- Attention queue -----
    dict(key="deploy", title="Deploy payments service v2.14", client="codex", browser=False, repo="acme-platform",
         project="Checkout redesign", created=190, updated=4, state="blocked",
         request="Roll out payments v2.14 to prod with the canary plan in docs/rollouts/payments.md.",
         checkpoints=[
             ("deployment", "Canary at 10% passed health checks for 20 minutes. The 50% step failed: the AWS SSO session expired before the Helm upgrade could pull the new image.",
              "Run `aws sso login --profile acme-prod` so the rollout can resume at 50%.", "user",
              [{"kind": "document", "label": "Rollout plan", "target": "docs/rollouts/payments.md"}]),
         ]),
    dict(key="cart", title="Cart totals rounding fix", client="claude", browser=True, repo="acme-storefront",
         project="Checkout redesign", created=140, updated=11, state="review", history=CART_HISTORY,
         request="Cart totals are off by a cent on orders with mixed tax rates. Find the cause, fix it, and add a regression test.",
         checkpoints=[
             ("investigation", "Reproduced the cent drift with fixtures/order_7731.json; each line was rounded before tax.", "Fix the rounding order.", "agent", []),
             ("validation", "Fixed per-line rounding in cart_totals.py and added test_order_7731. 48 tests pass. PR #482 is open.",
              "Review PR #482 (12 files). Focus on the rounding helper used by the receipt renderer.", "user",
              [{"kind": "pull_request", "label": "PR #482", "target": "https://github.com/acme/storefront/pull/482"},
               {"kind": "bead", "label": "acme-142", "target": "acme-142"}]),
         ]),
    dict(key="search", title="Why is /api/search p95 at 1.8s?", client="codex", browser=True, repo="acme-platform",
         project=None, created=95, updated=26, state="findings", history=SEARCH_HISTORY,
         request="Why is /api/search p95 at 1.8s this week? Don't change anything yet, just find out.",
         checkpoints=[
             ("investigation", "Profiled the search route: 71% of p95 is a sequential ILIKE scan on products.description (1.9M rows). Documented two reversible options.",
              "", "nobody", [{"kind": "document", "label": "Profile notes", "target": "docs/perf/search-p95.md"}]),
         ]),
    dict(key="backfill", title="Backfill order events to ClickHouse", client="claude", browser=False, repo="acme-platform",
         project="Q4 analytics", created=400, updated=41, state="update",
         request="Backfill the last 90 days of order events into ClickHouse. Report progress every hour.",
         checkpoints=[
             ("deployment", "Backfill is 62% complete (38.1M of 61.4M events) with zero rejected rows. Throughput is steady at 11k events/s; ETA 40 minutes.",
              "Finish the remaining partitions and verify row counts.", "agent", []),
         ]),
    dict(key="banner", title="Make the promo banner dismissible", client="claude", browser=True, repo="acme-storefront",
         project="Checkout redesign", created=60, updated=58, state="done", history=BANNER_HISTORY,
         request="Make the promo banner dismissible and remember the choice for 7 days.",
         checkpoints=[
             ("implementation", "Adding a close control to PromoBanner with a 7-day localStorage dismissal.", "Write the tests.", "agent", []),
             ("finished", "PromoBanner now has a close control that stores a 7-day dismissal in localStorage. Tests added; deployed to preview.",
              "", "nobody", [{"kind": "pull_request", "label": "PR #479", "target": "https://github.com/acme/storefront/pull/479"}]),
         ]),
    # ----- Working -----
    dict(key="autocomplete", title="Implement address autocomplete", client="claude", browser=True, repo="acme-storefront",
         project="Checkout redesign", created=120, updated=1, state="working", history=AUTOCOMPLETE_HISTORY, pinned=True,
         request="Add address autocomplete to checkout using the Places API. Keep the form accessible and add tests.",
         scopes=["src/address/**", "tests/address/**"],
         checkpoints=[
             ("implementation", "Places client and debounced useSuggestions hook are done; wiring AddressForm keyboard navigation now (3 of 5 components).",
              "Finish AddressForm tests and keyboard selection.", "agent", []),
         ]),
    dict(key="ci", title="Migrate CI to a GitHub Actions matrix", client="codex", browser=True, repo="acme-platform",
         project="Q4 infra", created=75, updated=2, state="working",
         request="Convert the CircleCI pipeline to GitHub Actions with a matrix over Python 3.11–3.13.",
         checkpoints=[
             ("implementation", "Workflow builds on 3.11 and 3.12; adding the 3.13 job and caching the uv environment.",
              "Add 3.13 to the matrix and re-run.", "agent", []),
         ]),
    # ----- Stages, nothing pending -----
    dict(key="inventory", title="Plan the inventory sync rewrite", client="claude", browser=True, repo="acme-storefront",
         project=None, created=300, updated=170, state="idle", pinned=True,
         request="Write an implementation plan for replacing the nightly inventory sync with an event-driven one.",
         checkpoints=[
             ("planning", "Drafted a three-phase plan: outbox table, consumer service, then retire the nightly job. Estimated two weeks.",
              "Draft the outbox schema migration.", "agent",
              [{"kind": "document", "label": "Plan", "target": "docs/plans/inventory-sync.md"}]),
         ]),
    dict(key="flaky", title="Investigate flaky checkout e2e test", client="codex", browser=False, repo="acme-storefront",
         project="Checkout redesign", created=240, updated=200, state="idle",
         request="checkout.spec.ts fails about one run in five on CI. Figure out why.",
         checkpoints=[
             ("investigation", "The failure correlates with the promo banner animation finishing after the click. Waiting on the animation end fixes 20 of 20 local runs.",
              "Confirm on CI with the retry disabled.", "agent", []),
         ]),
    dict(key="ratelimit", title="Rate limiter for the public API", client="claude", browser=True, repo="acme-platform",
         project="Q4 infra", created=500, updated=95, state="idle",
         request="Add a token-bucket rate limiter to the public API gateway: 600 req/min per key.",
         checkpoints=[
             ("validation", "Limiter is implemented behind a feature flag. Load test at 2x limit shows correct 429s; running the soak test.",
              "Finish the 30-minute soak test and report.", "agent", []),
         ]),
    dict(key="push", title="iOS push token refresh", client="codex", browser=True, repo="acme-mobile",
         project="Mobile 3.2", created=330, updated=150, state="idle",
         request="Push tokens stop refreshing after the app is backgrounded for a day. Fix it.",
         checkpoints=[
             ("implementation", "Reproduced on iOS 19 beta; the refresh task is cancelled when the BGTask budget expires. Rescheduling with an earliest-begin date.",
              "Finish the BGTask rescheduling and test on device.", "agent", []),
         ]),
    dict(key="notes", title="Release notes for Mobile 3.2", client="claude", browser=True, repo="acme-mobile",
         project="Mobile 3.2", created=700, updated=640, state="done_handled",
         request="Draft the 3.2 release notes from the merged PRs since 3.1.",
         checkpoints=[
             ("implementation", "Collecting the 23 merged PRs since 3.1 and grouping them by area.", "Draft the notes.", "agent", []),
             ("finished", "Release notes drafted from 23 merged PRs and added to CHANGELOG.md.", "", "nobody",
              [{"kind": "document", "label": "CHANGELOG", "target": "CHANGELOG.md"}]),
         ]),
    # ----- Later -----
    dict(key="bun", title="Evaluate Bun for the build pipeline", client="codex", browser=True, repo="acme-storefront",
         project=None, created=2000, updated=1900, state="idle", attention="later",
         request="Would switching the storefront build to Bun be worth it? Measure before recommending.",
         checkpoints=[
             ("planning", "Bun builds the storefront 2.4x faster locally, but two Vite plugins are unsupported. Needs a decision on dropping them.",
              "", "nobody", [])]),
    dict(key="darkadmin", title="Dark mode for the admin panel", client="claude", browser=True, repo="acme-storefront",
         project=None, created=2600, updated=2500, state="idle", attention="later",
         request="Sketch what dark mode for the admin panel would take.",
         checkpoints=[("discussion", "Admin panel uses 41 hard-coded colors; tokenizing them first is the real work.", "", "nobody", [])]),
    # ----- Closed -----
    dict(key="typo", title="Fix README typos", client="codex", browser=True, repo="acme-platform",
         project=None, created=4000, updated=3900, state="done_handled", attention="archived",
         request="Fix the typos in README.md.",
         checkpoints=[("implementation", "Fixing four typos.", "Commit.", "agent", []), ("finished", "Fixed four typos and merged.", "", "nobody", [])]),
    dict(key="sentry", title="Rotate the Sentry DSN", client="claude", browser=True, repo="acme-platform",
         project="Q4 infra", created=5000, updated=4800, state="done_handled", attention="archived",
         request="Rotate the Sentry DSN and update the sealed secret.",
         checkpoints=[("deployment", "Rotating the DSN and resealing the secret.", "Revoke the old key.", "agent", []), ("finished", "DSN rotated, sealed secret updated, and the old key revoked.", "", "nobody", [])]),
]

MODELS = {"codex": ("gpt-5-codex", "high"), "claude": ("claude-opus-5-5", "high")}


def seed() -> dict:
    if OUT.exists():
        for child in OUT.iterdir():
            if child.name in {"demo.sqlite3", "demo.sqlite3-wal", "demo.sqlite3-shm", "workspaces", "delegations", "state"}:
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    OUT.mkdir(parents=True, exist_ok=True)
    repos = build_repositories()
    store = CoordinationStore(DB, clock=lambda: CLOCK[0])
    # Creating the browser session manager installs its tables exactly as the app does.
    BrowserSessions(store, None).close()
    threads = store.threads
    organization = threads.organization
    views = ViewStore(store)

    at(6000)
    repo_ids = {name: organization.add_repository(str(path))["id"] for name, path in repos.items()}
    project_ids = {name: organization.create_project(name)["id"] for name in ("Checkout redesign", "Q4 infra", "Mobile 3.2", "Q4 analytics")}

    ids: dict[str, str] = {}
    for spec in THREADS:
        thread_id = new_id()
        ids[spec["key"]] = thread_id
        cwd = str(repos[spec["repo"]])
        at(spec["created"])
        store.register(session_id=thread_id, client=spec["client"], cwd=cwd, name=spec["title"])
        threads.ensure(thread_id)
        if spec.get("browser"):
            model, effort = MODELS[spec["client"]]
            with store._connection() as db:
                db.execute("INSERT INTO browser_sessions (thread_id, cwd, name, model, effort, created_at, updated_at, archived, yolo, client) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?)",
                           (thread_id, cwd, spec["title"], model, effort, CLOCK[0], CLOCK[0], spec["client"]))
                if spec["client"] == "claude":
                    db.execute("INSERT INTO claude_sessions (thread_id, started) VALUES (?, 1)", (thread_id,))
        # The first turn captures the original request and names the thread.
        threads.start_turn(thread_id, prompt=spec["request"])
        state = spec["state"]
        checkpoints = spec["checkpoints"]
        for index, (phase, summary, action, actor, links) in enumerate(checkpoints):
            last = index == len(checkpoints) - 1
            at(spec["updated"] + (0 if last else 30 * (len(checkpoints) - index)))
            if last:
                # Later turns start before their checkpoint so nothing reads as stale.
                at(spec["updated"] + 5)
                threads.start_turn(thread_id, prompt="Continue.")
                at(spec["updated"])
            threads.checkpoint(thread_id, {"phase": phase, "summary": summary, "next_action": action,
                                           "next_actor": actor, "links": links, "title": spec["title"]})
        at(spec["updated"])
        if state in {"findings", "update", "done", "done_handled", "idle", "review"}:
            threads.finish_turn(thread_id, status="completed")
        thread = threads.get(thread_id)
        completion = thread["turn_completion"]
        checkpoint = thread["checkpoint"]
        if state in {"findings", "update"} and completion:
            with store._connection() as db:
                db.execute("INSERT INTO response_classification_cache (completion_id, checkpoint_id, model, policy_version, status, choice, "
                           "confidence, probabilities_json, attempts, retry_at, lease_until, updated_at) VALUES (?, ?, 'jev-1.13.0', 2, 'classified', ?, ?, ?, 1, 0, 0, ?)",
                           (completion["id"], checkpoint["id"], state, 0.93,
                            json.dumps({state: 0.93, **{k: 0.0175 for k in ("blocked", "review", "update", "findings", "done") if k != state}}), CLOCK[0]))
        if state in {"idle", "done_handled"}:
            threads.update(thread_id, seen=True, seen_checkpoint_id=checkpoint["id"] if checkpoint else 0,
                           seen_completion_id=completion["id"] if completion else 0,
                           handled=True, handled_checkpoint_id=checkpoint["id"] if checkpoint else 0,
                           handled_completion_id=completion["id"] if completion else 0)
        if state == "working":
            with store._connection() as db:
                db.execute("UPDATE sessions SET turn_active = 1, last_seen_at = ? WHERE session_id = ?", (NOW, thread_id))
            if spec.get("scopes"):
                store.begin_work(session_id=thread_id, scopes=spec["scopes"], activity="implementing")
        elif spec.get("browser"):
            with store._connection() as db:
                db.execute("UPDATE sessions SET last_seen_at = ? WHERE session_id = ?", (NOW, thread_id))
        else:
            # Terminal conversations whose client has exited remain readable.
            store.end_session(thread_id)
        with store._connection() as db:
            db.execute("UPDATE sessions SET name = ? WHERE session_id = ?", (spec["title"], thread_id))
        with store._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            organization.update(db, thread_id, repository_id=repo_ids[spec["repo"]],
                                project_id=project_ids[spec["project"]] if spec.get("project") else None)
        if spec.get("attention"):
            threads.update(thread_id, attention=spec["attention"])
        if spec.get("pinned"):
            threads.update(thread_id, pinned=True)
        if spec.get("history"):
            history = {"id": thread_id, "turns": spec["history"], "cwd": cwd, "status": {"type": "idle"}}
            with store._connection() as db:
                db.execute("INSERT OR REPLACE INTO browser_history (thread_id, history_json) VALUES (?, ?)", (thread_id, json.dumps(history)))
        with store._connection() as db:
            db.execute("UPDATE work_threads SET updated_at = ? WHERE thread_id = ?", (NOW - spec["updated"] * 60, thread_id))

    # ----- Delegation tree for the coordination monitor -----
    at(150)
    parent = new_id()
    ids["coordinator"] = parent
    storefront = str(repos["acme-storefront"])
    store.register(session_id=parent, client="claude", cwd=storefront, name="Checkout redesign coordinator")
    threads.ensure(parent)
    threads.start_turn(parent, prompt="Coordinate the checkout redesign: delegate the cart totals fix and validate address autocomplete.")
    threads.checkpoint(parent, {"phase": "implementation", "summary": "Delegated the cart totals fix to a Codex worker and address autocomplete validation to a Claude validator.",
                                "next_action": "Collect the validator's verdict and merge PR #482.", "next_actor": "agent", "links": [], "title": "Checkout redesign coordinator"})
    with store._connection() as db:
        db.execute("BEGIN IMMEDIATE")
        organization.update(db, parent, repository_id=repo_ids["acme-storefront"], project_id=project_ids["Checkout redesign"])
    store.begin_work(session_id=parent, scopes=["docs/checkout/**"], activity="planning")
    with store._connection() as db:
        db.execute("UPDATE sessions SET last_seen_at = ?, turn_active = 0 WHERE session_id = ?", (NOW, parent))

    at(140)
    first = store.create_delegation(parent_session_id=parent, cwd=storefront, bead_id="acme-142",
                                    scopes=["src/checkout/**", "tests/test_cart_totals.py"],
                                    instructions="Fix the cent drift in mixed-rate cart totals, add a regression test, and open a PR.",
                                    mode="reviewed", model="gpt-5-codex", reasoning_effort="high", name="cart-totals-worker")
    first_id = first["delegation_id"]
    first_log = managed_pty.output_log_path(store, first_id)
    first_log.parent.mkdir(parents=True, exist_ok=True)
    first_log.write_text(
        "╭──────────────────────────────────────────────╮\n"
        "│ Codex · cart-totals-worker · acme-storefront  │\n"
        "╰──────────────────────────────────────────────╯\n"
        "› Claimed acme-142 and declared src/checkout/** (write)\n"
        "› pytest tests/test_cart_totals.py -k order_7731   FAILED (118.06 != 118.07)\n"
        "› Edited src/checkout/cart_totals.py (+6 −3)\n"
        "› pytest -q                                        48 passed in 2.13s\n"
        "› gh pr create                                     https://github.com/acme/storefront/pull/482\n"
        "✓ Result sent to parent: completed acme-142\n")
    store.mark_delegation_launched(first_id, runtime_kind="managed-pty", supervisor_pid=48120, output_log_path=str(first_log))
    child_one = new_id()
    ids["cart-worker"] = child_one
    store.register(session_id=child_one, client="codex", cwd=storefront, name="cart-totals-worker")
    store.set_delegation_child_process(first_id, supervisor_pid=48120, child_pid=48131, output_log_path=str(first_log))
    store.attach_delegation(first_id, child_one)
    at(100)
    store.finish_delegation(first_id, child_session_id=child_one, outcome="completed",
                            message="Completed acme-142: fixed per-line rounding, added test_order_7731, 48 tests pass, PR #482 opened.")
    store.end_session(child_one)

    at(30)
    second = store.create_delegation(parent_session_id=parent, cwd=storefront, bead_id="acme-151",
                                     scopes=["tests/e2e/**"],
                                     instructions="Validate address autocomplete against the staging Places key. Do not edit; report a verdict.",
                                     mode="reviewed", lease_mode="validation", model="claude-sonnet-5-5", reasoning_effort="medium",
                                     client="claude", name="autocomplete-validator")
    second_id = second["delegation_id"]
    second_log = managed_pty.output_log_path(store, second_id)
    second_log.parent.mkdir(parents=True, exist_ok=True)
    second_log.write_text(
        "╭──────────────────────────────────────────────╮\n"
        "│ Claude Code · autocomplete-validator          │\n"
        "╰──────────────────────────────────────────────╯\n"
        "› Declared validation lease on tests/e2e/** (read-only)\n"
        "› npm test -- src/address                        12 passed\n"
        "› npx playwright test checkout-address.spec.ts   3 passed, 1 running\n"
        "› axe-core: 0 violations on /checkout\n"
        "⠋ Soak: 180 of 300 suggestions resolved under 150 ms…\n")
    store.mark_delegation_launched(second_id, runtime_kind="managed-pty", supervisor_pid=50211, output_log_path=str(second_log))
    child_two = new_id()
    ids["validator"] = child_two
    store.register(session_id=child_two, client="claude", cwd=storefront, name="autocomplete-validator")
    store.set_delegation_child_process(second_id, supervisor_pid=50211, child_pid=50218, output_log_path=str(second_log))
    store.attach_delegation(second_id, child_two)
    store.begin_work(session_id=child_two, scopes=["tests/e2e/**"], bead_id="acme-151", activity="validating", lease_mode="validation")
    with store._connection() as db:
        db.execute("UPDATE sessions SET last_seen_at = ?, turn_active = 1 WHERE session_id = ?", (NOW, child_two))

    # ----- Durable messages -----
    at(98)
    store.send_message(sender_session_id=child_one, recipient_session_id=parent, classification="action_required", reply_required=False,
                       body="Completed acme-142. PR #482 is open; the receipt renderer calls line_total twice, so please review before merge.")
    at(28)
    store.send_message(sender_session_id=parent, recipient_session_id=child_two, classification="action_required",
                       body="Validate address autocomplete against the staging Places key. Report each command, exit status, and a verdict. Do not edit.")
    at(9)
    store.send_message(sender_session_id=child_two, recipient_session_id=parent, classification="informational",
                       body="Validation 60% through: unit and accessibility checks green; soak test running.")
    at(6)
    store.send_message(sender_session_id=ids["autocomplete"], recipient_session_id=ids["coordinator"], classification="action_required",
                       body="Can you release docs/checkout/address.md? I need to document the keyboard behaviour next to the component.")

    # ----- Saved views -----
    at(50)
    views.create({"name": "Checkout redesign", "filters": {"project": project_ids["Checkout redesign"]}, "group_by": "phase"})
    views.create({"name": "Infra", "filters": {"repository": repo_ids["acme-platform"]}, "group_by": "project"})
    views.create({"name": "Needs me", "filters": {"show": "attention"}, "group_by": "none"})

    CLOCK[0] = NOW
    manifest = {"db": str(DB), "workspaces": {k: str(v) for k, v in repos.items()}, "threads": ids,
                "projects": project_ids, "repositories": repo_ids, "delegations": [first_id, second_id]}
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


if __name__ == "__main__":
    manifest = seed()
    print(json.dumps({"db": manifest["db"], "threads": len(manifest["threads"])}, indent=2))
