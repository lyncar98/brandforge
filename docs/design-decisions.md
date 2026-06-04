# Design decisions

Why the toolkit is shaped the way it is. The throughline: **manage a brand's
creative assets as code** — a declarative spec, a plan/apply workflow, and a
state file — with the operational concerns real generative APIs need in
production, not a thin wrapper over a few HTTP calls.

## 0. Two modalities, one resilient workflow

A brand is images *and* words, so BrandForge spans two providers: Luma `uni-1`
for stills, Anthropic Claude for copy. Rather than two toolkits, the resilient
layer is modality-agnostic. `kit.KitRunner` routes each asset to the right client
by `kind`, but retries, the RPM limiter, the manifest/state file, the cost report,
and the gallery are written once and shared. Adding content was a *backend*, not a
*fork*. That is the bet of the design: the hard, reusable parts of putting a
generative API into production don't care whether the bytes are a JPEG or a
tagline.

The two paths differ in exactly one place: images are async (submit → poll →
download), content is synchronous (one call returns the text). So the content path
skips the poller and everything else is identical.

## 1. Brand-as-Code: declarative spec, plan/apply, state

The whole tool is modelled on infrastructure-as-code because the problems are the
same: many assets, drift, reproducibility, "did this already get made?".

- The **brandspec** (`brands/lautum/brandspec.json`) is the single source of
  truth. `style` is appended to every image prompt; `voice` is handed to every
  content brief — so on-brand-ness is structural, not a per-asset habit.
- **`plan`** diffs the spec against state with zero API calls and prints, per
  asset, whether `apply` would create or skip it.
- **`apply`** converges: it generates only what's missing/changed and records the
  result.
- **`manifest.json`** is the state file — the durable record that makes runs
  idempotent, resumable, and auditable.

## 2. A `Transport` seam, not a hard `httpx` dependency

`client.LumaAgentsClient` talks to a `Transport` protocol; the content client
talks to a swappable client object. Production uses real SDKs/`httpx`; tests and
`--mock` use in-process fakes (`MockTransport`, `MockContentClient`).
Consequences:

- The **entire workflow runs with no key and no spend** — critical for a demo,
  for CI, and for anyone evaluating the repo.
- `httpx`/SDKs are imported lazily, so the package, tests, and mock mode are
  effectively stdlib-only.
- Error mapping, retries, and orchestration are tested deterministically without
  touching the network.

## 3. Two failure surfaces → two mechanisms

Synchronous errors become typed exceptions (`AuthError`, `RateLimitError`, …)
carrying a `retryable` flag. Asynchronous/terminal failures (image
`failure_code`s, Claude moderation, empty completions) are classified into
retry / terminal / needs-attention. The runner branches on **behaviour**, never
on string matching. See `docs/api-notes.md` for the tables.

## 4. The manifest is the source of truth

Every asset is a job with a stable key. The manifest records its state, generation
id, output path, attempts, token usage, and cost. This buys three things:

- **Resumability** — a re-run skips assets already applied (file still on disk),
  so an interrupted run or a spec that grew by three assets only does outstanding
  work.
- **Auditability** — exactly what was generated, at what estimated cost, and which
  assets a human needs to revisit.
- **Idempotency** — re-running is safe and cheap by default; `--force`
  regenerates.

## 5. Concurrency model: threads, not async

Image work is I/O-bound (submit → poll → download) and the natural limit is the
API's concurrent-job ceiling. A bounded `ThreadPoolExecutor` maps onto that
directly, keeps the code readable and stdlib-only, and avoids forcing the whole
call graph async. An `RpmLimiter` rides on top to respect the submission-rate
window independently of in-flight count. Content calls ride the same pool and
limiter.

## 6. Brand consistency lives in the spec, applied automatically

`uni-1` has no public `seed`, so image reproducibility comes from a shared `style`
string (and optional `image_ref` anchors). Copy consistency comes from a shared
`voice` string injected as a system prompt. Both are defined once in the spec and
applied to every asset of that kind — the documented way to hold a look and a tone
across many outputs.

## 7. The spec is declarative; the gallery is a byproduct

A brand's needs are a *list of assets*. The brandspec is exactly that list — each
asset names its kind, model, and parameters — and the runner produces
`gallery.html` straight from the state file, in the brand's own design system
(monochrome, crisp, gridlined). The gallery doubles as a status view:
failed/needs-attention assets render as labelled placeholders, so the deliverable
and the audit are the same page.

## 8. Client-side validation

Mirroring the documented 400/422 rules in `GenerationRequest.validate()` (and the
content request's length/temperature checks) turns a class of failures into
instant, local, free errors with actionable messages — and doubles as living
documentation of each API's constraints.

## What this deliberately is *not*

- Not an official SDK. It's a workflow toolkit that wraps two small, clean
  clients behind one plan/apply surface.
- Not a UI builder. The deliverable is a CLI + library a brand can drive from its
  own pipeline/CI; the generated gallery is a review surface, not an app.
- Not a video tool. An earlier iteration generated video too; the project was
  scoped to the two modalities a brand uses most — images and words — to keep the
  Brand-as-Code story tight. The same seam would accept a video backend later.
