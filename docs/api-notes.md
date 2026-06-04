# Luma Agents API — implementation notes

Field notes on the `uni-1` image API (`https://agents.lumalabs.ai/v1`), and how
each quirk is handled in this codebase. These are the things that bite you in
production but aren't obvious from a quickstart.

## The shape of the API

- **Two endpoints.** `POST /v1/generations` submits a job and returns `201`
  immediately with an opaque id and `state: "queued"`. `GET /v1/generations/{id}`
  polls status and, on completion, returns `output[].url`.
- **Async by design.** Nothing is returned inline. You submit, poll, then
  download. → `client.create_generation` + `poller.poll_until_terminal`.
- **One image per request.** A brand's many assets = many requests, declared in
  the brandspec and converged by `kit.KitRunner`.

## Two failure surfaces, handled separately

| Surface | Where it shows up | Handled in |
| --- | --- | --- |
| Synchronous | HTTP status + `detail` on submit/get | `client.map_http_error` → typed `APIError` subclasses |
| Asynchronous | `state="failed"` + `failure_code` while polling | `models.Generation.raise_if_failed` → `GenerationFailed` |

Async `failure_code` handling (per the docs):

| `failure_code` | Retry? | Charged? | Our action |
| --- | --- | --- | --- |
| `generation_failed` | yes | refunded | retry up to `max_job_retries` |
| `output_not_found` | yes | refunded | retry |
| `content_moderated` | **no** | refunded | mark `needs_attention` (a human must change the prompt) |
| `budget_exhausted` | no | partial | mark `needs_attention` (add credits) |

## Rate limits — stay under, don't bounce off

Two per-**client** limits (not per-key): RPM on submit, and a concurrent-job
ceiling. Both surface as `429`; the docs distinguish them by `detail`
(`"Rate limit exceeded"` vs `"Too many concurrent jobs"`).

- We mirror **both** locally (`ratelimit.RpmLimiter`, bounded `ThreadPoolExecutor`)
  so we proactively stay under the ceiling instead of wasting round-trips.
- On a `429` we still honour `Retry-After` (`retry.call_with_retry`), and we
  parse `X-RateLimit-Limit/Remaining/Reset` off every successful submit.
- The GET poll endpoint is effectively not rate-limited; a flat 2–3s cadence is
  correct. The docs are explicit that exponential backoff "doesn't buy you
  anything" for 30–60s jobs, so the poller uses a fixed interval + hard timeout.

## Presigned output URLs expire in 1 hour

Every poll mints a **fresh** 1-hour URL. So:

- Download to your own storage **promptly**; never hand a presigned URL to an
  end user.
- If a download fails (likely expiry), re-`GET` the generation to mint a new URL
  and retry. → `download.download_output(..., refresh=client.get_generation)`.

## Client-side validation (fail fast, locally)

`models.GenerationRequest.validate()` enforces the documented 400/422 rules
before we spend a round-trip:

- prompt length 1–6,000 chars;
- `aspect_ratio` ∈ the nine allowed values;
- `style: "manga"` requires a **portrait** ratio (`2:3`, `9:16`, `1:2`, `1:3`)
  on `type: "image"` — ignored on edits;
- `source` required iff `type: "image_edit"`; `image_ref` ≤ 9 (≤ 8 on edit);
- each `image_ref`/`source` supplies exactly one of `url` or `data`
  (+ `media_type` with `data`).

## Tracing

We send a client-generated `X-Request-Id` on every call and capture the one the
server echoes back, storing it on the job record. That id is the first thing
Luma support asks for.

## Pricing

The docs don't publish the per-image dollar table publicly. `manifest.PRICE_TABLE`
holds **placeholder** values that encode the documented *relationships*
(`uni-1-max ≈ 2.5× uni-1`, plus a small per-reference-image increment). Swap in
real numbers from your Pricing page; the cost report reads straight from there.

---

# Anthropic Claude — content notes

The second modality (`https://api.anthropic.com`, Messages API via the official
`anthropic` SDK). → `content.ClaudeContentClient`.

## Synchronous, not async

Unlike image generation, Claude returns the text in a single `messages.create`
call — there's no submit/poll. So the content path skips the poller entirely;
everything else (typed errors, retry policy, the manifest, cost accounting) is
shared with the image path.

## Brand voice is a system prompt

Each content asset carries a `content_type` (`tagline`, `manifesto`,
`social_post`, `meta_description`, `alt_text`, …) and a `brief`. The brandspec's
`voice` string plus the `content_type` and an optional `max_words` are composed
into a **system prompt** (`ContentRequest.system_prompt`); the `brief` is the user
message. The system prompt also forces "output only the copy" so we can write the
result straight to a file without scrubbing preambles.

## Error mapping

`anthropic` exceptions are translated into the same `brandforge.errors` types as
the image client (`AuthError`, `RateLimitError`, `InvalidRequestError`,
`ServerError`). An empty completion is treated as a retryable `generation_failed`
so a transient blank gets a second attempt. The runner's retryable/terminal split
is therefore identical across modalities — `content_moderated` is terminal
(`needs_attention`), `generation_failed` retries.

## Token-aware cost

`ContentResult` carries `input_tokens` / `output_tokens` from the API's `usage`
block; these are stored on the job record and priced via
`manifest.CONTENT_PRICE_PER_MTOK` (per-million-token placeholder rates, keyed by
model family — opus / sonnet / haiku). So the run's cost report mixes per-image
estimates with real per-token content costs.

## Key

Claude uses an `ANTHROPIC_API_KEY` (`sk-ant-...`), entirely separate from the
Luma image key. `doctor` checks for both and that both SDKs are installed.
