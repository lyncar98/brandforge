# BrandForge

**Manage a brand's creative assets as code.** A brand's identity — its hero
imagery, social cards, domain tiles, taglines, manifesto, launch copy — lives in
one declarative **brandspec** checked into version control. Run `plan` to see
what's missing, `apply` to generate it. Images render on Luma
[`uni-1`](https://docs.agents.lumalabs.ai); copy renders on Anthropic
[Claude](https://docs.anthropic.com). One workflow, two modalities, one auditable
state file.

The flagship spec is real: it produces the brand assets for
**[Lautum](https://lautum.ai)**, an AI-governance startup with a strict
monochrome, crisp-digital identity. So this repo does three things at once:

1. **A production proof point** on a real generative stack — both modalities,
   the messy parts handled (rate limits, retries, moderation, expiring URLs,
   resumable state).
2. **Brand assets a real startup ships** — on-brand images and copy.
3. **A reusable engine** — point it at any brandspec, not just Lautum.

> The whole thing **runs with no API key and zero spend** via `--mock` (in-process
> fakes of both backends), so you can try it in 30 seconds.

## Brand-as-Code

The same idea that made infrastructure reproducible — declare desired state, diff
it, converge to it — applied to brand assets:

| Infra-as-Code | BrandForge |
| --- | --- |
| `main.tf` | `brandspec.json` |
| `terraform plan` | `brandforge plan` |
| `terraform apply` | `brandforge apply` |
| `terraform.tfstate` | `out/manifest.json` |
| providers | Luma `uni-1` (images) · Claude (content) |

A brandspec is the single source of truth. The `style` string is appended to
every image prompt and the `voice` string is handed to every content brief, so
the whole kit stays on-brand **by construction** — change the voice once,
re-apply, and every asset moves with it.

## Quick start (no API key needed)

```bash
git clone https://github.com/<you>/brandforge
cd brandforge

python -m brandforge plan  --mock           # see the 13-asset Lautum spec
python -m brandforge apply --mock --out out # generate against in-process fakes
```

`out/` now contains assets laid out by group (`hero/`, `social/`, `domains/`,
`messaging/`), a resumable `manifest.json` (the state file), a `report.md`
cost/audit report, and **`gallery.html`** — an on-brand page showing every image
and every piece of copy with its model and status. Open it:

```bash
start out/gallery.html      # Windows  (macOS: open / Linux: xdg-open)
```

Re-running is idempotent and cheap: assets already in state are skipped; only
outstanding ones run. Add `--force` to regenerate everything.

## Run against the real APIs

```bash
export LUMA_AGENTS_API_KEY="luma-api-..."   # uni-1 images (agents.lumalabs.ai)
export ANTHROPIC_API_KEY="sk-ant-..."       # Claude content (api.anthropic.com)
pip install -r requirements.txt

python -m brandforge apply \
  --spec brands/lautum/brandspec.json \
  --out out --concurrency 3 --rpm 30 --fail-on-error
```

(Keys can also live in a local `.env` — see [`.env.example`](.env.example).)

## The brandspec

```jsonc
{
  "brand_name": "Lautum",
  "style": "Strict monochrome ... crisp, flat, digital, vector-clean ... no texture, no grain ...",
  "voice": "Precise, sober, technical. No hype. Every claim backed by a mechanism ...",
  "assets": [
    { "id": "hero_dark", "kind": "image", "group": "hero",
      "prompt": "Abstract hero background: translucent planes stacked in depth ...",
      "model": "uni-1-max", "aspect_ratio": "16:9", "output_format": "jpeg" },

    { "id": "tagline", "kind": "content", "group": "messaging",
      "content_type": "tagline", "max_words": 9,
      "brief": "Write the primary tagline for Lautum ..." }
  ]
}
```

- **`kind: "image"`** → `prompt`, `model` (`uni-1` / `uni-1-max`), `aspect_ratio`,
  `output_format`. The brand `style` is appended automatically.
- **`kind: "content"`** → `brief`, `content_type` (`tagline`, `manifesto`,
  `social_post`, `meta_description`, `alt_text`, …), optional `max_words`,
  `temperature`, `model`. The brand `voice` is applied automatically.

See [`brands/lautum/brandspec.json`](brands/lautum/brandspec.json) for the full
13-asset set.

## Commands

| Command | What it does |
| --- | --- |
| `plan` | Diff the brandspec against state. Shows every asset and whether `apply` would create or skip it. **No API calls.** |
| `apply` | Generate missing/changed assets and update state; writes `manifest.json`, `report.md`, `gallery.html`. |
| `gallery` | (Re)render the HTML gallery from existing state. |
| `status` | Print the run report from state. |
| `doctor` | Validate API keys and that both SDKs are installed. |

`--mock` works on `plan`/`apply`/`doctor` (no key, no spend).

## What it handles for you

- **Two providers, one workflow** — `uni-1` images and Claude content, with the
  resilient layers shared across both.
- **Two failure surfaces** — synchronous HTTP errors *and* asynchronous
  `failure_code` / moderation — each mapped to typed, behaviour-aware handling.
- **Rate limits** — client-side RPM window mirrored so you stay under instead of
  bouncing off 429s.
- **Retries** that respect the retryable/terminal split (`generation_failed`
  retries; `content_moderated` doesn't) and honour `Retry-After`.
- **Expiring presigned URLs** — image outputs are downloaded promptly and the URL
  is re-minted if it goes stale.
- **Idempotent, resumable, auditable** — a state file makes re-runs cheap, with a
  per-asset/per-failure report and a transparent (token-aware) cost estimate.
- **Client-side validation** of documented request constraints before spend.

See [`docs/api-notes.md`](docs/api-notes.md) for API quirks and
[`docs/design-decisions.md`](docs/design-decisions.md) for the why.

## Layout

```
brandforge/
  client.py      # typed uni-1 (Agents) image client + HTTP error mapping
  sdk_image.py   # real image backend on the official luma-agents SDK
  content.py     # Claude content client (+ in-process mock), the second modality
  models.py      # image request/response models + client-side validation
  transport.py   # Transport seam (real httpx vs in-process fake)
  errors.py      # typed exceptions, retryable vs terminal
  ratelimit.py   # RPM window + concurrency limiter
  retry.py       # backoff honouring Retry-After
  poller.py      # poll-to-terminal with initial delay + hard timeout (images)
  download.py    # fetch image outputs, re-mint expired URLs
  manifest.py    # resumable state file + token-aware cost estimate + report
  kit.py         # the brandspec runner: plan / apply over image + content assets
  gallery.py     # on-brand HTML gallery of a run (Lautum design system)
  mock.py        # in-process fake of the image API (powers --mock and tests)
  cli.py         # doctor / plan / apply / gallery / status
brands/lautum/   docs/   tests/
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The suite (and CI) runs entirely against the in-process fakes — no key, no
network, no spend.

## License

MIT. See [`LICENSE`](LICENSE).

---

*Not affiliated with Luma AI or Anthropic. Built to demonstrate production use of
generative APIs. "Luma" and "uni-1" are trademarks of Luma AI; "Claude" is a
trademark of Anthropic. Lautum is the author's startup ([lautum.ai](https://lautum.ai)).*
