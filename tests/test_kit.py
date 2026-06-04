from pathlib import Path

from brandforge.client import LumaAgentsClient
from brandforge.content import MockContentClient
from brandforge.errors import RateLimitError
from brandforge.gallery import render_gallery
from brandforge.kit import BrandKit, KitAsset, KitConfig, KitRunner, load_kit
from brandforge.manifest import JobStatus, Manifest
from brandforge.mock import MockConfig, MockTransport
from brandforge.poller import PollConfig
from brandforge.retry import RetryPolicy


def make_runner(tmp_path, kit, *, polls=2, content_client=None):
    image_client = LumaAgentsClient("luma-api-test", transport=MockTransport(MockConfig(polls_until_complete=polls)))
    config = KitConfig(
        output_dir=str(tmp_path / "out"),
        max_concurrent=3,
        image_poll=PollConfig(initial_delay=0, interval=0, timeout=30),
    )
    return KitRunner(image_client, content_client or MockContentClient(), kit, config, log=lambda m: None)


def sample_kit():
    return BrandKit(
        brand_name="Lautum",
        style="strict monochrome, crisp, digital, no texture",
        voice="precise, sober, no hype",
        assets=[
            KitAsset(id="hero", kind="image", group="hero", label="Hero",
                     prompt="abstract stacked planes",
                     params={"model": "uni-1-max", "aspect_ratio": "16:9", "output_format": "jpeg"}),
            KitAsset(id="tile", kind="image", group="domains", label="Education",
                     prompt="education concept", params={"aspect_ratio": "1:1", "output_format": "png"}),
            KitAsset(id="tagline", kind="content", group="messaging", label="Tagline",
                     prompt="Write a tagline for Lautum, an AI governance control plane.",
                     params={"content_type": "tagline", "max_words": 9}),
        ],
    )


def test_apply_generates_images_and_content(tmp_path):
    runner = make_runner(tmp_path, sample_kit())
    manifest = runner.run()
    assert manifest.counts()[JobStatus.COMPLETED] == 3
    for rec in manifest.records.values():
        assert rec.output_path and Path(rec.output_path).exists()
    # content wrote a markdown file with the generated copy
    tag = manifest.get("Lautum::tagline")
    assert tag.output_path.endswith(".md")
    assert "Lautum" in Path(tag.output_path).read_text(encoding="utf-8")
    # image kept its chosen format
    assert manifest.get("Lautum::tile").output_path.endswith(".png")


def test_content_appends_voice_and_tokens_recorded(tmp_path):
    runner = make_runner(tmp_path, sample_kit())
    manifest = runner.run()
    tag = manifest.get("Lautum::tagline")
    assert tag.model.startswith("claude")
    assert tag.output_tokens > 0
    assert tag.estimated_cost() >= 0.0


def test_plan_diffs_against_state(tmp_path):
    kit = sample_kit()
    runner = make_runner(tmp_path, kit)
    # Nothing in state yet -> everything is "create".
    actions = runner.plan(None)
    assert {a.action for a in actions} == {"create"}
    # After a run, everything is "skip".
    manifest = runner.run()
    actions = runner.plan(manifest)
    assert {a.action for a in actions} == {"skip"}


def test_drift_re_plans_changed_asset(tmp_path):
    kit = sample_kit()
    runner = make_runner(tmp_path, kit)
    manifest = runner.run()
    # Editing one asset's prompt should mark only it as "update"; the rest "skip".
    kit.assets[2].prompt = "Write a different, edgier tagline for Lautum."
    actions = {a.asset_id: a.action for a in runner.plan(manifest)}
    assert actions["tagline"] == "update"
    assert actions["hero"] == "skip" and actions["tile"] == "skip"


def test_drift_in_brand_voice_re_plans_all_content(tmp_path):
    kit = sample_kit()
    runner = make_runner(tmp_path, kit)
    manifest = runner.run()
    kit.voice = "loud, hype-driven, lots of exclamation marks"
    actions = {a.asset_id: a.action for a in runner.plan(manifest)}
    assert actions["tagline"] == "update"   # content depends on voice
    assert actions["hero"] == "skip"        # images don't


def test_apply_with_selection_only_touches_selected(tmp_path):
    kit = sample_kit()
    runner = make_runner(tmp_path, kit)
    manifest = runner.run(select={"tagline"})
    assert manifest.get("Lautum::tagline").status == JobStatus.COMPLETED
    assert manifest.get("Lautum::hero") is None
    assert manifest.get("Lautum::tile") is None


def test_plan_flags_orphans_and_prune_removes_them(tmp_path):
    kit = sample_kit()
    runner = make_runner(tmp_path, kit)
    manifest = runner.run()
    # Drop an asset from the spec -> its state record is now an orphan.
    removed_asset = kit.assets.pop()  # tagline
    orphan_path = Path(manifest.get(f"Lautum::{removed_asset.id}").output_path)
    assert orphan_path.exists()
    actions = {a.action for a in runner.plan(manifest)}
    assert "destroy" in actions

    removed = runner.prune(manifest)
    assert [a.asset_id for a in removed] == [removed_asset.id]
    assert manifest.get(f"Lautum::{removed_asset.id}") is None
    assert not orphan_path.exists()


def test_content_moderation_needs_attention(tmp_path):
    kit = BrandKit(brand_name="Lautum", voice="v", assets=[
        KitAsset(id="bad", kind="content", group="messaging", label="Bad",
                 prompt="please moderate me now", params={"content_type": "tagline"})])
    runner = make_runner(tmp_path, kit)
    manifest = runner.run()
    rec = manifest.get("Lautum::bad")
    assert rec.status == JobStatus.NEEDS_ATTENTION
    assert rec.failure_code == "content_moderated"


def test_content_flake_is_retried(tmp_path):
    kit = BrandKit(brand_name="Lautum", voice="v", assets=[
        KitAsset(id="flaky", kind="content", group="messaging", label="Flaky",
                 prompt="flake once then write a tagline", params={"content_type": "tagline"})])
    runner = make_runner(tmp_path, kit)
    manifest = runner.run()
    rec = manifest.get("Lautum::flaky")
    assert rec.status == JobStatus.COMPLETED
    assert rec.attempts >= 2


def test_resume_skips_completed(tmp_path):
    runner = make_runner(tmp_path, sample_kit())
    runner.run()
    img_submits = runner.image_client.transport.submit_count
    txt_submits = runner.content_client.submit_count

    manifest = Manifest.load(tmp_path / "out" / "manifest.json")
    runner.run(manifest=manifest)
    assert runner.image_client.transport.submit_count == img_submits
    assert runner.content_client.submit_count == txt_submits


def test_gallery_renders_images_and_content(tmp_path):
    kit = sample_kit()
    runner = make_runner(tmp_path, kit)
    manifest = runner.run()
    path = render_gallery(kit, manifest, tmp_path / "out")
    html = path.read_text(encoding="utf-8")
    assert "Lautum" in html
    assert "<img" in html        # image card
    assert 'class="copy"' in html  # content card


class _RaisingImageClient:
    """Image client whose submit always raises, to test run-level resilience."""

    def __init__(self, exc):
        self._exc = exc

    def create_generation(self, request):
        raise self._exc

    def get_generation(self, gid):  # pragma: no cover - never reached
        raise AssertionError("should not poll")

    def get_output_bytes(self, url):  # pragma: no cover
        return b""


def _one_image_runner(tmp_path, image_client):
    kit = BrandKit(brand_name="X", style="mono", assets=[
        KitAsset(id="a", kind="image", group="g", label="A", prompt="p",
                 params={"aspect_ratio": "1:1"})])
    config = KitConfig(
        output_dir=str(tmp_path / "out"),
        max_job_retries=0,
        retry=RetryPolicy(max_attempts=1),
        image_poll=PollConfig(initial_delay=0, interval=0, timeout=5),
    )
    return kit, KitRunner(image_client, MockContentClient(), kit, config, log=lambda m: None)


def test_persistent_rate_limit_marks_asset_not_crash(tmp_path):
    kit, runner = _one_image_runner(tmp_path, _RaisingImageClient(RateLimitError("cap", status_code=429)))
    manifest = runner.run()  # must not raise
    rec = manifest.get("X::a")
    assert rec.status == JobStatus.NEEDS_ATTENTION
    assert rec.failure_code == "rate_limited"


def test_unexpected_error_marks_asset_not_crash(tmp_path):
    kit, runner = _one_image_runner(tmp_path, _RaisingImageClient(ValueError("boom")))
    manifest = runner.run()  # must not raise
    rec = manifest.get("X::a")
    assert rec.status == JobStatus.FAILED
    assert rec.failure_code == "unexpected_error"


def test_load_spec_file(tmp_path):
    p = tmp_path / "brandspec.json"
    p.write_text('{"brand_name":"X","style":"mono","assets":[{"id":"a","kind":"image","prompt":"p"}]}', encoding="utf-8")
    kit = load_kit(p)
    assert kit.brand_name == "X" and len(kit.assets) == 1


def test_load_spec_accepts_brief_alias(tmp_path):
    p = tmp_path / "brandspec.json"
    p.write_text('{"brand_name":"X","assets":[{"id":"t","kind":"content","brief":"write a tagline"}]}', encoding="utf-8")
    kit = load_kit(p)
    assert kit.assets[0].prompt == "write a tagline"
